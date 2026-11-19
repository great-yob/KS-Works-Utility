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

# 그림이 놓인 쪽 번호(로그의 "03p" 칩)를 계산하는 보조 모듈.
# 프리즈된 exe에서 번들이 누락돼도 변환 자체는 계속돼야 하므로 실패를 흡수한다.
try:
    from hwp_pagemap import (
        PageMap,
        apply_exact_pages,
        bin_item_streams,
        build_page_map_hwp,
        build_page_map_hwpx,
        is_approx_hwp,
        is_approx_hwpx,
        is_unused_hwp,
        is_unused_hwpx,
        lookup_hwp,
        lookup_hwpx,
        note_hwp,
        note_hwpx,
    )
except Exception:                                   # pragma: no cover - 방어적 폴백
    class PageMap:                                  # ok=False → 아무것도 제외하지 않는다
        def __init__(self):
            self.pages, self.used, self.notes, self.ok = {}, set(), {}, False

    def apply_exact_pages(page_map, resolver):
        return False

    def bin_item_streams(docinfo):                  # 표를 모르면 그림 번호에 기대는 일을 하지 않는다
        return {}

    def build_page_map_hwp(path):
        return PageMap()

    def build_page_map_hwpx(path):
        return PageMap()

    def lookup_hwp(page_map, name):
        return None

    def lookup_hwpx(page_map, name):
        return None

    def note_hwp(page_map, name):
        return None

    def note_hwpx(page_map, name):
        return None

    def is_approx_hwp(page_map, name):
        return False

    def is_approx_hwpx(page_map, name):
        return False

    def is_unused_hwp(page_map, name):
        return False

    def is_unused_hwpx(page_map, name):
        return False

# 회전된 그림을 바로 세우는 모듈(Acrobat으로 PDF를 만들 때 회전 그림이 찢어지는 문제).
# 번들이 빠져도 변환은 계속돼야 하므로 실패를 흡수하고, 그때는 회전을 그대로 둔다.
try:
    import hwp_rotation
except Exception:                                   # pragma: no cover - 방어적 폴백
    hwp_rotation = None


def plan_rotation_hwp(path: str):
    """바로 세울 회전 그림 계획(hwp_rotation.RotationPlan). 모듈이 없거나 실패하면 None."""
    if hwp_rotation is None:
        return None
    try:
        return hwp_rotation.plan_hwp(path)
    except Exception:
        return None


def plan_rotation_hwpx(path: str):
    """plan_rotation_hwp의 HWPX판."""
    if hwp_rotation is None:
        return None
    try:
        return hwp_rotation.plan_hwpx(path)
    except Exception:
        return None


def rotate_for_plan(rot_plan, key, name: str, jpg_data: bytes):
    """회전 계획에 든 그림의 픽셀을 미리 돌린다 → (제자리용 jpg | None, {각도: 사본 jpg}).

    문서에 기록하는 건 변환 루프가 끝난 뒤 일괄로 한다. 여기서 실패하면 (None, {})를 돌려
    그 그림만 회전을 그대로 둔다."""
    try:
        angle = rot_plan.in_place.get(key)
        here = hwp_rotation.rotate_jpg(jpg_data, angle) if angle is not None else None
        copies = {a: hwp_rotation.rotate_jpg(jpg_data, a) for a in rot_plan.copies.get(key, [])}
        return here, copies
    except Exception as e:
        emit_error(f"{name} 회전 준비 실패 — 이 그림은 회전을 그대로 둡니다: {e}")
        return None, {}


def _bin_id_of(stream_name: str):
    m = re.match(r'^BIN([0-9A-Fa-f]{4})\.', stream_name)
    return int(m.group(1), 16) if m else None


def sort_by_page(names: list, pages_of) -> list:
    """그림을 쪽 번호 순으로 정렬합니다(로그가 문서를 읽는 순서대로 나오도록).

    BinData 스트림 순서는 한글이 그림을 저장한 순서(작업자가 넣은 순서)라서 최종 쪽 순서와
    무관합니다. 변환 자체는 순서를 타지 않으므로(그림끼리 독립적이고 DocInfo 패치는 루프가
    끝난 뒤 일괄 처리) 보기 좋은 순서로 돌려도 결과는 같습니다.
    쪽을 모르는 그림(채우기 등 위치를 못 매긴 것)은 뒤로 보내고, 같은 쪽끼리는 원래 순서를
    유지합니다."""
    keyed = []
    for index, name in enumerate(names):
        pages = pages_of(name)
        page = pages[0][0] if pages else None
        keyed.append(((page is None, page or 0, index), name))
    keyed.sort(key=lambda item: item[0])
    return [name for _key, name in keyed]


def refine_pages_with_hangul(page_map, path: str):
    """한글이 설치돼 있으면 쪽 번호를 한글에게 직접 물어 정확한 값으로 바꿉니다.

    파일만 보고 세는 쪽 번호는 표가 여러 쪽에 걸치면 어긋난다(그 정보가 파일에 없다).
    한글은 조판 결과를 알고 있으므로, 그림이 매달린 위치를 물어보면 정확한 값을 준다.
    한글이 없거나 느리거나 실패하면 아무 일도 일어나지 않고 추정값이 그대로 쓰인다."""
    try:
        from hwp_pageapi import resolve_pages
    except Exception:
        return False
    try:
        return apply_exact_pages(page_map, lambda anchors: resolve_pages(path, anchors))
    except Exception:
        return False


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
# 확장자 정합성 (이름과 실제 내용이 다른 그림)
# ─────────────────────────────────────────
#
# 그림의 이름(BINxxxx.png, DocInfo 확장자, HWPX href)과 실제 내용이 다른 문서가 있다.
# 실측: 한국양성평등교육진흥원 교재 첫 장 로고 BIN001B.png 의 내용은 온전한 JPEG였다.
# 한글·그림판은 내용을 보고 열어서 멀쩡히 보이지만, 포토샵처럼 확장자로 해석기를 고르는
# 프로그램은 '파일 형식 모듈이 파일을 분석할 수 없다'며 거부한다. 내용(시그니처)으로만
# 형식을 판정하던 '선택 정리'는 이 그림을 '이미 JPG'로 보고 건너뛰어 그대로 남겼다.
# 그래서 이름과 내용이 다르면 형식과 무관하게 변환 대상으로 삼고, 결과는 항상 .jpg 이름에
# JPEG 내용으로 맞춘다. 내용이 이미 JPEG이면 다시 인코딩하지 않고 이름만 바꾼다(무손실).

