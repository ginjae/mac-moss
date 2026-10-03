"""OpenAI-compatible transcription server backed by mlx-audio.

Serves ``/v1/audio/transcriptions`` and ``/v1/models`` for Mac MOSS on Apple
Silicon. Run it with ``scripts/run-mlx.sh``.

MOSS generation is single-threaded and holds the Metal device, so requests are
serialised behind one lock and executed off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import tempfile
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse


ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = Path(os.getenv("MLX_MODEL_PATH", "models/moss-mlx-8bit")).expanduser()
MODEL_PATH = (MODEL_PATH if MODEL_PATH.is_absolute() else ROOT / MODEL_PATH).resolve()
SERVED_MODEL_NAME = os.getenv("MOSS_MODEL_NAME", "moss-mtd")
# Leave room for timestamps, speaker tags, and text from a 30-minute chunk.
DEFAULT_MAX_TOKENS = int(os.getenv("MLX_MAX_TOKENS", "32768"))
KV_CACHE_BITS = int(os.getenv("MLX_KV_BITS", "8"))
# Quantized attention materializes scores; smaller prefill batches keep their
# temporary memory bounded when a 30-minute recording has ~23k audio tokens.
PREFILL_STEP_SIZE = max(1, int(os.getenv("MLX_PREFILL_STEP_SIZE", "512")))
# Without this instruction MOSS returns bare text with no timestamps or speaker
# tags, which the app cannot parse. The app sends the same prompt; this default
# keeps direct calls (curl, health checks) producing the canonical format too.
DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号（[S01]、[S02]、[S03]…）开头，"
    "正文为对应的语音内容，并在段末标注结束时间戳，以清晰标明该段语音范围。"
)
LANGUAGE_NAMES = {"ko": "韩语", "en": "英语", "ja": "日语", "zh": "中文"}

MODEL: Any = None
MODEL_LOCK = asyncio.Lock()
STREAM_TASKS: set[asyncio.Task] = set()
ACTIVE_REQUESTS: dict[str, tuple[threading.Event, threading.Event, asyncio.Task]] = {}
LOGGER = logging.getLogger("uvicorn.error")


def load_once() -> Any:
    global MODEL
    if MODEL is None:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("Mac MOSS 전사 서버는 Apple Silicon(macOS arm64) 전용입니다.")
        from mlx_audio.stt.utils import load_model
        from src.mlx_kv_cache import configure_kv_cache

        if not MODEL_PATH.exists():
            raise RuntimeError(
                f"MLX 모델이 없습니다: {MODEL_PATH}\n"
                "./scripts/download-mlx-model.sh 를 실행하세요."
            )
        loaded_model = load_model(str(MODEL_PATH))
        configure_kv_cache(loaded_model, bits=KV_CACHE_BITS)
        MODEL = loaded_model
    return MODEL


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Load the weights before accepting traffic. A cold first request otherwise
    # pages ~1.2 GB off disk mid-transcription and looks like a hang to the app.
    await asyncio.to_thread(load_once)
    yield


app = FastAPI(title="Mac MOSS MLX transcription server", lifespan=lifespan)


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": SERVED_MODEL_NAME,
                "object": "model",
                "owned_by": "mlx-audio",
                "created": 0,
            }
        ],
    }


@app.get("/healthz", include_in_schema=False)
def liveness() -> dict[str, bool]:
    return {"ok": True}


def split_hotwords(hotwords: str) -> list[str] | None:
    """MOSS takes a list; the app sends one comma/newline separated string."""
    terms = [
        term.strip()
        for term in hotwords.replace("\n", ",").split(",")
        if term.strip()
    ]
    return terms or None


def transcription_prompt(prompt: str, language: str) -> str:
    prompt = prompt or DEFAULT_PROMPT
    language_tag = language.strip().replace("_", "-")
    normalized_tag = language_tag.lower()
    if normalized_tag in {"", "auto"}:
        return prompt
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{1,8})*", normalized_tag):
        raise HTTPException(400, "언어 코드는 ko, en, pt-BR 같은 형식으로 입력해 주세요.")
    language_name = LANGUAGE_NAMES.get(normalized_tag.split("-")[0], language_tag)
    return f"{prompt}\n语言提示：主要语音为{language_name}。请按语音原语言转写，不要翻译。"


def run_transcription(
    audio_path: str, prompt: str, hotwords: str, max_tokens: int,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    model = load_once()
    started = time.time()
    options = dict(
        max_tokens=max_tokens,
        temperature=0.0,
        prompt=prompt or DEFAULT_PROMPT,
        hotwords=split_hotwords(hotwords),
        prefill_step_size=PREFILL_STEP_SIZE,
    )
    if on_progress is None:
        result = model.generate(audio_path, **options)
    else:
        from src.mlx_progress import generate_with_progress

        result = generate_with_progress(model, audio_path, on_progress, **options)
    return {
        "text": result.text,
        "segments": result.segments or [],
        "duration": round(time.time() - started, 2),
    }


def progress_stream(
    audio_path: str, prompt: str, hotwords: str, max_tokens: int, request_id: str,
) -> StreamingResponse:
    """Stream measured progress and the final result as newline-delimited JSON."""
    loop = asyncio.get_running_loop()
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    disconnected = threading.Event()
    cancelled = threading.Event()
    started = threading.Event()
    if request_id and request_id in ACTIVE_REQUESTS:
        raise HTTPException(409, "같은 전사 요청이 이미 실행 중입니다.")

    def deliver(event: dict[str, Any]) -> None:
        if not disconnected.is_set():
            events.put_nowait(event)

    def report(progress: dict[str, Any]) -> None:
        if disconnected.is_set() or cancelled.is_set():
            raise RuntimeError("전사가 취소되었습니다.")
        LOGGER.info("transcription_progress %s", json.dumps({"request_id": request_id, **progress}))
        loop.call_soon_threadsafe(deliver, {"type": "progress", **progress})

    async def worker() -> None:
        try:
            # This independent task retains the lock until the GPU thread exits,
            # including when the HTTP client disconnects during generation.
            async with MODEL_LOCK:
                if disconnected.is_set() or cancelled.is_set():
                    raise RuntimeError("전사가 취소되었습니다.")
                started.set()
                payload = await asyncio.to_thread(
                    run_transcription, audio_path, prompt, hotwords, max_tokens, report
                )
            deliver({"type": "result", **payload})
        except Exception as error:
            deliver({"type": "error", "message": f"MLX 전사 실패: {error}"})
        except asyncio.CancelledError:
            deliver({"type": "error", "message": "전사가 취소되었습니다."})

    events.put_nowait({"type": "progress", "stage": "waiting"})
    task = asyncio.create_task(worker())
    STREAM_TASKS.add(task)

    def cleanup(completed: asyncio.Task) -> None:
        STREAM_TASKS.discard(completed)
        Path(audio_path).unlink(missing_ok=True)
        if request_id:
            ACTIVE_REQUESTS.pop(request_id, None)
        if completed.cancelled():
            deliver({"type": "error", "message": "전사가 취소되었습니다."})

    task.add_done_callback(cleanup)
    if request_id:
        ACTIVE_REQUESTS[request_id] = (cancelled, started, task)

    async def body():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(events.get(), timeout=10)
                except TimeoutError:
                    event = {"type": "heartbeat"}
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if event["type"] in {"result", "error"}:
                    break
        finally:
            disconnected.set()

    return StreamingResponse(
        body(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/audio/transcriptions/{request_id}/cancel", status_code=204)
async def cancel_transcription(request_id: str) -> Response:
    request = ACTIVE_REQUESTS.get(request_id)
    if request is None:
        raise HTTPException(404, "실행 중인 전사 요청을 찾을 수 없습니다.")
    cancelled, started, task = request
    cancelled.set()
    if not started.is_set():
        task.cancel()
    # Keep the model locked until its generation thread has actually stopped.
    with suppress(asyncio.CancelledError):
        await asyncio.shield(task)
    return Response(status_code=204)


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    model: str = Form(SERVED_MODEL_NAME),
    prompt: str = Form(""),
    hotwords: str = Form(""),
    language: str = Form(""),
    temperature: str = Form("0"),
    response_format: str = Form("json"),
    max_completion_tokens: str = Form(""),
    stream: bool = Form(False),
    request_id: str = Form("", max_length=128),
) -> Any:
    resolved_prompt = transcription_prompt(prompt, language)
    try:
        max_tokens = max(1, min(int(max_completion_tokens or DEFAULT_MAX_TOKENS), 32768))
    except ValueError:
        max_tokens = DEFAULT_MAX_TOKENS

    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    handle, temp_path = tempfile.mkstemp(suffix=suffix)
    streaming = False
    try:
        with os.fdopen(handle, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                buffer.write(chunk)
        if stream:
            response = progress_stream(temp_path, resolved_prompt, hotwords, max_tokens, request_id)
            streaming = True
            return response
        # One Metal context, one request at a time.
        async with MODEL_LOCK:
            payload = await asyncio.to_thread(
                run_transcription, temp_path, resolved_prompt, hotwords, max_tokens
            )
    except HTTPException:
        raise
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
    except Exception as error:  # pragma: no cover - surfaced to the app as 500
        raise HTTPException(500, f"MLX 전사 실패: {error}") from error
    finally:
        if not streaming:
            Path(temp_path).unlink(missing_ok=True)

    return JSONResponse(payload)
