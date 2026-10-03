# Mac MOSS

Apple Silicon Mac에서 음성·영상 파일을 전사하고, 화자와 시간을 구분해 편집하는 로컬 웹 앱입니다.

**이 저장소는 [JoNebula/moss-note](https://github.com/JoNebula/moss-note)를 포크한
[jiseungjeong/moss-note](https://github.com/jiseungjeong/moss-note)를 기반으로 개선한 파생 프로젝트입니다.**
원본 MOSS Note와 기반 포크의 코드 및 기여를 바탕으로 하며, 아래에 출처와 이 버전의 변경 사항을 정리했습니다.

## 요구사항

- Apple Silicon Mac과 macOS (`arm64`, Intel Mac 미지원)
- [Homebrew](https://brew.sh/), `uv`, `ffmpeg`
- Python 3.12: 설치 스크립트가 `uv`를 통해 준비합니다.
- 최초 설치 시 패키지와 모델을 다운로드할 인터넷 연결 및 저장 공간

기본 모델은 [vanch007의 MLX 8비트 변환 모델](https://huggingface.co/vanch007/mlx-MOSS-Transcribe-Diarize-8bit)입니다.
필요한 메모리와 전사 속도는 Mac 사양 및 청크 길이에 따라 달라집니다.

## 빠른 시작

이 저장소를 클론하거나 내려받은 뒤, `README.md`가 있는 저장소 루트에서 실행합니다.

```bash
brew install uv ffmpeg
cp .env.example .env
./scripts/setup-mlx.sh  # 최초 1회: Python 3.12, 의존성, 모델 설치
./scripts/start-mac.sh
```

브라우저에서 <http://127.0.0.1:8000>에 접속합니다. 종료는 실행한 터미널에서 `Ctrl+C`를 누릅니다.
이후에는 `./scripts/start-mac.sh`만 실행하면 됩니다.

1. **새 노트 만들기**에서 녹음 또는 영상 파일을 선택합니다.
2. 음성 길이 확인이 끝나면 청크 길이, 주 언어, 중요 단어를 설정하고 전사를 시작합니다.
3. 완료된 노트에서 녹음을 재생하며 본문과 화자 이름을 수정하고 원하는 형식으로 내보냅니다.

두 서버를 따로 실행하려면 각각 별도의 터미널에서 실행합니다.

```bash
./scripts/run-mlx.sh  # 전사 서버: 127.0.0.1:8001
./scripts/run-app.sh  # 웹 앱: 127.0.0.1:8000
```

## 프로젝트 출처

| 단계 | 프로젝트 | 기여 |
|---|---|---|
| 원본 | [JoNebula/moss-note](https://github.com/JoNebula/moss-note) | MOSS Note의 전사·화자 구분·노트 편집 기능과 애플리케이션 기반 |
| 기반 포크 | [jiseungjeong/moss-note](https://github.com/jiseungjeong/moss-note) | Apple Silicon에서 MLX를 사용하는 로컬 전사 구현 |
| 이 개선판 | Mac MOSS | 로컬 실행 환경 정리, 메모리 설정, 전사 진행 표시와 작업 제어, UI 개선 |

원본의 MIT 라이선스와 저작권 표기를 [LICENSE](LICENSE)에 그대로 보존합니다.
모델 개발 및 MLX 변환에 대한 출처는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 있습니다.

## 주요 기능

- 음성·영상 업로드와 FFmpeg를 통한 16 kHz 모노 음성 정규화
- MOSS-Transcribe-Diarize 기반 다국어 전사, 타임스탬프와 화자 구분
- Apple Silicon GPU(Metal)에서 MLX로 로컬 추론
- 긴 녹음 자동 분할·병합과 경계 브리지 전사를 통한 화자 연결
- 전사 원본과 편집본 확인, 화자 이름·본문 편집, 검색과 자동 저장
- TXT, SRT, WebVTT, JSON 내보내기

### 이 버전의 주요 변경

- 웹 앱·MLX 가상환경, 모델, 다운로드 캐시를 저장소 내부 경로로 통일하고 폴더 이동 시 실행 환경 경로를 복구합니다.
- 8비트 모델 가중치와 별도로 8비트 KV 캐시를 적용하고, 음성 입력 처리 배치 크기를 조절해 긴 청크의 메모리 사용을 관리합니다.
- 업로드 단계에서 음성 길이를 확인하고, 노트마다 1~90분 사이의 청크 길이를 선택할 수 있습니다. 기본값은 30분입니다.
- 음성 입력 처리·텍스트 생성·화자 정리 단계의 진행 상황과 처리 시간을 표시합니다.
- 전사를 취소한 뒤 녹음 파일을 보관하고, 취소되거나 실패한 작업을 처음부터 다시 시작할 수 있습니다.
- 노트 목록, 편집 화면, 내보내기 메뉴, 취소·삭제 확인 UI를 개선했습니다.

이 버전은 로컬 전사와 수동 편집에 집중하며, 기반 포크의 Claude API/에이전트 CLI를 통한 AI 교정 기능은 포함하지 않습니다.

## 설정

실행 스크립트는 `.env`를 자동으로 읽습니다. 전체 설정 예시는 [.env.example](.env.example)에 있습니다.
상대 경로는 저장소 루트를 기준으로 해석합니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MOSS_APP_HOST` / `MOSS_APP_PORT` | `127.0.0.1` / `8000` | 웹 앱 주소와 포트 |
| `MOSS_MLX_URL` / `MOSS_MLX_PORT` | `http://127.0.0.1:8001/v1` / `8001` | 전사 서버 주소와 포트; 변경 시 함께 수정 |
| `MLX_MODEL_ID` | `vanch007/mlx-MOSS-Transcribe-Diarize-8bit` | 다운로드할 모델 저장소 |
| `MLX_MODEL_PATH` | `models/moss-mlx-8bit` | 모델 저장 경로 |
| `MLX_VENV` | `.venv-mlx` | MLX 가상환경 경로 |
| `MLX_KV_BITS` | `8` | KV 캐시 양자화; `0`으로 비활성화 |
| `MLX_PREFILL_STEP_SIZE` | `512` | 음성 입력 처리 배치 크기 |
| `MLX_MAX_TOKENS` / `MOSS_MAX_COMPLETION_TOKENS` | `32768` / `32768` | 서버 기본값과 앱 요청의 생성 토큰 상한 |
| `MOSS_CHUNK_SECONDS` | `1800` | 기본 전사 청크 길이(초); 노트 생성 시 변경 가능 |
| `MOSS_BRIDGE_SECONDS` | `30` | 청크 경계의 화자 연결에 사용하는 음성 길이(초) |
| `MOSS_MAX_UPLOAD_GB` | `5` | 파일당 업로드 크기 제한 |
| `MOSS_AUTH_USERNAME` / `MOSS_AUTH_PASSWORD` | 빈 값 | 선택형 HTTP Basic 인증; 두 값 모두 설정 |

메모리가 부족하면 청크 길이나 `MLX_PREFILL_STEP_SIZE`를 줄여 다시 시도하세요.
모델 ID를 바꾼 경우 `./scripts/download-mlx-model.sh`로 해당 모델을 받은 뒤 서버를 재시작합니다.

## 데이터와 실행 구조

```text
브라우저 → FastAPI 웹 앱 (:8000) → MLX 전사 서버 (:8001, Apple GPU)
                  ↓
            data/ (SQLite + 녹음 파일)
```

기본 설정에서는 음성과 전사 결과를 Mac 안에서 처리하고 저장합니다.
최초 설치와 모델 다운로드에는 PyPI 및 Hugging Face 등의 네트워크 접근이 필요합니다.
`MOSS_MLX_URL`을 외부 서버로 바꾸면 전사할 음성이 해당 서버로 전송됩니다.

| 경로 | 내용 |
|---|---|
| `data/` | 업로드 원본, 정규화 음성, 전사 청크, SQLite 노트 DB |
| `models/` | 다운로드한 모델 가중치 |
| `logs/` | 전사 서버 로그 (`logs/mlx.log`) |
| `.venv/`, `.venv-mlx/` | 웹 앱 및 MLX 실행 환경 |
| `.cache/` | Python, 패키지 및 모델 다운로드 캐시 |
| `.env` | 로컬 설정과 인증 정보 |

위 경로는 기본 `.gitignore`에서 제외합니다. 별도의 저장 경로를 지정했다면 해당 경로도 업로드 대상에서 제외하세요.
노트를 보관하려면 앱을 종료한 뒤 `data/` 전체를 백업합니다.
앱과 전사 서버는 각각 `--workers 1`로 실행합니다.

## 문제 해결과 제한

- 서버가 시작되지 않으면 `logs/mlx.log`를 확인합니다. 설치 또는 모델 다운로드가 누락되었다면 `./scripts/setup-mlx.sh`를 다시 실행합니다.
- 상태 확인: <http://127.0.0.1:8000/healthz>는 앱 실행 상태, <http://127.0.0.1:8000/readyz>는 전사 서버 준비 상태를 반환합니다.
- 파일 업로드 후 처리하는 방식이며, 마이크 실시간 전사는 지원하지 않습니다.
- 화자 연결은 인접 청크의 경계 음성에서 같은 화자를 찾는 방식입니다. 경계에서 발화하지 않은 사람은 별도 화자로 남을 수 있습니다.
- 취소·실패 후 **전사 다시 시작**은 처음부터 처리합니다. 중간 결과를 이어서 처리하는 방식은 아닙니다.
- 전사 정확도는 언어, 녹음 품질, 겹쳐 말하기, 고유명사에 따라 달라집니다. 결과는 직접 확인하고 편집하세요.
- 기본 실행 주소는 localhost입니다. 외부 접근을 허용할 경우 HTTPS와 앱 인증을 구성하고, 인증 기능이 없는 MLX 전사 서버는 로컬에 유지하세요.

## 라이선스

애플리케이션 코드는 원본 및 기반 포크와 동일한 [MIT License](LICENSE)를 따릅니다.
`Copyright (c) 2026 Jo Seongwoon`과 라이선스 전문을 보존합니다.
코드 또는 그 상당 부분을 재배포할 때에도 해당 저작권 및 허가 고지를 함께 포함해야 합니다.

MOSS 모델과 MLX 8비트 변환 모델은 각 모델 카드에 Apache-2.0 라이선스를 명시합니다.
모델 가중치와 외부 의존성은 이 소스 저장소에 포함하지 않으며, 각 구성 요소의 라이선스를 따릅니다.
자세한 출처와 라이선스 근거는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 참고하세요.
