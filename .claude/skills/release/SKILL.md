---
name: release
description: KS-Works-Utility 새 버전 배포. 사용자가 "릴리스", "배포", "release", "1.2.3으로 배포" 등으로 배포를 요청할 때 사용한다. 워커 exe 최신화 확인 → 버전 올리기 → 검사 → 커밋·태그·푸시 → GitHub Releases 게시 → 검증 → 이전 릴리스 전부 삭제(최신 하나만 유지)까지 한 번에 끝낸다.
---

# 릴리스 (KS-Works-Utility)

수정 세션이 끝난 뒤 "릴리스해줘"라고 하면 이 절차를 처음부터 끝까지 수행한다. 사용자가 배포를
요청한 것이므로 푸시·게시·이전 릴리스 삭제는 따로 묻지 않는다. 단, 아래 **멈춤 조건**에 걸리면
그 자리에서 멈추고 보고한다.

명령은 전부 **Bash 도구**로 실행한다(PowerShell은 커밋 메시지의 큰따옴표에서 깨진다).
작업 디렉터리는 저장소 루트. 빌드 로그 같은 임시 파일은 저장소 밖 scratchpad에 둔다
(아래 `$SCRATCH` = 이 세션의 scratchpad 경로).

## 0. 사전 확인

```bash
git status -sb            # main 이고 origin/main 보다 뒤처지지 않았는지
gh auth status            # great-yob 로그인 + 'repo' scope
gh release list --limit 100
```

- **멈춤 조건**: main이 아님 / origin보다 behind / gh 미로그인. (gh가 안 되면 대안은
  사용자가 직접 `.\release.local.ps1` 실행 — 토큰이 든 gitignore 파일, 내용 출력 금지.)

## 1. 버전 정하기

- 현재 값: `package.json`의 `"version"`. 사용자가 버전을 말했으면 그 값, 아니면 patch +1.
- 새 태그가 비어 있는지: `git ls-remote --tags origin vX.Y.Z` → 출력이 없어야 한다.
- `package.json`의 `"version"`만 고친다. `package-lock.json`은 1.0.0으로 방치돼 있고 건드리지 않는다
  (사이드바 버전은 Vite `__APP_VERSION__`이 package.json에서 읽는다).

## 2. 무엇이 바뀌었나

```bash
LAST=$(git describe --tags --abbrev=0)
git log --oneline $LAST..HEAD
git status --short
git diff --stat $LAST
```

이 목록이 커밋 범위다. `desktop.ini` 같은 무관한 미추적 파일은 **커밋하지 않는다**
(`git add -A` 금지, 파일을 명시해서 stage).

## 3. 워커 exe가 소스와 맞는지 (가장 자주 빠뜨리는 단계)

앱은 dev에서도 `resources/` 아래 exe를 쓴다. 파이썬만 고치고 exe를 다시 안 만들면 **배포본엔
수정이 들어가지 않는다.** `$LAST` 이후 바뀐 파이썬 파일로 필요한 exe를 정한다:

| 바뀐 소스 | 다시 만들 exe |
|---|---|
| `python_worker/worker.py` | `image_worker.exe` **와** `hwp_worker.exe` (hwp_worker가 worker를 번들) |
| `hwp_worker.py`, `hwp_pagemap.py`, `hwp_pageapi.py`, `hwp_rotation.py` | `hwp_worker.exe` |

해당 exe가 변경 목록(`git diff --stat $LAST`)에 없거나 소스보다 mtime이 오래됐으면 다시 빌드한다:

```bash
cd python_worker
python -m PyInstaller --noconfirm --onefile --console --name image_worker --distpath dist worker.py > "$SCRATCH/build_img.log" 2>&1
python -m PyInstaller --onefile --name hwp_worker --distpath dist --noconfirm \
  --hidden-import pythoncom --hidden-import pywintypes --hidden-import win32timezone \
  --hidden-import win32com --hidden-import win32com.client --hidden-import worker \
  --hidden-import hwp_pagemap --hidden-import hwp_pageapi --hidden-import hwp_rotation \
  hwp_worker.py > "$SCRATCH/build_hwp.log" 2>&1
cp dist/image_worker.exe ../resources/image_worker/image_worker.exe
cp dist/hwp_worker.exe   ../resources/hwp_worker/hwp_worker.exe
```

