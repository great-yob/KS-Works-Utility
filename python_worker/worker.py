import argparse
import sys
import json
import ctypes
import struct
import base64
from pathlib import Path
from PIL import Image, ImageChops
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────
# GDI 바인딩 (64비트 핸들 안전)
# ─────────────────────────────────────────

GDI32  = ctypes.windll.gdi32
USER32 = ctypes.windll.user32

HANDLE  = ctypes.c_void_p
UINT    = ctypes.c_uint
INT     = ctypes.c_int
BOOL    = ctypes.c_bool
DWORD   = ctypes.c_uint32
LPVOID  = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p
LONG    = ctypes.c_long

BI_RGB = 0; DIB_RGB_COLORS = 0; WHITE_BRUSH = 0; MM_ANISOTROPIC = 8
WMF_PLACEABLE_MAGIC = 0x9AC6CDD7

USER32.GetDC.restype=HANDLE;               USER32.GetDC.argtypes=[HANDLE]
USER32.ReleaseDC.restype=INT;              USER32.ReleaseDC.argtypes=[HANDLE,HANDLE]
USER32.FillRect.restype=INT;               USER32.FillRect.argtypes=[HANDLE,LPVOID,HANDLE]
GDI32.CreateCompatibleDC.restype=HANDLE;  GDI32.CreateCompatibleDC.argtypes=[HANDLE]
GDI32.CreateDIBSection.restype=HANDLE;    GDI32.CreateDIBSection.argtypes=[HANDLE,LPVOID,UINT,LPVOID,HANDLE,DWORD]
GDI32.SelectObject.restype=HANDLE;        GDI32.SelectObject.argtypes=[HANDLE,HANDLE]
GDI32.DeleteObject.restype=BOOL;          GDI32.DeleteObject.argtypes=[HANDLE]
GDI32.DeleteDC.restype=BOOL;              GDI32.DeleteDC.argtypes=[HANDLE]
GDI32.GetStockObject.restype=HANDLE;      GDI32.GetStockObject.argtypes=[INT]
GDI32.SetMapMode.restype=INT;             GDI32.SetMapMode.argtypes=[HANDLE,INT]
GDI32.SetWindowExtEx.restype=BOOL;        GDI32.SetWindowExtEx.argtypes=[HANDLE,INT,INT,LPVOID]
GDI32.SetViewportExtEx.restype=BOOL;      GDI32.SetViewportExtEx.argtypes=[HANDLE,INT,INT,LPVOID]
GDI32.SetWindowOrgEx.restype=BOOL;        GDI32.SetWindowOrgEx.argtypes=[HANDLE,INT,INT,LPVOID]
GDI32.SetViewportOrgEx.restype=BOOL;      GDI32.SetViewportOrgEx.argtypes=[HANDLE,INT,INT,LPVOID]
GDI32.SetMetaFileBitsEx.restype=HANDLE;   GDI32.SetMetaFileBitsEx.argtypes=[UINT,LPVOID]
GDI32.SetWinMetaFileBits.restype=HANDLE;  GDI32.SetWinMetaFileBits.argtypes=[UINT,LPVOID,HANDLE,LPVOID]
GDI32.PlayMetaFile.restype=BOOL;          GDI32.PlayMetaFile.argtypes=[HANDLE,HANDLE]
GDI32.DeleteMetaFile.restype=BOOL;        GDI32.DeleteMetaFile.argtypes=[HANDLE]
GDI32.GetEnhMetaFileW.restype=HANDLE;     GDI32.GetEnhMetaFileW.argtypes=[LPCWSTR]
GDI32.GetEnhMetaFileBits.restype=UINT;    GDI32.GetEnhMetaFileBits.argtypes=[HANDLE,UINT,LPVOID]
GDI32.GetEnhMetaFileHeader.restype=UINT;  GDI32.GetEnhMetaFileHeader.argtypes=[HANDLE,UINT,LPVOID]
GDI32.PlayEnhMetaFile.restype=BOOL;       GDI32.PlayEnhMetaFile.argtypes=[HANDLE,HANDLE,LPVOID]
GDI32.DeleteEnhMetaFile.restype=BOOL;     GDI32.DeleteEnhMetaFile.argtypes=[HANDLE]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_=[("biSize",DWORD),("biWidth",LONG),("biHeight",LONG),
              ("biPlanes",ctypes.c_uint16),("biBitCount",ctypes.c_uint16),
              ("biCompression",DWORD),("biSizeImage",DWORD),
              ("biXPelsPerMeter",LONG),("biYPelsPerMeter",LONG),
              ("biClrUsed",DWORD),("biClrImportant",DWORD)]
