from __future__ import annotations

import json
import re
from typing import Any


TIMESTAMP = r"\d+(?:\.\d+)?"
SEGMENT_HEADER = rf"\[{TIMESTAMP}(?:-{TIMESTAMP})?\]\[S\d+\]"
SEGMENT_PATTERN = re.compile(
    rf"\[(?P<start>{TIMESTAMP})\]\[(?P<speaker>S\d+)\]"
    r"(?P<text>.*?)"
    rf"\[(?P<end>{TIMESTAMP})\]"
    rf"(?=\s*{SEGMENT_HEADER}|\s*$)",
    re.DOTALL,
)
RANGE_SEGMENT_PATTERN = re.compile(
    rf"\[(?P<start>{TIMESTAMP})-(?P<end>{TIMESTAMP})\]\[(?P<speaker>S\d+)\]"
    r"(?P<text>.*?)"
    rf"(?=\s*{SEGMENT_HEADER}|\s*$)",
    re.DOTALL,
)


def parse_transcript(raw: str, *, partial: bool = False) -> list[dict[str, Any]]:
    """Parse both ``[start][Sxx]text[end]`` and ``[start-end][Sxx]text``.

    While streaming, a range segment's text is complete only when the next
    segment header arrives. Its end time alone does not mean it is finished.
    """
    raw = raw.strip()
    matches = [
        (match, ranged)
        for pattern, ranged in ((SEGMENT_PATTERN, False), (RANGE_SEGMENT_PATTERN, True))
        for match in pattern.finditer(raw)
    ]
    matches.sort(key=lambda item: item[0].start())
    segments: list[dict[str, Any]] = []
    for match, ranged in matches:
        if partial and ranged and match.end() == len(raw):
            continue
        start, end = float(match["start"]), float(match["end"])
        if end < start:
            continue
        text = match["text"]
        if ranged:
            # MOSS sometimes appends a final timestamp even with range headers.
            text = re.sub(rf"\s*\[{TIMESTAMP}\]$", "", text.rstrip())
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        segments.append(
            {
                "id": len(segments),
                "start": start,
                "end": end,
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
