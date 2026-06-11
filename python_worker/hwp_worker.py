"""
HWP/HWPX 문서 내 이미지 → JPG 자동 변환 워커

듀얼 프로세서:
  - HwpProcessor  : HWP (한글 2010) — OLE Compound File 직접 조작
  - HwpxProcessor : HWPX (한글 2022) — ZIP 아카이브 조작

기존 worker.py의 to_rgb() 로직을 그대로 재사용합니다.
NDJSON 스트리밍 출력으로 프론트엔드에 실시간 진행률을 보고합니다.
"""

import argparse
import sys
import json
import io
import os
import re
import shutil
import struct
import tempfile
import zipfile
from pathlib import Path
from PIL import Image

# ─────────────────────────────────────────
# 이미지 형식 감지 (바이너리 시그니처)
# ─────────────────────────────────────────

def detect_format(data: bytes) -> str:
    """바이너리 시그니처로 이미지 형식을 감지합니다."""
    if len(data) < 12:
        return "unknown"

    # PNG
    if data[:4] == b'\x89PNG':
        return "png"
    # JPG/JPEG
    if data[:3] == b'\xff\xd8\xff':
        return "jpg"
    # GIF
    if data[:4] == b'GIF8':
        return "gif"
    # BMP
    if data[:2] == b'BM':
        return "bmp"
    # TIFF (Little Endian)
    if data[:4] == b'II\x2a\x00':
        return "tif"
    # TIFF (Big Endian)
    if data[:4] == b'MM\x00\x2a':
        return "tif"
    # WEBP (RIFF container)
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "webp"
    # EMF (Enhanced Metafile)
    if len(data) >= 44 and data[40:44] == b' EMF':
        return "emf"
    # WMF (Placeable)
    if len(data) >= 4:
        magic = struct.unpack_from("<I", data, 0)[0]
        if magic == 0x9AC6CDD7:
            return "wmf"
    # WMF (standard — check for standard WMF header type 1 or 2)
    if len(data) >= 4:
        file_type = struct.unpack_from("<H", data, 0)[0]
        header_size = struct.unpack_from("<H", data, 2)[0]
        if file_type in (1, 2) and header_size == 9:
            return "wmf"
    # SVG (text-based, check for XML or <svg)
    try:
        head = data[:256].decode("utf-8", errors="ignore").strip().lower()
        if head.startswith("<?xml") or head.startswith("<svg") or "<svg" in head[:512]:
            return "svg"
    except Exception:
        pass

    return "unknown"


# ─────────────────────────────────────────
# 모드별 필터
# ─────────────────────────────────────────

# 모드 selective: jpg/jpeg/bmp/emf 제외
EXCLUDE_SELECTIVE = {"jpg", "jpeg", "bmp", "emf"}
# 모드 all: 제외 없음
EXCLUDE_ALL = set()


def should_convert(fmt: str, mode: str) -> bool:
    """해당 형식이 변환 대상인지 확인합니다."""
    if fmt == "unknown":
        return False
    excludes = EXCLUDE_SELECTIVE if mode == "selective" else EXCLUDE_ALL
    return fmt not in excludes


# ─────────────────────────────────────────
# 이미지 변환 (기존 worker.py의 to_rgb 재사용)
# ─────────────────────────────────────────

def to_rgb(img):
    """이미지를 RGB 모드로 변환합니다. (기존 worker.py 로직 그대로)"""
    if img.mode == "P":
        img = img.convert("RGBA")
    if img.mode in ("RGBA", "LA", "RGBa"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    if img.mode == "CMYK":
        return img.convert("RGB")
    if img.mode in ("I", "I;16", "I;16B"):
        return img.point(lambda x: x >> 8).convert("RGB")
    return img.convert("RGB")


def convert_image_to_jpg(data: bytes, source_format: str, quality: int = 95) -> bytes:
    """이미지 바이트를 JPG 바이트로 변환합니다. (PIL이 여는 일반 래스터 전용)"""
    img = Image.open(io.BytesIO(data))
    if img.mode != "RGB":
        img = to_rgb(img)

    # 원본 이미지의 DPI(해상도)를 유지합니다. 없으면 96으로 간주합니다.
    dpi = img.info.get("dpi", (96, 96))

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, dpi=dpi)
    return buf.getvalue()


def render_metafile_to_jpg(data: bytes, fmt: str) -> bytes:
    """WMF/EMF 바이트를 GDI로 렌더링하여 JPG 바이트로 변환합니다.

    PIL은 WMF/EMF를 열지 못하므로(특히 HWPX 경로에서 'cannot identify image file' 오류),
    worker.py의 GDI 렌더러를 사용해야 합니다. WMF는 한글의 그림 자르기(Crop)가 잘라낼
    여백을 보존하도록 선언된 전체 캔버스(window extent)로 렌더링합니다."""
    sys.path.insert(0, os.path.dirname(__file__))
    import worker
    suffix = ".wmf" if fmt == "wmf" else ".emf"
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(tmp_fd)
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
        if fmt == "wmf":
            img = worker.render_wmf_gdi_fullframe(tmp_path, 300)
        else:
            img = worker.render_emf_gdi(tmp_path, 300)
        if img.mode != "RGB":
            img = worker.to_rgb(img)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=95, dpi=(300, 300))
        return out.getvalue()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def image_bytes_to_jpg(data: bytes, fmt: str) -> bytes:
    """이미지 바이트를 JPG 바이트로 변환합니다. WMF/EMF는 GDI 렌더링, 그 외는 PIL을 사용합니다.
    HWP(OLE)·HWPX(ZIP) 두 경로가 동일하게 이 함수를 거치도록 해, 포맷 처리가 갈라지지 않게 합니다."""
    if fmt in ("wmf", "emf"):
        return render_metafile_to_jpg(data, fmt)
    return convert_image_to_jpg(data, fmt)