새로 만든 hwp_worker.exe는 실제 문서 하나로 스모크 테스트한다(`D:\작업방` 아래 작은 .hwp):
`resources/hwp_worker/hwp_worker.exe --scan --input <문서>` → 그림 목록이 **0개가 아니어야** 한다.
0개면 hidden import(특히 `win32timezone`) 누락이다 — **멈춤 조건**.

## 4. 정적 검사

```bash
npm run lint
```

실패하면 **멈춤 조건**.

## 5. 커밋 · 태그 · 푸시

기존 스타일대로 버전당 커밋 하나: 제목 `vX.Y.Z: <한 줄 요약>`, 본문엔 무엇을 왜 바꿨는지와
실측 결과(`git log -3`로 형식 확인). 본문에 큰따옴표(`"`)를 쓰지 않는다. 마지막 줄은 이 세션의
attribution 지시(Co-Authored-By)를 따른다.

```bash
git add package.json <바뀐 소스> <다시 만든 exe> ...
git commit -F - <<'EOF'
vX.Y.Z: ...

...
EOF
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z
```

## 6. 게시

```bash
GH_TOKEN="$(gh auth token)" npm run release      # timeout 600000, 수 분 걸린다
```

- `npm run release` = 프런트·서버 빌드 + `electron-builder --publish always`.
  `releaseType: "release"`라 draft 없이 바로 공개되고, 산출물은 `release_build/`
  (`release/`는 OS 파일 잠금이 남아 있어 쓰지 않는다).
- 401/403 → 토큰 권한. `existing type not compatible` → 같은 태그 릴리스가 이미 있음.
  둘 다 **멈춤 조건**.

## 7. 검증 (통과해야 8단계로 간다)

```bash
gh release view vX.Y.Z --json isDraft,isPrerelease,assets --jq '{isDraft,isPrerelease,assets:[.assets[].name]}'
curl -s https://api.github.com/repos/great-yob/KS-Works-Utility/releases/latest | python -c "import json,sys;print(json.load(sys.stdin)['tag_name'])"
curl -sL https://github.com/great-yob/KS-Works-Utility/releases/latest/download/latest.yml | head -3
curl -sIL https://github.com/great-yob/KS-Works-Utility/releases/latest/download/KS-Works-Utility-Setup.exe | grep -i "^HTTP\|^content-length"
```

기대값: draft·prerelease 아님, 자산 3개(`KS-Works-Utility-Setup.exe`, `.blockmap`, `latest.yml`),
익명 API의 latest가 새 태그, `latest.yml`의 `version: X.Y.Z`, 고정 설치 링크가 최종 200.
하나라도 어긋나면 **멈춤 조건** — 이전 릴리스를 지우지 말 것(설치 링크가 사라질 수 있다).

## 8. 릴리스 노트 + 이전 릴리스 정리

릴리스 본문에 사용자(직원) 눈높이의 짧은 변경 요약을 단다:

```bash
gh release edit vX.Y.Z --notes-file - <<'EOF'
- 무엇이 좋아졌는지 한 줄씩
EOF
```

GitHub Releases에는 **최신 릴리스 하나만** 남긴다. 새 태그를 뺀 나머지를 전부 지운다:

```bash
for t in $(gh release list --limit 100 --json tagName --jq '.[].tagName'); do
  [ "$t" = "vX.Y.Z" ] || gh release delete "$t" --yes
done
gh release list
```

- `--cleanup-tag`는 쓰지 않는다. **git 태그는 남긴다** — 버전별 변경 비교(`git diff v1.1.2..v1.1.3`)와
  버그가 들어온 버전 추적에 쓴다. 지우는 건 릴리스(설치 파일 묶음)뿐이다.
- 이전 릴리스가 없으면 구버전 클라이언트는 차등(blockmap) 다운로드를 못 하고 electron-updater가
  알아서 전체 다운로드로 넘어간다. 정상이다.

## 9. 보고

새 버전, 커밋 해시, 릴리스 URL(`https://github.com/great-yob/KS-Works-Utility/releases/tag/vX.Y.Z`),
다시 빌드한 exe, 지운 릴리스 목록, 7단계 검증 결과를 짧게 알린다.