class BITMAPINFO(ctypes.Structure):
    _fields_=[("bmiHeader",BITMAPINFOHEADER),("bmiColors",DWORD*3)]
class RECT(ctypes.Structure):
    _fields_=[("left",LONG),("top",LONG),("right",LONG),("bottom",LONG)]
class SIZE_S(ctypes.Structure):
    _fields_=[("cx",LONG),("cy",LONG)]
class ENHMETAHEADER(ctypes.Structure):
    _fields_=[("iType",DWORD),("nSize",DWORD),("rclBounds",LONG*4),
              ("rclFrame",LONG*4),("dSignature",DWORD),("nVersion",DWORD),
              ("nBytes",DWORD),("nRecords",DWORD),("nHandles",ctypes.c_uint16),
              ("sReserved",ctypes.c_uint16),("nDescription",DWORD),
              ("offDescription",DWORD),("nPalEntries",DWORD),
              ("szlDevice",LONG*2),("szlMillimeters",LONG*2)]

def _dib_to_pil(bits_ptr, pw, ph):
    row_bytes=(pw*3+3)&~3; total=row_bytes*ph
    raw=bytes((ctypes.c_ubyte*total).from_address(bits_ptr.value))
    packed=(raw if row_bytes==pw*3
            else b"".join(raw[y*row_bytes:y*row_bytes+pw*3] for y in range(ph)))
    return Image.frombuffer("RGB",(pw,ph),packed,"raw","BGR",0,1)

def _make_dib(sdc,pw,ph):
    mdc=GDI32.CreateCompatibleDC(sdc)
    bmi=BITMAPINFO(); bmi.bmiHeader.biSize=ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth=pw; bmi.bmiHeader.biHeight=-ph
    bmi.bmiHeader.biPlanes=1; bmi.bmiHeader.biBitCount=24
    bmi.bmiHeader.biCompression=BI_RGB
    bp=ctypes.c_void_p()
    dib=GDI32.CreateDIBSection(sdc,ctypes.byref(bmi),DIB_RGB_COLORS,ctypes.byref(bp),None,0)
    if not dib: GDI32.DeleteDC(mdc); raise RuntimeError("CreateDIBSection failed")
    return mdc,dib,GDI32.SelectObject(mdc,dib),bp

def _fill_white(mdc,pw,ph):
    USER32.FillRect(mdc,ctypes.byref(RECT(0,0,pw,ph)),GDI32.GetStockObject(WHITE_BRUSH))

def _cleanup(mdc,dib,old,sdc):
    GDI32.SelectObject(mdc,old); GDI32.DeleteObject(dib)
    GDI32.DeleteDC(mdc); USER32.ReleaseDC(None,sdc)

def _render_hemf(hemf,pw,ph):
    sdc=USER32.GetDC(None); mdc,dib,old,bp=_make_dib(sdc,pw,ph)
    _fill_white(mdc,pw,ph)
    GDI32.PlayEnhMetaFile(mdc,hemf,ctypes.byref(RECT(0,0,pw,ph)))
    GDI32.DeleteEnhMetaFile(hemf)
    img=_dib_to_pil(bp,pw,ph); _cleanup(mdc,dib,old,sdc); return img

def _emf_size(hemf,dpi):
    hdr=ENHMETAHEADER(); GDI32.GetEnhMetaFileHeader(hemf,ctypes.sizeof(hdr),ctypes.byref(hdr))
    f=hdr.rclFrame; w=abs(f[2]-f[0])/100; h=abs(f[3]-f[1])/100
    if w>0.1 and h>0.1:
        return max(1,round(w/25.4*dpi)),max(1,round(h/25.4*dpi))
    b=hdr.rclBounds; s=dpi/96
    return max(1,round(abs(b[2]-b[0])*s)),max(1,round(abs(b[3]-b[1])*s))