# ─────────────────────────────────────────
# 사이즈 조정 (변환 후처리 — 용량 절감)
# ─────────────────────────────────────────
#
# 목적: 그림이 '확대/축소 비율' 100% 미만으로 삽입돼 있으면, 화면에 보이는 크기보다
#       이미지가 더 큰(픽셀이 남아도는) 상태라 파일이 불필요하게 무겁다. 이를 정리한다.
# 규칙(사용자 정의):
#   - 그림크기(curSz, 화면에 보이는 너비/높이)는 그대로 유지한다.
#   - 확대/축소 비율(가로/세로) 중 '큰 값'을 100%로 맞추고, 같은 배율로 나머지도 조정한다.
#     (예: 50%/10% → 100%/20%). 가로·세로 하드코딩이 아니라 둘 중 큰 값 기준.
#   - 가로/세로 중 하나라도 100%를 넘으면(이미 확대 상태) 건너뛴다(축소 시 화질 손상).
# HWPX 구현: 비율 = curSz/orgSz. orgSz를 k=max(비율)배로 줄이고 scaMatrix·imgRect를
#   같은 비율로 맞춘 뒤 JPG 픽셀도 k배로 리샘플한다. curSz·imgClip·imgDim·위치행렬(e3)은
#   유지되므로 화면 표시는 수학적으로 동일하고, 비율만 큰 축 100%가 된다.
# 전제: 반드시 '변환 로직이 끝난 출력 파일'에 후처리로 적용한다(변환 로직은 미변경).

# 사이즈 조정 공통 상수
SIZE_TARGET_DPI = 300                  # 표시(화면) 기준 목표 해상도 — 인쇄 품질, 시각적 손실 없음
SIZE_ADJUST_OVERSAMPLE = 2.0           # 벡터(WMF) 재렌더 시 여유 배수(확대 대비). 1.0=표시 300dpi


def _vector_target_px(cw: int, ch: int, oversample: float = SIZE_ADJUST_OVERSAMPLE):
    """표시 크기(curSz, HWPUNIT)를 '표시 300dpi×oversample' 픽셀 크기로 환산한다.
    1inch=7200HWPUNIT 이므로 px = curSz/7200 × (300×oversample). 반환 (nw, nh, eff_dpi)."""
    eff = SIZE_TARGET_DPI * oversample
    nw = max(1, round(cw * eff / 7200.0))
    nh = max(1, round(ch * eff / 7200.0))
    return nw, nh, int(round(eff))


def _rerender_vector_jpg(original_bytes: bytes, fmt: str, cw: int, ch: int):
    """WMF를 표시 해상도(×oversample)로 '직접 다시 렌더'해 또렷한 작은 JPG를 만든다.

    큰 래스터를 다운샘플하면 가는 벡터 선이 흰 배경과 평균돼 회색으로 흐려진다(검정 0→197).
    GDI로 작은 크기를 새로 그리면 선이 최소 1px 검정으로 또렷하게 남는다(검정 0 유지). DPI를
    300×oversample로 저장 → 한글이 계산하는 원래 크기(JPG픽셀÷DPI)가 표시 크기와 같아져
    비율 100%·자르기 없음으로 나온다. 전체 캔버스(window extent)를 그려 자르기 여백도 보존한다.

    반환: (jpg_bytes, nw, nh, eff_dpi) | None(안전 렌더 불가 → 호출자가 축소 포기, 고해상 유지)."""
    sys.path.insert(0, os.path.dirname(__file__))
    import worker
    nw, nh, eff_dpi = _vector_target_px(cw, ch)
    suffix = ".wmf" if fmt == "wmf" else ".emf"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(original_bytes)
        if fmt == "wmf":
            img = worker.render_wmf_window_px(tmp, nw, nh)
        else:
            try:
                img = worker.render_emf_gdi_custom(tmp, nw, nh)
            except Exception:
                img = None
        if img is None:
            return None
        if img.mode != "RGB":
            img = worker.to_rgb(img)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=95, dpi=(eff_dpi, eff_dpi))
        return out.getvalue(), nw, nh, eff_dpi
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _resize_jpg_bytes(data: bytes, f: float, target_dpi: int = 300) -> bytes:
    """JPG 픽셀을 f배(0<f<1, 축소)로 리샘플링하고 DPI를 target_dpi로 설정한다.

    호출 측에서 '표시 해상도가 정확히 target_dpi가 되도록' f를 계산하므로, DPI를
    target_dpi로 맞추면 한글이 보이는 확대/축소 비율이 100%가 된다(한글은 그림의
    원래 크기를 JPG픽셀÷DPI로 재계산함). f가 표시 해상도와 무관하게 잘못 계산되면
    비율이 틀어지므로(예전 96→300 버그), f는 반드시 표시 해상도 기준으로 산출해야 한다."""
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    nw, nh = max(1, round(w * f)), max(1, round(h * f))
    if nw >= w and nh >= h:
        return data
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = img.resize((nw, nh), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95, dpi=(target_dpi, target_dpi))
    return out.getvalue()


