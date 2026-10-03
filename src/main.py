from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from src.store import NoteStore
from src.transcript import json_text, parse_transcript, plain_text, srt_text, vtt_text


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("MOSS_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
STATIC_DIR = ROOT / "static"
MLX_URL = os.getenv("MOSS_MLX_URL", "http://127.0.0.1:8001/v1").rstrip("/")
MODEL_NAME = os.getenv("MOSS_MODEL_NAME", "moss-mtd")
AUTH_USERNAME = os.getenv("MOSS_AUTH_USERNAME", "")
AUTH_PASSWORD = os.getenv("MOSS_AUTH_PASSWORD", "")
MAX_UPLOAD_BYTES = int(float(os.getenv("MOSS_MAX_UPLOAD_GB", "5")) * 1024**3)
MAX_COMPLETION_TOKENS = int(os.getenv("MOSS_MAX_COMPLETION_TOKENS", "32768"))
# Default to 30-minute chunks with the server's 8-bit KV cache.
# The official model limit remains an upper bound of 90 minutes.
MAX_SUPPORTED_CHUNK_SECONDS = 5400
MAX_AUDIO_CHUNK_SECONDS = max(
    1, min(int(os.getenv("MOSS_CHUNK_SECONDS", "1800")), MAX_SUPPORTED_CHUNK_SECONDS)
)
BRIDGE_AUDIO_SECONDS = max(
    2, min(int(os.getenv("MOSS_BRIDGE_SECONDS", "30")), MAX_SUPPORTED_CHUNK_SECONDS)
)
STORE = NoteStore(DATA_DIR / "moss-note.sqlite3")
QUEUE: asyncio.Queue[str] = asyncio.Queue()
PREPARATION_TASKS: dict[str, asyncio.Task] = {}
TRANSCRIPTION_TASKS: dict[str, asyncio.Task] = {}
CANCEL_REQUESTED: set[str] = set()
NOTE_ACTION_LOCKS: dict[str, asyncio.Lock] = {}
AUDIO_CANCELLATION: ContextVar[threading.Event | None] = ContextVar("audio_cancellation", default=None)

DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号（[S01]、[S02]、[S03]…）开头，"
    "正文为对应的语音内容，并在段末标注结束时间戳，以清晰标明该段语音范围。"
)


class NotePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    segments: list[dict[str, Any]] | None = None
    speaker_names: dict[str, str] | None = None


def public_note(note: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in note.items()
        if key
        not in {
            "source_path",
            "normalized_path",
            "raw_transcript",
        }
    }
    result["processing_elapsed_seconds"] = note.get("processing_seconds")
    if note["status"] == "processing" and note.get("processing_started_at"):
        result["processing_elapsed_seconds"] = (note.get("preprocessing_seconds") or 0) + max(
            0.0, (datetime.now(UTC) - datetime.fromisoformat(note["processing_started_at"])).total_seconds()
        )
    return result


def run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    cancelled = AUDIO_CANCELLATION.get()
    if cancelled is None:
        return subprocess.run(arguments, check=True, capture_output=True, text=True)
    if cancelled.is_set():
        raise RuntimeError("전사가 취소되었습니다.")
    with subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        while True:
            if cancelled.is_set():
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise RuntimeError("전사가 취소되었습니다.")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, arguments, stdout, stderr)
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


async def run_audio_work(function: Callable, *arguments: Any) -> Any:
    """Stop audio subprocesses and join their thread before cleaning its files."""
    cancelled = threading.Event()

    def work():
        token = AUDIO_CANCELLATION.set(cancelled)
        try:
            return function(*arguments)
        finally:
            AUDIO_CANCELLATION.reset(token)

    task = asyncio.create_task(asyncio.to_thread(work))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        with suppress(Exception):
            await task
        raise


def probe_duration(path: Path) -> float | None:
    try:
        result = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
        return round(float(result.stdout.strip()), 3)
    except (subprocess.CalledProcessError, ValueError):
        return None


def normalize_audio(source: Path, destination: Path) -> None:
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def public_upload(upload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in upload.items() if key not in {"source_path", "normalized_path"}}


