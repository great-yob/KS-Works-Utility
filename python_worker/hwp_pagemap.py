"""HWP/HWPX 문서에서 '각 그림이 실제로 놓이는 쪽 번호'를 계산합니다.

변환 로그에 `[성공] 03p BIN0003.jpg` 처럼 쪽을 함께 찍어, 실패하거나 이상한 그림을
원본에서 바로 찾아갈 수 있게 하는 용도입니다. 읽기 전용이며(문서를 절대 수정하지 않음)
실패하면 빈 dict를 돌려주어 변환 자체는 그대로 진행되게 합니다.

── 쪽 번호를 어떻게 아는가 ────────────────────────────────────────────────
HWP/HWPX 어디에도 "이 그림은 12쪽"이라는 필드는 없다. 대신 한글이 저장해 두는
**레이아웃 캐시(줄 정보, LineSeg)** 를 이용한다. 본문 문단마다 줄 목록이 있고 각 줄은
'그 쪽 안에서의 세로 위치(vertpos)'를 갖는다. 따라서

  * 다음 줄의 세로 위치가 이전 줄보다 **작아지면** → 새 쪽으로 넘어간 것
  * 문단에 '쪽 나누기'(OLE: 문단머리 나누기종류 0x04 / HWPX: pageBreak="1")가 걸려 있고
    위 규칙이 아직 안 걸렸으면 → 새 쪽
  * 구역(Section)이 바뀌면 → 새 쪽

그림이 어느 줄에 매달렸는지는 **문단 안 문자 위치**로 찾는다. 본문 글자열에서 개체는
8글자를 차지하고(확장 제어문자), 줄 정보의 textpos가 그 글자 위치를 가리키므로
`textpos <= 개체위치` 인 마지막 줄이 그림이 놓인 줄 = 쪽이 된다. 교재류처럼 그림 수십
장이 **한 문단에 몰려 있는 문서**(문단 하나가 수십 쪽에 걸침)도 이 방식이라야 맞는다.
단순히 레코드 순서대로 세면 전부 마지막 쪽으로 몰린다.

실측(2026-09, `D:\\작업방`): 한글에서 내보낸 PDF 쪽수와 비교해 교재 2건은 46/46,
150/150으로 정확히 일치했고, 각주·여러 쪽에 걸친 표가 많은 연구보고서 1건은 153/160로
약 5% 적게 나왔다. 표가 여러 쪽에 걸치는 경우는 표 안쪽 문단이 별도 좌표계를 쓰기 때문에
쪽 증가를 셀 수 없다(표 안 그림은 '표가 시작된 쪽'으로 보고된다). 즉 **정확한 위치 표시가
아니라 안내용 근사치**다.
"""

import re
import struct
import zipfile
from pathlib import Path

# 본문 글자열에서 8글자를 차지하는 제어문자(개체·필드 등). 한글 5.0 포맷 기준.
EXTENDED_CTRL_CODES = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}
INLINE_CTRL_CODES = {4, 5, 6, 7, 8, 9, 19, 20}
CTRL_CHAR_WIDTH = 8

# BodyText 레코드 태그
TAG_PARA_HEADER = 66
TAG_PARA_TEXT = 67
TAG_PARA_LINE_SEG = 69
TAG_CTRL_HEADER = 71
TAG_SHAPE_PICTURE = 85

LINE_SEG_SIZE = 36          # 줄 정보 1개 크기(바이트)
PARA_BREAK_PAGE = 0x04      # 문단머리 '나누기 종류' 중 쪽 나누기 비트
MAX_PAGES_PER_IMAGE = 20    # 같은 그림이 여러 곳에 쓰인 경우 보관할 쪽 수 상한


# ─────────────────────────────────────────
# 공통
# ─────────────────────────────────────────

def _iter_records(data: bytes):
    """HWP 레코드 스트림을 (tag, level, payload)로 순회합니다."""
    pos, n = 0, len(data)
    while pos + 4 <= n:
        header = struct.unpack_from("<I", data, pos)[0]
        tag = header & 0x3FF
        level = (header >> 10) & 0x3FF
        size = (header >> 20) & 0xFFF
        hdr = 4
        if size == 0xFFF:                       # 확장 크기(4바이트 추가)
            if pos + 8 > n:
                break
            size = struct.unpack_from("<I", data, pos + 4)[0]
            hdr = 8
        if pos + hdr + size > n:
            break
        yield tag, level, data[pos + hdr: pos + hdr + size]
        pos += hdr + size