def size_adjust_hwpx(path: str, vector_origins: dict = None) -> None:
    """변환 완료된 HWPX에 '사이즈 조정'을 적용한다(변환과 분리된 후처리).

    한글이 그림에 표시하는 '확대/축소 비율'(실측으로 확정한 식):
        비율 = curSz × DPI / (7200 × 잘린비율 × JPG픽셀)
    즉 한글은 그림의 원래 크기를 JPG픽셀÷DPI로 재계산하고, 자르기(clip) 영역만큼만
    보이는 부분으로 환산한다. (XML의 orgSz/scaMatrix는 stale 값이라 무시된다.)

    따라서 사이즈 조정은 **JPG 픽셀만** r배로 줄이면 된다(DPI·XML 미변경):
      - 현재 비율 r = max(가로비율, 세로비율) 을 위 식으로 계산
      - r > 100% (이미 확대 상태)면 건너뜀(축소 시 화질 손상) — 규칙 7
      - JPG를 r배로 리샘플 → 비율이 큰 축 100%가 되고, 보이는 해상도는 동일하게 유지,
        남아돌던 픽셀만 제거되어 용량이 준다. curSz·clip 불변이라 표시 크기·자르기도 동일.
    전제: 반드시 '변환이 끝난 출력 파일'에 후처리로만 적용(변환 로직 미변경).

    벡터 예외: vector_origins={출력ZIP경로:(fmt,원본바이트)}에 든 그림(WMF/EMF에서 변환됨)은
    JPG 다운샘플 대신 **원본 벡터를 작은 크기로 다시 렌더**한다 — 가는 선이 다운샘플로 회색이
    되는 걸 막는다. HWPX는 자르기가 비율(imgClip/imgDim=1.0)이라 픽셀이 바뀌어도 XML 수정이
    불필요하다(OLE의 off44와 달리). EMF는 좌표 검증이 어려워 건너뛴다(원본 렌더 유지)."""
    vector_origins = vector_origins or {}
    PIC_RE = re.compile(r'<hp:pic\b.*?</hp:pic>', re.S)
    SECTION_RE = re.compile(r'section\d+\.xml$', re.I)

    with zipfile.ZipFile(path, "r") as zf:
        infos = zf.infolist()
        contents = {i.filename: zf.read(i.filename) for i in infos}

    # 매니페스트(content.hpf): itemID → href(BinData 경로)
    id_to_href: dict = {}
    for fn, raw in contents.items():
        if fn.lower().endswith(".hpf"):
            txt = raw.decode("utf-8", "replace")
            for m in re.finditer(r'<(?:\w+:)?item\b[^>]*?id="([^"]+)"[^>]*?href="([^"]+)"', txt):
                id_to_href[m.group(1)] = m.group(2)

    # 각 그림 블록에서 curSz와 잘린비율(clip/dim) 수집 + ref 등장 횟수
    ref_count: dict = {}
    ref_info: dict = {}   # ref -> (curW, curH, clipFracW, clipFracH)
    for fn, raw in contents.items():
        if not SECTION_RE.search(fn):
            continue
        txt = raw.decode("utf-8", "replace")
        for blk in PIC_RE.findall(txt):
            ref_m = re.search(r'binaryItemIDRef="([^"]+)"', blk)
            cur_m = re.search(r'<hp:curSz width="(\d+)" height="(\d+)"', blk)
            if not (ref_m and cur_m):
                continue
            ref = ref_m.group(1)
            ref_count[ref] = ref_count.get(ref, 0) + 1
            cw, ch = int(cur_m.group(1)), int(cur_m.group(2))
            clip_m = re.search(r'<hp:imgClip left="(-?\d+)" right="(-?\d+)" top="(-?\d+)" bottom="(-?\d+)"', blk)
            dim_m = re.search(r'<hp:imgDim dimwidth="(\d+)" dimheight="(\d+)"', blk)
            cfw = cfh = 1.0
            if clip_m and dim_m:
                cl, cr, ct, cb = map(int, clip_m.groups())
                dw, dh = map(int, dim_m.groups())
                if dw > 0:
                    cfw = (cr - cl) / dw
                if dh > 0:
                    cfh = (cb - ct) / dh
            ref_info[ref] = (cw, ch, cfw, cfh)

    # 화면 표시 해상도(보이는 픽셀 ÷ 표시 inch)가 이 값을 넘는 그림만 이 값으로 낮춘다.
    # 300dpi는 인쇄 품질 기준이라 시각적 손실이 없고, 그 이하 그림은 손대지 않아 화질을 보존한다.
    TARGET_DPI = 300.0
    adjusted = 0
    skipped = 0
    for ref, (cw, ch, cfw, cfh) in ref_info.items():
        if ref_count.get(ref, 0) != 1:    # 같은 이미지가 여러 곳에 다른 크기로 → 안전하게 skip
            skipped += 1
            continue
        href = id_to_href.get(ref)
        key = None
        if href:
            key = next((fn for fn in contents
                        if fn.lower() == href.lower() or fn.lower().endswith("/" + href.lower())), None)
        if key is None:
            skipped += 1
            continue
        try:
            img = Image.open(io.BytesIO(contents[key]))
            pw, ph = img.size
        except Exception:
            skipped += 1
            continue
        if min(cw, ch, pw, ph, cfw, cfh) <= 0:
            skipped += 1
            continue
        # 표시 해상도(dpi) = 보이는(=잘린 후) 픽셀 / 표시 크기(inch). 축마다 계산.
        eff_w = cfw * pw * 7200.0 / cw
        eff_h = cfh * ph * 7200.0 / ch
        eff = min(eff_w, eff_h)           # 더 낮은 축 기준(이 축이 100%가 되고 나머지는 그 이상 유지)
        if eff <= TARGET_DPI * 1.1:       # 이미 ~300dpi 이하 → 잉여 픽셀 없음 → 건너뜀(화질 보존)
            skipped += 1
            continue
        try:
            before = len(contents[key])
            origin = vector_origins.get(key)
            if origin is not None:
                vfmt, vbytes = origin
                if vfmt != "wmf":            # EMF 등: 좌표 검증 어려움 → 원본 렌더 유지(안전)
                    skipped += 1
                    continue
                out = _rerender_vector_jpg(vbytes, vfmt, cw, ch)   # 벡터를 작게 '재렌더'(또렷)
                if out is None:              # 안전 렌더 불가 → 고해상 유지
                    skipped += 1
                    continue
                new_jpg, nw, nh, _ = out
                contents[key] = new_jpg      # 자르기 비율(imgClip/imgDim)은 그대로 1.0 → XML 미수정
                msg = (f"사이즈 조정(재렌더): 표시 {round(eff)}dpi→ {nw}x{nh}px "
                       f"(용량 {before // 1024}KB→{len(new_jpg) // 1024}KB)")
            else:                            # 일반 래스터: 다운샘플(사진은 손실 없이 줄어듦)
                f = TARGET_DPI / eff
                resized = _resize_jpg_bytes(contents[key], f, int(TARGET_DPI))
                if len(resized) < before:    # 재인코딩으로 커지면 원본 유지(용량은 절대 안 늘림)
                    contents[key] = resized
                msg = (f"사이즈 조정: 표시 {round(eff)}dpi→{int(TARGET_DPI)}dpi, 비율→100% "
                       f"(용량 {before // 1024}KB→{len(contents[key]) // 1024}KB)")
            adjusted += 1
            _print_json({"event": "size", "name": href.split("/")[-1], "message": msg})
        except Exception as e:
            skipped += 1
            _print_json({"event": "size", "name": href, "message": f"사이즈 조정 실패: {e}"})

    if adjusted == 0:
        _print_json({"event": "sizeDone", "adjusted": 0, "skipped": skipped})
        return

    # zip 재작성 (원본 ZipInfo 보존 — 압축방식/순서 유지). JPG 바이트만 교체, XML 미변경.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".hwpx")
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
            for info in infos:
                zf_out.writestr(info, contents[info.filename])
        shutil.move(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    _print_json({"event": "sizeDone", "adjusted": adjusted, "skipped": skipped})


def read_ole_picture_info(hwp_path: str) -> dict:
    """OLE HWP의 BodyText를 읽어 bin_id → (curW, curH, off44(L,T,R,B)) 매핑을 만든다(읽기 전용).

    - curSz(표시 크기)  : 그림 부모 Tag76(SHAPE_COMPONENT)의 offset 28/32. (실측 검증됨)
    - off44(보이는 영역): 그림 Tag85(PICTURE)의 offset 44, 이미지 고유좌표(px×75/wext×75)의 가시 rect.
    같은 bin이 여러 그림에 다른 크기로 쓰이면(중복) 안전하게 제외한다."""
    import zlib
    try:
        import olefile
    except Exception:
        return {}
    info, count = {}, {}
    try:
        ole = olefile.OleFileIO(hwp_path)
    except Exception:
        return {}
    try:
        sections = sorted([n for n in ole.listdir()
                           if len(n) >= 2 and n[0] == 'BodyText' and n[1].startswith('Section')])
        for sn in sections:
            try:
                data = ole.openstream(sn).read()
                try:
                    data = zlib.decompress(data, -15)
                except Exception:
                    try:
                        data = zlib.decompress(data)
                    except Exception:
                        pass
            except Exception:
                continue
            pos, t76 = 0, {}
            while pos < len(data) - 4:
                header = struct.unpack_from('<I', data, pos)[0]
                tag = header & 0x3FF
                level = (header >> 10) & 0x3FF
                size = (header >> 20) & 0xFFF
                hdr = 4
                if size == 0xFFF:
                    if pos + 8 > len(data):
                        break
                    size = struct.unpack_from('<I', data, pos + 4)[0]
                    hdr = 8
                rec = data[pos + hdr: pos + hdr + size]
                if tag == 76:
                    t76[level] = rec
                elif tag == 85 and size >= 72:
                    try:
                        binid = struct.unpack_from('<H', rec, 71)[0]
                        crop = struct.unpack_from('<4i', rec, 44)
                        p = t76.get(level - 1)
                        if p and len(p) >= 36:
                            cw, ch = struct.unpack_from('<2i', p, 28)
                            if 1 <= binid <= 0xFFFF and cw > 0 and ch > 0:
                                count[binid] = count.get(binid, 0) + 1
                                info[binid] = (cw, ch, crop)
                    except Exception:
                        pass
                pos = pos + hdr + size
    except Exception:
        pass
    finally:
        try:
            ole.close()
        except Exception:
            pass
    return {b: v for b, v in info.items() if count.get(b, 0) == 1}


def size_adjust_jpg_for_record(jpg_data: bytes, original_bytes: bytes, fmt: str,
                               pic: tuple, target_dpi: int = SIZE_TARGET_DPI):
    """변환된 JPG 1장을 '표시 해상도 target_dpi' 기준으로 정리한다(OLE 그림 1장).

    ⚠ 문서 지오메트리(Tag85 off44 자르기 사각형, off12 네 점, curSz)는 **절대 건드리지 않는다.**
    실측 결과(2026-06, `D:\\작업방\\test_2010_*`), off44를 JPG 픽셀에 맞춰 다시 쓰면 오히려
    한글이 그림을 비우거나(빈 박스) 엉뚱하게 자른다(잘림) — 한글은 off44를 그림 고유 좌표계
    (래스터=px, WMF=window-extent)에서 해석하고 off12와 함께 쓰기 때문에, off44만 바꾸면
    그 관계가 깨진다. 자르기된 WMF(off44 그대로 유지)는 변환 후에도 정상 표시되는 게 그 증거다.
    따라서 픽셀(바이트)만 바꾼다:
      - WMF: 벡터를 표시 크기로 '다시 렌더'(또렷). 다운샘플하면 가는 선이 회색으로 흐려진다.
             off44는 window-extent 단위 그대로 둬 한글이 jpg를 그 프레임에 매핑하게 한다.
      - 일반 래스터: JPG를 target_dpi로 다운샘플(사진은 손실 없이 줄어듦). off44(원래 px단위)는
             새 px보다 커지지만, 한글이 '전체 표시'로 클램프하므로 그대로 둔다.
      - EMF/그 외 벡터: 건너뜀(원본 렌더 유지 — 안전 우선).
    자르기된 그림은 모두 건너뛴다(off44 left/top≠0 → 보이는 영역 일부만, 보통 조정 대상도 아님).

    반환: (jpg_bytes, eff_dpi_before | None). eff=None이면 미조정."""
    cw, ch, crop = pic
    try:
        img = Image.open(io.BytesIO(jpg_data))
        pw, ph = img.size
    except Exception:
        return jpg_data, None
    if min(cw, ch, pw, ph) <= 0:
        return jpg_data, None
    left, top, right, bottom = crop

    if fmt == "wmf":
        # 전체 캔버스(off44 단위)는 window extent×75. 자르기 없음 = off44가 그 전체를 덮음.
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            import worker
            _, _, wext, _ = worker._wmf_window(original_bytes)
        except Exception:
            return jpg_data, None
        if not (wext and wext[0] > 0 and wext[1] > 0):
            return jpg_data, None            # 익스텐트 불명 → skip
        fullw, fullh = wext[0] * 75, wext[1] * 75
        uncropped = (left == 0 and top == 0
                     and abs(right - fullw) <= fullw * 0.03
                     and abs(bottom - fullh) <= fullh * 0.03)
        if not uncropped:
            return jpg_data, None            # 잘린 그림 → 원본 렌더 유지(깨짐 방지)
        eff = min(pw * 7200.0 / cw, ph * 7200.0 / ch)
        if eff <= target_dpi * 1.1:
            return jpg_data, None            # 이미 ~목표 이하 → 잉여 픽셀 없음
        out = _rerender_vector_jpg(original_bytes, fmt, cw, ch)
        if out is None:
            return jpg_data, None            # 안전 렌더 불가 → 고해상 유지
        new_jpg, _, _, _ = out
        return new_jpg, eff

    if fmt == "emf":
        return jpg_data, None                # EMF: 좌표 미검증 → 원본 렌더 유지(안전)

    # 일반 래스터: 자르기 없으면 다운샘플(off44는 그대로 — 한글이 전체표시로 클램프).
    fullw, fullh = pw * 75, ph * 75
    uncropped = (left == 0 and top == 0
                 and abs(right - fullw) <= fullw * 0.03
                 and abs(bottom - fullh) <= fullh * 0.03)
    if not uncropped:
        return jpg_data, None
    eff = min(pw * 7200.0 / cw, ph * 7200.0 / ch)
    if eff <= target_dpi * 1.1:
        return jpg_data, None
    new = _resize_jpg_bytes(jpg_data, target_dpi / eff, target_dpi)
    if len(new) >= len(jpg_data):
        return jpg_data, None                # 재인코딩으로 커지면 원본 유지
    return new, eff


# ─────────────────────────────────────────
# HWP 프로세서 (OLE Compound File — 한글 2010)
# ─────────────────────────────────────────

class HwpProcessor:
    """HWP(OLE) 파일의 BinData 이미지를 JPG로 변환합니다."""

    STGM_READWRITE = 0x00000002
    STGM_SHARE_EXCLUSIVE = 0x00000010
    STGM_DIRECT = 0x00000000
    STGM_READ = 0x00000000
    STGM_SHARE_DENY_WRITE = 0x00000020

    def _open_storage(self, hwp_path: str, read_only: bool = False):
        """OLE 스토리지를 엽니다. 경로 버그를 우회하기 위해 임시 파일로 복사하여 처리할 수 있습니다."""
        import pythoncom
        import tempfile
        import shutil
        if read_only:
            mode = self.STGM_READ | self.STGM_SHARE_DENY_WRITE | self.STGM_DIRECT
        else:
            mode = self.STGM_READWRITE | self.STGM_SHARE_EXCLUSIVE | self.STGM_DIRECT

        # StgOpenStorage의 비ASCII/긴 경로 문제를 우회하기 위해 임시 파일로 복사해 연다.
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".hwp")
        os.close(tmp_fd)
        try:
            shutil.copy2(hwp_path, tmp_path)
            self._current_tmp_path = tmp_path
            return pythoncom.StgOpenStorage(tmp_path, None, mode)
        except Exception:
            # 열기 실패 시 임시 사본을 남기지 않는다
            self._current_tmp_path = None
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def _enum_bindata_streams(self, bindata_storage) -> list:
        """BinData 스토리지 내 스트림 이름 목록을 반환합니다."""
        streams = []
        try:
            enum = bindata_storage.EnumElements(0, None, 0)
            while True:
                items = enum.Next(1)
                if not items:
                    break
                stat = items[0]
                name = stat[0]
                streams.append(name)
        except Exception as e:
            # 빈 목록을 조용히 반환하면 "이미지 0개"로만 보여 원인 추적이 어렵다.
            # (예: PyInstaller 빌드에 win32timezone 누락 시 enum.Next가 여기서 실패)
            emit_error(f"BinData 스트림 열거 실패: {e}")
        return streams

    def _decompress(self, data: bytes) -> tuple[bytes, str]:
        """zlib 압축 해제 (해제된데이터, 압축타입)"""
        import zlib
        try:
            return zlib.decompress(data, -15), 'raw'
        except zlib.error:
            pass
        try:
            return zlib.decompress(data), 'zlib'
        except zlib.error:
            pass
        return data, 'none'

    def _compress(self, data: bytes, comp_type: str) -> bytes:
        """원래 압축 방식과 동일하게 재압축"""
        import zlib
        if comp_type == 'raw':
            comp = zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -15)
            return comp.compress(data) + comp.flush()
        elif comp_type == 'zlib':
            return zlib.compress(data)
        return data

    def _read_stream(self, storage, stream_name: str) -> bytes:
        """스토리지에서 스트림 데이터를 읽습니다."""
        mode = self.STGM_READ | self.STGM_SHARE_EXCLUSIVE
        stream = storage.OpenStream(stream_name, None, mode, 0)
        stat = stream.Stat(0)
        size = stat[2]
        return stream.Read(size)

    def _write_stream(self, storage, stream_name: str, data: bytes):
        """스토리지의 스트림에 데이터를 씁니다."""
        mode = self.STGM_READWRITE | self.STGM_SHARE_EXCLUSIVE
        stream = storage.OpenStream(stream_name, None, mode, 0)
        stream.SetSize(len(data))
        stream.Seek(0, 0)
        stream.Write(data)

    def _patch_docinfo(self, docinfo_data: bytes, conversions: dict) -> bytes:
        """HWPTAG_BIN_DATA(tag18) 임베딩 그림의 확장자를 conversions={bin_id:new_ext}로 바꾼다.

        ⚠ 'jpeg'(4자)·'tiff'·'webp' → 'jpg'(3자)처럼 **길이가 다른** 확장자도 처리해야 한다.
        예전엔 같은 길이(3자)만 치환해서, 한글 2022가 .jpeg로 저장한 그림 등은 DocInfo엔
        'jpeg'로 남고 스트림만 BIN....jpg로 바뀌어 한글이 스트림을 못 찾고 **빈 박스**가 됐다.
        길이가 바뀌면 레코드가 짧아지므로 레코드를 다시 만들고 4바이트 헤더의 size도 갱신한다.

        BIN_DATA(임베딩) 레이아웃: prop(2) + binId(2) + extLen(2) + ext(extLen*2). ext가 마지막
        필드라 끝부분만 줄이면 된다. 안 건드리는 레코드는 원본 바이트 그대로 보존한다."""
        import struct
        out = bytearray()
        n = len(docinfo_data)
        pos = 0
        while pos + 4 <= n:
            header = struct.unpack_from('<I', docinfo_data, pos)[0]
            tag_id = header & 0x3FF
            level = (header >> 10) & 0x3FF
            size = (header >> 20) & 0xFFF
            hdr_sz = 4
            if size == 0xFFF:
                if pos + 8 > n:
                    break
                size = struct.unpack_from('<I', docinfo_data, pos + 4)[0]
                hdr_sz = 8
            rec_end = pos + hdr_sz + size

            patched = None
            if tag_id == 18 and size >= 6:
                rec = docinfo_data[pos + hdr_sz: rec_end]
                prop = struct.unpack_from('<H', rec, 0)[0]
                bin_id = struct.unpack_from('<H', rec, 2)[0]
                # 임베딩(type=prop&0xF==1) 그림만, 변환된 bin만.
                if (prop & 0x000F) == 1 and bin_id in conversions:
                    new_ext = conversions[bin_id]
                    ext_len = struct.unpack_from('<H', rec, 4)[0]
                    old_end = 6 + ext_len * 2
                    if old_end <= len(rec):
                        new_rec = (rec[:4]
                                   + struct.pack('<H', len(new_ext))
                                   + new_ext.encode('utf-16-le')
                                   + rec[old_end:])     # 보통 비어 있음(ext가 마지막 필드)
                        new_size = len(new_rec)
                        if new_size < 0xFFF:
                            new_hdr = ((tag_id & 0x3FF) | ((level & 0x3FF) << 10)
                                       | ((new_size & 0xFFF) << 20))
                            patched = struct.pack('<I', new_hdr) + new_rec
                        else:
                            new_hdr = (tag_id & 0x3FF) | ((level & 0x3FF) << 10) | (0xFFF << 20)
                            patched = struct.pack('<I', new_hdr) + struct.pack('<I', new_size) + new_rec

            if patched is not None:
                out += patched
            else:
                out += docinfo_data[pos:rec_end]      # 미변경 레코드는 바이트 보존
            pos = rec_end
        if pos < n:
            out += docinfo_data[pos:]
        return bytes(out)

    def scan(self, hwp_path: str) -> list:
        """HWP 파일 내 이미지 목록을 반환합니다."""
        images = []
        self._current_tmp_path = None
        try:
            storage = self._open_storage(hwp_path, read_only=True)
            try:
                bindata = storage.OpenStorage(
                    "BinData", None,
                    self.STGM_READ | self.STGM_SHARE_EXCLUSIVE, None, 0
                )
            except Exception as e:
                emit_error(f"BinData 스토리지를 열 수 없습니다: {e}")
                return images

            stream_names = self._enum_bindata_streams(bindata)
            for name in stream_names:
                try:
                    data = self._read_stream(bindata, name)
                    uncomp_data, _ = self._decompress(data)
                    fmt = detect_format(uncomp_data)
                    if fmt == "unknown":
                        fmt = name.split(".")[-1].lower()
                    images.append({
                        "name": name,
                        "format": fmt,
                        "size": len(uncomp_data),
                    })
                except Exception:
                    pass

            del bindata
            del storage
        except Exception as e:
            emit_error(f"HWP 스캔 실패: {e}")
        finally:
            if hasattr(self, '_current_tmp_path') and self._current_tmp_path:
                try:
                    os.remove(self._current_tmp_path)
                except OSError:
                    pass
                self._current_tmp_path = None
        return images

    def convert(self, hwp_path: str, output_path: str, mode: str, size_adjust: bool = False):
        """HWP 파일 내 이미지를 JPG로 변환합니다. size_adjust=True면 변환 후 사이즈 조정."""
        import pythoncom
        import shutil
        import tempfile
        import sys
        import re

        # worker.py 모듈 임포트를 위해 경로 추가
        sys.path.insert(0, os.path.dirname(__file__))
        import worker

        # 사이즈 조정용 그림 정보(curSz/off44)는 변환이 BodyText를 바꾸지 않으므로
        # 원본에서 미리 읽어둔다(읽기 전용). COM 세션과 충돌하지 않게 먼저 수행.
        pic_info = read_ole_picture_info(hwp_path) if size_adjust else {}
        size_adjusted = 0
        size_skipped = 0

        # 임시 파일 경로를 초기화
        self._current_tmp_path = None
        storage = self._open_storage(hwp_path, read_only=False)
        try:
            bindata = storage.OpenStorage(
                "BinData", None,
                self.STGM_READWRITE | self.STGM_SHARE_EXCLUSIVE, None, 0
            )
        except Exception as e:
            emit_error(f"BinData 스토리지를 열 수 없습니다: {e}")
            return

        stream_names = self._enum_bindata_streams(bindata)
        converted = 0
        skipped = 0

        conversions = {}
        stream_renames = []

        for name in stream_names:
            try:
                data = self._read_stream(bindata, name)
                uncomp_data, comp_type = self._decompress(data)
                fmt = detect_format(uncomp_data)
                if fmt == "unknown":
                    fmt = name.split(".")[-1].lower()

                if not should_convert(fmt, mode):
                    skipped += 1
                    continue

                # 변환 (WMF/EMF는 GDI 렌더링 — 그림 자르기 여백 보존 포함, 그 외는 PIL)
                # HWPX 경로와 동일한 image_bytes_to_jpg()를 거쳐 포맷 처리가 갈라지지 않게 한다.
                try:
                    jpg_data = image_bytes_to_jpg(uncomp_data, fmt)
                except Exception as e:
                    emit_progress(False, name, fmt, "jpg", f"변환 실패: {e}")
                    skipped += 1
                    continue

                m = re.match(r'^BIN([0-9A-Fa-f]{4})\.', name)
                bin_id = int(m.group(1), 16) if m else None

                # 사이즈 조정(변환이 끝난 JPG에만 후처리로 적용 — HWPX와 동일 규칙)
                if size_adjust and bin_id is not None and bin_id in pic_info:
                    try:
                        new_jpg, eff = size_adjust_jpg_for_record(
                            jpg_data, uncomp_data, fmt, pic_info[bin_id])
                        if eff is not None:
                            before_kb = len(jpg_data) // 1024
                            jpg_data = new_jpg
                            size_adjusted += 1
                            _print_json({"event": "size", "name": name,
                                         "message": f"사이즈 조정: 표시 {round(eff)}dpi "
                                                    f"(용량 {before_kb}KB→{len(jpg_data)//1024}KB)"})
                        else:
                            size_skipped += 1
                    except Exception as e:
                        size_skipped += 1
                        _print_json({"event": "size", "name": name, "message": f"사이즈 조정 실패: {e}"})

                # 압축 유지
                jpg_data_out = self._compress(jpg_data, comp_type)

                # 변환된 데이터를 기존 이름의 스트림에 우선 덮어씁니다.
                self._write_stream(bindata, name, jpg_data_out)

                # 변환 성공 시 DocInfo 패치 및 스트림 이름 변경을 위해 기록
                if m:
                    conversions[bin_id] = "jpg"
                    new_name = f"BIN{m.group(1)}.jpg"
                    if new_name != name:
                        stream_renames.append((name, new_name))

                converted += 1
                emit_progress(True, name, fmt, "jpg", "변환 완료")

            except Exception as e:
                emit_progress(False, name, "unknown", "jpg", f"처리 오류: {e}")
                skipped += 1

        # 모든 스트림 변환 완료 후 DocInfo 일괄 패치
        if conversions:
            try:
                docinfo_data = self._read_stream(storage, "DocInfo")
                docinfo_uncomp, comp_type = self._decompress(docinfo_data)
                
                docinfo_patched = self._patch_docinfo(docinfo_uncomp, conversions)
                
                docinfo_comp = self._compress(docinfo_patched, comp_type)
                self._write_stream(storage, "DocInfo", docinfo_comp)
                
                # 스트림 이름 일괄 변경
                for old_name, new_name in stream_renames:
                    try:
                        bindata.RenameElement(old_name, new_name)
                    except Exception:
                        pass
            except Exception as e:
                emit_error(f"DocInfo 패치 및 스트림 이름 변경 실패: {e}")

        # OLE 커밋
        try:
            bindata.Commit(0)
            storage.Commit(0)
        except Exception as e:
            emit_error(f"OLE 커밋 실패: {e}")

        # 리소스 정리 후, 임시 파일을 최종 output_path로 복사
        del bindata
        del storage
        if hasattr(self, '_current_tmp_path') and self._current_tmp_path:
            try:
                shutil.copy2(self._current_tmp_path, output_path)
                os.remove(self._current_tmp_path)
                self._current_tmp_path = None
            except Exception as e:
                emit_error(f"최종 파일 저장 실패: {e}")

        emit_done(converted, skipped)
        if size_adjust:
            _print_json({"event": "sizeDone", "adjusted": size_adjusted, "skipped": size_skipped})


