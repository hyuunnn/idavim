# idavim

[English](README.md) | **한국어**

[Vimium](https://github.com/philc/vimium)과 [IdeaVim](https://github.com/JetBrains/ideavim)에서 영감을 받은, IDA Pro용 vim 스타일 키보드 내비게이션 플러그인입니다.

**디스어셈블리 뷰**와 **Hex-Rays 슈도코드 뷰** 양쪽에서 모두 동작합니다.

## 동작 방식

idavim은 vim처럼 모달(modal) 방식으로 동작합니다:

- **NORMAL 모드** (기본): 아래 키들을 가로채서 내비게이션에 사용합니다. IDA의 단일 키 단축키(`n` 이름 변경, `d` 데이터, `u` 정의 해제, `g` 주소 이동 등)보다 우선합니다.
- **INSERT 모드**: 모든 키가 IDA로 그대로 전달되어, IDA 기본 단축키를 평소처럼 사용할 수 있습니다.

| 동작 | 키 |
|---|---|
| INSERT(통과) 모드 진입 | `i` |
| NORMAL 모드 복귀 | `Ctrl+[` 또는 `Shift+Esc` (macOS: `⌃[`, `⌘[` 모두 가능) |
| idavim 전체 켜기/끄기 | `Ctrl+Shift+V` (또는 Edit → Plugins → idavim) |

키 가로채기는 애플리케이션 레벨 Qt 이벤트 필터로 구현되어 있어서, 다이얼로그·CLI 입력창·기타 텍스트 입력에서는 키를 절대 빼앗지 않습니다 — 리스팅 뷰에서만 동작합니다.

## 키 목록 (NORMAL 모드)

### 이동

| 키 | 동작 |
|---|---|
| `h` `j` `k` `l` | 왼쪽 / 아래 / 위 / 오른쪽 |
| `d` / `u` | 반 페이지 아래 / 위 (Vimium 스타일) |
| `gg` / `G` | 리스팅 맨 위 / 맨 아래 (디스어셈블리에서는 데이터베이스 시작 / 끝) |
| `0` / `^` / `$` | 줄 시작 / 첫 non-blank 문자 / 줄 끝 |
| `w` / `e` / `b` | 다음 단어 시작 / 단어 끝 / 이전 단어 시작 |
| `1`–`9` | 카운트 접두사, 예: `12j`, `3w`, `2d` |

### 찾기 & 검색

| 키 | 동작 |
|---|---|
| `f{문자}` / `F{문자}` | 현재 줄에서 문자를 앞 / 뒤 방향으로 찾기 |
| `;` / `,` | 마지막 `f`/`F` 반복 (같은 / 반대 방향) |
| `/` | 검색 (패턴 입력 프롬프트, 대소문자 무시 substring) |
| `n` / `N` | 다음 / 이전 검색 매치로 이동 |

슈도코드 뷰에서 `/`는 현재 함수의 전체 라인을 검색하며 끝에 도달하면 처음부터 이어서(wrap-around) 검색합니다.
디스어셈블리 뷰에서는 커서 위치부터 item head 단위로 디스어셈블리 텍스트와 심볼 이름을 검색합니다.

그 외 모든 키(`Esc`, `Enter`, `x`, `Space`, 방향키 등)는 NORMAL 모드에서도 IDA로 그대로 전달됩니다.

## 요구 사항

- IDA Pro 9.0+ (GUI, PySide6 또는 PyQt5 기반 Qt)
- 외부 Python 의존성 없음

## 설치

[IDA Plugin Manager](https://plugins.hex-rays.com/) 사용:

```sh
hcli plugin install idavim
```

수동 설치: `idavim_entry.py`, `idavim.py`, `ida-plugin.json`을 IDA 플러그인 디렉토리 아래 `idavim/` 폴더에 복사합니다 (예: `~/.idapro/plugins/idavim/`).

개발용으로는 리포를 심볼릭 링크로 연결해도 됩니다:

```sh
ln -sfn /path/to/idavim ~/.idapro/plugins/idavim
```

## 라이선스

MIT
