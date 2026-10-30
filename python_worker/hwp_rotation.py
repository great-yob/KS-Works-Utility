"""회전된 그림을 '바로 세운 그림'으로 바꿉니다(그림 회전 해제).

한글에서 90°·270° 등으로 돌려 넣은 그림은 Acrobat(PDF 프린터)으로 변환할 때 그림이
돌아가거나 수많은 조각으로 찢어진다. 사람이 하던 해결책은 그림을 꺼내 포토샵에서 돌려
저장한 뒤 다시 넣고 한글의 회전각은 0°로 두는 것이었다. 여기서는 그 작업을 변환과 함께
한다: 그림 픽셀을 화면에 보이던 방향으로 돌려 저장하고, 문서에는 '회전 없음'으로 적는다.

── 한글이 회전을 어떻게 저장하는가 (OLE HWP, 실측 2026-10 `회전이미지.hwp`) ──────────
그림 하나는 CTRL_HEADER('gso ') → SHAPE_COMPONENT(Tag76) → PICTURE(Tag85) 세 레코드다.
  * gso의 너비·높이(offset 16/20)는 **돌린 뒤 화면에서 차지하는 상자**다. 가로 그림을
    90° 세우면 세로로 긴 상자가 되고, 개체 속성 대화상자의 크기가 이 값이다. 건드리지 않는다.
  * Tag76: 그림 자체(돌리기 전)의 크기와 회전. 최상위 개체는 개체 ID가 두 번 들어가 필드가
    offset 8부터 시작한다 — x/y 오프셋, 묶음 수준, 버전, 원래 크기(init), 현재 크기(cur),
    속성, 회전각(INT16, 시계 방향), 회전 중심, 렌더링 행렬(이동 1개 + [배율, 회전] 쌍 n개,
    각각 double 6개). 회전된 그림은 속성에 0x02000000이 켜져 있다(회전 없는 그림 123장은
    전부 꺼져 있었다).
  * Tag85: 네 꼭짓점(12, = init 사각형), 자르기 사각형(44, '원본 크기' 프레임 기준),
    안쪽 여백(60), BinData ID(71), 그림 효과(78), 원본 크기(82, 래스터는 픽셀×75).

바로 세우기 = 픽셀을 시계 방향 angle°로 돌리고 '그림 자체' 좌표계도 같이 돌린다. init·cur·
꼭짓점·원본 크기의 가로세로를 바꾸고(90°/270°), 자르기 사각형을 같은 회전으로 옮기고,
회전각 0·회전 플래그 해제·행렬은 회전 없는 그림과 같은 꼴(이동=단위, 배율=cur/init,
회전=단위)로 쓴다. 바뀐 cur가 gso 상자와 같아지므로 쪽 위의 위치·크기는 그대로다.

── 같은 그림이 여러 곳에 쓰일 때 ───────────────────────────────────────────
BinData 하나를 회전된 그림과 회전 안 된 그림이 함께 쓰면(실측: 68쪽은 270°, 70쪽은 0°)
픽셀을 제자리에서 돌릴 수 없다. 그때는 돌린 사본을 새 BinData로 추가하고 회전된 그림만
사본을 가리키게 한다. 그림 채우기·글머리표 등 본문 그림 밖에서 쓰이는 그림도 같은 이유로
사본을 만든다(판단이 애매하면 항상 사본 쪽 — 원본을 돌려 다른 곳이 망가지는 것보다 낫다).

── 건너뛰는 경우 ─────────────────────────────────────────────────────────
90° 단위가 아닌 회전, 뒤집기, 묶음(그룹) 안의 그림, 그림 효과·안쪽 여백이 있는 그림, 레코드
모양이 위와 다른 그림은 건드리지 않고 사유를 로그로 알린다. 해석이 조금이라도 어긋나면
아무것도 바꾸지 않는 쪽으로 떨어진다(빈 계획).
"""

import io
import re
import struct
from collections import Counter

from PIL import Image

# BodyText / DocInfo 레코드 태그
TAG_ID_MAPPINGS = 17
TAG_BIN_DATA = 18
TAG_CTRL_HEADER = 71
TAG_SHAPE_COMPONENT = 76
TAG_SHAPE_OLE = 84
TAG_SHAPE_PICTURE = 85

GSO_CTRL_ID = b" osg"               # 'gso '를 뒤집어 저장한다
PIC_CTRL_ID = b"cip$"               # '$pic'
CONTAINER_CTRL_ID = b"noc$"         # '$con' (묶음)

ROTATED_FLAG = 0x02000000           # Tag76 속성: 회전된 그림
FLIP_MASK = 0x00000003              # Tag76 속성: 좌우/상하 뒤집기

SHAPE_FMT = "<iiHHIIIIIhiiH"        # x/y 오프셋, 묶음 수준, 버전, init w/h, cur w/h, 속성, 회전각, 회전 중심 x/y, 행렬 쌍 수
SHAPE_SIZE = struct.calcsize(SHAPE_FMT)
MATRIX_SIZE = 48                    # double 6개(2×3 행렬, 행 우선)
IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)