async def save_audio_upload(file: UploadFile, upload_id: str) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(400, "파일 이름이 없습니다.")
    directory = UPLOAD_DIR / upload_id
    directory.mkdir(parents=True)
    source = directory / f"source{Path(file.filename).suffix.lower()[:12]}"
    size = 0
    try:
        with source.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "업로드 제한을 초과했습니다.")
                destination.write(chunk)
        if not size:
            raise HTTPException(400, "빈 파일은 전사할 수 없습니다.")
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        await file.close()
    return {
        "id": upload_id, "original_filename": Path(file.filename).name,
        "media_type": (
            mimetypes.guess_type(file.filename)[0]
            if file.content_type in {None, "application/octet-stream"}
            else file.content_type
        ),
        "source_path": str(source),
    }


async def prepare_upload(upload_id: str) -> None:
    upload = STORE.get_upload(upload_id)
    source = STORE.resolve_path(upload_id, upload["source_path"])
    normalized = source.parent / "normalized.wav"
    started = time.monotonic()
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(normalized),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        if process.returncode:
            raise RuntimeError("음성 파일을 읽을 수 없습니다. 다른 파일을 선택해 주세요.")
        duration = await asyncio.to_thread(probe_duration, normalized)
        if duration is None or duration <= 0:
            raise RuntimeError("음성 길이를 확인할 수 없습니다. 다른 파일을 선택해 주세요.")
        STORE.update_upload(
            upload_id, status="ready", normalized_path=str(normalized), duration=duration,
            preprocessing_seconds=round(time.monotonic() - started, 3), error=None,
        )
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            await process.communicate()
        normalized.unlink(missing_ok=True)
        raise
    except Exception as error:
        normalized.unlink(missing_ok=True)
        STORE.update_upload(upload_id, status="error", error=str(error))


def start_upload_preparation(upload_id: str) -> None:
    task = asyncio.create_task(prepare_upload(upload_id))
    PREPARATION_TASKS[upload_id] = task
    task.add_done_callback(lambda _: PREPARATION_TASKS.pop(upload_id, None))


async def discard_prepared_upload(upload_id: str) -> None:
    # Remove the manifest first; promotion then cannot race file deletion.
    STORE.discard_upload(upload_id)
    task = PREPARATION_TASKS.get(upload_id)
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    shutil.rmtree(STORE.upload_directory(upload_id), ignore_errors=True)


async def clean_expired_uploads() -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    for upload in STORE.list_uploads():
        if upload["created_at"] < cutoff:
            with suppress(KeyError):
                await discard_prepared_upload(upload["id"])


def split_audio(
    normalized: Path, duration: float | None, chunk_seconds: int = MAX_AUDIO_CHUNK_SECONDS,
) -> list[tuple[Path, float]]:
    """Return WAV chunks of the configured duration and timeline offsets."""
    if duration is None or duration <= chunk_seconds:
        return [(normalized, 0.0)]

    chunk_dir = normalized.parent / "chunks"
    shutil.rmtree(chunk_dir, ignore_errors=True)
    chunk_dir.mkdir()
    chunks: list[tuple[Path, float]] = []
    offset = 0.0
    index = 0
    while offset < duration - 0.01:
        chunk = chunk_dir / f"chunk-{index:03d}.wav"
        chunk_duration = min(chunk_seconds, duration - offset)
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(offset),
                "-t",
                str(chunk_duration),
                "-i",
                str(normalized),
                "-vn",
                "-c:a",
                "pcm_s16le",
                str(chunk),
            ]
        )
        chunks.append((chunk, offset))
        offset += chunk_duration
        index += 1
    return chunks


def make_bridge_chunks(
    normalized: Path,
    duration: float | None,
    chunks: list[tuple[Path, float]],
    chunk_seconds: int = MAX_AUDIO_CHUNK_SECONDS,
) -> list[tuple[Path, float]]:
    """Create short clips spanning both sides of every main-chunk boundary."""
    if duration is None or len(chunks) <= 1:
        return []
    chunk_dir = normalized.parent / "chunks"
    bridge_seconds = min(BRIDGE_AUDIO_SECONDS, chunk_seconds)
    half = bridge_seconds / 2
    bridges: list[tuple[Path, float]] = []
    for index, (_, boundary) in enumerate(chunks[1:]):
        start = max(0.0, boundary - half)
        bridge_duration = min(bridge_seconds, duration - start)
        bridge = chunk_dir / f"bridge-{index:03d}.wav"
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(start),
                "-t",
                str(bridge_duration),
                "-i",
                str(normalized),
                "-vn",
                "-c:a",
                "pcm_s16le",
                str(bridge),
            ]
        )
        bridges.append((bridge, start))
    return bridges