# 같은 형식의 다른 표기 → detect_format이 쓰는 이름
_EXT_ALIASES = {"jpeg": "jpg", "jpe": "jpg", "jfif": "jpg", "tiff": "tif"}
# 이름·내용 비교는 둘 다 그림 형식일 때만 한다(.OLE 등 그림이 아닌 BinData는 제외).
_IMAGE_FORMATS = {"png", "jpg", "gif", "bmp", "tif", "webp", "emf", "wmf"}


def name_ext(name: str) -> str:
    """이름의 확장자를 detect_format과 같은 표기로 돌려줍니다(jpeg→jpg, tiff→tif). 없으면 ""."""
    ext = Path(name).suffix.lower().lstrip(".")
    return _EXT_ALIASES.get(ext, ext)


def ext_mismatch(name: str, fmt: str) -> bool:
    """이름의 확장자와 실제 내용(시그니처로 감지한 fmt)이 다른 그림인지 확인합니다.
    .jpeg/.jpg처럼 표기만 다른 경우는 같은 형식으로 보고, 어느 한쪽이라도 그림 형식이
    아니면(내용을 모름 등) False — 확실히 다른 그림만 고친다."""
    ext = name_ext(name)
    return ext in _IMAGE_FORMATS and fmt in _IMAGE_FORMATS and ext != fmt


def to_jpg_for_mode(data: bytes, fmt: str, mode: str, mismatch: bool) -> tuple[bytes, bool, bool]:
    """변환 대상 그림을 JPG 바이트로 만듭니다.
    (jpg 바이트, 원본 그대로 썼는지, 투명한 부분이 흰색으로 채워졌는지)를 돌려줍니다.

    이름만 틀렸고 내용이 이미 JPEG인 그림은, 이 모드가 JPG를 다시 인코딩하지 않는 경우
    ('선택 정리') 원본 바이트를 그대로 쓴다 — 확장자만 맞추고 화질·용량은 그대로다."""
    if mismatch and fmt == "jpg" and not should_convert(fmt, mode):
        return data, True, False
    jpg, see_through = image_bytes_to_jpg(data, fmt)
    return jpg, False, see_through


# ─────────────────────────────────────────
# 투명 배경 그림 → 글 뒤로
# ─────────────────────────────────────────
#
# JPG에는 투명이 없어 PNG의 투명한 부분이 흰색으로 채워진다. 그 그림이 '글 앞으로' 놓여
# 글자 위에 겹쳐 있으면 흰 상자가 글자를 가린다(실측: 서명 그림 밑의 '(직인)'·'(인)'이 사라짐,
# 과학전시 심사자료집 내지 34쪽). 한글에서 그 그림을 '글 뒤로'로 바꾸면 원본과 같아졌다
# (렌더 차이 = JPG 잡음, 쪽 수 그대로). '맨 뒤로'(개체 순서)는 결과가 같았고, 다른 개체 위에
# 겹친 그림이면 오히려 그 개체 밑으로 숨기 때문에 하지 않는다.
#   * 바꾸는 것은 '본문과의 배치' 값 하나뿐이다. 한글이 같은 변경을 저장한 파일과 비교해
#     OLE는 개체 공통 속성 21~23비트(3 → 2)만 달랐다. HWPX는 <hp:pic textWrap>.
#   * '글 앞으로'일 때만 바꾼다: 글 앞으로·글 뒤로는 둘 다 본문 글을 밀어내지 않아 쪽 구성이
#     그대로다. 어울림·자리 차지를 바꾸면 글이 다시 흘러 쪽이 바뀌므로 손대지 않는다.
#   * 묶음 안의 그림(배치는 묶음 전체의 것)과 글자처럼 취급한 그림은 건너뛴다.
#   * 글 뒤로가 해결하지 못하는 경우: 투명한 부분 밑이 글자가 아니라 색 있는 표 칸·도형·다른
#     그림이면 흰 상자는 그대로 남는다(흰색을 그 배경색으로 바꿀 정보가 문서에 없다).

GSO_CTRL_ID = b" osg"                   # CTRL_HEADER 개체 ID 'gso '(뒤집혀 저장)
PIC_SHAPE_ID = b"cip$"                  # SHAPE_COMPONENT 개체 ID '$pic'
WRAP_SHIFT = 21                         # 개체 공통 속성: 본문과의 배치(3비트)
WRAP_MASK = 0x7 << WRAP_SHIFT
WRAP_BEHIND_TEXT = 2                    # 0 어울림, 1 자리 차지, 2 글 뒤로, 3 글 앞으로
WRAP_IN_FRONT_OF_TEXT = 3
TREAT_AS_CHAR = 0x1                     # 개체 공통 속성 0비트: 글자처럼 취급


def _iter_ole_records(data: bytes):
    """레코드를 (tag, level, 본문 시작, 본문 끝)으로 순회합니다."""
    pos, n = 0, len(data)
    while pos + 4 <= n:
        header = struct.unpack_from("<I", data, pos)[0]
        tag, level, size = header & 0x3FF, (header >> 10) & 0x3FF, (header >> 20) & 0xFFF
        hdr = 4
        if size == 0xFFF:
            if pos + 8 > n:
                break
            size = struct.unpack_from("<I", data, pos + 4)[0]
            hdr = 8
        start = pos + hdr
        if start + size > n:
            break
        yield tag, level, start, start + size
        pos = start + size


def move_behind_text(section: bytes, bin_items: dict, streams: set) -> tuple[bytes, dict]:
    """OLE 구역 1개에서 '글 앞으로' 놓인 투명 배경 그림을 '글 뒤로' 옮깁니다(길이 불변).

    streams = 투명한 부분이 흰색이 된 그림의 스트림 번호, bin_items = 그림 번호 → 스트림 번호.
    (고친 바이트, {스트림 번호: 옮긴 개수})를 돌려줍니다."""
    buf = bytearray(section)
    ctrls, shapes = {}, {}
    moved: dict = {}
    for tag, level, start, end in _iter_ole_records(section):
        if tag == 71:                                       # CTRL_HEADER
            ctrls[level] = (start, end)
        elif tag == 76:                                     # SHAPE_COMPONENT
            shapes[level] = (start, end)
        elif tag == 85 and end - start > 72:                # PICTURE
            stream = bin_items.get(struct.unpack_from("<H", section, start + 71)[0])
            if stream not in streams:
                continue
            shape, gso = shapes.get(level - 1), ctrls.get(level - 2)
            # 최상위 그림만: 그림 자신의 개체 ID가 두 번 들어간 모양 + 바로 위가 gso 개체
            if not shape or section[shape[0]:shape[0] + 8] != PIC_SHAPE_ID * 2:
                continue
            if not gso or gso[1] - gso[0] < 8 or section[gso[0]:gso[0] + 4] != GSO_CTRL_ID:
                continue
            attr = struct.unpack_from("<I", buf, gso[0] + 4)[0]
            if attr & TREAT_AS_CHAR or (attr & WRAP_MASK) >> WRAP_SHIFT != WRAP_IN_FRONT_OF_TEXT:
                continue
            struct.pack_into("<I", buf, gso[0] + 4,
                             (attr & ~WRAP_MASK) | (WRAP_BEHIND_TEXT << WRAP_SHIFT))
            moved[stream] = moved.get(stream, 0) + 1
    return bytes(buf), moved


