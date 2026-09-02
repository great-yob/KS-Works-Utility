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

── 인쇄되는 쪽 번호 vs 문서 몇 번째 쪽 ──────────────────────────────────
위에서 센 것은 '문서의 몇 번째 쪽'(물리 쪽)이다. 그런데 표지·목차 뒤에서 본문을 1쪽으로
다시 시작하는 문서가 흔하다(한글 '새 번호로 시작'). 사용자가 한글에서 찾아갈 때 보는 것은
**인쇄되는 번호**이므로, 새 번호 컨트롤(OLE: CTRL_HEADER `nwno`, 번호 종류 0=쪽 /
HWPX: `<hp:newNum numType="PAGE" num="N"/>`)을 만나면 그 쪽부터 번호를 재설정한다.
따라서 매핑 결과는 (인쇄 번호, 물리 쪽) 쌍이고, 로그 칩은 인쇄 번호를, 툴팁이 물리 쪽을
보여 준다. 한글 '문서 정보 → 그림 정보'의 쪽 수는 물리 쪽이라 서로 다를 수 있다.

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

# DocInfo 레코드 태그 (본문 밖 그림 참조를 찾기 위한 것)
TAG_BORDER_FILL = 20
TAG_BULLET = 24

# 쪽을 못 매기는 쓰임새(로그 칩에 그대로 표시된다)
REASON_FILL = "채우기"        # 문단·표의 그림 채우기(배경)
REASON_BULLET = "글머리"      # 그림 글머리표
REASON_OUTSIDE = "본문 밖"    # HWPX 바탕쪽·머리말 등 구역 XML 밖의 참조

# 채우기 종류 비트 (BORDER_FILL의 채우기 정보)
FILL_SOLID = 1
FILL_IMAGE = 2
FILL_GRADIENT = 4

TAG_PARA_SHAPE = 25

# BodyText 레코드 태그
TAG_PARA_HEADER = 66
TAG_LIST_HEADER = 72        # 표 셀 등 — 셀 배경(테두리/채우기 ID)을 들고 있다
TAG_PARA_TEXT = 67
TAG_PARA_LINE_SEG = 69
TAG_CTRL_HEADER = 71
TAG_SHAPE_PICTURE = 85

LINE_SEG_SIZE = 36          # 줄 정보 1개 크기(바이트)
PARA_BREAK_PAGE = 0x04      # 문단머리 '나누기 종류' 중 쪽 나누기 비트
MAX_PAGES_PER_IMAGE = 20    # 같은 그림이 여러 곳에 쓰인 경우 보관할 쪽 수 상한

# '새 번호로 시작' 컨트롤. OLE는 CTRL_HEADER 앞 4바이트에 ID가 뒤집혀 들어간다('onwn').
CTRL_ID_NEW_NUMBER = b"nwno"
NEW_NUMBER_KIND_PAGE = 0    # 속성 하위 4비트의 번호 종류. 0=쪽(그림/표/수식 번호는 무시)


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


class _Numbering:
    """물리 쪽 → 문서에 인쇄되는 쪽 번호. '새 번호로 시작'을 만나면 그 쪽부터 재설정한다."""

    def __init__(self):
        self.offset = 0

    def restart(self, physical_page: int, start_number: int):
        self.offset = start_number - physical_page

    def display(self, physical_page: int) -> int:
        return physical_page + self.offset


class PageMap:
    """그림 위치 조회 결과.

    pages : 키 → [(인쇄 번호, 물리 쪽), ...]  — 본문에 배치돼 쪽을 특정할 수 있는 그림
    used  : 쪽은 몰라도 **문서가 참조하고 있는** 키 전부. 본문 그림에 더해 그림 채우기
            (문단·표 배경), 그림 글머리표, 바탕쪽/머리말 등 쪽으로 환산할 수 없는 참조까지
            포함한다. '변환에서 제외해도 되는가'는 오직 이 집합으로 판단한다.
    ok    : 참조 수집이 정상적으로 끝났는가. False면 어떤 그림도 미사용으로 단정하지 않는다.
    """

    def __init__(self):
        self.pages: dict = {}
        self.used: set = set()
        self.notes: dict = {}       # 키 → 쪽을 못 매기는 쓰임새('채우기' 등)
        self.ok: bool = False

    def lookup(self, *keys):
        for key in keys:
            found = self.pages.get(key)
            if found:
                return found
        return None

    def note(self, *keys):
        """쪽은 없지만 쓰이고 있는 그림의 쓰임새. 본문 그림이거나 미사용이면 None."""
        for key in keys:
            if key in self.pages:
                return None
            if key in self.notes:
                return self.notes[key]
        return None

    def mark_used(self, key, reason: str):
        self.used.add(key)
        self.notes.setdefault(key, reason)

    def is_unused(self, *keys) -> bool:
        """문서 어디에서도 참조되지 않는 그림인가(= 변환에서 빼도 되는가).

        ⚠ 수집이 실패했거나(ok=False) 참조를 하나라도 찾으면 반드시 False. 실제로 쓰이는
        그림을 건너뛰는 쪽이 쓰레기 한 장을 변환하는 쪽보다 훨씬 나쁘기 때문에, 애매하면
        항상 '사용 중'으로 본다."""
        if not self.ok:
            return False
        return not any(key in self.used for key in keys)


