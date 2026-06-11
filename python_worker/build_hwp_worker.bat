@echo off
REM ──────────────────────────────────────────
REM  HWP Worker 빌드 스크립트
REM  PyInstaller로 hwp_worker.py → hwp_worker.exe 빌드 후
REM  resources/hwp_worker/ 에 복사합니다.
REM ──────────────────────────────────────────

cd /d "%~dp0"

echo [1/3] PyInstaller로 hwp_worker.exe 빌드 중...
REM pythoncom/pywintypes(=COM, StgOpenStorage/EnumElements)와 sibling 모듈 worker는
REM 함수 안에서 지연 import되어 PyInstaller 정적 분석이 놓칠 수 있으므로 명시한다.
REM 특히 win32timezone: IStorage 열거 시 enum.Next()가 STATSTG의 시간 필드를 변환하며
REM win32timezone를 지연 import하는데, 이게 빠지면 빌드는 되지만 런타임에 열거가 실패해
REM 스캔이 0개로 나오고 변환도 0개가 된다(앱에서 "이미지 0개"의 실제 원인이었음).
pyinstaller --onefile --name hwp_worker --distpath dist ^
    --hidden-import pythoncom ^
    --hidden-import pywintypes ^
    --hidden-import win32timezone ^
    --hidden-import win32com ^
    --hidden-import win32com.client ^
    --hidden-import worker ^
    hwp_worker.py

if errorlevel 1 (
    echo [오류] PyInstaller 빌드 실패
    exit /b 1
)

echo [2/3] resources/hwp_worker/ 폴더 생성...
if not exist "..\resources\hwp_worker" mkdir "..\resources\hwp_worker"

echo [3/3] exe 복사 중...
copy /Y "dist\hwp_worker.exe" "..\resources\hwp_worker\hwp_worker.exe"

echo [완료] resources\hwp_worker\hwp_worker.exe 준비됨
