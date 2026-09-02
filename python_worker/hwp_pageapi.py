"""한글(HWP Automation)에게 '이 위치가 몇 쪽인가'를 직접 물어봅니다.

쪽 번호를 파일에서 추정하는 `hwp_pagemap`은 표가 여러 쪽에 걸치면 어긋납니다(한글은 표가
어느 행에서 쪽을 넘겼는지 저장하지 않음). 한글이 설치돼 있으면 한글에게 물어보는 쪽이
정확하므로, 여기서 그 경로를 제공하고 실패하면 조용히 추정으로 되돌아갑니다.

핵심은 `hwp.SetPos(0, 문단번호, 글자위치)` + `hwp.KeyIndicator()[3]` 입니다. KeyIndicator가
돌려주는 쪽은 **문서에 인쇄되는 번호**라 '새 번호로 시작'까지 반영돼 있습니다.

실측(2026-09, `D:\\작업방`): 회의자료(본문이 통째로 표) 그림 8장이 5·31·135~140으로 한글
'문서 정보 → 그림 정보'와 완전히 일치했고, 171MB 교재도 열기 포함 2.1초였다. 별도
프로세스에서 Dispatch하면 독립 인스턴스가 떠서, 사용자가 열어 둔 한글 세션은 건드리지
않는다(실측 확인).

주의:
  * `Open()`은 한글 보안 모듈이 등록돼 있지 않으면 **승인 대화상자를 띄우고 멈춥니다.**
    그래서 앱이 동봉한 FilePathCheckerModule.dll을 사용자 폴더에 두고 레지스트리에
    등록한 뒤(`ensure_security_module`) 호출합니다.
  * 그래도 멈출 수 있으므로(다른 버전·정책) 별도 스레드에서 제한 시간을 두고 실행합니다.
"""

import os
import shutil
import sys
import threading

# 한글이 응답하지 않을 때 포기하고 추정으로 돌아가기까지의 시간(초).
# 실측(6개 문서, 8~269개 앵커)에서 가장 느린 경우가 2.5초라 넉넉한 값이다.
AUTOMATION_TIMEOUT = 30

# 빠른 경로로 훑을 컨트롤 — 그림·그리기 개체만. 표(tbl)까지 넣으면 큰 표가 많은 교재에서
# 커서 이동이 급격히 느려진다(실측: 269개 앵커에 45초 초과 → gso만 훑으면 3초).
# 표 셀 배경처럼 표에 매달린 앵커는 아래 SetPos 폴백이 처리한다.
ANCHORED_CTRL_IDS = ("gso ", "gso")

SECURITY_MODULE_NAME = "FilePathCheckerModule"
SECURITY_MODULE_DLL = "FilePathCheckerModule.dll"
SECURITY_REGISTRY_KEY = r"Software\HNC\HwpAutomation\Modules"


# ─────────────────────────────────────────
# 보안 모듈 준비
# ─────────────────────────────────────────