def _add(page_map: PageMap, key, page: int, numbering: "_Numbering"):
    """그림 1장의 위치를 (인쇄 번호, 물리 쪽) 쌍으로 기록합니다."""
    entry = (numbering.display(page), page)
    page_map.used.add(key)
    pages = page_map.pages.setdefault(key, [])
    if len(pages) < MAX_PAGES_PER_IMAGE and (not pages or pages[-1] != entry):
        pages.append(entry)


def _add_entries(page_map: PageMap, key, entries: list):
    """이미 만들어진 (인쇄, 물리) 목록을 합칩니다(중복 제거, 쪽 순서 정렬)."""
    if not entries:
        return
    page_map.used.add(key)
    merged = page_map.pages.setdefault(key, [])
    merged.extend(e for e in entries if e not in merged)
    merged.sort(key=lambda e: e[1])
    del merged[MAX_PAGES_PER_IMAGE:]


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


def build_page_map_hwp(hwp_path: str) -> PageMap:
    """HWP(OLE)의 bin id → 쪽/사용 여부. 실패하면 비어 있는(ok=False) PageMap."""
    import zlib
    page_map = PageMap()
    try:
        import olefile
    except Exception:
        return page_map
    try:
        ole = olefile.OleFileIO(hwp_path)
    except Exception:
        return page_map

    try:
        sections = [n for n in ole.listdir()
                    if len(n) >= 2 and n[0] == "BodyText" and n[1].startswith("Section")]
        sections.sort(key=lambda n: int(re.sub(r"\D", "", n[1]) or 0))

        page = 1
        numbering = _Numbering()
        # 그림 채우기(배경)의 쪽을 알아내기 위한 중간 수집물.
        #   fill_pages  : 테두리/채우기 ID → 그 배경이 깔린 표 셀들의 쪽
        #   shape_pages : 문단 모양 ID     → 그 모양을 쓰는 문단들의 쪽
        # DocInfo에서 '그림 채우기 → BinData' 연결을 읽은 뒤 이 둘을 이어 붙인다.
        fill_pages: dict = {}
        shape_pages: dict = {}
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
            para_shape = None       # 현재 문단의 문단 모양 ID(쪽이 정해지면 기록)
            current_page = page     # 가장 최근 개체(CTRL_HEADER)가 놓인 쪽

            for tag, level, rec in _iter_records(data):
                if tag == TAG_PARA_HEADER and level == 0:
                    ctrl_positions, segs, ctrl_index = [], [], 0
                    pending_break = bool((rec[11] if len(rec) > 11 else 0) & PARA_BREAK_PAGE)
                    first_seg = True
                    # 문단 모양 ID(offset 8) — 이 문단이 어느 쪽에 앉는지는 줄 정보가 와야 안다.
                    para_shape = struct.unpack_from("<H", rec, 8)[0] if len(rec) >= 10 else None

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
                    if para_shape is not None and segs:
                        _note_page(shape_pages, para_shape, segs[0][1], numbering)
                        para_shape = None

                elif tag == TAG_CTRL_HEADER and level == 1:
                    # 최상위 문단에 직접 매달린 개체 → 앵커 문자 위치로 쪽 결정.
                    if ctrl_index < len(ctrl_positions) and segs:
                        current_page = _page_of_pos(segs, ctrl_positions[ctrl_index])
                    else:
                        current_page = page
                    ctrl_index += 1
                    # '새 번호로 시작' → 이 쪽부터 인쇄되는 번호를 다시 매긴다.
                    if rec[0:4][::-1] == CTRL_ID_NEW_NUMBER and len(rec) >= 10:
                        prop, start = struct.unpack_from("<IH", rec, 4)
                        if (prop & 0xF) == NEW_NUMBER_KIND_PAGE:
                            numbering.restart(current_page, start)

                elif tag == TAG_LIST_HEADER and len(rec) >= 34:
                    # 표 셀: offset 32의 테두리/채우기 ID. 셀 배경이 그림이면 그 그림이
                    # 이 셀(=표가 놓인 쪽)에 보인다. (실측: 길이 47짜리 셀 레코드에서
                    # 값의 6288/6307이 유효 범위 안 — 이 위치가 맞다.)
                    fill_id = struct.unpack_from("<H", rec, 32)[0]
                    if fill_id:
                        _note_page(fill_pages, fill_id, current_page, numbering)

                elif tag == TAG_SHAPE_PICTURE and len(rec) >= 72:
                    # 그림은 자기를 감싼 개체(표·그룹 포함)의 쪽을 따른다.
                    bin_id = struct.unpack_from("<H", rec, 71)[0]
                    if 1 <= bin_id <= 0xFFFF:
                        _add(page_map, bin_id, current_page, numbering)

            page += 1               # 다음 구역은 새 쪽에서 시작

        # 본문 밖 참조(그림 채우기·그림 글머리표)까지 모아야 '미사용' 판단이 안전해진다.
        _collect_docinfo_refs(ole, page_map, fill_pages, shape_pages)
        page_map.ok = True
    except Exception:
        page_map.ok = False         # 중간에 깨졌으면 아무것도 미사용으로 보지 않는다
    finally:
        try:
            ole.close()
        except Exception:
            pass
    return page_map


