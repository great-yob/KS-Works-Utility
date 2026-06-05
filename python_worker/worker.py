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
        buf=ctypes.create_string_buffer(data)
        rdc=USER32.GetDC(None)
        hemf=GDI32.SetWinMetaFileBits(len(data),buf,rdc,None)
        USER32.ReleaseDC(None,rdc)
        if not hemf: raise RuntimeError("WMF->EMF failed")
        pw,ph=_emf_size(hemf,dpi); return _render_hemf(hemf,pw,ph)

def render_emf_gdi(emf_path, dpi=300):
    hemf=GDI32.GetEnhMetaFileW(str(emf_path))
    if not hemf: raise RuntimeError("EMF load failed")
    pw,ph=_emf_size(hemf,dpi); return _render_hemf(hemf,pw,ph)

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