_GDIPLUS = None
def _gdiplus():
    """gdiplus.dll 지연 초기화. 실패하면 None (호출측은 GDI 폴백)."""
    global _GDIPLUS
    if _GDIPLUS is None:
        try:
            gp = ctypes.WinDLL("gdiplus")
            class _StartupInput(ctypes.Structure):
                _fields_ = [("GdiplusVersion", DWORD), ("DebugEventCallback", LPVOID),
                            ("SuppressBackgroundThread", BOOL), ("SuppressExternalCodecs", BOOL)]
            tok = ctypes.c_size_t()
            if gp.GdiplusStartup(ctypes.byref(tok), ctypes.byref(_StartupInput(1, None, False, False)), None) == 0:
                # 핸들/포인터 절단 방지(특히 64비트 파이썬으로 재빌드할 경우)를 위해 명시
                P = ctypes.POINTER(ctypes.c_void_p)
                gp.GdipCreateMetafileFromEmf.restype=INT; gp.GdipCreateMetafileFromEmf.argtypes=[HANDLE,INT,P]
                gp.GdipDisposeImage.restype=INT;          gp.GdipDisposeImage.argtypes=[LPVOID]
                gp.GdipCreateFromHDC.restype=INT;         gp.GdipCreateFromHDC.argtypes=[HANDLE,P]
                gp.GdipDeleteGraphics.restype=INT;        gp.GdipDeleteGraphics.argtypes=[LPVOID]
                gp.GdipSetInterpolationMode.restype=INT;  gp.GdipSetInterpolationMode.argtypes=[LPVOID,INT]
                gp.GdipSetSmoothingMode.restype=INT;      gp.GdipSetSmoothingMode.argtypes=[LPVOID,INT]
                gp.GdipDrawImageRectI.restype=INT;        gp.GdipDrawImageRectI.argtypes=[LPVOID,LPVOID,INT,INT,INT,INT]
                _GDIPLUS = gp
            else:
                _GDIPLUS = False
        except Exception:
            _GDIPLUS = False
    return _GDIPLUS or None

def _render_hemf_gdiplus(hemf, pw, ph):
    """EMF를 GDI+로 재생한다(hemf 소유권은 호출자에 남음). 실패 시 None.

    PowerPoint류 차트의 EMF는 'EMF+ 듀얼' — 같은 그림이 GDI+ 전용 레코드(EMF+
    COMMENT)와 순수 GDI 폴백 레코드로 두 벌 들어 있다. 부드러운 드롭섀도/투명도는
    EMF+ 쪽에만 있고 GDI 폴백은 디더링(점박이)으로 근사하므로, GDI PlayEnhMetaFile로
    재생하면 그림자가 깨진다. 한글처럼 GDI+로 재생해야 EMF+ 레코드가 사용된다.
    GDI+는 16bpp DIB도 비트복제로 정확히 8비트 확장하므로(흰색 31→255) LUT가 필요없다."""
    gp = _gdiplus()
    if not gp:
        return None
    img = ctypes.c_void_p()
    if gp.GdipCreateMetafileFromEmf(hemf, False, ctypes.byref(img)) != 0 or not img:
        return None
    try:
        sdc=USER32.GetDC(None); mdc,dib,old,bp=_make_dib(sdc,pw,ph)
        _fill_white(mdc,pw,ph)
        gfx = ctypes.c_void_p()
        ok = gp.GdipCreateFromHDC(mdc, ctypes.byref(gfx)) == 0 and gfx
        if ok:
            gp.GdipSetInterpolationMode(gfx, 7)      # HighQualityBicubic
            gp.GdipSetSmoothingMode(gfx, 4)          # AntiAlias
            ok = gp.GdipDrawImageRectI(gfx, img, 0, 0, pw, ph) == 0
            gp.GdipDeleteGraphics(gfx)
        out=_dib_to_pil(bp,pw,ph); _cleanup(mdc,dib,old,sdc)
        return out if ok else None
    except Exception:
        return None
    finally:
        gp.GdipDisposeImage(img)