def _note_page(store: dict, key: int, page: int, numbering: "_Numbering"):
    """'이 ID가 이 쪽에서 쓰였다'를 기록합니다(중복 없이, 상한까지만)."""
    entries = store.setdefault(key, [])
    entry = (numbering.display(page), page)
    if len(entries) < MAX_PAGES_PER_IMAGE and entry not in entries:
        entries.append(entry)


def _collect_docinfo_refs(ole, page_map: PageMap, fill_pages: dict = None,
                          shape_pages: dict = None):
    """DocInfo에서 '쪽으로 환산할 수 없는' 그림 참조를 모읍니다.

    실측(2026-09, 한국공인회계사회 실무해설 내지): BinData 148개 중 106개가 본문 그림이
    아니라 **문단·표의 그림 채우기**(HWPTAG_BORDER_FILL)였다. 본문만 보고 미사용으로
    판단했다면 실제로 쓰이는 배경 그림 106장이 통째로 변환에서 빠졌을 것이다.

    BORDER_FILL 레이아웃: 속성(2) + 4방향 테두리(각 6) + 대각선(6) = 32바이트 뒤부터
    채우기 정보. 채우기 종류(4)는 비트 조합(1=단색, 2=이미지, 4=그러데이션)이고, 단색이
    함께 켜져 있으면 색 정보 12바이트가 앞에 붙는다. 이미지 채우기는
    유형(1)+밝기(1)+명암(1)+효과(1)+BinItem ID(2) 순서다.

    그러데이션이 섞이면 길이가 가변이라 위치를 특정할 수 없다. 그럴 때와 글머리표
    (BULLET/NUMBERING)처럼 배치가 확실치 않은 레코드는 **레코드 안의 모든 UINT16을 참조로
    간주**한다 — 실제보다 넓게 잡히지만, 쓰이는 그림을 빠뜨리지 않는 안전한 방향이다.

    채우기 그림에도 쪽을 매긴다: 본문 순회에서 모아 둔 fill_pages(테두리/채우기 ID → 쪽)와
    shape_pages(문단 모양 ID → 쪽)를 여기서 이어 붙인다. 즉 '이 배경 그림을 쓰는 표 셀·문단이
    몇 쪽에 있나'를 그림의 쪽으로 삼는다. 여러 곳에 깔렸으면 쪽이 여러 개가 되고(칩은 첫 쪽에
    +를 붙여 보여 준다), 한 곳도 못 찾으면 쪽 없이 '채우기'로만 표시한다."""
    import zlib
    try:
        raw = ole.openstream("DocInfo").read()
    except Exception:
        raise                        # DocInfo를 못 읽으면 ok=False로 떨어뜨린다
    try:
        data = zlib.decompress(raw, -15)
    except Exception:
        try:
            data = zlib.decompress(raw)
        except Exception:
            data = raw

    fill_pages = fill_pages or {}
    shape_pages = shape_pages or {}
    image_fills: dict = {}          # 테두리/채우기 ID(1부터) → BinData id
    shape_to_fill: dict = {}        # 문단 모양 ID(0부터) → 테두리/채우기 ID
    fill_index = 0
    shape_index = -1

    for tag, _level, rec in _iter_records(data):
        if tag == TAG_BORDER_FILL and len(rec) >= 36:
            fill_index += 1         # 다른 레코드가 참조하는 ID는 1부터 세는 등장 순서
            fill_type = struct.unpack_from("<I", rec, 32)[0]
            if not (fill_type & FILL_IMAGE):
                continue
            if fill_type & FILL_GRADIENT:
                _mark_all_uint16(rec, page_map, REASON_FILL)   # 길이 가변 → 넓게 잡는다
                continue
            pos = 36 + (12 if fill_type & FILL_SOLID else 0)
            if pos + 6 <= len(rec):
                image_fills[fill_index] = struct.unpack_from("<H", rec, pos + 4)[0]
            else:
                _mark_all_uint16(rec, page_map, REASON_FILL)
        elif tag == TAG_PARA_SHAPE and len(rec) >= 34:
            shape_index += 1        # 문단이 참조하는 문단 모양 ID는 0부터 세는 등장 순서
            shape_to_fill[shape_index] = struct.unpack_from("<H", rec, 32)[0]
        elif tag == TAG_BULLET and len(rec) >= 16:
            # 글머리표: offset 14의 '이미지 글머리 여부'가 켜진 것만 그림을 참조한다.
            # 그림 정보의 정확한 위치는 판본마다 흔들려서, 해당 레코드 안의 UINT16을 모두
            # 참조로 넣는다(그림 글머리표는 드물어 과잉 포함의 부작용이 거의 없다).
            # 번호 매기기(NUMBERING)는 그림을 참조하지 않으므로 보지 않는다 — 넣으면 레코드가
            # 길어 우연히 작은 id들이 전부 '사용 중'으로 잡히고 제외가 무력화된다.
            if struct.unpack_from("<h", rec, 14)[0] != 0:
                _mark_all_uint16(rec, page_map, REASON_BULLET)

    # 채우기 그림 → 그 배경이 실제로 깔린 쪽들
    for fill_id, bin_id in image_fills.items():
        entries = list(fill_pages.get(fill_id, []))                  # 표 셀 배경
        for shape_id, ref in shape_to_fill.items():                  # 문단 배경
            if ref == fill_id:
                entries.extend(shape_pages.get(shape_id, []))
        if entries:
            _add_entries(page_map, bin_id, entries)
        else:
            page_map.mark_used(bin_id, REASON_FILL)