def transcription_form(note: dict[str, Any]) -> dict[str, str]:
    prompt = DEFAULT_PROMPT
    if note.get("hotwords"):
        prompt += f"热词提示：{note['hotwords']}"
    form = {
        "model": MODEL_NAME,
        "response_format": "json",
        "temperature": "0",
        "prompt": prompt,
        "max_completion_tokens": str(MAX_COMPLETION_TOKENS),
    }
    if note.get("hotwords"):
        form["hotwords"] = note["hotwords"]
    if note.get("language"):
        form["language"] = note["language"]
    return form


async def transcribe_chunk(
    client: httpx.AsyncClient, audio_path: Path, note: dict[str, Any],
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    request_id: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    form = transcription_form(note)
    if on_progress:
        form.update(stream="true", request_id=request_id)
    payload: dict[str, Any] | None = None
    try:
        with audio_path.open("rb") as audio:
            async with client.stream(
                "POST",
                f"{MLX_URL}/audio/transcriptions",
                data=form,
                files={"file": ("audio.wav", audio, "audio/wav")},
            ) as response:
                if not response.is_success:
                    await response.aread()
                    response.raise_for_status()
                if "application/x-ndjson" in response.headers.get("content-type", ""):
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if event.get("type") == "progress" and on_progress:
                            on_progress({key: value for key, value in event.items() if key != "type"})
                        elif event.get("type") == "result":
                            payload = event
                        elif event.get("type") == "error":
                            raise RuntimeError(event.get("message", "전사 서버 오류"))
                else:
                    # An older/custom OpenAI-compatible server may only return JSON.
                    await response.aread()
                    payload = response.json()
    except asyncio.CancelledError:
        if request_id:
            # Closing the stream also cancels generation. The explicit request
            # waits for the GPU thread to release the model before reporting done.
            with suppress(httpx.HTTPError):
                await client.post(
                    f"{MLX_URL}/audio/transcriptions/{quote(request_id, safe='')}/cancel",
                    timeout=httpx.Timeout(5, read=None),
                )
        raise
    if payload is None:
        raise RuntimeError("전사 서버 연결이 결과를 받기 전에 종료되었습니다.")
    raw = payload.get("text", "")
    segments = parse_transcript(raw)
    if not segments and payload.get("segments"):
        segments = [
            {
                "id": index,
                "start": float(segment.get("start", 0)),
                "end": float(segment.get("end", 0)),
                "speaker": segment.get("speaker", f"S{index + 1:02d}"),
                "text": segment.get("text", "").strip(),
            }
            for index, segment in enumerate(payload["segments"])
        ]
    return raw, segments


def record_model_progress(
    note_id: str, progress: dict[str, Any], *, index: int, kind: str,
    offset: float, duration: float | None,
    chunk_seconds: int = MAX_AUDIO_CHUNK_SECONDS,
) -> None:
    changes: dict[str, Any] = {
        "model_progress": {**progress, "chunk_index": index, "kind": kind},
    }
    if kind == "main" and duration is not None:
        # Timestamps are local to each chunk. Only completed segment ends count.
        local_seconds = max(0.0, float(progress.get("audio_seconds", 0)))
        chunk_duration = min(chunk_seconds, duration - offset)
        changes["processed_audio_seconds"] = round(
            min(duration, offset + min(local_seconds, chunk_duration)), 3
        )
    STORE.update(note_id, **changes)


def match_speakers_by_overlap(
    source_offset: float,
    source_segments: list[dict[str, Any]],
    bridge_offset: float,
    bridge_segments: list[dict[str, Any]],
) -> dict[str, str]:
    """Greedily find a one-to-one speaker mapping on duplicated audio."""
    scores: dict[tuple[str, str], float] = {}
    for source in source_segments:
        source_start = float(source["start"]) + source_offset
        source_end = float(source["end"]) + source_offset
        for bridge in bridge_segments:
            bridge_start = float(bridge["start"]) + bridge_offset
            bridge_end = float(bridge["end"]) + bridge_offset
            overlap = min(source_end, bridge_end) - max(source_start, bridge_start)
            if overlap > 0:
                key = (source["speaker"], bridge["speaker"])
                scores[key] = scores.get(key, 0.0) + overlap

    mapping: dict[str, str] = {}
    used_bridge_speakers: set[str] = set()
    for (source_speaker, bridge_speaker), score in sorted(
        scores.items(), key=lambda item: item[1], reverse=True
    ):
        if score < 0.25:
            continue
        if source_speaker in mapping or bridge_speaker in used_bridge_speakers:
            continue
        mapping[source_speaker] = bridge_speaker
        used_bridge_speakers.add(bridge_speaker)
    return mapping


def merge_chunk_segments(
    results: list[tuple[float, list[dict[str, Any]]]],
    bridges: list[tuple[float, list[dict[str, Any]]]] | None = None,
) -> list[dict[str, Any]]:
    """Offset timestamps and connect adjacent speaker IDs through bridges."""
    parent: dict[tuple[int, str], tuple[int, str]] = {}

    def find(node: tuple[int, str]) -> tuple[int, str]:
        parent.setdefault(node, node)
        if parent[node] != node:
            parent[node] = find(parent[node])
        return parent[node]

    def union(left: tuple[int, str], right: tuple[int, str]) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for chunk_index, (_, segments) in enumerate(results):
        for segment in segments:
            find((chunk_index, segment["speaker"]))

    for boundary_index, (bridge_offset, bridge_segments) in enumerate(bridges or []):
        if boundary_index + 1 >= len(results):
            break
        left_offset, left_segments = results[boundary_index]
        right_offset, right_segments = results[boundary_index + 1]
        left_map = match_speakers_by_overlap(
            left_offset, left_segments, bridge_offset, bridge_segments
        )
        right_map = match_speakers_by_overlap(
            right_offset, right_segments, bridge_offset, bridge_segments
        )
        right_by_bridge = {
            bridge_speaker: source_speaker
            for source_speaker, bridge_speaker in right_map.items()
        }
        for left_speaker, bridge_speaker in left_map.items():
            if right_speaker := right_by_bridge.get(bridge_speaker):
                union(
                    (boundary_index, left_speaker),
                    (boundary_index + 1, right_speaker),
                )

    merged: list[dict[str, Any]] = []
    global_speakers: dict[tuple[int, str], str] = {}
    for chunk_index, (offset, segments) in enumerate(results):
        for segment in segments:
            root = find((chunk_index, segment["speaker"]))
            if root not in global_speakers:
                global_speakers[root] = f"S{len(global_speakers) + 1:02d}"
            merged.append(
                {
                    "id": len(merged),
                    "start": round(float(segment["start"]) + offset, 3),
                    "end": round(float(segment["end"]) + offset, 3),
                    "speaker": global_speakers[root],
                    "text": segment["text"],
                    "chunk": chunk_index + 1,
                }
            )
    return merged


def canonical_transcript(segments: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{segment['start']:.2f}][{segment['speaker']}]"
        f"{segment['text']}[{segment['end']:.2f}]"
        for segment in segments
    )