def _page_of_pos(segs: list, char_pos: int) -> int:
    """줄 목록 [(textpos, page)]에서 문자 위치가 속한 줄의 쪽을 찾습니다."""
    page = segs[0][1]
    for textpos, pg in segs:
        if textpos <= char_pos:
            page = pg
        else:
            break
    return page


def _add(page_map: dict, key, page: int):
    pages = page_map.setdefault(key, [])
    if len(pages) < MAX_PAGES_PER_IMAGE and (not pages or pages[-1] != page):
        pages.append(page)


# ─────────────────────────────────────────
# HWP (OLE Compound File)
# ─────────────────────────────────────────

def _ctrl_char_positions(para_text: bytes) -> list:
    """PARA_TEXT에서 확장 제어문자(개체 앵커)들의 문자 위치 목록을 만듭니다.

    CTRL_HEADER 레코드는 이 목록과 같은 순서로 나오므로, k번째 CTRL_HEADER는
    k번째 확장 제어문자 위치에 매달려 있다."""
    positions = []
    i, n = 0, len(para_text) // 2
    while i < n:
        code = struct.unpack_from("<H", para_text, i * 2)[0]
        if code in EXTENDED_CTRL_CODES:
            positions.append(i)
            i += CTRL_CHAR_WIDTH
        elif code in INLINE_CTRL_CODES:
            i += CTRL_CHAR_WIDTH
        else:
            i += 1
    return positions