_HWPX_PIC_RE = re.compile(r"<hp:pic\b.*?</hp:pic>", re.S)


def move_behind_text_hwpx(section_xml: str, refs: set) -> tuple[str, dict]:
    """HWPX 구역 XML에서 '글 앞으로' 놓인 투명 배경 그림을 '글 뒤로' 옮깁니다.
    refs = 그런 그림의 binaryItemIDRef. (고친 XML, {ref: 옮긴 개수})"""
    moved: dict = {}

    def fix(m):
        block = m.group(0)
        ref = re.search(r'binaryItemIDRef="([^"]+)"', block)
        if not ref or ref.group(1) not in refs:
            return block
        head_end = block.find(">") + 1
        head = block[:head_end]
        if 'textWrap="IN_FRONT_OF_TEXT"' not in head:
            return block
        group = re.search(r'\bgroupLevel="(\d+)"', head)
        if group and group.group(1) != "0":
            return block                                    # 묶음 안
        if re.search(r'<hp:pos\b[^>]*\btreatAsChar="1"', block):
            return block
        moved[ref.group(1)] = moved.get(ref.group(1), 0) + 1
        return head.replace('textWrap="IN_FRONT_OF_TEXT"', 'textWrap="BEHIND_TEXT"') + block[head_end:]

    return _HWPX_PIC_RE.sub(fix, section_xml), moved


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


# 알파 128 미만(절반 넘게 비치는) 픽셀이 그림의 이 비율 이상이면 '투명 배경 그림'으로 본다.
# 서명·도장·배경 없는 도식은 40~99%, 거의 불투명한 그림(가장자리 몇 픽셀만 투명)은 0%였다
# (실측 2026-11, 과학전시 심사자료집 내지의 알파 있는 PNG 37장).
SEE_THROUGH_MIN_RATIO = 0.01


def has_see_through(img) -> bool:
    """그림에 실제로 비치는(투명한) 부분이 있는가.

    JPG에는 투명이 없어 변환하면 그 부분이 흰색으로 채워진다. 그림이 '글 앞으로' 놓여
    글자 위에 겹쳐 있었다면(서명·도장 위의 '(인)' 등) 흰 상자가 글자를 가린다 — 이런 그림을
    move_behind_text가 '글 뒤로' 옮긴다. 알파가 없거나 거의 불투명하면 False."""
    if img.mode not in ("RGBA", "LA", "RGBa", "La", "PA") and "transparency" not in img.info:
        return False
    try:
        alpha = img.convert("RGBA").getchannel("A")
    except Exception:
        return False
    clear = sum(alpha.histogram()[:128])
    return clear >= SEE_THROUGH_MIN_RATIO * img.width * img.height


def convert_image_to_jpg(data: bytes, source_format: str, quality: int = 95) -> tuple[bytes, bool]:
    """이미지 바이트를 JPG 바이트로 변환합니다. (일반 래스터 전용)
    (jpg 바이트, 투명한 부분이 있었는지)를 돌려줍니다.

    PIL이 못 여는 손상 파일(포토샵도 거부하지만 그림판은 여는 PNG 등)은 worker.open_raster가
    WIC — 그림판과 같은 Windows 디코더 — 로 다시 엽니다."""
    sys.path.insert(0, os.path.dirname(__file__))
    import worker
    img = worker.open_raster(data)
    see_through = has_see_through(img)
    if img.mode != "RGB":
        img = to_rgb(img)

    # 원본 이미지의 DPI(해상도)를 유지합니다. 없으면 96으로 간주합니다.
    dpi = img.info.get("dpi", (96, 96))

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, dpi=dpi)
    return buf.getvalue(), see_through


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


def image_bytes_to_jpg(data: bytes, fmt: str) -> tuple[bytes, bool]:
    """이미지 바이트를 JPG 바이트로 변환합니다. WMF/EMF는 GDI 렌더링, 그 외는 PIL을 사용합니다.
    HWP(OLE)·HWPX(ZIP) 두 경로가 동일하게 이 함수를 거치도록 해, 포맷 처리가 갈라지지 않게 합니다.
    (jpg 바이트, 투명한 부분이 있었는지)를 돌려줍니다 — 메타파일은 판단하지 않는다(False)."""
    if fmt in ("wmf", "emf"):
        return render_metafile_to_jpg(data, fmt), False
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


def _hwpx_clip_is_full(clip: tuple, frame_w: int, frame_h: int, tol: float = 0.03) -> bool:
    """imgClip(속성 순서 left, right, top, bottom의 값)이 '자르기 없음'인가.

    판본에 따라 이름대로(left=0 right=W top=0 bottom=H) 또는 값 순서대로(left=0 right=0
    top=W bottom=H, 한글 2010) 쓰이는데, 자르기 없음은 두 표기가 겹치지 않아 둘 다 본다."""
    left, right, top, bottom = clip

    def near(value, full):
        return abs(value - full) <= full * tol

    labelled = left == 0 and top == 0 and near(right, frame_w) and near(bottom, frame_h)
    positional = left == 0 and right == 0 and near(top, frame_w) and near(bottom, frame_h)
    return labelled or positional