async def transcribe(note_id: str) -> None:
    started = time.monotonic()
    note = STORE.update(
        note_id,
        status="processing",
        error=None,
        processed_chunks=0,
        bridge_count=0,
        processed_bridges=0,
        progress_phase="normalizing",
        progress_completed_steps=0,
        progress_total_steps=0,
        processed_audio_seconds=0.0,
        model_progress={},
        processing_started_at=datetime.now(UTC).isoformat(),
        processing_finished_at=None,
        processing_seconds=None,
    )
    chunk_dir: Path | None = None
    preprocessing_seconds = note.get("preprocessing_seconds") or 0
    try:
        chunk_seconds = note.get("chunk_seconds") or MAX_AUDIO_CHUNK_SECONDS
        if note.get("chunk_seconds") is None:
            note = STORE.update(note_id, chunk_seconds=chunk_seconds)
        source = STORE.resolve_path(note_id, note["source_path"])
        normalized = (
            STORE.resolve_path(note_id, note["normalized_path"])
            if note.get("normalized_path") else source.parent / "normalized.wav"
        )
        chunk_dir = normalized.parent / "chunks"
        if note.get("normalized_path") and normalized.is_file():
            duration = note.get("duration") or await run_audio_work(probe_duration, normalized)
        else:
            await run_audio_work(normalize_audio, source, normalized)
            duration = await run_audio_work(probe_duration, normalized)
        chunks = await run_audio_work(split_audio, normalized, duration, chunk_seconds)
        bridges = await run_audio_work(
            make_bridge_chunks, normalized, duration, chunks, chunk_seconds
        )
        STORE.update(
            note_id,
            normalized_path=str(normalized),
            duration=duration,
            chunk_count=len(chunks),
            bridge_count=len(bridges),
            progress_phase="transcribing",
            progress_total_steps=len(chunks) + len(bridges) + 1,
        )

        timeout = httpx.Timeout(connect=30, read=6 * 60 * 60, write=60 * 60, pool=30)
        chunk_results: list[tuple[float, list[dict[str, Any]]]] = []
        bridge_results: list[tuple[float, list[dict[str, Any]]]] = []
        raw_results: list[str] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            for index, (chunk_path, offset) in enumerate(chunks, start=1):
                def on_progress(progress: dict[str, Any]) -> None:
                    record_model_progress(
                        note_id, progress, index=index, kind="main", offset=offset,
                        duration=duration, chunk_seconds=chunk_seconds,
                    )

                raw, segments = await transcribe_chunk(
                    client, chunk_path, note, on_progress, f"{note_id}:main:{index}"
                )
                raw_results.append(raw)
                chunk_results.append((offset, segments))
                processed_seconds = (
                    round(min(duration, offset + chunk_seconds), 3)
                    if duration is not None
                    else None
                )
                STORE.update(
                    note_id,
                    processed_chunks=index,
                    progress_completed_steps=index,
                    processed_audio_seconds=processed_seconds,
                    model_progress={},
                )

            if bridges:
                STORE.update(note_id, progress_phase="linking")
            for index, (bridge_path, offset) in enumerate(bridges, start=1):
                def on_progress(progress: dict[str, Any]) -> None:
                    record_model_progress(
                        note_id, progress, index=index, kind="bridge", offset=offset,
                        duration=duration, chunk_seconds=chunk_seconds,
                    )

                _, segments = await transcribe_chunk(
                    client, bridge_path, note, on_progress, f"{note_id}:bridge:{index}"
                )
                bridge_results.append((offset, segments))
                STORE.update(
                    note_id,
                    processed_bridges=index,
                    progress_completed_steps=len(chunks) + index,
                    model_progress={},
                )

        STORE.update(note_id, progress_phase="finalizing")
        segments = await asyncio.to_thread(
            merge_chunk_segments, chunk_results, bridge_results
        )
        if not segments:
            preview = "\n".join(raw_results)[:500]
            raise RuntimeError(f"모델 결과를 구간으로 해석하지 못했습니다: {preview}")
        speakers = sorted({segment["speaker"] for segment in segments})
        STORE.update(
            note_id,
            status="done",
            progress_phase="done",
            progress_completed_steps=len(chunks) + len(bridges) + 1,
            processed_audio_seconds=duration,
            model_progress={},
            processing_finished_at=datetime.now(UTC).isoformat(),
            processing_seconds=round(preprocessing_seconds + time.monotonic() - started, 3),
            raw_transcript=canonical_transcript(segments),
            raw_segments=segments,
            segments=segments,
            speaker_names={speaker: speaker for speaker in speakers},
        )
    except asyncio.CancelledError:
        if note_id in CANCEL_REQUESTED:
            STORE.update(
                note_id, status="cancelled", progress_phase="cancelled", error=None,
                model_progress={}, processing_finished_at=datetime.now(UTC).isoformat(),
                processing_seconds=round(preprocessing_seconds + time.monotonic() - started, 3),
            )
        raise
    except Exception as error:
        detail = str(error)
        if isinstance(error, httpx.HTTPStatusError):
            detail = f"전사 서버 {error.response.status_code}: {error.response.text[:1000]}"
        STORE.update(
            note_id, status="error", progress_phase="error", error=detail,
            processing_finished_at=datetime.now(UTC).isoformat(),
            processing_seconds=round(preprocessing_seconds + time.monotonic() - started, 3),
        )
    finally:
        if chunk_dir is not None:
            shutil.rmtree(chunk_dir, ignore_errors=True)