# ─────────────────────────────────────────
# HWPX 프로세서 (ZIP 아카이브 — 한글 2022)
# ─────────────────────────────────────────

class HwpxProcessor:
    """HWPX(ZIP) 파일의 BinData 이미지를 JPG로 변환합니다."""

    def _find_bindata_entries(self, zf: zipfile.ZipFile) -> list:
        """ZIP 내 BinData/ 경로의 파일 목록을 반환합니다."""
        entries = []
        for info in zf.infolist():
            # BinData/ 폴더 내 파일 (대소문자 무시)
            if info.filename.lower().startswith("bindata/") and not info.is_dir():
                entries.append(info.filename)
        return entries

    def _get_format_from_name(self, filename: str) -> str:
        """파일명에서 확장자 기반 형식을 반환합니다."""
        ext = Path(filename).suffix.lower().lstrip(".")
        if ext in ("jpg", "jpeg"):
            return "jpg"
        if ext in ("tif", "tiff"):
            return "tif"
        return ext if ext else "unknown"

    def scan(self, hwpx_path: str) -> list:
        """HWPX 파일 내 이미지 목록을 반환합니다."""
        images = []
        try:
            with zipfile.ZipFile(hwpx_path, "r") as zf:
                entries = self._find_bindata_entries(zf)
                for entry in entries:
                    data = zf.read(entry)
                    # 우선 바이너리 시그니처로 감지, 실패 시 확장자
                    fmt = detect_format(data)
                    if fmt == "unknown":
                        fmt = self._get_format_from_name(entry)
                    images.append({
                        "name": Path(entry).name,
                        "format": fmt,
                        "size": len(data),
                    })
        except Exception as e:
            emit_error(f"HWPX 스캔 실패: {e}")
        return images

    def convert(self, hwpx_path: str, output_path: str, mode: str):
        """HWPX 파일 내 이미지를 JPG로 변환합니다."""
        # 원본 → 출력 복사
        shutil.copy2(hwpx_path, output_path)

        converted = 0
        skipped = 0
        # 변환 대상 매핑: {원래 ZIP 경로: (새 ZIP 경로, jpg 바이트)}
        replacements: dict[str, tuple[str, bytes]] = {}
        # 이름 변경 매핑: {원래 파일명: 새 파일명} (XML 참조 수정용)
        name_map: dict[str, str] = {}
        # 사이즈 조정용: 변환된 벡터(WMF/EMF)의 '원본 바이트'를 출력 ZIP 경로로 보관.
        # size_adjust_hwpx가 다운샘플(선 흐려짐) 대신 원본 벡터를 작은 크기로 재렌더하도록.
        self.vector_origins: dict[str, tuple[str, bytes]] = {}

        with zipfile.ZipFile(output_path, "r") as zf:
            entries = self._find_bindata_entries(zf)

            for entry in entries:
                data = zf.read(entry)
                fmt = detect_format(data)
                if fmt == "unknown":
                    fmt = self._get_format_from_name(entry)

                original_name = Path(entry).name

                if not should_convert(fmt, mode):
                    skipped += 1
                    continue

                try:
                    jpg_data = image_bytes_to_jpg(data, fmt)
                except Exception as e:
                    emit_progress(False, original_name, fmt, "jpg", f"변환 실패: {e}")
                    skipped += 1
                    continue

                # 새 경로: 확장자를 .jpg로 변경
                stem = Path(entry).stem
                parent = str(Path(entry).parent).replace("\\", "/")
                new_entry = f"{parent}/{stem}.jpg"

                replacements[entry] = (new_entry, jpg_data)
                name_map[original_name] = f"{stem}.jpg"
                if fmt in ("wmf", "emf"):
                    self.vector_origins[new_entry] = (fmt, data)

                converted += 1
                emit_progress(True, original_name, fmt, "jpg", "변환 완료")

        if not replacements:
            emit_done(converted, skipped)
            return

        # 새 ZIP 생성 (임시 파일)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".hwpx")
        os.close(tmp_fd)

        try:
            with zipfile.ZipFile(output_path, "r") as zf_in, \
                 zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_out:

                for info in zf_in.infolist():
                    data = zf_in.read(info.filename)

                    if info.filename in replacements:
                        # 변환된 이미지로 교체
                        new_name, jpg_data = replacements[info.filename]
                        new_info = zipfile.ZipInfo(new_name, date_time=info.date_time)
                        new_info.compress_type = zipfile.ZIP_DEFLATED
                        zf_out.writestr(new_info, jpg_data)
                    else:
                        # XML 파일이면 이미지 참조 수정
                        if info.filename.lower().endswith((".xml", ".hpf")):
                            text = data.decode("utf-8", errors="replace")
                            for old_name, new_name_str in name_map.items():
                                text = text.replace(old_name, new_name_str)
                            # OPF 매니페스트(content.hpf): 변환된 이미지의 media-type을 image/jpeg로 교체.
                            # href만 .jpg로 바뀌고 media-type이 image/wmf로 남으면 한글이 여전히
                            # wmf로 인식한다(OLE 경로에서 DocInfo 확장자를 패치하는 것과 같은 처리).
                            if info.filename.lower().endswith(".hpf"):
                                new_jpgs = set(name_map.values())

                                def _fix_mediatype(m):
                                    tag = m.group(0)
                                    hm = re.search(r'href="[^"]*?([^/"]+)"', tag)
                                    if hm and hm.group(1) in new_jpgs:
                                        tag = re.sub(r'media-type="[^"]*"',
                                                     'media-type="image/jpeg"', tag)
                                    return tag

                                text = re.sub(r'<(?:\w+:)?item\b[^>]*?/>',
                                              _fix_mediatype, text)
                            data = text.encode("utf-8")
                            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                            new_info.compress_type = zipfile.ZIP_DEFLATED
                            zf_out.writestr(new_info, data)
                        else:
                            # 그 외 파일은 그대로 복사
                            zf_out.writestr(info, data)

            # 임시 파일을 출력 경로로 이동
            shutil.move(tmp_path, output_path)

        except Exception as e:
            emit_error(f"HWPX 재패키징 실패: {e}")
            # 임시 파일 정리
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        emit_done(converted, skipped)