def size_adjust_hwpx(path: str, vector_origins: dict = None, page_map: PageMap = None,
                     vector_rotations: dict = None) -> None:
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
    vector_rotations = vector_rotations or {}
    page_map = page_map or PageMap()
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
    # imgDim이 없는 판본(한글 2010~2020 초기)의 자르기 값. 이때 한글은 자르기 기준을 그림
    # 픽셀×75로 다시 잡으므로, 픽셀을 줄이면 자르기 좌표가 엉뚱한 곳을 가리킨다(실측: 자른
    # 그림이 왼쪽 위로 밀려 테두리가 잘려 보임). 그래서 자르기 없음이 확실할 때만 조정한다.
    ref_clip_nodim: dict = {}
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
            elif clip_m:
                ref_clip_nodim[ref] = tuple(map(int, clip_m.groups()))
            ref_info[ref] = (cw, ch, cfw, cfh)

    # 화면 표시 해상도(보이는 픽셀 ÷ 표시 inch)가 이 값을 넘는 그림만 이 값으로 낮춘다.
    # 300dpi는 인쇄 품질 기준이라 시각적 손실이 없고, 그 이하 그림은 손대지 않아 화질을 보존한다.
    TARGET_DPI = 300.0
    adjusted = 0
    skipped = 0
    for ref, (cw, ch, cfw, cfh) in ref_info.items():
        # 로그에 붙일 쪽 번호(변환 전 이름 기준 맵). ref는 'image1' 같은 itemID라 확장자 없이 찾는다.
        page_fields = _page_fields(lookup_hwpx(page_map, ref), note_hwpx(page_map, ref),
                                   is_approx_hwpx(page_map, ref))
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
        if ref in ref_clip_nodim and not _hwpx_clip_is_full(ref_clip_nodim[ref], pw * 75, ph * 75):
            skipped += 1                  # imgDim 없는 자른 그림 → 픽셀을 줄이면 자르기가 어긋난다
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
                # 회전을 바로 세운 그림은 원본 벡터(돌리기 전) 기준 크기로 그린 뒤 같은 각도로 돌린다.
                angle = vector_rotations.get(key, 0)
                rw, rh = (ch, cw) if angle in (90, 270) else (cw, ch)
                out = _rerender_vector_jpg(vbytes, vfmt, rw, rh)   # 벡터를 작게 '재렌더'(또렷)
                if out is None:              # 안전 렌더 불가 → 고해상 유지
                    skipped += 1
                    continue
                new_jpg, nw, nh, _ = out
                if angle:
                    new_jpg = hwp_rotation.rotate_jpg(new_jpg, angle)
                    nw, nh = (nh, nw) if angle in (90, 270) else (nw, nh)
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
            _print_json({"event": "size", "name": href.split("/")[-1], "message": msg,
                         **page_fields})
        except Exception as e:
            skipped += 1
            _print_json({"event": "size", "name": href, "message": f"사이즈 조정 실패: {e}",
                         **page_fields})

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
    """OLE HWP의 BodyText를 읽어 스트림 번호 → (curW, curH, off44(L,T,R,B)) 매핑을 만든다(읽기 전용).

    - curSz(표시 크기)  : 그림 부모 Tag76(SHAPE_COMPONENT)의 offset 28/32. (실측 검증됨)
    - off44(보이는 영역): 그림 Tag85(PICTURE)의 offset 44, 이미지 고유좌표(px×75/wext×75)의 가시 rect.
    같은 그림이 여러 곳에 다른 크기로 쓰이면(중복) 안전하게 제외한다.
    Tag85 offset 71은 그림 번호라 DocInfo 표로 스트림 번호에 옮긴다(연결 그림 뒤로 밀림 —
    hwp_pagemap.bin_item_streams). 표를 못 읽으면 어느 그림의 크기인지 확신할 수 없어 빈 dict."""
    import zlib
    try:
        import olefile
        from hwp_pagemap import bin_item_streams
    except Exception:
        return {}
    info, count = {}, {}
    try:
        ole = olefile.OleFileIO(hwp_path)
    except Exception:
        return {}
    try:
        docinfo = ole.openstream('DocInfo').read()
        try:
            docinfo = zlib.decompress(docinfo, -15)
        except Exception:
            pass
        items = bin_item_streams(docinfo)
        if not items:
            return {}
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
                        binid = items.get(struct.unpack_from('<H', rec, 71)[0])
                        crop = struct.unpack_from('<4i', rec, 44)
                        p = t76.get(level - 1)
                        if p and len(p) >= 36:
                            cw, ch = struct.unpack_from('<2i', p, 28)
                            if binid is not None and cw > 0 and ch > 0:
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
    STGM_CREATE = 0x00001000

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
        """HWP 파일 내 이미지 목록을 반환합니다. used=False면 변환에서 제외될 그림입니다."""
        images = []
        page_map = build_page_map_hwp(hwp_path)
        # 제자리에서 바로 세울 회전 그림은 '선택 정리'에서도 변환되므로 대상 수에 넣게 표시한다.
        rot_plan = plan_rotation_hwp(hwp_path)
        rotated_bins = set(rot_plan.in_place) if rot_plan else set()
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
                        "used": not is_unused_hwp(page_map, name),
                        "rotated": _bin_id_of(name) in rotated_bins,
                        # 이름과 내용이 다르면 '선택 정리'에서도 변환되므로 대상 수에 넣게 표시
                        **mismatch_fields(name, fmt),
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

        # 그림별 쪽 번호도 같은 이유(BodyText 읽기 전용)로 COM 세션 전에 미리 만든다.
        # 한글이 있으면 이 시점에 정확한 값으로 갈아끼운다(한글 COM은 이 뒤의 OLE 쓰기
        # 세션과 겹치면 안 되므로 반드시 먼저 끝낸다).
        page_map = build_page_map_hwp(hwp_path)
        refine_pages_with_hangul(page_map, hwp_path)

        # 회전된 그림을 바로 세울 계획도 BodyText를 읽기만 하므로 COM 세션 전에 세운다.
        # 픽셀은 아래 루프에서 돌려 두고(rotated/rot_copies), 문서 쪽 수정은 루프가 끝난 뒤
        # DocInfo·BodyText를 한꺼번에 고칠 수 있을 때만 함께 기록한다.
        rot_plan = plan_rotation_hwp(hwp_path)
        rotated = {}               # 스트림 번호 → (스트림 이름, 돌린 jpg, 압축 방식) — 제자리
        rot_copies = {}            # (스트림 번호, 각도) → (돌린 jpg, 압축 방식) — 새 BinData 사본
        see_through = set()        # 투명한 부분이 흰색이 된 그림의 스트림 번호(→ 글 뒤로)

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
        stream_names = sort_by_page(stream_names, lambda n: lookup_hwp(page_map, n))
        converted = 0
        skipped = 0
        unused_names = []          # 문서가 참조하지 않아 변환에서 제외한 그림

        conversions = {}
        stream_renames = []

        def stage_rotation(bin_id, name, jpg_data, comp_type):
            here, copies = rotate_for_plan(rot_plan, bin_id, name, jpg_data)
            if here is not None:
                rotated[bin_id] = (name, here, comp_type)
            for angle, jpg in copies.items():
                rot_copies[(bin_id, angle)] = (jpg, comp_type)

        for name in stream_names:
            # 문서 어디에서도 안 쓰이는 그림(편집 중 지운 그림의 잔여 BinData)은 건드리지
            # 않는다. 판정은 본문 그림 + 그림 채우기 + 그림 글머리표를 모두 본 뒤에만 참이며,
            # 해석이 조금이라도 어긋나면 항상 False가 되어 실제 그림이 빠지지 않는다.
            if is_unused_hwp(page_map, name):
                unused_names.append(name)
                continue
            # 성공·실패 어느 쪽이든 로그에 쪽을 붙일 수 있도록 먼저 조회해 둔다.
            pages = lookup_hwp(page_map, name)
            note = note_hwp(page_map, name)
            approx = is_approx_hwp(page_map, name)
            try:
                data = self._read_stream(bindata, name)
                uncomp_data, comp_type = self._decompress(data)
                fmt = detect_format(uncomp_data)
                if fmt == "unknown":
                    fmt = name.split(".")[-1].lower()

                m = re.match(r'^BIN([0-9A-Fa-f]{4})\.', name)
                bin_id = int(m.group(1), 16) if m else None

                # 회전 그림은 제자리(in_place)로 세울 거면 '선택 정리'에서 빠지는 형식이어도
                # 변환한다. 사본(copies)만 필요하면 원본은 규칙대로 두고 사본만 만든다.
                rotate_here = bool(rot_plan) and bin_id in rot_plan.in_place
                needs_copy = bool(rot_plan) and bin_id in rot_plan.copies
                # 이름(BINxxxx.png·DocInfo 확장자)과 내용이 다른 그림도 형식과 무관하게 고친다.
                mismatch = ext_mismatch(name, fmt)
                convert_this = should_convert(fmt, mode) or rotate_here or mismatch
                if not convert_this and not needs_copy:
                    skipped += 1
                    continue

                # 변환 (WMF/EMF는 GDI 렌더링 — 그림 자르기 여백 보존 포함, 그 외는 PIL)
                # HWPX 경로와 동일한 to_jpg_for_mode()를 거쳐 포맷 처리가 갈라지지 않게 한다.
                try:
                    jpg_data, kept, filled = to_jpg_for_mode(uncomp_data, fmt, mode, mismatch)
                except Exception as e:
                    emit_progress(False, name, fmt, "jpg", f"변환 실패: {e}", pages, note, approx,
                                  mismatch_fields(name, fmt))
                    skipped += 1
                    continue

                if not convert_this:
                    skipped += 1
                    stage_rotation(bin_id, name, jpg_data, comp_type)
                    continue
                if filled and bin_id is not None:
                    see_through.add(bin_id)

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
                                                    f"(용량 {before_kb}KB→{len(jpg_data)//1024}KB)",
                                         **_page_fields(pages, note, approx)})
                        else:
                            size_skipped += 1
                    except Exception as e:
                        size_skipped += 1
                        _print_json({"event": "size", "name": name,
                                     "message": f"사이즈 조정 실패: {e}", **_page_fields(pages, note, approx)})

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
                emit_progress(True, name, fmt, "jpg",
                              "확장자 정정(내용은 원래 JPG)" if kept else "변환 완료",
                              pages, note, approx, mismatch_fields(name, fmt, kept))
                if rotate_here or needs_copy:
                    stage_rotation(bin_id, name, jpg_data, comp_type)

            except Exception as e:
                emit_progress(False, name, "unknown", "jpg", f"처리 오류: {e}", pages, note, approx)
                skipped += 1

        # 모든 스트림 변환 완료 후 DocInfo 일괄 패치(확장자 + 회전 그림 사본)
        rot_sections = {}
        if rot_plan:
            rot_plan.keep(set(rotated), set(rot_copies))
        if conversions or rot_plan:
            try:
                docinfo_data = self._read_stream(storage, "DocInfo")
                docinfo_uncomp, comp_type = self._decompress(docinfo_data)

                docinfo_patched = self._patch_docinfo(docinfo_uncomp, conversions)

                # 회전 그림: 사본 BinData 추가와 BodyText 수정을 메모리에서 먼저 끝내 본다.
                # 하나라도 실패하면 아무것도 기록하지 않는다 — 픽셀만 돌고 회전각이 남으면
                # 그림이 두 번 돌아간 채로 보이기 때문이다.
                if rot_plan:
                    try:
                        with_copies = hwp_rotation.add_bin_copies(
                            docinfo_patched, rot_plan,
                            taken_streams={_bin_id_of(n) for n in stream_names} - {None})
                        rot_sections = self._patch_rotated_sections(storage, rot_plan)
                        docinfo_patched = with_copies
                    except Exception as e:
                        rot_sections = {}
                        emit_error(f"회전 그림 바로 세우기 실패 — 회전은 원래대로 둡니다: {e}")

                docinfo_comp = self._compress(docinfo_patched, comp_type)
                self._write_stream(storage, "DocInfo", docinfo_comp)

                if rot_sections:
                    try:
                        self._write_rotation(storage, bindata, rot_plan, rot_sections,
                                             rotated, rot_copies)
                    except Exception as e:
                        rot_sections = {}
                        emit_error(f"회전 그림 기록 실패: {e}")

                # 스트림 이름 일괄 변경
                for old_name, new_name in stream_renames:
                    try:
                        bindata.RenameElement(old_name, new_name)
                    except Exception:
                        pass
            except Exception as e:
                emit_error(f"DocInfo 패치 및 스트림 이름 변경 실패: {e}")

        # 투명 배경이 흰색이 된 그림 중 '글 앞으로' 놓인 것 → '글 뒤로'(글자를 가리지 않게).
        # 회전 기록까지 끝난 구역을 다시 읽어 고치므로 두 수정이 서로 덮어쓰지 않는다.
        moved = {}
        if see_through:
            try:
                moved = self._move_behind_text(storage, see_through)
            except Exception as e:
                moved = {}
                emit_error(f"투명 배경 그림 배치 조정 실패 — 배치는 그대로 둡니다: {e}")

        # OLE 커밋
        try:
            bindata.Commit(0)
            storage.Commit(0)
        except Exception as e:
            emit_error(f"OLE 커밋 실패: {e}")

        # 압축 정리된 사본을 만든 뒤 리소스 정리, 최종 output_path로 복사
        del bindata
        compact_path = self._compact(storage)
        del storage
        if hasattr(self, '_current_tmp_path') and self._current_tmp_path:
            try:
                shutil.copy2(compact_path or self._current_tmp_path, output_path)
                os.remove(self._current_tmp_path)
                self._current_tmp_path = None
            except Exception as e:
                emit_error(f"최종 파일 저장 실패: {e}")
        if compact_path:
            try:
                os.remove(compact_path)
            except OSError:
                pass

        emit_approx(page_map)
        emit_unused(unused_names)
        stream_of = {_bin_id_of(n): n for n in stream_names}
        page_fields_of = lambda n: _page_fields(lookup_hwp(page_map, n),           # noqa: E731
                                                note_hwp(page_map, n),
                                                is_approx_hwp(page_map, n))
        if rot_plan is not None:
            emit_rotation(rot_plan, bool(rot_sections), stream_of, page_fields_of)
        emit_wrap({stream_of.get(k, str(k)): n for k, n in moved.items()}, page_fields_of)
        emit_done(converted, skipped)
        if size_adjust:
            _print_json({"event": "sizeDone", "adjusted": size_adjusted, "skipped": size_skipped})

    def _compact(self, storage):
        """커밋된 문서를 새 복합 파일에 통째로 복사해 빈 공간을 뺀 사본 경로를 돌려줍니다.

        OLE 복합 파일은 스트림을 줄이거나 바꿔 써도 원래 쓰던 섹터를 파일에서 돌려주지 않는다.
        그래서 BMP→JPG 변환·사이즈 조정으로 그림이 작아져도 파일 크기는 그대로였다(실측:
        실제 데이터 82MB인 문서가 205MB). 새 파일에 CopyTo하면 쓰이는 섹터만 옮겨져 한글에서
        다시 저장한 것과 같은 크기가 된다(실측 205.5MB→82.8MB, 0.2초, 스트림 190개 바이트 일치).
        실패하면 None — 호출자는 정리 전 파일을 그대로 쓴다."""
        import pythoncom
        fd, path = tempfile.mkstemp(suffix=".hwp")
        os.close(fd)
        try:
            dest = pythoncom.StgCreateDocfile(
                path, self.STGM_CREATE | self.STGM_READWRITE | self.STGM_SHARE_EXCLUSIVE, 0)
            storage.CopyTo(None, None, dest)
            dest.SetClass(storage.Stat(0)[8])
            dest.Commit(0)
            del dest
            return path
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            return None

    def _move_behind_text(self, storage, streams: set) -> dict:
        """모든 구역에서 streams(스트림 번호)를 쓰는 '글 앞으로' 그림을 '글 뒤로' 옮겨 기록합니다.
        {스트림 번호: 옮긴 개수}. 그림 번호 → 스트림 번호 표는 지금의 DocInfo로 만든다."""
        docinfo, _comp = self._decompress(self._read_stream(storage, "DocInfo"))
        items = bin_item_streams(docinfo)
        if not items:
            return {}
        moved: dict = {}
        body = storage.OpenStorage("BodyText", None,
                                   self.STGM_READWRITE | self.STGM_SHARE_EXCLUSIVE, None, 0)
        try:
            for section in self._enum_bindata_streams(body):
                if not section.startswith("Section"):
                    continue
                data, comp_type = self._decompress(self._read_stream(body, section))
                patched, counts = move_behind_text(data, items, streams)
                if counts:
                    self._write_stream(body, section, self._compress(patched, comp_type))
                    for key, n in counts.items():
                        moved[key] = moved.get(key, 0) + n
            body.Commit(0)
        finally:
            del body
        return moved

    def _patch_rotated_sections(self, storage, rot_plan) -> dict:
        """바로 세울 그림이 있는 구역을 메모리에서 고쳐 {구역 이름: 압축된 바이트}로 돌려줍니다."""
        out = {}
        body = storage.OpenStorage("BodyText", None,
                                   self.STGM_READ | self.STGM_SHARE_EXCLUSIVE, None, 0)
        try:
            for section, targets in rot_plan.targets().items():
                data, comp_type = self._decompress(self._read_stream(body, section))
                out[section] = self._compress(
                    hwp_rotation.patch_section(data, targets, rot_plan.bin_items), comp_type)
        finally:
            del body
        return out

    def _write_rotation(self, storage, bindata, rot_plan, sections: dict,
                        rotated: dict, rot_copies: dict):
        """돌린 픽셀(제자리·사본)과 고친 구역을 기록합니다. DocInfo는 호출 전에 이미 기록됨."""
        for copy_key, stream_id in rot_plan.copy_streams.items():
            jpg_data, comp_type = rot_copies[copy_key]
            stream = bindata.CreateStream(
                f"BIN{stream_id:04X}.jpg",
                self.STGM_READWRITE | self.STGM_SHARE_EXCLUSIVE | self.STGM_CREATE, 0, 0)
            stream.Write(self._compress(jpg_data, comp_type))
            del stream
        for bin_id in rot_plan.in_place:
            name, jpg_data, comp_type = rotated[bin_id]
            self._write_stream(bindata, name, self._compress(jpg_data, comp_type))
        body = storage.OpenStorage("BodyText", None,
                                   self.STGM_READWRITE | self.STGM_SHARE_EXCLUSIVE, None, 0)
        try:
            for section, data in sections.items():
                self._write_stream(body, section, data)
            body.Commit(0)
        finally:
            del body


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
        return name_ext(filename) or "unknown"

    def _manifest_ids(self, zf: zipfile.ZipFile) -> dict:
        """매니페스트(content.hpf) → {그림 ZIP 경로(소문자): 항목 id}. 구역 XML의 그림은
        binaryItemIDRef로 이 id를 가리킨다. 못 읽으면 빈 dict."""
        try:
            hpf = next(n for n in zf.namelist() if n.lower().endswith(".hpf"))
            text = zf.read(hpf).decode("utf-8", errors="replace")
        except Exception:
            return {}
        ids = {}
        for tag in re.findall(r"<(?:\w+:)?item\b[^>]*>", text):
            item_id = re.search(r'\bid="([^"]+)"', tag)
            href = re.search(r'\bhref="([^"]+)"', tag)
            if item_id and href:
                ids[href.group(1).lower()] = item_id.group(1)
        return ids

    def scan(self, hwpx_path: str) -> list:
        """HWPX 파일 내 이미지 목록을 반환합니다. used=False면 변환에서 제외될 그림입니다."""
        images = []
        page_map = build_page_map_hwpx(hwpx_path)
        rot_plan = plan_rotation_hwpx(hwpx_path)
        rotated_entries = {rot_plan.entries.get(ref) for ref in rot_plan.in_place} if rot_plan else set()
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
                        "used": not is_unused_hwpx(page_map, entry),
                        "rotated": entry in rotated_entries,
                        **mismatch_fields(entry, fmt),
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
        unused_names = []          # 문서가 참조하지 않아 변환에서 제외한 그림
        # 변환 대상 매핑: {원래 ZIP 경로: (새 ZIP 경로, jpg 바이트)}
        replacements: dict[str, tuple[str, bytes]] = {}
        # 이름 변경 매핑: {원래 파일명: 새 파일명} (XML 참조 수정용)
        name_map: dict[str, str] = {}
        # 사이즈 조정용: 변환된 벡터(WMF/EMF)의 '원본 바이트'를 출력 ZIP 경로로 보관.
        # size_adjust_hwpx가 다운샘플(선 흐려짐) 대신 원본 벡터를 작은 크기로 재렌더하도록.
        self.vector_origins: dict[str, tuple[str, bytes]] = {}
        # 그중 회전을 바로 세운 그림: 출력 ZIP 경로 → 각도(재렌더 결과도 같은 각도로 돌린다)
        self.vector_rotations: dict[str, int] = {}
        # 그림별 쪽 번호(로그 표시용). 사이즈 조정 후처리에서도 쓰도록 보관한다.
        self.page_map = build_page_map_hwpx(hwpx_path)
        refine_pages_with_hangul(self.page_map, hwpx_path)

        # 회전된 그림을 바로 세울 계획(원본을 읽기만 한다). 픽셀은 아래 루프에서 돌려 두고,
        # XML 수정은 재패키징 직전에 메모리에서 끝내 본 뒤 성공했을 때만 함께 반영한다.
        rot_plan = plan_rotation_hwpx(hwpx_path)
        ref_of = {entry: ref for ref, entry in rot_plan.entries.items()} if rot_plan else {}
        rotated = {}               # ref → 돌린 jpg (제자리)
        rot_copies = {}            # (ref, 각도) → 돌린 jpg (새 그림 사본)
        see_through = set()        # 투명한 부분이 흰색이 된 그림의 ZIP 경로(→ 글 뒤로)

        with zipfile.ZipFile(output_path, "r") as zf:
            entries = sort_by_page(self._find_bindata_entries(zf),
                                   lambda e: lookup_hwpx(self.page_map, e))
            item_ids = self._manifest_ids(zf)

            for entry in entries:
                data = zf.read(entry)
                fmt = detect_format(data)
                if fmt == "unknown":
                    fmt = self._get_format_from_name(entry)

                original_name = Path(entry).name
                if is_unused_hwpx(self.page_map, entry):
                    unused_names.append(original_name)   # 참조 없는 잔여 이미지 → 손대지 않음
                    continue
                pages = lookup_hwpx(self.page_map, entry)
                note = note_hwpx(self.page_map, entry)
                approx = is_approx_hwpx(self.page_map, entry)

                # 회전 그림은 OLE와 같은 규칙: 제자리로 세울 거면 형식과 무관하게 변환,
                # 사본만 필요하면 원본은 규칙대로 두고 사본만 만든다.
                ref = ref_of.get(entry)
                rotate_here = bool(rot_plan) and ref in rot_plan.in_place
                needs_copy = bool(rot_plan) and ref in rot_plan.copies
                # 이름(href)과 내용이 다른 그림도 형식과 무관하게 고친다(OLE와 같은 규칙).
                mismatch = ext_mismatch(entry, fmt)
                convert_this = should_convert(fmt, mode) or rotate_here or mismatch
                if not convert_this and not needs_copy:
                    skipped += 1
                    continue

                try:
                    jpg_data, kept, filled = to_jpg_for_mode(data, fmt, mode, mismatch)
                except Exception as e:
                    emit_progress(False, original_name, fmt, "jpg", f"변환 실패: {e}", pages, note, approx,
                                  mismatch_fields(entry, fmt))
                    skipped += 1
                    continue

                if rotate_here or needs_copy:
                    here, copies = rotate_for_plan(rot_plan, ref, original_name, jpg_data)
                    if here is not None:
                        rotated[ref] = here
                    for angle, jpg in copies.items():
                        rot_copies[(ref, angle)] = jpg
                if not convert_this:
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
                if filled and entry.lower() in item_ids:
                    see_through.add(item_ids[entry.lower()])

                converted += 1
                emit_progress(True, original_name, fmt, "jpg",
                              "확장자 정정(내용은 원래 JPG)" if kept else "변환 완료",
                              pages, note, approx, mismatch_fields(entry, fmt, kept))

        emit_approx(self.page_map)
        emit_unused(unused_names)

        # 회전 그림: 사본을 매니페스트에 넣고 구역 XML을 메모리에서 고쳐 본다. 하나라도
        # 실패하면 회전은 원래대로 둔다(돌린 픽셀도 쓰지 않음 — 두 번 돌아가 보이지 않게).
        rot_sections = {}          # 구역 XML 경로 → 고친 텍스트
        rot_manifest = None        # (content.hpf 경로, 사본을 넣은 텍스트)
        if rot_plan:
            rot_plan.keep(set(rotated), set(rot_copies))
        if rot_plan:
            try:
                with zipfile.ZipFile(output_path, "r") as zf:
                    names = zf.namelist()
                    if rot_plan.copies:
                        hpf = next(n for n in names if n.lower().endswith(".hpf"))
                        rot_manifest = (hpf, hwp_rotation.add_hwpx_copies(
                            zf.read(hpf).decode("utf-8"), rot_plan, names))
                    for section, targets in rot_plan.targets().items():
                        rot_sections[section] = hwp_rotation.patch_hwpx_section(
                            zf.read(section).decode("utf-8"), targets, rot_plan.clip_order)
            except Exception as e:
                rot_sections, rot_manifest = {}, None
                emit_error(f"회전 그림 바로 세우기 실패 — 회전은 원래대로 둡니다: {e}")
        if rot_sections:
            for ref, jpg in rotated.items():
                entry = rot_plan.entries[ref]
                new_entry = replacements[entry][0]
                replacements[entry] = (new_entry, jpg)
                if new_entry in self.vector_origins:
                    # 사이즈 조정이 원본 벡터를 다시 렌더할 때도 같은 각도로 돌려야 한다.
                    self.vector_rotations[new_entry] = rot_plan.in_place[ref]
        if rot_plan is not None:
            emit_rotation(rot_plan, bool(rot_sections),
                          {ref: Path(entry).name for ref, entry in rot_plan.entries.items()},
                          lambda n: _page_fields(lookup_hwpx(self.page_map, n),
                                                 note_hwpx(self.page_map, n),
                                                 is_approx_hwpx(self.page_map, n)))

        if not replacements and not rot_sections:
            emit_done(converted, skipped)
            return

        # 새 ZIP 생성 (임시 파일)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".hwpx")
        os.close(tmp_fd)
        moved = {}                 # binaryItemIDRef → '글 뒤로' 옮긴 개수

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
                            text = rot_sections.get(info.filename)
                            if text is None and rot_manifest and info.filename == rot_manifest[0]:
                                text = rot_manifest[1]
                            if text is None:
                                text = data.decode("utf-8", errors="replace")
                            for old_name, new_name_str in name_map.items():
                                text = text.replace(old_name, new_name_str)
                            # 투명 배경이 흰색이 된 그림: '글 앞으로' → '글 뒤로'(OLE와 같은 규칙)
                            if see_through and re.search(r"(^|/)section\d+\.xml$",
                                                         info.filename, re.I):
                                text, counts = move_behind_text_hwpx(text, see_through)
                                for ref, n in counts.items():
                                    moved[ref] = moved.get(ref, 0) + n
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

                # 회전 그림 사본(다른 곳에서도 쓰여 원본을 못 돌린 그림)
                if rot_sections:
                    for key, new_id in rot_plan.new_ids.items():
                        copy_info = zipfile.ZipInfo(rot_plan.entries[new_id])
                        copy_info.compress_type = zipfile.ZIP_DEFLATED
                        zf_out.writestr(copy_info, rot_copies[key])

            # 임시 파일을 출력 경로로 이동
            shutil.move(tmp_path, output_path)

            entry_of = {ref: entry for entry, ref in item_ids.items()}
            emit_wrap({Path(entry_of.get(ref, ref)).name: n for ref, n in moved.items()},
                      lambda n: _page_fields(lookup_hwpx(self.page_map, n),
                                             note_hwpx(self.page_map, n),
                                             is_approx_hwpx(self.page_map, n)))

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