def _bundled_dll_path():
    """앱이 동봉한 보안 모듈 DLL을 찾습니다. 개발/설치 두 배치를 모두 봅니다."""
    here = os.path.dirname(os.path.abspath(__file__))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    candidates = [
        # 설치본: resources\hwp_worker\hwp_worker.exe 옆의 resources\hwp_automation\
        os.path.join(os.path.dirname(exe_dir), "hwp_automation", SECURITY_MODULE_DLL),
        # 개발: 저장소 루트의 resources\hwp_automation\
        os.path.join(os.path.dirname(here), "resources", "hwp_automation", SECURITY_MODULE_DLL),
        os.path.join(here, SECURITY_MODULE_DLL),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _install_dir():
    """레지스트리에 등록할 안정적인 위치(앱을 지워도 경로가 깨지지 않도록 사용자 폴더)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "KS-Works-Utility")


def ensure_security_module() -> bool:
    """보안 모듈 DLL을 사용자 폴더에 두고 레지스트리에 등록합니다.

    이게 없으면 한글이 문서를 열 때 '보안 승인' 대화상자를 띄우고 멈춥니다.
    이미 등록돼 있으면(다른 도구가 등록해 둔 경우 포함) 그대로 둡니다."""
    try:
        import winreg
    except Exception:
        return False

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SECURITY_REGISTRY_KEY) as key:
            registered, _ = winreg.QueryValueEx(key, SECURITY_MODULE_NAME)
            if registered and os.path.isfile(registered):
                return True
    except Exception:
        pass

    source = _bundled_dll_path()
    if not source:
        return False
    target = os.path.join(_install_dir(), SECURITY_MODULE_DLL)
    try:
        os.makedirs(_install_dir(), exist_ok=True)
        if not os.path.isfile(target):
            shutil.copy2(source, target)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, SECURITY_REGISTRY_KEY) as key:
            winreg.SetValueEx(key, SECURITY_MODULE_NAME, 0, winreg.REG_SZ, target)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────
# 쪽 번호 조회
# ─────────────────────────────────────────

def _hangul_pids() -> set:
    """실행 중인 한글(Hwp.exe) 프로세스 ID 목록. 실패하면 빈 집합."""
    import ctypes
    import ctypes.wintypes as wintypes

    class ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]

    pids = set()
    try:
        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
        if snapshot == -1:
            return pids
        try:
            entry = ENTRY()
            entry.dwSize = ctypes.sizeof(ENTRY)
            ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.decode("mbcs", "ignore").lower() == "hwp.exe":
                    pids.add(int(entry.th32ProcessID))
                ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        pass
    return pids


def _kill(pid: int):
    """제한 시간을 넘겨 버려둔 한글 인스턴스를 정리합니다(그대로 두면 다음 실행이 느려진다)."""
    import ctypes
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)   # PROCESS_TERMINATE
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 0)
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass


def _ask_hangul(path: str, anchors: list, result: dict):
    """한글을 띄워 각 (문단번호, 글자위치)의 인쇄 쪽 번호를 받아옵니다."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    hwp = None
    try:
        before_pids = _hangul_pids()
        hwp = win32com.client.Dispatch("HWPFrame.HwpObject")
        # 방금 뜬 인스턴스의 PID를 적어 둔다. 시간 초과로 이 스레드를 버릴 때
        # 그 프로세스만 정리하기 위한 것이다(사용자가 쓰던 한글은 건드리지 않는다).
        new_pids = _hangul_pids() - before_pids
        if len(new_pids) == 1:
            result["pid"] = new_pids.pop()
        try:
            hwp.RegisterModule("FilePathCheckDLL", SECURITY_MODULE_NAME)
        except Exception:
            pass                                   # 등록 실패해도 열리는 환경이 있다
        try:
            hwp.XHwpWindows.Item(0).Visible = False
        except Exception:
            pass
        if not hwp.Open(path, "", "forceopen:true"):
            return

        # 커서를 옮기는 방법이 둘인데 속도가 크게 다르다. 개체(컨트롤)의 앵커로 옮기는
        # SetPosBySet은 즉시 끝나지만, 좌표를 직접 주는 SetPos는 큰 문서에서 한 번에
        # 100ms씩 걸린다(144MB 교재: 269회 = 29초). 그래서 컨트롤을 훑어 필요한 앵커를
        # 먼저 채우고, 거기서 못 찾은 것(표 셀 배경 등)만 SetPos로 마저 묻는다.
        needed = set(anchors)
        found = {}
        ctrl = hwp.HeadCtrl
        while ctrl is not None and len(found) < len(needed):
            try:
                if ctrl.CtrlID in ANCHORED_CTRL_IDS:
                    hwp.SetPosBySet(ctrl.GetAnchorPos(0))
                    listid, para, pos = hwp.GetPos()           # 앵커 좌표는 한 번에 읽는다
                    key = (int(para), int(pos))
                    if int(listid) == 0 and key in needed and key not in found:
                        found[key] = int(hwp.KeyIndicator()[3])
            except Exception:
                pass
            ctrl = ctrl.Next

        pages = []
        for anchor in anchors:
            if anchor not in found:
                hwp.SetPos(0, anchor[0], anchor[1])
                found[anchor] = int(hwp.KeyIndicator()[3])
            pages.append(found[anchor])
        result["pages"] = pages
        result["total"] = int(hwp.PageCount)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        if hwp is not None:
            try:
                hwp.Clear(1)
                hwp.Quit()          # 내가 띄운 인스턴스만 닫힌다(사용자 세션과 분리됨)
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def resolve_pages(path: str, anchors: list):
    """[(문단번호, 글자위치)] → [인쇄 쪽 번호]. 한글이 없거나 실패하면 None.

    한글이 대화상자 때문에 멈출 수 있으므로 별도 스레드에서 제한 시간을 두고 기다립니다.
    시간을 넘기면 그 스레드는 버리고(데몬) 호출자는 추정 로직으로 돌아갑니다."""
    if not anchors:
        return None
    ensure_security_module()
    result: dict = {}
    # 한글은 상대 경로를 열지 못한다(작업 폴더가 워커와 다르다).
    target = os.path.abspath(path)
    worker = threading.Thread(target=_ask_hangul, args=(target, anchors, result), daemon=True)
    worker.start()
    worker.join(AUTOMATION_TIMEOUT)
    pages = result.get("pages")
    if not pages or len(pages) != len(anchors):
        if worker.is_alive() and result.get("pid"):
            _kill(result["pid"])          # 버려둔 인스턴스가 남으면 다음 실행이 느려진다
        return None
    return pages
