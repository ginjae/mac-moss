"""Measure MOSS progress without changing its generation or final transcript."""

from __future__ import annotations

import time
from types import MethodType
from typing import Any, Callable

from src.transcript import parse_transcript


class ModelProgress:
    def __init__(self, callback: Callable[[dict[str, Any]], None]):
        self.callback = callback
        self.started = time.monotonic()
        self.last_report = float("-inf")
        self.values: dict[str, Any] = {
            "stage": "encoding", "audio_seconds": 0.0, "audio_duration": 0.0,
            "prompt_processed": 0, "prompt_total": 0, "generation_tokens": 0,
        }

    def due(self) -> bool:
        return time.monotonic() - self.last_report >= 1

    def observe_text(self, text: str, *, partial: bool = True) -> None:
        duration = self.values["audio_duration"]
        for segment in parse_transcript(text, partial=partial):
            start, end = segment["start"], segment["end"]
            if 0 <= start <= end <= duration:
                self.values["audio_seconds"] = max(self.values["audio_seconds"], end)

    def report(self, *, force: bool = False, **values: Any) -> None:
        self.values.update(values)
        if force or self.due():
            now = time.monotonic()
            self.last_report = now
            self.callback({**self.values, "elapsed_seconds": round(now - self.started, 1)})


def generate_with_progress(
    model: Any, audio_path: str, callback: Callable[[dict[str, Any]], None],
    **generation_options: Any,
) -> Any:
    """Instrument the pinned mlx-audio MOSS path, under the server's model lock.

    The public generate method ignores progress callbacks. Scoped hooks expose
    its input preparation, GPU prefill callback, and generated tokens while
    preserving the original tokenizer, hotword handling, and segment parsing.
    """
    from mlx_audio.lm import generate as generation

    progress = ModelProgress(callback)
    original_prepare = model._prepare_generation_inputs
    original_step = generation.generate_step
    # Restore the instance attribute exactly; normally prepare lives on its class.
    had_prepare_override = "_prepare_generation_inputs" in model.__dict__
    prepare_override = model.__dict__.get("_prepare_generation_inputs")
    eos = model._eos_token_ids()
    tokens: list[int] = []

    def prepare(self: Any, *args: Any, **kwargs: Any) -> Any:
        progress.report(stage="encoding", force=True)
        result = original_prepare(*args, **kwargs)
        _, _, prompt_tokens, duration = result
        progress.report(
            stage="prefill", audio_duration=duration, prompt_total=prompt_tokens,
            force=True,
        )
        return result

    def generate_step(*args: Any, **kwargs: Any):
        original_callback = kwargs.get("prompt_progress_callback")

        def prefill(processed: int, total: int) -> None:
            if original_callback:
                original_callback(processed, total)
            progress.report(
                stage="decoding" if processed == total else "prefill",
                prompt_processed=processed, prompt_total=total,
                force=processed in (0, total),
            )

        kwargs["prompt_progress_callback"] = prefill
        for token, logprobs in original_step(*args, **kwargs):
            if int(token) not in eos:
                tokens.append(int(token))
                if progress.due():
                    # Decode the full prefix to preserve UTF-8 across token edges.
                    progress.observe_text(model._tokenizer.decode(tokens, skip_special_tokens=True))
                    progress.report(stage="decoding", generation_tokens=len(tokens), force=True)
            yield token, logprobs

    model._prepare_generation_inputs = MethodType(prepare, model)
    generation.generate_step = generate_step
    try:
        result = model.generate(audio_path, **generation_options)
        progress.observe_text(result.text, partial=False)
        progress.report(
            stage="complete", audio_seconds=progress.values["audio_duration"],
            generation_tokens=result.generation_tokens, force=True,
        )
        return result
    finally:
        generation.generate_step = original_step
        if had_prepare_override:
            model._prepare_generation_inputs = prepare_override
        else:
            delattr(model, "_prepare_generation_inputs")