def emit_progress(success: bool, name: str, from_fmt: str, to_fmt: str, message: str,
                  pages: list = None, note: str = None, approx: bool = False,
                  extra: dict = None):
    """진행 상황 1건. pages는 그림이 놓인 쪽 목록(문서 순서) — 없으면 필드를 생략한다.
    from은 실제 내용의 형식이다. extra는 추가 필드(확장자 정정의 nameExt/kept 등)."""
    data = {
        "event": "progress",
        "success": success,
        "name": name,
        "from": from_fmt,
        "to": to_fmt,
        "message": message,
    }
    data.update(_page_fields(pages, note, approx))
    if extra:
        data.update(extra)
    _print_json(data)


def mismatch_fields(name: str, fmt: str, kept: bool = False) -> dict:
    """확장자 정정 그림의 NDJSON 필드. nameExt = 이름의 확장자(실제 내용은 from/format),
    kept = 내용이 이미 JPEG라 다시 인코딩하지 않고 이름만 바꿈."""
    if not ext_mismatch(name, fmt):
        return {}
    fields = {"nameExt": name_ext(name)}
    if kept:
        fields["kept"] = True
    return fields


def _page_fields(pages: list, note: str = None, approx: bool = False) -> dict:
    """쪽 정보를 NDJSON 필드로 만듭니다. pages는 [(인쇄 번호, 물리 쪽), ...].

    page          = 문서에 인쇄되는 쪽 번호(사용자가 한글에서 보는 번호)
    physicalPage  = 문서의 몇 번째 쪽인지. '새 번호로 시작' 때문에 다를 때만 넣는다.
    pages         = 같은 그림이 여러 곳에 쓰였을 때 인쇄 번호 전체
    pageNote      = 쪽을 못 매기는 쓰임새('채우기' 등). 쪽이 없을 때만.
    """
    if not pages:
        return {"pageNote": note} if note else {}
    display, physical = pages[0]
    out = {"page": display}
    if physical != display:
        out["physicalPage"] = physical
    if len(pages) > 1:
        out["pages"] = [d for d, _ in pages]
    if approx:
        out["pageApprox"] = True     # 여러 쪽에 걸친 표 뒤라 실제보다 작게 나올 수 있다
    return out