PIC_CORNERS = 12
PIC_CROP = 44
PIC_MARGIN = 60
PIC_BIN_ID = 71
PIC_EFFECT = 78
PIC_ORIGINAL = 82
PIC_MIN_SIZE = 90                   # 원본 크기(82~89)까지 있어야 고칠 수 있다

BOX_TOLERANCE = 0.01                # gso 상자와 돌린 cur의 허용 오차(비율)

# 채우기 종류 비트(DocInfo BORDER_FILL과 그리기 개체의 채우기 정보가 같은 구조)
FILL_SOLID = 1
FILL_IMAGE = 2
FILL_GRADIENT = 4

# PIL의 ROTATE_*는 반시계 방향이다. 한글 회전각은 시계 방향.
_TRANSPOSE = {
    90: Image.Transpose.ROTATE_270,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_90,
}


# ─────────────────────────────────────────
# 공통
# ─────────────────────────────────────────

def _iter_records(data: bytes):
    """레코드를 (tag, level, 헤더 시작, 본문 시작, 본문 끝)으로 순회합니다."""
    pos, n = 0, len(data)
    while pos + 4 <= n:
        header = struct.unpack_from("<I", data, pos)[0]
        tag = header & 0x3FF
        level = (header >> 10) & 0x3FF
        size = (header >> 20) & 0xFFF
        hdr = 4
        if size == 0xFFF:
            if pos + 8 > n:
                break
            size = struct.unpack_from("<I", data, pos + 4)[0]
            hdr = 8
        start = pos + hdr
        if start + size > n:
            break
        yield tag, level, pos, start, start + size
        pos = start + size


def _record_header(tag: int, level: int, size: int) -> bytes:
    if size < 0xFFF:
        return struct.pack("<I", (tag & 0x3FF) | ((level & 0x3FF) << 10) | (size << 20))
    return struct.pack("<II", (tag & 0x3FF) | ((level & 0x3FF) << 10) | (0xFFF << 20), size)


def _decompress(raw: bytes) -> bytes:
    import zlib
    try:
        return zlib.decompress(raw, -15)
    except zlib.error:
        pass
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return raw


def _rotate_rect(rect, angle: int, frame_w: int, frame_h: int):
    """frame 안의 사각형 (L, T, R, B)를 그림과 함께 시계 방향 angle°로 돌린 좌표."""
    left, top, right, bottom = rect
    if angle == 90:                      # (x, y) → (H - y, x)
        return frame_h - bottom, left, frame_h - top, right
    if angle == 180:                     # (x, y) → (W - x, H - y)
        return frame_w - right, frame_h - bottom, frame_w - left, frame_h - top
    return top, frame_w - right, bottom, frame_w - left     # 270: (x, y) → (y, W - x)


def rotate_jpg(data: bytes, angle: int) -> bytes:
    """JPG를 화면에 보이던 방향(시계 방향 angle°)으로 돌려 다시 저장합니다."""
    img = Image.open(io.BytesIO(data))
    dpi = img.info.get("dpi") or (96, 96)
    if angle in (90, 270):
        dpi = (dpi[1], dpi[0])
    img = img.transpose(_TRANSPOSE[angle])
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=95, dpi=dpi)
    return out.getvalue()


class RotationPlan:
    """바로 세울 그림 목록. 비어 있으면(False) 아무것도 바꾸지 않는다.

    키(key)는 그림 데이터를 가리키는 값이다 — OLE는 bin id(int), HWPX는 binaryItemIDRef(str).

    usages     : [(구역 이름, 구역 안 몇 번째 그림, 키, 각도, 자르기 프레임)] — 고칠 그림 하나하나.
                 자르기 프레임은 HWPX만 쓴다(OLE는 레코드에 원본 크기가 있어 None).
    in_place   : 키 → 각도. 그 그림을 쓰는 곳이 전부 같은 각도라 픽셀을 제자리에서 돌린다.
    copies     : 키 → [각도]. 다른 곳에서도 쓰여 돌린 사본을 새 그림으로 추가한다.
    new_ids    : (키, 각도) → 사본의 새 키. 사본 목록(DocInfo/매니페스트)에 넣은 뒤 채워진다.
    copy_names : (키, 각도) → 사본 파일 이름(로그 표시용)
    entries    : HWPX 키 → ZIP 안 그림 경로
    clip_order : HWPX imgClip 표기(hwpx_clip_order)
    skipped    : [(키, 각도, 사유)] — 회전돼 있지만 손대지 않는 그림
    """

    def __init__(self):
        self.usages: list = []
        self.in_place: dict = {}
        self.copies: dict = {}
        self.new_ids: dict = {}
        self.copy_names: dict = {}
        self.entries: dict = {}
        self.clip_order: tuple = CLIP_LABELLED
        self.skipped: list = []

    def __bool__(self):
        return bool(self.in_place or self.copies)

    def keep(self, in_place_done: set, copies_done: set):
        """픽셀을 실제로 돌린 그림만 남깁니다(변환에 실패한 그림은 회전을 그대로 둔다)."""
        self.in_place = {b: a for b, a in self.in_place.items() if b in in_place_done}
        copies = {b: [a for a in angles if (b, a) in copies_done]
                  for b, angles in self.copies.items()}
        self.copies = {b: angles for b, angles in copies.items() if angles}

    def targets(self) -> dict:
        """구역 이름 → {몇 번째 그림: (키, 각도, 새 키 | None, 프레임)} — 구역 패치 입력."""
        out: dict = {}
        for section, ordinal, key, angle, frame in self.usages:
            if self.in_place.get(key) == angle:
                new_key = None
            elif (key, angle) in self.new_ids:
                new_key = self.new_ids[(key, angle)]
            else:
                continue
            out.setdefault(section, {})[ordinal] = (key, angle, new_key, frame)
        return out

    def _split(self, fixable: list, shared_keys):
        """고칠 그림을 키별로 묶어 제자리/사본을 정한다. shared_keys(키) → 다른 곳에서도 쓰이는가."""
        angles_of: dict = {}
        for _section, _ordinal, key, angle, _frame in fixable:
            angles_of.setdefault(key, Counter())[angle] += 1
        for key, angles in angles_of.items():
            ordered = [a for a, _count in angles.most_common()]
            if shared_keys(key):
                self.copies[key] = ordered
            else:
                self.in_place[key] = ordered[0]
                if len(ordered) > 1:
                    self.copies[key] = ordered[1:]
        self.usages = fixable