def _emf_16bpp_correction(emf_bytes):
    """EMF가 래스터를 16bpp(RGB555/565)로 담고 있으면 8비트 확장 보정 LUT를 돌려준다.

    GDI는 16bpp DIB의 5비트 채널을 8비트로 펼칠 때 <<3 만 하므로 최대값이
    31×8=248이 된다(흰색이 회색으로). ×255/248을 곱하면 모든 5비트 값 v가
    정확한 확장값 round(v×255/31)로 복원된다(248=31×8이라 무손실). 565의
    G(6비트)는 최대 63×4=252라 ×255/252를 쓴다. 16bpp DIB가 없으면 None."""
    n = len(emf_bytes)
    off = 0
    found = None          # None=16bpp 없음, False=555, True=565(G 6비트)
    BMI_FIELD_OFF = {76: 84, 77: 84, 80: 48, 81: 48}   # BITBLT/STRETCHBLT/SETDIBITSTODEVICE/STRETCHDIBITS
    while off + 8 <= n:
        itype, sz = struct.unpack_from("<II", emf_bytes, off)
        if sz < 8 or off + sz > n:
            break
        fo = BMI_FIELD_OFF.get(itype)
        if fo is not None and off + fo + 8 <= off + sz:
            off_bmi, cb_bmi = struct.unpack_from("<II", emf_bytes, off + fo)
            if cb_bmi >= 20 and off_bmi + 20 <= sz:
                bpp, = struct.unpack_from("<H", emf_bytes, off + off_bmi + 14)
                comp, = struct.unpack_from("<I", emf_bytes, off + off_bmi + 16)
                if bpp == 16:
                    g6 = False
                    if comp == 3 and off_bmi + 52 <= sz:   # BI_BITFIELDS: 헤더 뒤 마스크 3개
                        _, gm, _ = struct.unpack_from("<III", emf_bytes, off + off_bmi + 40)
                        g6 = (gm == 0x07E0)
                    found = g6 if found is None else (found or g6)
        if itype == 14:    # EMR_EOF
            break
        off += sz
    if found is None:
        return None
    lut5 = [min(255, (v * 255 + 124) // 248) for v in range(256)]
    lut6 = [min(255, (v * 255 + 126) // 252) for v in range(256)]
    return lut5 + (lut6 if found else lut5) + lut5     # R, G, B 채널 순


def render_wmf_gdi(wmf_path, dpi=300):
    with open(wmf_path,"rb") as f: data=f.read()
    magic=struct.unpack_from("<I",data,0)[0]
    if magic==WMF_PLACEABLE_MAGIC:
        l,t,r,b=struct.unpack_from("<hhhh",data,6)
        upi=struct.unpack_from("<H",data,14)[0] or 96
        payload=data[22:]
        wu,hu=abs(r-l),abs(b-t)
        pw,ph=max(1,round(wu/upi*dpi)),max(1,round(hu/upi*dpi))
        buf=ctypes.create_string_buffer(payload)
        hmf=GDI32.SetMetaFileBitsEx(len(payload),buf)
        if not hmf: raise RuntimeError("SetMetaFileBitsEx failed")
        sdc=USER32.GetDC(None); mdc,dib,old,bp=_make_dib(sdc,pw,ph)
        _fill_white(mdc,pw,ph)
        sz=SIZE_S()
        GDI32.SetMapMode(mdc,MM_ANISOTROPIC)
        GDI32.SetWindowExtEx(mdc,wu,hu,ctypes.byref(sz))
        GDI32.SetViewportExtEx(mdc,pw,ph,ctypes.byref(sz))
        GDI32.SetWindowOrgEx(mdc,l,t,None); GDI32.SetViewportOrgEx(mdc,0,0,None)
        GDI32.PlayMetaFile(mdc,hmf); GDI32.DeleteMetaFile(hmf)
        img=_dib_to_pil(bp,pw,ph); _cleanup(mdc,dib,old,sdc); return img
    else:
        # 표준 WMF: SetWinMetaFileBits로 EMF화해 재생한다(내장 EMF가 있으면 그것이
        # 복원됨 — 글자/곡선이 매끈한 벡터 패스로 그려져 품질이 가장 좋다. 원시 WMF
        # 레코드의 글자는 16비트 좌표로 양자화된 다각형이라 확대 시 울퉁불퉁해진다).
        # 단, PowerPoint류의 내장 EMF는 래스터를 16bpp(RGB555/565)로 담는 경우가 많고
        # GDI가 5비트 채널을 <<3 으로만 펼쳐 흰색(31)이 248 회색이 되므로, EMF 안에
        # 16bpp DIB가 있으면 무손실 LUT 보정(×255/248)으로 정확한 색을 복원한다.
        buf=ctypes.create_string_buffer(data)
        rdc=USER32.GetDC(None)
        hemf=GDI32.SetWinMetaFileBits(len(data),buf,rdc,None)
        USER32.ReleaseDC(None,rdc)
        if not hemf: raise RuntimeError("WMF->EMF failed")
        pw,ph=_emf_size(hemf,dpi)
        img=_render_hemf_gdiplus(hemf,pw,ph)          # EMF+ 그림자/16bpp 색 모두 정확
        if img is not None:
            GDI32.DeleteEnhMetaFile(hemf)
            return img
        cb=GDI32.GetEnhMetaFileBits(hemf,0,None)      # GDI 폴백 (+16bpp LUT 보정)
        lut=None
        if cb:
            ebuf=ctypes.create_string_buffer(cb)
            if GDI32.GetEnhMetaFileBits(hemf,cb,ebuf):
                lut=_emf_16bpp_correction(ebuf.raw)
        img=_render_hemf(hemf,pw,ph)
        return img.point(lut) if lut else img

def render_wmf_gdi_custom(wmf_path, pw, ph):
    with open(wmf_path,"rb") as f: data=f.read()
    magic=struct.unpack_from("<I",data,0)[0]
    if magic==WMF_PLACEABLE_MAGIC:
        l,t,r,b=struct.unpack_from("<hhhh",data,6)
        wu,hu=abs(r-l),abs(b-t)
        payload=data[22:]
    else:
        l,t,r,b=0,0,0,0
        wu,hu=0,0
        payload=data
        
    buf=ctypes.create_string_buffer(payload)
    if wu > 0 and hu > 0:
        hmf=GDI32.SetMetaFileBitsEx(len(payload),buf)
        if not hmf: raise RuntimeError("SetMetaFileBitsEx failed")
        sdc=USER32.GetDC(None); mdc,dib,old,bp=_make_dib(sdc,pw,ph)
        _fill_white(mdc,pw,ph)
        sz=SIZE_S()
        GDI32.SetMapMode(mdc,MM_ANISOTROPIC)
        GDI32.SetWindowExtEx(mdc,wu,hu,ctypes.byref(sz))
        GDI32.SetViewportExtEx(mdc,pw,ph,ctypes.byref(sz))
        GDI32.SetWindowOrgEx(mdc,l,t,None); GDI32.SetViewportOrgEx(mdc,0,0,None)
        GDI32.PlayMetaFile(mdc,hmf); GDI32.DeleteMetaFile(hmf)
        img=_dib_to_pil(bp,pw,ph); _cleanup(mdc,dib,old,sdc); return img
    else:
        rdc=USER32.GetDC(None)
        hemf=GDI32.SetWinMetaFileBits(len(payload),buf,rdc,None)
        USER32.ReleaseDC(None,rdc)
        if not hemf: raise RuntimeError("WMF->EMF failed")
        sdc=USER32.GetDC(None); mdc,dib,old,bp=_make_dib(sdc,pw,ph)
        _fill_white(mdc,pw,ph)
        GDI32.PlayEnhMetaFile(mdc,hemf,ctypes.byref(RECT(0,0,pw,ph)))
        GDI32.DeleteEnhMetaFile(hemf)
        img=_dib_to_pil(bp,pw,ph); _cleanup(mdc,dib,old,sdc); return img

def render_emf_gdi_custom(emf_path, pw, ph):
    hemf=GDI32.GetEnhMetaFileW(str(emf_path))
    if not hemf: raise RuntimeError("EMF load failed")
    sdc=USER32.GetDC(None); mdc,dib,old,bp=_make_dib(sdc,pw,ph)
    _fill_white(mdc,pw,ph)
    GDI32.PlayEnhMetaFile(mdc,hemf,ctypes.byref(RECT(0,0,pw,ph)))
    GDI32.DeleteEnhMetaFile(hemf)
    img=_dib_to_pil(bp,pw,ph); _cleanup(mdc,dib,old,sdc); return img

def render_emf_gdi(emf_path, dpi=300):
    hemf=GDI32.GetEnhMetaFileW(str(emf_path))
    if not hemf: raise RuntimeError("EMF load failed")
    pw,ph=_emf_size(hemf,dpi)
    img=_render_hemf_gdiplus(hemf,pw,ph)              # 한글과 동일하게 GDI+ 우선
    if img is not None:
        GDI32.DeleteEnhMetaFile(hemf)
        return img
    return _render_hemf(hemf,pw,ph)

def _wmf_window(data):
    """WMF가 선언한 '논리 좌표계(window)'를 추출한다.
    반환: (placeable여부, (org_x, org_y), (ext_w, ext_h) | None, 재생용 payload)

    배치형(placeable)은 22바이트 헤더의 bounding-box가 곧 전체 캔버스다.
    표준 WMF는 본문 레코드의 META_SETWINDOWORG/EXT 가 전체 캔버스를 정의한다.
    한글이 그림을 그릴 때 쓰는 기준이 바로 이 window(여백 포함)이므로,
    이 값으로 렌더링하면 '그림 자르기'가 잘라낼 여백까지 그대로 재현된다."""
    if len(data) < 4:
        return False, (0,0), None, data
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic == WMF_PLACEABLE_MAGIC:
        l,t,r,b = struct.unpack_from("<hhhh", data, 6)
        return True, (l,t), (abs(r-l), abs(b-t)), data[22:]
    # 표준 WMF: 18바이트 헤더 다음부터 레코드. META_SETWINDOWEXT(0x020C) / ORG(0x020B) 추적.
    worg, wext = (0,0), None
    off, guard = 18, 0
    while off + 6 <= len(data) and guard < 500000:
        size = struct.unpack_from("<I", data, off)[0]   # 레코드 크기(워드 단위)
        if size < 3:
            break                                       # META_EOF(size=3) 또는 손상
        func = struct.unpack_from("<H", data, off+4)[0]
        params = data[off+6 : off+size*2]
        if func == 0x020C and len(params) >= 4:         # META_SETWINDOWEXT: y(height), x(width)
            yext, xext = struct.unpack_from("<hh", params, 0)
            if xext and yext:
                wext = (abs(xext), abs(yext))
        elif func == 0x020B and len(params) >= 4:       # META_SETWINDOWORG: y, x
            yorg, xorg = struct.unpack_from("<hh", params, 0)
            worg = (xorg, yorg)
        if func == 0x0000:                              # META_EOF
            break
        off += size*2; guard += 1
    return False, worg, wext, data

def _nonwhite_bbox(img):
    """거의 흰색(>=245)이 아닌 픽셀의 경계 상자. 완전 백지면 None."""
    mask = img.convert("L").point(lambda v: 255 if v < 245 else 0)
    return mask.getbbox()

def render_wmf_gdi_fullframe(wmf_path, dpi=300):
    """WMF를 '선언된 전체 캔버스(window extent)' 기준으로 렌더링한다 — 단, 실제로
    숨은 여백이 있을 때만. 그 외에는 기존 render_wmf_gdi()(tight)를 그대로 쓴다.

    [배경] render_wmf_gdi() 는 표준 WMF를 GDI가 계산한 '실제 그려진 영역(rclBounds)'에
    타이트하게 맞춰 렌더링하므로, 차트 오른쪽 등 빈 여백이 사라진다. 한글은 그 여백까지
    포함한 원래 캔버스(window extent)를 기준으로 '그림 자르기(Crop)'를 적용하므로,
    타이트 JPG를 다시 끼워 넣으면 자르기가 실제 차트를 베어버린다. window extent 전체를
    렌더링해 여백을 보존하면, 한글의 자르기가 (차트가 아니라) 그 여백만 정확히 잘라낸다.

    [안전장치]
      - 배치형 WMF: render_wmf_gdi() 가 이미 bounding-box(전체 캔버스)로 렌더 → 위임.
      - window extent의 종횡비 ≈ tight 렌더의 종횡비 → 숨은 여백 없음 → tight 사용
        (예: window/viewport를 1:1로 잡고 비트맵만 박은 WMF. 전체 렌더 시 오히려 깨짐).
      - 전체 렌더 결과 내용이 캔버스 구석에 갇히면(자체 viewport 설정 등 매핑 실패) → tight 폴백."""
    tight = render_wmf_gdi(wmf_path, dpi)               # 기존과 동일한 안전 렌더(기준선)
    with open(wmf_path, "rb") as f:
        data = f.read()
    placeable, worg, wext, payload = _wmf_window(data)
    if placeable or not wext or wext[0] <= 0 or wext[1] <= 0:
        return tight
    wu, hu = wext
    win_ar = wu / hu
    tight_ar = tight.size[0] / max(1, tight.size[1])
    if abs(win_ar - tight_ar) <= tight_ar * 0.05:       # 숨은 여백 없음 → tight가 이미 정확
        return tight
    # 여백 존재 → 선언된 window 전체를 렌더하여 여백 보존
    # 해상도: 긴 변이 ~dpi*20px(=300일 때 6000)이 되도록. 큰 캔버스는 축소도 허용하고
    # (window extent가 24000처럼 큰 경우 그대로 렌더하면 1억 픽셀이 되어 메모리 폭주),
    # 작은 캔버스는 최대 8배까지만 확대한다. 결과적으로 긴 변은 항상 dpi*20px 이하.
    scale = min(8.0, (dpi*20) / max(wu, hu))
    pw, ph = max(1, round(wu*scale)), max(1, round(hu*scale))
    buf = ctypes.create_string_buffer(payload)
    hmf = GDI32.SetMetaFileBitsEx(len(payload), buf)
    if not hmf:
        return tight
    sdc=USER32.GetDC(None); mdc,dib,old,bp=_make_dib(sdc,pw,ph)
    _fill_white(mdc,pw,ph)
    sz=SIZE_S()
    GDI32.SetMapMode(mdc,MM_ANISOTROPIC)
    GDI32.SetWindowExtEx(mdc,wu,hu,ctypes.byref(sz))
    GDI32.SetViewportExtEx(mdc,pw,ph,ctypes.byref(sz))
    GDI32.SetWindowOrgEx(mdc,worg[0],worg[1],None); GDI32.SetViewportOrgEx(mdc,0,0,None)
    GDI32.PlayMetaFile(mdc,hmf); GDI32.DeleteMetaFile(hmf)
    full=_dib_to_pil(bp,pw,ph); _cleanup(mdc,dib,old,sdc)
    bb = _nonwhite_bbox(full)                           # 매핑 실패 감지
    if bb is None or ((bb[2]-bb[0]) < pw*0.5 and (bb[3]-bb[1]) < ph*0.5):
        return tight                                    # 내용이 구석에 갇힘 → tight 폴백
    return full

def render_wmf_window_px(wmf_path, pw, ph):
    """WMF의 '선언된 전체 캔버스(window extent)'를 정확히 pw×ph 픽셀 박스에 직접 렌더링한다.

    render_wmf_gdi_fullframe() 와 동일한 window→viewport 매핑(여백 보존)을 쓰되, 자동
    해상도 대신 호출자가 지정한 픽셀 크기로 그린다. '사이즈 조정'이 큰 래스터를 다운샘플
    (LANCZOS)하면 가는 벡터 선이 흰 배경과 평균돼 회색으로 흐려지는데(검정 0→197), 작은
    크기를 GDI로 '다시 렌더'하면 선이 최소 1px 검정으로 또렷하게 남는다(검정 0 유지).

    반환: PIL.Image, 또는 안전하게 렌더할 수 없으면 None
      - window extent 불명 → None (호출자가 축소 포기, 원본 렌더 유지)
      - 배치형(placeable): bounding-box 전체가 캔버스이므로 render_wmf_gdi_custom 위임
      - 전체-window 렌더 결과가 구석에 갇히면(매핑 실패) → None (폴백)"""
    with open(wmf_path, "rb") as f:
        data = f.read()
    placeable, worg, wext, payload = _wmf_window(data)
    if placeable:
        try:
            return render_wmf_gdi_custom(wmf_path, pw, ph)
        except Exception:
            return None
    if not wext or wext[0] <= 0 or wext[1] <= 0:
        return None
    wu, hu = wext
    # 본 렌더(render_wmf_gdi)와 동일 품질을 작은 크기에서도 유지하기 위해 GDI+를
    # 먼저 시도한다(EMF+ 그림자, 매끈한 글자, 정확한 16bpp 색 — 원시 레코드 재생은
    # 차트류에서 점박이 그림자/각진 글자가 된다). 단 변환 EMF의 frame 종횡비가
    # 목표 박스와 다르면(숨은 여백 보존이 목적인 호출) 원시 window 재생으로 넘어간다.
    try:
        ebuf = ctypes.create_string_buffer(data)
        rdc = USER32.GetDC(None)
        hemf = GDI32.SetWinMetaFileBits(len(data), ebuf, rdc, None)
        USER32.ReleaseDC(None, rdc)
        if hemf:
            ew, eh = _emf_size(hemf, 96)
            if abs(ew / eh - pw / ph) <= (ew / eh) * 0.05:
                img = _render_hemf_gdiplus(hemf, pw, ph)
                if img is not None:
                    GDI32.DeleteEnhMetaFile(hemf)
                    return img
            GDI32.DeleteEnhMetaFile(hemf)
    except Exception:
        pass
    buf = ctypes.create_string_buffer(payload)
    hmf = GDI32.SetMetaFileBitsEx(len(payload), buf)
    if not hmf:
        return None
    sdc=USER32.GetDC(None); mdc,dib,old,bp=_make_dib(sdc,pw,ph)
    _fill_white(mdc,pw,ph)
    sz=SIZE_S()
    GDI32.SetMapMode(mdc,MM_ANISOTROPIC)
    GDI32.SetWindowExtEx(mdc,wu,hu,ctypes.byref(sz))
    GDI32.SetViewportExtEx(mdc,pw,ph,ctypes.byref(sz))
    GDI32.SetWindowOrgEx(mdc,worg[0],worg[1],None); GDI32.SetViewportOrgEx(mdc,0,0,None)
    GDI32.PlayMetaFile(mdc,hmf); GDI32.DeleteMetaFile(hmf)
    img=_dib_to_pil(bp,pw,ph); _cleanup(mdc,dib,old,sdc)
    bb = _nonwhite_bbox(img)
    if bb is None or ((bb[2]-bb[0]) < pw*0.5 and (bb[3]-bb[1]) < ph*0.5):
        return None
    return img

def trim_whitespace(img):
    bg=Image.new(img.mode,img.size,img.getpixel((0,0)))
    diff=ImageChops.difference(img,bg); bbox=diff.getbbox()
    return img.crop(bbox) if bbox else img

def to_rgb(img):
    if img.mode=="P": img=img.convert("RGBA")
    if img.mode in ("RGBA","LA","RGBa"):
        bg=Image.new("RGB",img.size,(255,255,255)); bg.paste(img,mask=img.split()[-1]); return bg
    if img.mode=="CMYK": return img.convert("RGB")
    if img.mode in ("I","I;16","I;16B"): return img.point(lambda x:x>>8).convert("RGB")
    return img.convert("RGB")

def convert_single(file_path, output_dir, dpi, uppercase_ext):
    file = Path(file_path)
    ext = file.suffix.lower()
    try:
        if ext == ".wmf":
            img = render_wmf_gdi(file, dpi)
            img = trim_whitespace(img)
        elif ext == ".emf":
            img = render_emf_gdi(file, dpi)
            img = trim_whitespace(img)
        else:
            img = Image.open(file)
            
        if img.mode != "RGB":
            img = to_rgb(img)
            
        out_ext = ".JPEG" if uppercase_ext else ".jpg"
        out_path = output_dir / (file.stem + out_ext)
        
        # Ensure output directory exists
        output_dir.mkdir(parents=True, exist_ok=True)
        
        img.save(out_path, "JPEG", quality=95, dpi=(dpi, dpi))
        return {"event": "progress", "success": True, "inputFile": str(file), "outputFile": str(out_path)}
    except Exception as e:
        return {"event": "progress", "success": False, "inputFile": str(file), "error": str(e)}

def main():
    parser = argparse.ArgumentParser(description="Image Converter Worker")
    parser.add_argument("--input", required=False, help="Input file path")
    parser.add_argument("--input-json", required=False, help="Path to JSON file containing array of input paths")
    parser.add_argument("--output", required=True, help="Output directory path")
    parser.add_argument("--dpi", type=int, default=300, help="Output DPI")
    parser.add_argument("--uppercase", action="store_true", help="Use .JPEG instead of .jpg")
    
    args = parser.parse_args()
    
    output_dir = Path(args.output)
    dpi = args.dpi
    uppercase_ext = args.uppercase
    
    paths = []
    if args.input_json:
        with open(args.input_json, "r", encoding="utf-8") as f:
            paths = json.load(f)
    elif args.input:
        paths = [args.input]
    else:
        print(json.dumps({"error": "Either --input or --input-json must be provided"}))
        sys.exit(1)
        
    wmf_files = [p for p in paths if Path(p).suffix.lower() in (".wmf", ".emf")]
    normal_files = [p for p in paths if Path(p).suffix.lower() not in (".wmf", ".emf")]
    
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(convert_single, p, output_dir, dpi, uppercase_ext): p for p in normal_files}
        for fut in as_completed(futs):
            print(json.dumps(fut.result()), flush=True)
            
    # WMF/EMF use GDI which should be on main thread (or single threaded)
    for p in wmf_files:
        res = convert_single(p, output_dir, dpi, uppercase_ext)
        print(json.dumps(res), flush=True)

    print(json.dumps({"event": "done"}), flush=True)
    sys.exit(0)

if __name__ == "__main__":
    main()