def emit_approx(page_map):
    """쪽 번호가 근사치가 되는 지점을 한 번 알립니다(여러 쪽에 걸친 표가 있는 문서)."""
    start = getattr(page_map, "approx_from", None)
    if start:
        _print_json({"event": "approx", "fromPage": start})


def emit_unused(names: list):
    """문서가 참조하지 않아 변환에서 제외한 그림 목록(있을 때만)."""
    if names:
        _print_json({"event": "unused", "count": len(names), "names": names[:30]})


def emit_rotation(plan, applied: bool, name_of: dict, page_fields_of):
    """회전 그림 처리 결과를 그림마다 한 줄씩 알립니다. name_of = 키 → 로그에 쓸 그림 이름.

    applied=False면 바로 세운 그림이 없다(실패했다면 오류는 이미 알렸다). 회전돼 있지만
    손대지 않은 그림은 사유와 함께 실패 줄로 보낸다 — PDF 변환 전에 직접 확인해야 하므로."""
    fixed = 0
    if applied:
        for key, angle in plan.in_place.items():
            name = name_of.get(key, str(key))
            _print_json({"event": "rotate", "success": True, "name": name,
                         "message": f"회전 {angle}° → 0° (그림을 돌려 저장)",
                         **page_fields_of(name)})
            fixed += 1
        for (key, angle), copy_name in plan.copy_names.items():
            name = name_of.get(key, str(key))
            _print_json({"event": "rotate", "success": True, "name": name,
                         "message": f"회전 {angle}° → 0° (다른 곳에도 쓰인 그림이라 "
                                    f"돌린 사본 {copy_name}로 분리)",
                         **page_fields_of(name)})
            fixed += 1
    kept = 0
    for key, angle, reason in dict.fromkeys(plan.skipped):
        name = name_of.get(key, str(key))
        _print_json({"event": "rotate", "success": False, "name": name,
                     "message": f"회전 {angle}° 그대로 둠 — {reason}",
                     **page_fields_of(name)})
        kept += 1
    if fixed or kept:
        _print_json({"event": "rotateDone", "fixed": fixed, "kept": kept})


def emit_wrap(moved: dict, page_fields_of):
    """'글 앞으로' → '글 뒤로' 옮긴 투명 배경 그림을 그림마다 한 줄씩 알립니다.
    moved = 그림 이름 → 옮긴 개수(같은 그림이 여러 곳에 쓰였으면 2 이상)."""
    for name, count in moved.items():
        where = f" {count}곳" if count > 1 else ""
        _print_json({"event": "wrap", "name": name,
                     "message": f"투명 배경 → 글 뒤로{where} (흰 바탕이 글자를 가리지 않게)",
                     **page_fields_of(name)})


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
                                         vector_origins=getattr(processor, "vector_origins", None),
                                         page_map=getattr(processor, "page_map", None),
                                         vector_rotations=getattr(processor, "vector_rotations", None))
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