# ─────────────────────────────────────────
# OLE HWP
# ─────────────────────────────────────────

def _check_picture(shape: bytes, pic: bytes, box):
    """(각도, 사유). 각도 0 = 회전 없음, 사유 None = 바로 세울 수 있음."""
    top_level = shape[0:4] == PIC_CTRL_ID and shape[4:8] == PIC_CTRL_ID
    base = 8 if top_level else 4
    if len(shape) < base + SHAPE_SIZE:
        return 0, None
    (_xo, _yo, _grp, _ver, iw, ih, cw, ch,
     prop, rot, _cx, _cy, n) = struct.unpack_from(SHAPE_FMT, shape, base)
    angle = rot % 360
    if angle == 0:
        return 0, None
    if not top_level:
        return angle, "묶음 개체 안의 그림"
    if angle % 90:
        return angle, "90° 단위가 아닌 회전"
    if prop & FLIP_MASK:
        return angle, "뒤집기가 함께 적용된 그림"
    if n != 1 or len(shape) < base + SHAPE_SIZE + MATRIX_SIZE * 3:
        return angle, "렌더링 정보 형식이 다름"
    if min(iw, ih, cw, ch) <= 0 or len(pic) < PIC_MIN_SIZE:
        return angle, "그림 정보 형식이 다름"
    if struct.unpack_from("<I", pic, PIC_EFFECT)[0] != 0:
        return angle, "그림 효과(그림자·네온 등)가 있는 그림"
    if any(struct.unpack_from("<4H", pic, PIC_MARGIN)):
        return angle, "안쪽 여백이 있는 그림"
    if struct.unpack_from("<8i", pic, PIC_CORNERS) != (0, 0, iw, 0, iw, ih, 0, ih):
        return angle, "그림 꼭짓점 형식이 다름"
    left, top, right, bottom = struct.unpack_from("<4i", pic, PIC_CROP)
    ow, oh = struct.unpack_from("<2i", pic, PIC_ORIGINAL)
    if not (0 <= left < right <= ow and 0 <= top < bottom <= oh):
        return angle, "자르기 정보 형식이 다름"
    if box:
        # 돌린 그림(cur)이 화면 상자(gso)를 정확히 채워야 상자를 그대로 둔 채 바로 세울 수 있다.
        want_w, want_h = (ch, cw) if angle in (90, 270) else (cw, ch)
        box_w, box_h = box
        if (abs(box_w - want_w) > max(2, box_w * BOX_TOLERANCE)
                or abs(box_h - want_h) > max(2, box_h * BOX_TOLERANCE)):
            return angle, "개체 크기 정보가 맞지 않음"
    return angle, None


def _shape_fill_refs(shape: bytes) -> set:
    """그리기 개체(사각형 등)의 그림 채우기가 참조하는 bin id. 모양을 못 읽으면 넓게 잡는다."""
    if shape[0:4] in (PIC_CTRL_ID, CONTAINER_CTRL_ID):
        return set()
    base = 8 if shape[0:4] == shape[4:8] else 4
    if len(shape) < base + SHAPE_SIZE:
        return set()
    n = struct.unpack_from("<H", shape, base + SHAPE_SIZE - 2)[0]
    tail = base + SHAPE_SIZE + MATRIX_SIZE * (1 + 2 * n)
    fill = tail + 13                 # 테두리 선(색 4 + 굵기 4 + 속성 4 + 외곽선 1) 뒤가 채우기
    if fill + 4 > len(shape):
        return set()
    kind = struct.unpack_from("<I", shape, fill)[0]
    if not kind & FILL_IMAGE:
        return set()
    pos = fill + 4 + (12 if kind & FILL_SOLID else 0)
    if kind & FILL_GRADIENT or pos + 6 > len(shape):
        return {struct.unpack_from("<H", shape, o)[0] for o in range(tail, len(shape) - 1)}
    return {struct.unpack_from("<H", shape, pos + 4)[0]}