def build_page_map_hwp(hwp_path: str) -> dict:
    """HWP(OLE)의 bin id → 그림이 놓인 쪽 목록. 실패 시 {}."""
    import zlib
    try:
        import olefile
    except Exception:
        return {}
    try:
        ole = olefile.OleFileIO(hwp_path)
    except Exception:
        return {}

    page_map: dict = {}
    try:
        sections = [n for n in ole.listdir()
                    if len(n) >= 2 and n[0] == "BodyText" and n[1].startswith("Section")]
        sections.sort(key=lambda n: int(re.sub(r"\D", "", n[1]) or 0))

        page = 1
        for section in sections:
            try:
                raw = ole.openstream(section).read()
            except Exception:
                continue
            try:
                data = zlib.decompress(raw, -15)
            except Exception:
                try:
                    data = zlib.decompress(raw)
                except Exception:
                    data = raw

            prev_vert = None        # 직전 줄의 세로 위치(쪽 넘김 판단용)
            ctrl_positions: list = []
            segs: list = []         # [(textpos, page)] — 현재 최상위 문단의 줄들
            ctrl_index = 0
            pending_break = False   # 현재 문단에 '쪽 나누기'가 걸려 있는가
            first_seg = True
            current_page = page     # 가장 최근 개체(CTRL_HEADER)가 놓인 쪽

            for tag, level, rec in _iter_records(data):
                if tag == TAG_PARA_HEADER and level == 0:
                    ctrl_positions, segs, ctrl_index = [], [], 0
                    pending_break = bool((rec[11] if len(rec) > 11 else 0) & PARA_BREAK_PAGE)
                    first_seg = True

                elif tag == TAG_PARA_TEXT and level == 1:
                    ctrl_positions = _ctrl_char_positions(rec)

                elif tag == TAG_PARA_LINE_SEG and level == 1:
                    # 최상위 문단의 줄만 쪽 넘김을 만든다(표 안 문단은 셀 기준 좌표라 제외).
                    segs = []
                    for i in range(len(rec) // LINE_SEG_SIZE):
                        textpos, vert = struct.unpack_from("<Ii", rec, i * LINE_SEG_SIZE)
                        if prev_vert is not None and vert < prev_vert:
                            page += 1
                        elif first_seg and pending_break and prev_vert is not None:
                            page += 1
                        prev_vert = vert
                        first_seg = False
                        segs.append((textpos, page))

                elif tag == TAG_CTRL_HEADER and level == 1:
                    # 최상위 문단에 직접 매달린 개체 → 앵커 문자 위치로 쪽 결정.
                    if ctrl_index < len(ctrl_positions) and segs:
                        current_page = _page_of_pos(segs, ctrl_positions[ctrl_index])
                    else:
                        current_page = page
                    ctrl_index += 1

                elif tag == TAG_SHAPE_PICTURE and len(rec) >= 72:
                    # 그림은 자기를 감싼 개체(표·그룹 포함)의 쪽을 따른다.
                    bin_id = struct.unpack_from("<H", rec, 71)[0]
                    if 1 <= bin_id <= 0xFFFF:
                        _add(page_map, bin_id, current_page)

            page += 1               # 다음 구역은 새 쪽에서 시작
    except Exception:
        return page_map
    finally:
        try:
            ole.close()
        except Exception:
            pass
    return page_map


# ─────────────────────────────────────────
# HWPX (ZIP + XML)
# ─────────────────────────────────────────

def _local(elem) -> str:
    """네임스페이스를 뗀 태그 이름."""
    return elem.tag.rsplit("}", 1)[-1]


def _hwpx_manifest(zf: zipfile.ZipFile) -> dict:
    """content.hpf의 itemID → BinData 파일명(소문자) 매핑."""
    out = {}
    for name in zf.namelist():
        if not name.lower().endswith(".hpf"):
            continue
        try:
            txt = zf.read(name).decode("utf-8", "replace")
        except Exception:
            continue
        for m in re.finditer(r'<(?:\w+:)?item\b[^>]*?id="([^"]+)"[^>]*?href="([^"]+)"', txt):
            out[m.group(1)] = m.group(2).split("/")[-1].lower()
    return out


def build_page_map_hwpx(hwpx_path: str) -> dict:
    """HWPX의 그림 → 쪽 목록. 키는 BinData 파일명(소문자)과 binaryItemIDRef 둘 다. 실패 시 {}."""
    try:
        import xml.etree.ElementTree as ET
    except Exception:
        return {}

    page_map: dict = {}
    try:
        with zipfile.ZipFile(hwpx_path, "r") as zf:
            id_to_file = _hwpx_manifest(zf)
            sections = [n for n in zf.namelist() if re.search(r"section\d+\.xml$", n, re.I)]
            sections.sort(key=lambda n: int(re.search(r"section(\d+)\.xml$", n, re.I).group(1)))

            page = 1
            for section in sections:
                try:
                    root = ET.fromstring(zf.read(section).decode("utf-8", "replace"))
                except Exception:
                    continue
                prev_vert = None
                for para in root:
                    if _local(para) != "p":
                        continue
                    segs, page, prev_vert = _hwpx_para_lines(para, page, prev_vert)
                    if segs:
                        _hwpx_collect_images(para, segs, page_map, id_to_file)
                page += 1
    except Exception:
        return page_map
    return page_map


def _hwpx_para_lines(para, page: int, prev_vert):
    """최상위 문단의 줄 목록 [(textpos, page)]과 갱신된 (page, prev_vert)."""
    segs = []
    page_break = para.get("pageBreak") == "1"
    first_seg = True
    for child in para:
        if _local(child) != "linesegarray":
            continue
        for seg in child:
            if _local(seg) != "lineseg":
                continue
            try:
                vert = int(seg.get("vertpos", "0"))
                textpos = int(seg.get("textpos", "0"))
            except ValueError:
                continue
            if prev_vert is not None and vert < prev_vert:
                page += 1
            elif first_seg and page_break and prev_vert is not None:
                page += 1
            prev_vert = vert
            first_seg = False
            segs.append((textpos, page))
    return segs, page, prev_vert


def _hwpx_collect_images(para, segs: list, page_map: dict, id_to_file: dict):
    """문단 안의 그림을 문자 위치 기준으로 줄(=쪽)에 매핑합니다.

    표·그룹 등 컨테이너 개체는 하위 트리를 통째로 훑어(iter) 그 안의 그림까지
    '컨테이너가 놓인 쪽'으로 기록한다(표가 여러 쪽에 걸치면 시작 쪽으로 보고됨)."""
    char_pos = 0
    for run in para:
        if _local(run) != "run":
            continue
        for node in run:
            kind = _local(node)
            if kind == "t":
                char_pos += len("".join(node.itertext()))
                continue
            if kind == "secPr":            # 구역 정의는 글자를 차지하지 않는다
                continue
            page = _page_of_pos(segs, char_pos)
            for elem in node.iter():
                if _local(elem) != "img":
                    continue
                ref = elem.get("binaryItemIDRef")
                if not ref:
                    continue
                _add(page_map, ref.lower(), page)
                filename = id_to_file.get(ref)
                if filename:
                    _add(page_map, filename, page)
            char_pos += CTRL_CHAR_WIDTH


# ─────────────────────────────────────────
# 조회 헬퍼
# ─────────────────────────────────────────

def lookup_hwp(page_map: dict, stream_name: str):
    """'BIN0003.png' → 쪽 목록(없으면 None)."""
    m = re.match(r"^BIN([0-9A-Fa-f]{4})\.", stream_name)
    if not m:
        return None
    return page_map.get(int(m.group(1), 16))


def lookup_hwpx(page_map: dict, entry_name: str):
    """'BinData/image1.BMP' 또는 'image1' → 쪽 목록(없으면 None)."""
    name = entry_name.split("/")[-1].lower()
    return page_map.get(name) or page_map.get(Path(name).stem)
