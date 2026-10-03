from __future__ import annotations

import json
import re
from typing import Any


SEGMENT_PATTERN = re.compile(
    r"\[(?P<start>\d+(?:\.\d+)?)\]\[(?P<speaker>S\d+)\]"
    r"(?P<text>.*?)"
    r"\[(?P<end>\d+(?:\.\d+)?)\]"
    r"(?=\s*\[\d+(?:\.\d+)?\]\[S\d+\]|\s*$)",
    re.DOTALL,
)


def parse_transcript(raw: str) -> list[dict[str, Any]]:
    """Parse MOSS's canonical ``[start][Sxx]text[end]`` output."""
    segments: list[dict[str, Any]] = []
    for index, match in enumerate(SEGMENT_PATTERN.finditer(raw.strip())):
        text = re.sub(r"\s+", " ", match.group("text")).strip()
        segments.append(
            {
                "id": index,
                "start": float(match.group("start")),
                "end": float(match.group("end")),
                "speaker": match.group("speaker"),
                "text": text,
            }
        )
    return segments


def plain_text(segments: list[dict[str, Any]], names: dict[str, str]) -> str:
    lines = []
    for segment in segments:
        speaker = names.get(segment["speaker"], segment["speaker"])
        lines.append(f"[{format_clock(segment['start'])}] {speaker}: {segment['text']}")
    return "\n".join(lines) + ("\n" if lines else "")


def srt_text(segments: list[dict[str, Any]], names: dict[str, str]) -> str:
    blocks = []
    for index, segment in enumerate(segments, start=1):
        speaker = names.get(segment["speaker"], segment["speaker"])
        blocks.append(
            f"{index}\n{format_srt_time(segment['start'])} --> {format_srt_time(segment['end'])}\n"
            f"{speaker}: {segment['text']}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def vtt_text(segments: list[dict[str, Any]], names: dict[str, str]) -> str:
    blocks = ["WEBVTT"]
    for segment in segments:
        speaker = names.get(segment["speaker"], segment["speaker"])
        blocks.append(
            f"{format_vtt_time(segment['start'])} --> {format_vtt_time(segment['end'])}\n"
            f"<v {speaker}>{segment['text']}"
        )
    return "\n\n".join(blocks) + "\n"


def json_text(note: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in note.items()
        if key not in {"source_path", "normalized_path", "error"}
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def format_clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_vtt_time(seconds: float) -> str:
    return format_srt_time(seconds).replace(",", ".")