def _docinfo_refs(ole):
    """DocInfo의 그림 채우기·그림 글머리표가 참조하는 bin id. 모르면 None(전부 공유로 본다)."""
    try:
        from hwp_pagemap import PageMap, _collect_docinfo_refs
        page_map = PageMap()
        _collect_docinfo_refs(ole, page_map)
        return set(page_map.used)
    except Exception:
        return None


def _section_names(ole) -> list:
    names = [n[1] for n in ole.listdir()
             if len(n) == 2 and n[0] == "BodyText" and n[1].startswith("Section")]
    return sorted(names, key=lambda s: int(re.sub(r"\D", "", s) or 0))


def plan_hwp(hwp_path: str) -> RotationPlan:
    """원본을 읽기만 해서 바로 세울 그림을 찾습니다. 실패하면 빈 계획."""
    plan = RotationPlan()
    try:
        import olefile
        ole = olefile.OleFileIO(hwp_path)
    except Exception:
        return plan
    try:
        fixable = []
        plain = set()               # 회전 없이(또는 못 고치고) 그대로 쓰이는 bin
        other_refs = set()          # 본문 그림이 아닌 참조(그리기 개체 채우기, OLE)
        for name in _section_names(ole):
            data = _decompress(ole.openstream(["BodyText", name]).read())
            ctrls, shapes = {}, {}
            ordinal = -1
            for tag, level, _hdr, start, end in _iter_records(data):
                if tag == TAG_CTRL_HEADER:
                    ctrls[level] = data[start:end]
                elif tag == TAG_SHAPE_COMPONENT:
                    shapes[level] = data[start:end]
                    other_refs |= _shape_fill_refs(shapes[level])
                elif tag == TAG_SHAPE_OLE and end - start >= 14:
                    other_refs.add(struct.unpack_from("<H", data, start + 12)[0])
                elif tag == TAG_SHAPE_PICTURE and end - start > PIC_BIN_ID + 1:
                    ordinal += 1
                    pic = data[start:end]
                    bin_id = struct.unpack_from("<H", pic, PIC_BIN_ID)[0]
                    shape = shapes.get(level - 1)
                    gso = ctrls.get(level - 2)
                    box = (struct.unpack_from("<2i", gso, 16)
                           if gso and gso[0:4] == GSO_CTRL_ID and len(gso) >= 24 else None)
                    angle, reason = _check_picture(shape, pic, box) if shape else (0, None)
                    if angle and reason is None:
                        fixable.append((name, ordinal, bin_id, angle, None))
                    else:
                        plain.add(bin_id)
                        if angle:
                            plan.skipped.append((bin_id, angle, reason))

        docinfo_refs = _docinfo_refs(ole)
        plan._split(fixable, lambda b: (docinfo_refs is None or b in docinfo_refs
                                        or b in plain or b in other_refs))
    except Exception:
        return RotationPlan()
    finally:
        try:
            ole.close()
        except Exception:
            pass
    return plan


def add_bin_copies(docinfo: bytes, plan: RotationPlan, ext: str = "jpg") -> bytes:
    """돌린 사본용 BIN_DATA 레코드를 DocInfo 끝 BinData 목록 뒤에 붙이고 plan.new_ids를 채웁니다.

    새 id는 기존 개수 다음부터 차례로(BinData ID = 등장 순서). ID_MAPPINGS의 첫 값(BinData
    개수)도 늘린다. 임베딩이 아닌(연결) 그림은 사본을 만들 수 없어 건너뛴다."""
    records = list(_iter_records(docinfo))
    id_map = next((r for r in records if r[0] == TAG_ID_MAPPINGS), None)
    bins = [r for r in records if r[0] == TAG_BIN_DATA]
    if id_map is None or not bins:
        raise ValueError("DocInfo에 BinData 목록이 없습니다")
    count = struct.unpack_from("<i", docinfo, id_map[3])[0]
    if count != len(bins):
        raise ValueError("DocInfo의 BinData 개수가 맞지 않습니다")

    added = bytearray()
    next_id = count + 1
    for bin_id in sorted(plan.copies):
        if not 1 <= bin_id <= count:
            continue
        _tag, level, _hdr, start, _end = bins[bin_id - 1]
        prop = struct.unpack_from("<H", docinfo, start)[0]
        if (prop & 0x000F) != 1:
            for angle in plan.copies[bin_id]:
                plan.skipped.append((bin_id, angle, "문서에 포함되지 않은(연결된) 그림"))
            continue
        for angle in plan.copies[bin_id]:
            payload = (struct.pack("<HHH", prop, next_id, len(ext))
                       + ext.encode("utf-16-le"))
            added += _record_header(TAG_BIN_DATA, level, len(payload)) + payload
            plan.new_ids[(bin_id, angle)] = next_id
            plan.copy_names[(bin_id, angle)] = f"BIN{next_id:04X}.{ext}"
            next_id += 1
    if not added:
        return docinfo

    out = bytearray(docinfo)
    insert_at = bins[-1][4]
    out[insert_at:insert_at] = added
    struct.pack_into("<i", out, id_map[3], next_id - 1)    # 삽입 위치가 뒤라 offset 그대로
    return bytes(out)


