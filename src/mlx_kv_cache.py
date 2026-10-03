"""Connect mlx-lm's quantized KV cache to the MOSS text decoder.

mlx-audio 0.5.7 creates floating-point caches and uses an attention dispatcher
that cannot consume quantized keys/values. Both need to change together; passing
``kv_bits`` to MOSS.generate alone is silently ignored by that version.
"""

from __future__ import annotations

from types import MethodType
from typing import Any


def configure_kv_cache(model: Any, bits: int = 8) -> None:
    """Use fresh 8-bit caches for every generation; 0 keeps the original cache."""
    if bits not in (0, 8):
        raise ValueError("MLX_KV_BITS는 8(양자화) 또는 0(비활성화)이어야 합니다.")
    if bits == 0:
        return

    from mlx_audio.stt.models.qwen3_asr import qwen3_asr
    from mlx_lm.models.base import scaled_dot_product_attention
    from mlx_lm.models.cache import QuantizedKVCache

    layers = model.layers
    if not layers or any(
        not isinstance(layer.self_attn, qwen3_asr.TextAttention)
        or layer.self_attn.head_dim % 64
        for layer in layers
    ):
        raise RuntimeError("이 모델의 텍스트 디코더는 8비트 KV 캐시를 지원하지 않습니다.")

    # MOSS imports this Qwen3 attention class. mlx-lm's dispatcher supports the
    # packed cache directly and retains the normal fast path for float caches.
    qwen3_asr.scaled_dot_product_attention = scaled_dot_product_attention

    def make_cache(self: Any) -> list[Any]:
        return [QuantizedKVCache(group_size=64, bits=bits) for _ in self.layers]

    model.make_cache = MethodType(make_cache, model)