async def queue_worker() -> None:
    while True:
        note_id = await QUEUE.get()
        try:
            try:
                note = STORE.get(note_id)
            except KeyError:
                continue
            if note["status"] != "queued":
                continue
            task = asyncio.create_task(transcribe(note_id))
            TRANSCRIPTION_TASKS[note_id] = task
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    if not task.cancelling():
                        task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    raise
                # Cancelling a note must leave the queue worker available.
            finally:
                TRANSCRIPTION_TASKS.pop(note_id, None)
                CANCEL_REQUESTED.discard(note_id)
        finally:
            QUEUE.task_done()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if bool(AUTH_USERNAME) != bool(AUTH_PASSWORD):
        raise RuntimeError(
            "MOSS_AUTH_USERNAME과 MOSS_AUTH_PASSWORD는 함께 설정해야 합니다."
        )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    await clean_expired_uploads()
    for upload in STORE.list_uploads():
        if upload["status"] == "preparing":
            start_upload_preparation(upload["id"])
    for note_id in STORE.pending_ids():
        QUEUE.put_nowait(note_id)
    worker = asyncio.create_task(queue_worker())
    try:
        yield
    finally:
        worker.cancel()
        preparations = list(PREPARATION_TASKS.values())
        for task in preparations:
            task.cancel()
        await asyncio.gather(*preparations, return_exceptions=True)
        with suppress(asyncio.CancelledError):
            await worker