def _rewrite_picture(buf: bytearray, shape_off: int, pic_off: int, angle: int):
    """그림 1장의 Tag76/Tag85를 '바로 세운' 값으로 제자리에서 고칩니다(길이 불변)."""
    (_xo, _yo, grp, ver, iw, ih, cw, ch,
     prop, _rot, _cx, _cy, n) = struct.unpack_from(SHAPE_FMT, buf, shape_off)
    ow, oh = struct.unpack_from("<2i", buf, pic_off + PIC_ORIGINAL)
    crop = _rotate_rect(struct.unpack_from("<4i", buf, pic_off + PIC_CROP), angle, ow, oh)
    if angle in (90, 270):
        iw, ih, cw, ch, ow, oh = ih, iw, ch, cw, oh, ow

    struct.pack_into(SHAPE_FMT, buf, shape_off, 0, 0, grp, ver, iw, ih, cw, ch,
                     prop & ~ROTATED_FLAG, 0, cw // 2, ch // 2, n)
    matrices = shape_off + SHAPE_SIZE
    struct.pack_into("<6d", buf, matrices, *IDENTITY)                                   # 이동
    struct.pack_into("<6d", buf, matrices + MATRIX_SIZE, cw / iw, 0.0, 0.0, 0.0, ch / ih, 0.0)  # 배율
    struct.pack_into("<6d", buf, matrices + MATRIX_SIZE * 2, *IDENTITY)                 # 회전

    struct.pack_into("<8i", buf, pic_off + PIC_CORNERS, 0, 0, iw, 0, iw, ih, 0, ih)
    struct.pack_into("<4i", buf, pic_off + PIC_CROP, *crop)
    struct.pack_into("<2i", buf, pic_off + PIC_ORIGINAL, ow, oh)


def patch_section(data: bytes, targets: dict) -> bytes:
    """구역 1개에서 targets = {몇 번째 그림: (bin id, 각도, 새 bin id | None, _)}를 바로 세웁니다.

    계획을 세울 때와 같은 순서로 그림을 세므로, bin id가 계획과 다르면 예외로 멈춘다."""
    buf = bytearray(data)
    shapes = {}
    ordinal = -1
    done = 0
    for tag, level, _hdr, start, end in _iter_records(data):
        if tag == TAG_SHAPE_COMPONENT:
            shapes[level] = start
        elif tag == TAG_SHAPE_PICTURE and end - start > PIC_BIN_ID + 1:
            ordinal += 1
            target = targets.get(ordinal)
            if target is None:
                continue
            bin_id, angle, new_id, _frame = target
            if struct.unpack_from("<H", buf, start + PIC_BIN_ID)[0] != bin_id:
                raise ValueError("그림 순서가 계획과 다릅니다")
            _rewrite_picture(buf, shapes[level - 1] + 8, start, angle)
            if new_id:
                struct.pack_into("<H", buf, start + PIC_BIN_ID, new_id)
            done += 1
    if done != len(targets):
        raise ValueError("바로 세울 그림을 모두 찾지 못했습니다")
    return bytes(buf)


# ─────────────────────────────────────────
# HWPX
# ─────────────────────────────────────────
#
# 같은 모델이 XML로 들어 있다(같은 문서를 한글이 HWPX로 저장해 실측): <hp:offset>=x/y 오프셋,
# <hp:orgSz>=init, <hp:curSz>=cur, <hp:rotationInfo angle>, <hp:renderingInfo>의 세 행렬,
# <hp:imgRect>=꼭짓점, <hp:imgClip>=자르기, <hp:imgDim>=원본 크기(OLE offset 82),
# <hp:sz>=화면 상자, groupLevel>0 = 묶음 안.
#
# 판본마다 자르기 표기가 다르다(실측 2026-10, `D:\작업방` HWPX 577장 + 이 PC 한글의 저장본):
#   * 형식 5.1 이상(한글 2018~): 속성 이름대로 left=L right=R top=T bottom=B.
#     imgDim은 2018 후기 판부터 있고(420장), 초기 2018/2020은 없다(157장).
#   * 형식 5.0(한글 2010, version.xml minor="0"): 속성 이름과 값 순서가 어긋나 left/right/top/
#     bottom에 차례로 L, T, R, B가 들어 있다(자르기 없는 1104×844 그림: left=0 right=0
#     top=82800 bottom=63300). 한글 2010은 이걸 같은 방식으로 다시 읽는다(렌더 일치 확인).
#   그래서 version.xml로 표기를 정하고, 읽을 때와 같은 방식으로 다시 쓴다.
#   imgDim이 없으면 자르기 기준 프레임은 한글이 그림 픽셀로 다시 계산하는 값(OLE와 비교하면
#   픽셀×75, DPI 무관, 반올림 오차 ±30)이라 원본 그림을 열어 구한다.
# 바로 세운 결과는 한글이 '바로 세운 HWP'를 HWPX로 저장한 꼴과 똑같이 쓴다:
#   <hp:rotationInfo angle="0"/>, 이동·회전 행렬 = 단위, 배율 = cur/init(소수 6자리).

HWPX_PIC_RE = re.compile(r"<hp:pic\b.*?</hp:pic>", re.S)
HWPX_SECTION_RE = re.compile(r"(^|/)section\d+\.xml$", re.I)
HWPX_REF_RE = re.compile(r'binaryItemIDRef="([^"]+)"')
HWPX_ITEM_RE = re.compile(r'<(\w+:)?item\b[^>]*?\bid="([^"]+)"[^>]*?\bhref="([^"]+)"[^>]*?/>')
CROP_TOLERANCE = 0.03               # '자르기 없음'으로 볼 오차(원본 크기 반올림 차이 흡수)
ASPECT_TOLERANCE = 0.01


def _tag(block: str, name: str) -> dict:
    """block 안 첫 <name .../> 요소의 속성. 없으면 빈 dict."""
    m = re.search(rf"<{name}\b([^>]*?)/>", block)
    return dict(re.findall(r'([\w:]+)="([^"]*)"', m.group(1))) if m else {}


def _int(value: str) -> int:
    """HWPX는 음수를 UINT32로 쓴다(offset x="4294957351")."""
    n = int(value)
    return n - (1 << 32) if n >= (1 << 31) else n


def _fmt(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _set_attrs(block: str, name: str, **attrs) -> str:
    """block 안 첫 <name .../> 요소의 속성 값만 바꾼다(다른 속성은 보존)."""
    m = re.search(rf"<{name}\b[^>]*?/>", block)
    if m is None:
        raise ValueError(f"{name} 없음")
    tag = m.group(0)
    for key, value in attrs.items():
        tag, n = re.subn(rf'\b{key}="[^"]*"', f'{key}="{value}"', tag, count=1)
        if n != 1:
            raise ValueError(f"{name}@{key} 없음")
    return block[:m.start()] + tag + block[m.end():]


CLIP_LABELLED = ("left", "top", "right", "bottom")       # 형식 5.1 이상
CLIP_POSITIONAL = ("left", "right", "top", "bottom")     # 형식 5.0: 이 속성들에 차례로 L, T, R, B


def hwpx_clip_order(version_xml: str) -> tuple:
    """version.xml로 imgClip 표기를 정한다 → L, T, R, B가 들어 있는 속성 이름 순서."""
    m = re.search(r'<\w+:HCFVersion\b[^>]*?\bmajor="(\d+)"[^>]*?\bminor="(\d+)"', version_xml or "")
    if m and (int(m.group(1)), int(m.group(2))) < (5, 1):
        return CLIP_POSITIONAL
    return CLIP_LABELLED


def _hwpx_clip(block: str, order: tuple):
    clip = _tag(block, "hp:imgClip")
    return tuple(int(clip[k]) for k in order)


def _hwpx_dim(block: str):
    dim = _tag(block, "hp:imgDim")
    if not dim:
        return None
    return int(dim["dimwidth"]), int(dim["dimheight"])


def _hwpx_frame(clip, init, dim, natural):
    """자르기 사각형을 돌릴 기준 프레임. 알 수 없으면 None.

    imgDim이 있으면 그것이 프레임이다(OLE의 원본 크기와 같은 값). 없으면 natural = 원본 그림
    픽셀×75(래스터)로 판단한다. 자르기 없음(L=T=0, R·B ≈ natural)이면 저장된 R·B를 그대로
    프레임으로 써 반올림 차이를 없앤다. 벡터처럼 natural을 모를 때는 L=T=0이고 R:B가
    원래 크기(init) 비율과 같을 때만 자르기 없음으로 본다(오른쪽·아래만 잘린 그림과 구별)."""
    left, top, right, bottom = clip
    if dim:
        dw, dh = dim
        return dim if 0 <= left < right <= dw and 0 <= top < bottom <= dh else None
    if natural:
        nw, nh = natural
        if (left == 0 and top == 0 and abs(right - nw) <= nw * CROP_TOLERANCE
                and abs(bottom - nh) <= nh * CROP_TOLERANCE):
            return right, bottom
        if (0 <= left < right <= nw * (1 + CROP_TOLERANCE)
                and 0 <= top < bottom <= nh * (1 + CROP_TOLERANCE)):
            return nw, nh
        return None
    iw, ih = init
    if left == 0 and top == 0 and right > 0 and bottom > 0:
        if abs(right / bottom - iw / ih) <= (iw / ih) * ASPECT_TOLERANCE:
            return right, bottom
    return None


def _check_hwpx_pic(block: str, natural_of, order: tuple):
    """(각도, 사유, 프레임). 각도 0 = 회전 없음, 사유 None = 바로 세울 수 있음."""
    rotation = _tag(block, "hp:rotationInfo")
    angle = _int(rotation.get("angle", "0")) % 360
    if angle == 0:
        return 0, None, None
    pic = re.match(r"<hp:pic\b[^>]*>", block).group(0)
    if re.search(r'\bgroupLevel="0"', pic) is None:
        return angle, "묶음 개체 안의 그림", None
    if angle % 90:
        return angle, "90° 단위가 아닌 회전", None
    flip = _tag(block, "hp:flip")
    if flip.get("horizontal") == "1" or flip.get("vertical") == "1":
        return angle, "뒤집기가 함께 적용된 그림", None
    if re.search(r"<hp:effects\b[^>]*?(?<!/)>\s*<(?!/hp:effects)", block):     # 자식이 있는 effects
        return angle, "그림 효과(그림자·네온 등)가 있는 그림", None
    try:
        if any(int(v) for v in _tag(block, "hp:inMargin").values()):
            return angle, "안쪽 여백이 있는 그림", None
        org, cur, size = _tag(block, "hp:orgSz"), _tag(block, "hp:curSz"), _tag(block, "hp:sz")
        iw, ih = int(org["width"]), int(org["height"])
        cw, ch = int(cur["width"]), int(cur["height"])
        box = int(size["width"]), int(size["height"])
        clip = _hwpx_clip(block, order)
        dim = _hwpx_dim(block)
        _tag(block, "hp:offset")["x"]
    except (KeyError, ValueError):
        return angle, "그림 정보 형식이 다름", None
    if min(iw, ih, cw, ch) <= 0:
        return angle, "그림 정보 형식이 다름", None
    rendering = re.search(r"<hp:renderingInfo>(.*?)</hp:renderingInfo>", block, re.S)
    if (rendering is None or rendering.group(1).count("<hc:scaMatrix") != 1
            or rendering.group(1).count("<hc:rotMatrix") != 1):
        return angle, "렌더링 정보 형식이 다름", None
    corners = re.search(r"<hp:imgRect>(.*?)</hp:imgRect>", block, re.S)
    points = [tuple(int(v) for v in p) for p in
              re.findall(r'<hc:pt\d x="(-?\d+)" y="(-?\d+)"/>', corners.group(1))] if corners else []
    if points != [(0, 0), (iw, 0), (iw, ih), (0, ih)]:
        return angle, "그림 꼭짓점 형식이 다름", None
    want_w, want_h = (ch, cw) if angle in (90, 270) else (cw, ch)
    if (abs(box[0] - want_w) > max(2, box[0] * BOX_TOLERANCE)
            or abs(box[1] - want_h) > max(2, box[1] * BOX_TOLERANCE)):
        return angle, "개체 크기 정보가 맞지 않음", None
    frame = _hwpx_frame(clip, (iw, ih), dim, None if dim else natural_of())
    if frame is None:
        return angle, "자르기 기준을 확인할 수 없는 그림", None
    return angle, None, frame


def _natural_size(data: bytes):
    """래스터 그림의 원본 크기(픽셀×75 HWPUNIT). 벡터·알 수 없음은 None."""
    try:
        img = Image.open(io.BytesIO(data))
        if img.format in ("WMF", "EMF"):
            return None
        return img.size[0] * 75, img.size[1] * 75
    except Exception:
        return None


def plan_hwpx(hwpx_path: str) -> RotationPlan:
    """원본 HWPX를 읽기만 해서 바로 세울 그림을 찾습니다. 실패하면 빈 계획."""
    import zipfile
    plan = RotationPlan()
    try:
        zf = zipfile.ZipFile(hwpx_path)
    except Exception:
        return plan
    try:
        names = zf.namelist()
        by_lower = {n.lower(): n for n in names}
        version = by_lower.get("version.xml")
        plan.clip_order = hwpx_clip_order(
            zf.read(version).decode("utf-8", "replace") if version else "")
        for name in names:
            if name.lower().endswith(".hpf"):
                for m in HWPX_ITEM_RE.finditer(zf.read(name).decode("utf-8", "replace")):
                    href = m.group(3).lower()
                    entry = by_lower.get(href) or next(
                        (n for low, n in by_lower.items() if low.endswith("/" + href)), None)
                    if entry:
                        plan.entries[m.group(2)] = entry

        naturals: dict = {}

        def natural_of(ref):
            if ref not in naturals:
                entry = plan.entries.get(ref)
                naturals[ref] = _natural_size(zf.read(entry)) if entry else None
            return naturals[ref]

        ref_total = Counter()       # 문서 전체(채우기·머리말 포함)에서 그림을 가리키는 횟수
        fixable = []
        for name in names:
            if not name.lower().endswith(".xml"):
                continue
            text = zf.read(name).decode("utf-8", "replace")
            ref_total.update(HWPX_REF_RE.findall(text))
            if not HWPX_SECTION_RE.search(name):
                continue
            for ordinal, m in enumerate(HWPX_PIC_RE.finditer(text)):
                block = m.group(0)
                ref = HWPX_REF_RE.search(block)
                if ref is None:
                    continue
                ref = ref.group(1)
                angle, reason, frame = _check_hwpx_pic(block, lambda: natural_of(ref),
                                                       plan.clip_order)
                if angle and reason is None:
                    if ref in plan.entries:
                        fixable.append((name, ordinal, ref, angle, frame))
                    else:
                        plan.skipped.append((ref, angle, "문서에 포함되지 않은(연결된) 그림"))
                elif angle:
                    plan.skipped.append((ref, angle, reason))

        fixed_count = Counter(ref for _n, _o, ref, _a, _f in fixable)
        plan._split(fixable, lambda ref: ref_total[ref] > fixed_count[ref])
    except Exception:
        return RotationPlan()
    finally:
        zf.close()
    return plan


def add_hwpx_copies(manifest: str, plan: RotationPlan, existing: list) -> str:
    """돌린 사본을 매니페스트(content.hpf)에 추가하고 plan.new_ids/copy_names를 채웁니다.

    새 id는 기존 'imageN' 다음 번호(ZIP 안 파일 이름과도 겹치지 않게). 항목은 마지막 item 뒤에
    같은 접두사(opf:)·같은 폴더로 넣는다."""
    items = list(HWPX_ITEM_RE.finditer(manifest))
    if not items:
        raise ValueError("매니페스트에 그림 목록이 없습니다")
    taken = {m.group(2).lower() for m in items}
    taken |= {re.sub(r"\.[^./]*$", "", n.split("/")[-1]).lower() for n in existing}
    number = max([int(n) for n in re.findall(r"image(\d+)", " ".join(taken))] or [0]) + 1
    prefix = items[-1].group(1) or ""
    added = []
    for ref in sorted(plan.copies):
        source = next((m for m in items if m.group(2) == ref), None)
        folder = source.group(3).rsplit("/", 1)[0] + "/" if source and "/" in source.group(3) else ""
        for angle in plan.copies[ref]:
            while f"image{number}" in taken:
                number += 1
            new_id = f"image{number}"
            taken.add(new_id)
            plan.new_ids[(ref, angle)] = new_id
            plan.copy_names[(ref, angle)] = f"{new_id}.jpg"
            plan.entries[new_id] = f"{folder}{new_id}.jpg"
            added.append(f'<{prefix}item id="{new_id}" href="{folder}{new_id}.jpg" '
                         f'media-type="image/jpeg" isEmbeded="1"/>')
    end = items[-1].end()
    return manifest[:end] + "".join(added) + manifest[end:]


def _rewrite_hwpx_pic(block: str, angle: int, new_ref, frame, order: tuple) -> str:
    org, cur = _tag(block, "hp:orgSz"), _tag(block, "hp:curSz")
    iw, ih = int(org["width"]), int(org["height"])
    cw, ch = int(cur["width"]), int(cur["height"])
    fw, fh = frame
    clip = [max(0, v) for v in _rotate_rect(_hwpx_clip(block, order), angle, fw, fh)]
    dim = _hwpx_dim(block)
    if angle in (90, 270):
        iw, ih, cw, ch = ih, iw, ch, cw
        if dim:
            block = _set_attrs(block, "hp:imgDim", dimwidth=dim[1], dimheight=dim[0])

    block = _set_attrs(block, "hp:offset", x="0", y="0")
    block = _set_attrs(block, "hp:orgSz", width=iw, height=ih)
    block = _set_attrs(block, "hp:curSz", width=cw, height=ch)
    block = re.sub(r"<hp:rotationInfo\b[^>]*?/>", '<hp:rotationInfo angle="0"/>', block, count=1)
    block = re.sub(
        r"<hp:renderingInfo>.*?</hp:renderingInfo>",
        '<hp:renderingInfo>'
        '<hc:transMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/>'
        f'<hc:scaMatrix e1="{_fmt(cw / iw)}" e2="0" e3="0" e4="0" e5="{_fmt(ch / ih)}" e6="0"/>'
        '<hc:rotMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/>'
        '</hp:renderingInfo>', block, count=1, flags=re.S)
    block = re.sub(
        r"<hp:imgRect>.*?</hp:imgRect>",
        f'<hp:imgRect><hc:pt0 x="0" y="0"/><hc:pt1 x="{iw}" y="0"/>'
        f'<hc:pt2 x="{iw}" y="{ih}"/><hc:pt3 x="0" y="{ih}"/></hp:imgRect>',
        block, count=1, flags=re.S)
    # 읽을 때와 같은 표기로 L, T, R, B를 쓴다(형식 5.0은 속성 이름과 값 순서가 어긋나 있다).
    block = _set_attrs(block, "hp:imgClip", **dict(zip(order, clip)))
    if new_ref:
        block = HWPX_REF_RE.sub(f'binaryItemIDRef="{new_ref}"', block, count=1)
    return block


def patch_hwpx_section(text: str, targets: dict, order: tuple) -> str:
    """구역 XML 1개에서 targets = {몇 번째 그림: (ref, 각도, 새 ref | None, 프레임)}를 바로 세웁니다.
    order = 그 파일의 imgClip 표기(hwpx_clip_order)."""
    out, last, done = [], 0, 0
    for ordinal, m in enumerate(HWPX_PIC_RE.finditer(text)):
        target = targets.get(ordinal)
        if target is None:
            continue
        ref, angle, new_ref, frame = target
        found = HWPX_REF_RE.search(m.group(0))
        if found is None or found.group(1) != ref:
            raise ValueError("그림 순서가 계획과 다릅니다")
        out.append(text[last:m.start()])
        out.append(_rewrite_hwpx_pic(m.group(0), angle, new_ref, frame, order))
        last = m.end()
        done += 1
    if done != len(targets):
        raise ValueError("바로 세울 그림을 모두 찾지 못했습니다")
    out.append(text[last:])
    return "".join(out)