# ─────────────────────────────────────────
# NDJSON 출력 헬퍼
# ─────────────────────────────────────────

def _print_json(data: dict):
    # 안전한 UTF-8 출력 (Windows 한글 인코딩 문제 회피)
    json_bytes = json.dumps(data, ensure_ascii=False).encode('utf-8')
    sys.stdout.buffer.write(json_bytes + b'\n')
    sys.stdout.buffer.flush()

def emit_progress(success: bool, name: str, from_fmt: str, to_fmt: str, message: str):
    _print_json({
        "event": "progress",
        "success": success,
        "name": name,
        "from": from_fmt,
        "to": to_fmt,
        "message": message,
    })

def emit_done(total_converted: int, total_skipped: int):
    _print_json({
        "event": "done",
        "totalConverted": total_converted,
        "totalSkipped": total_skipped,
    })

def emit_scan(images: list):
    _print_json({
        "event": "scan",
        "images": images,
    })

def emit_error(message: str):
    _print_json({
        "event": "error",
        "error": message,
    })


# ─────────────────────────────────────────
# 프로세서 선택
# ─────────────────────────────────────────

def get_processor(file_path: str):
    """파일의 매직 바이트를 검사하여 적절한 프로세서를 반환합니다."""
    try:
        with open(file_path, "rb") as f:
            magic = f.read(8)
            
        # OLE Compound File (HWP 2010 이하)
        if magic.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return HwpProcessor()
            
        # ZIP Archive (HWPX 2022 이상)
        if magic.startswith(b"PK\x03\x04"):
            return HwpxProcessor()
            
        # 매직 바이트 판별 실패 시 확장자 폴백
        ext = Path(file_path).suffix.lower()
        if ext == ".hwp":
            return HwpProcessor()
        elif ext == ".hwpx":
            return HwpxProcessor()
    except Exception as e:
        emit_error(f"파일을 읽을 수 없습니다: {e}")
        return None
    return None