def _mark_all_uint16(rec: bytes, page_map: PageMap, reason: str):
    """레코드 안의 모든 UINT16을 '참조'로 넣습니다(과잉 포함 = 안전한 쪽)."""
    for off in range(0, len(rec) - 1):
        page_map.mark_used(struct.unpack_from("<H", rec, off)[0], reason)


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


def build_page_map_hwpx(hwpx_path: str) -> PageMap:
    """HWPX의 그림 → 쪽/사용 여부. 키는 BinData 파일명(소문자)과 binaryItemIDRef 둘 다."""
    page_map = PageMap()
    try:
        import xml.etree.ElementTree as ET
    except Exception:
        return page_map

    try:
        with zipfile.ZipFile(hwpx_path, "r") as zf:
            id_to_file = _hwpx_manifest(zf)
            # 쪽을 못 매기는 참조(바탕쪽·머리말·그림 채우기 등)까지 빠짐없이 모은다.
            # XML 전체에서 binaryItemIDRef를 긁으면 되므로 OLE보다 단순·확실하다.
            for name in zf.namelist():
                if not name.lower().endswith((".xml", ".hpf")):
                    continue
                try:
                    text = zf.read(name).decode("utf-8", "replace")
                except Exception:
                    continue
                # 구역 XML 안인데 쪽을 못 매긴 참조는 대개 그림 채우기(<hc:imgBrush>)이고,
                # 바탕쪽·머리말 파일의 참조는 본문 밖이다. 쪽이 잡히면 이 표시는 무시된다.
                in_section = bool(re.search(r"section\d+\.xml$", name, re.I))
                reason = REASON_FILL if in_section else REASON_OUTSIDE
                for ref in re.findall(r'binaryItemIDRef="([^"]+)"', text):
                    page_map.mark_used(ref.lower(), reason)
                    filename = id_to_file.get(ref)
                    if filename:
                        page_map.mark_used(filename, reason)
            sections = [n for n in zf.namelist() if re.search(r"section\d+\.xml$", n, re.I)]
            sections.sort(key=lambda n: int(re.search(r"section(\d+)\.xml$", n, re.I).group(1)))

            page = 1
            numbering = _Numbering()
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
                        _hwpx_collect_images(para, segs, page_map, id_to_file, numbering)
                page += 1
            page_map.ok = True
    except Exception:
        page_map.ok = False
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