app = FastAPI(
    title="Mac MOSS",
    version="0.2.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


def valid_basic_auth(header: str | None) -> bool:
    if not AUTH_USERNAME:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(
        password, AUTH_PASSWORD
    )


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if request.url.path not in {"/healthz", "/readyz"} and not valid_basic_auth(
        request.headers.get("Authorization")
    ):
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Mac MOSS", charset="UTF-8"'},
        )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path in {"/", "/index.html", "/app.js", "/styles.css"}:
        # Revalidate the UI after code changes instead of heuristically caching
        # an older progress renderer against the current API response.
        response.headers["Cache-Control"] = "no-cache"
    return response


async def model_health() -> dict[str, Any]:
    """Probe the local MLX transcription server."""
    services: dict[str, dict[str, Any]] = {
        "moss": {"ok": False, "url": MLX_URL},
    }

    async with httpx.AsyncClient(timeout=3) as client:
        try:
            result: Any = await client.get(f"{MLX_URL}/models")
        except httpx.HTTPError as error:
            result = error
    moss = services["moss"]
    if isinstance(result, httpx.Response):
        moss["ok"] = result.is_success
        if result.is_success:
            moss["models"] = [item["id"] for item in result.json().get("data", [])]
        else:
            moss["error"] = f"HTTP {result.status_code}"
    else:
        moss["error"] = str(result)
    return services


@app.get("/healthz", include_in_schema=False)
def liveness() -> dict[str, bool]:
    return {"ok": True}