# ─────────────────────────────────────────
# 메인
# ─────────────────────────────────────────

def main():
    try:
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass

        parser = argparse.ArgumentParser(description="HWP/HWPX 이미지 변환 워커")
        parser.add_argument("--input", required=True, help="입력 HWP/HWPX 파일 경로")
        parser.add_argument("--output", required=False, help="출력 파일 경로 (변환 모드 전용)")
        parser.add_argument("--mode", choices=["selective", "all"], default="selective",
                            help="변환 모드: selective (jpg/bmp/emf 제외) 또는 all (전체)")
        parser.add_argument("--scan", action="store_true", help="스캔 모드 (이미지 목록만 반환)")
        parser.add_argument("--size-adjust", action="store_true",
                            help="변환 후 사이즈 조정 적용 (확대/축소 비율 큰 축을 100%로, JPG 용량 절감)")

        args = parser.parse_args()

        input_path = args.input
        if not os.path.isfile(input_path):
            emit_error(f"입력 파일을 찾을 수 없습니다: {input_path}")
            sys.exit(1)

        processor = get_processor(input_path)
        if processor is None:
            emit_error(f"지원하지 않는 파일 형식입니다: {Path(input_path).suffix}")
            sys.exit(1)

        if args.scan:
            # 스캔 모드
            images = processor.scan(input_path)
            emit_scan(images)
        else:
            # 변환 모드
            if not args.output:
                emit_error("변환 모드에서는 --output 인자가 필요합니다.")
                sys.exit(1)
            if isinstance(processor, HwpxProcessor):
                processor.convert(input_path, args.output, args.mode)
                # HWPX: 변환이 끝난 출력 zip에 후처리로 사이즈 조정
                # (변환된 벡터는 원본 바이트를 넘겨 다운샘플 대신 작게 재렌더 — 선 또렷)
                if args.size_adjust:
                    try:
                        size_adjust_hwpx(args.output,
                                         vector_origins=getattr(processor, "vector_origins", None))
                    except Exception as e:
                        emit_error(f"사이즈 조정 실패: {e}")
            else:
                # HWP(OLE): COM 세션 안에서 변환 직후 사이즈 조정(WMF 윈도우 익스텐트가 필요)
                processor.convert(input_path, args.output, args.mode, size_adjust=args.size_adjust)

        sys.exit(0)
    except Exception as e:
        import traceback
        emit_error(f"워커 내부 오류: {e}\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__":
    main()