def _hwpx_collect_images(para, segs: list, page_map: dict, id_to_file: dict,
                         numbering: "_Numbering"):
    """문단 안의 그림을 문자 위치 기준으로 줄(=쪽)에 매핑합니다.

    표·그룹 등 컨테이너 개체는 하위 트리를 통째로 훑어(iter) 그 안의 그림까지
    '컨테이너가 놓인 쪽'으로 기록한다(표가 여러 쪽에 걸치면 시작 쪽으로 보고됨).
    같은 순회에서 '새 번호로 시작'(`<hp:newNum numType="PAGE">`)도 처리해, 그 쪽부터
    인쇄되는 쪽 번호를 다시 매긴다."""
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
                local = _local(elem)
                if local == "newNum" and elem.get("numType", "").upper() == "PAGE":
                    try:
                        numbering.restart(page, int(elem.get("num", "1")))
                    except ValueError:
                        pass
                    continue
                if local != "img":
                    continue
                ref = elem.get("binaryItemIDRef")
                if not ref:
                    continue
                _add(page_map, ref.lower(), page, numbering)
                filename = id_to_file.get(ref)
                if filename:
                    _add(page_map, filename, page, numbering)
            char_pos += CTRL_CHAR_WIDTH


# ─────────────────────────────────────────
# 조회 헬퍼
# ─────────────────────────────────────────

def _hwp_keys(stream_name: str) -> tuple:
    """'BIN0003.png' → (bin id,). 이름 형식이 아니면 빈 튜플."""
    m = re.match(r"^BIN([0-9A-Fa-f]{4})\.", stream_name)
    return (int(m.group(1), 16),) if m else ()


def _hwpx_keys(entry_name: str) -> tuple:
    """'BinData/image1.BMP' → ('image1.bmp', 'image1')."""
    name = entry_name.split("/")[-1].lower()
    return (name, Path(name).stem)


def lookup_hwp(page_map: PageMap, stream_name: str):
    """'BIN0003.png' → [(인쇄 번호, 물리 쪽), ...] (없으면 None)."""
    return page_map.lookup(*_hwp_keys(stream_name))


def lookup_hwpx(page_map: PageMap, entry_name: str):
    """'BinData/image1.BMP' 또는 'image1' → [(인쇄 번호, 물리 쪽), ...] (없으면 None)."""
    return page_map.lookup(*_hwpx_keys(entry_name))


def note_hwp(page_map: PageMap, stream_name: str):
    """쪽은 없지만 쓰이고 있는 그림의 쓰임새('채우기' 등). 아니면 None."""
    return page_map.note(*_hwp_keys(stream_name))


def note_hwpx(page_map: PageMap, entry_name: str):
    """쪽은 없지만 쓰이고 있는 그림의 쓰임새('본문 밖' 등). 아니면 None."""
    return page_map.note(*_hwpx_keys(entry_name))


def is_unused_hwp(page_map: PageMap, stream_name: str) -> bool:
    """이 BinData 스트림이 문서 어디에서도 안 쓰이는가(= 변환에서 빼도 되는가)."""
    keys = _hwp_keys(stream_name)
    return bool(keys) and page_map.is_unused(*keys)


def is_unused_hwpx(page_map: PageMap, entry_name: str) -> bool:
    """이 BinData 항목이 문서 어디에서도 안 쓰이는가(= 변환에서 빼도 되는가)."""
    return page_map.is_unused(*_hwpx_keys(entry_name))