@app.get("/readyz", include_in_schema=False)
async def readiness() -> JSONResponse:
    services = await model_health()
    ready = services["moss"]["ok"]
    return JSONResponse(
        {
            "ready": ready,
            "moss": services["moss"]["ok"],
        },
        status_code=200 if ready else 503,
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    services = await model_health()
    return {
        "ok": True,
        **services,
        "queue_size": STORE.queued_count(),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "default_chunk_seconds": MAX_AUDIO_CHUNK_SECONDS,
        "max_chunk_seconds": MAX_SUPPORTED_CHUNK_SECONDS,
    }


@app.post("/api/uploads", status_code=202)
async def upload_audio(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    await clean_expired_uploads()
    upload_id = uuid.uuid4().hex
    saved = await save_audio_upload(file, upload_id)
    try:
        duration = await asyncio.to_thread(probe_duration, Path(saved["source_path"]))
        if await request.is_disconnected():
            raise HTTPException(400, "파일 업로드가 취소되었습니다.")
        upload = STORE.create_upload({**saved, "duration": duration})
    except BaseException:
        shutil.rmtree(UPLOAD_DIR / upload_id, ignore_errors=True)
        raise
    start_upload_preparation(upload_id)
    return public_upload(upload)


@app.get("/api/uploads/{upload_id}")
def upload_status(upload_id: str) -> dict[str, Any]:
    try:
        return public_upload(STORE.get_upload(upload_id))
    except KeyError:
        raise HTTPException(404, "준비 중인 파일을 찾을 수 없습니다. 파일을 다시 선택해 주세요.") from None


@app.delete("/api/uploads/{upload_id}", status_code=204)
async def cancel_upload(upload_id: str) -> Response:
    try:
        await discard_prepared_upload(upload_id)
    except KeyError:
        raise HTTPException(404, "준비 중인 파일을 찾을 수 없습니다.") from None
    return Response(status_code=204)


@app.post("/api/notes", status_code=202)
async def create_note(
    file: UploadFile | None = File(None),
    title: str = Form(""),
    language: str = Form("ko"),
    hotwords: str = Form(""),
    chunk_minutes: int | None = Form(None, ge=1, le=MAX_SUPPORTED_CHUNK_SECONDS // 60),
    upload_id: str | None = Form(None),
) -> dict[str, Any]:
    if bool(file) == bool(upload_id):
        raise HTTPException(400, "전사할 음성 파일을 하나 선택해 주세요.")
    settings = {
        "language": language.strip() or None, "hotwords": hotwords.strip() or None,
        "chunk_seconds": chunk_minutes * 60 if chunk_minutes is not None else MAX_AUDIO_CHUNK_SECONDS,
    }
    if upload_id:
        try:
            upload = STORE.get_upload(upload_id)
            settings["title"] = (title.strip() or Path(upload["original_filename"]).stem)[:200]
            note = STORE.consume_upload(upload_id, settings)
        except KeyError:
            raise HTTPException(404, "준비한 파일을 찾을 수 없습니다. 파일을 다시 선택해 주세요.") from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
    else:
        saved = await save_audio_upload(file, uuid.uuid4().hex)
        note = STORE.create({
            **saved, **settings, "status": "queued",
            "title": (title.strip() or Path(saved["original_filename"]).stem)[:200],
        })
    QUEUE.put_nowait(note["id"])
    return public_note(note)


@app.get("/api/notes")
def list_notes() -> list[dict[str, Any]]:
    return [public_note(note) for note in STORE.list()]


@app.get("/api/notes/{note_id}")
def get_note(note_id: str) -> dict[str, Any]:
    try:
        return public_note(STORE.get(note_id))
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None


@app.patch("/api/notes/{note_id}")
def patch_note(note_id: str, patch: NotePatch) -> dict[str, Any]:
    changes = patch.model_dump(exclude_none=True)
    if "title" in changes:
        changes["title"] = changes["title"].strip()
        if not changes["title"]:
            raise HTTPException(400, "노트 제목을 입력해 주세요.")
    try:
        note = STORE.get(note_id)
        if "segments" in changes:
            base = note["segments"]
            submitted = changes["segments"]
            if len(base) != len(submitted):
                raise HTTPException(400, "발화 구간의 개수는 변경할 수 없습니다.")
            submitted_by_id = {item.get("id"): item for item in submitted}
            if set(submitted_by_id) != {item.get("id") for item in base}:
                raise HTTPException(400, "발화 ID는 변경할 수 없습니다.")
            changes["segments"] = [
                {
                    **item,
                    "text": str(submitted_by_id[item["id"]].get("text", "")).strip(),
                }
                for item in base
            ]
        return public_note(STORE.update(note_id, **changes))
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None


@app.post("/api/notes/{note_id}/cancel")
async def cancel_note(note_id: str) -> dict[str, Any]:
    async with NOTE_ACTION_LOCKS.setdefault(note_id, asyncio.Lock()):
        return await _cancel_note(note_id)


async def _cancel_note(note_id: str) -> dict[str, Any]:
    try:
        note = STORE.get(note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    if note["status"] == "cancelled":
        return public_note(note)
    if note["status"] not in {"queued", "processing"}:
        raise HTTPException(409, "대기 중이거나 전사 중인 노트만 취소할 수 있습니다.")
    task = TRANSCRIPTION_TASKS.get(note_id)
    if task is not None:
        CANCEL_REQUESTED.add(note_id)
        if not task.cancelling():
            task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        finally:
            if task.done():
                CANCEL_REQUESTED.discard(note_id)
        note = STORE.get(note_id)
    if note["status"] in {"queued", "processing"}:
        note = STORE.update(
            note_id, status="cancelled", progress_phase="cancelled", error=None,
            model_progress={}, processing_finished_at=datetime.now(UTC).isoformat(),
        )
    return public_note(note)


def requeue_note(note_id: str) -> None:
    """Replace stale entries and append one new job, preserving queue order."""
    queued = [QUEUE.get_nowait() for _ in range(QUEUE.qsize())]
    for queued_id in queued:
        if queued_id != note_id:
            QUEUE.put_nowait(queued_id)
    QUEUE.put_nowait(note_id)
    # Requeue before completing old entries so join() cannot finish early.
    for _ in queued:
        QUEUE.task_done()


@app.post("/api/notes/{note_id}/restart", status_code=202)
async def restart_note(note_id: str) -> dict[str, Any]:
    async with NOTE_ACTION_LOCKS.setdefault(note_id, asyncio.Lock()):
        try:
            note = STORE.get(note_id)
        except KeyError:
            raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
        if note["status"] not in {"cancelled", "error"}:
            raise HTTPException(409, "취소되었거나 오류가 발생한 노트만 다시 시작할 수 있습니다.")
        task = TRANSCRIPTION_TASKS.get(note_id)
        if task is not None and not task.done():
            raise HTTPException(409, "작업을 정리 중입니다. 잠시 후 다시 시작해 주세요.")
        try:
            source = STORE.resolve_path(note_id, note["source_path"])
        except ValueError:
            raise HTTPException(409, "보관된 녹음 파일 경로를 확인해 주세요.") from None
        if not source.is_file():
            raise HTTPException(409, "보관된 녹음 파일을 찾을 수 없습니다. 파일을 다시 올려 주세요.")
        normalized_path = note.get("normalized_path")
        if normalized_path:
            try:
                normalized = STORE.resolve_path(note_id, normalized_path)
                if not normalized.is_file():
                    normalized_path = None
            except ValueError:
                normalized_path = None
        note = STORE.update(
            note_id, status="queued", error=None, normalized_path=normalized_path,
            chunk_count=1, processed_chunks=0, bridge_count=0, processed_bridges=0,
            progress_phase="queued", progress_completed_steps=0, progress_total_steps=0,
            processed_audio_seconds=0, model_progress={}, processing_started_at=None,
            processing_finished_at=None, processing_seconds=None, preprocessing_seconds=0,
            raw_transcript=None, raw_segments=[], segments=[], speaker_names={},
        )
        requeue_note(note_id)
        return public_note(note)


@app.delete("/api/notes/{note_id}", status_code=204)
def delete_note(note_id: str) -> Response:
    try:
        note = STORE.get(note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    if note["status"] in {"queued", "processing"}:
        raise HTTPException(409, "전사를 취소하거나 완료한 후 노트를 삭제할 수 있습니다.")
    try:
        source = STORE.resolve_path(note_id, note["source_path"])
        note_dir = STORE.upload_directory(note_id)
    except ValueError:
        raise HTTPException(409, "노트의 녹음 파일 경로를 확인해 주세요.") from None
    if source.parent != note_dir:
        raise HTTPException(409, "노트의 녹음 파일이 전용 저장 폴더에 없습니다.")
    STORE.delete(note_id)
    shutil.rmtree(note_dir, ignore_errors=True)
    return Response(status_code=204)


@app.get("/api/notes/{note_id}/media")
def note_media(note_id: str) -> FileResponse:
    try:
        note = STORE.get(note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    try:
        path = STORE.resolve_path(note_id, note["source_path"])
    except ValueError:
        raise HTTPException(409, "노트의 녹음 파일 경로를 확인해 주세요.") from None
    if not path.is_file():
        raise HTTPException(404, "노트의 녹음 파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type=note.get("media_type"), filename=note["original_filename"])


@app.get("/api/notes/{note_id}/export/{format_name}")
def export_note(
    note_id: str,
    format_name: str,
    version: str = Query(default="edited", pattern="^(edited|raw)$"),
) -> Response:
    try:
        note = STORE.get(note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    selected_segments = note["segments"]
    if version == "raw":
        selected_segments = note["raw_segments"] or note["segments"]
    export_note_data = {**note, "segments": selected_segments}
    exporters = {
        "txt": (plain_text, "text/plain; charset=utf-8"),
        "srt": (srt_text, "application/x-subrip; charset=utf-8"),
        "vtt": (vtt_text, "text/vtt; charset=utf-8"),
    }
    if format_name == "json":
        content, media_type = json_text(export_note_data), "application/json; charset=utf-8"
    elif format_name in exporters:
        exporter, media_type = exporters[format_name]
        content = exporter(selected_segments, note["speaker_names"])
    else:
        raise HTTPException(400, "지원하지 않는 내보내기 형식입니다.")
    safe_title = "".join(char for char in note["title"] if char.isalnum() or char in "-_ ").strip()
    encoded_name = quote(f"{safe_title or 'transcript'}.{format_name}")
    headers = {
        "Content-Disposition": (
            f"attachment; filename=transcript.{format_name}; filename*=UTF-8''{encoded_name}"
        )
    }
    return Response(content, media_type=media_type, headers=headers)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
