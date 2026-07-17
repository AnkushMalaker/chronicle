"""Monkeypatch for the gemma4 use_cache prefill bug (transformers 5.5.0).

Bug: `create_causal_mask_mapping` only applies the audio/vision BIDIRECTIONAL
attention mask (`or_mask_function`) on the "first iteration" (prefill). It infers
prefill via:

    past_key_values is None or not past_key_values.is_initialized or pixel_values is not None

But a freshly-created DynamicCache reports `is_initialized=True` *before any token
is cached*, so on the prefill forward (with use_cache=True / generate) this is
False → the audio soft tokens are masked purely causally instead of bidirectionally
→ the audio conditioning is wrong → logits collapse toward the base model
(measured: loss 4.49 with cache vs 0.10 without on a fully-overfit adapter).

Fix: detect prefill by `get_seq_length() == 0` (no tokens cached yet) instead of
`is_initialized`. This makes generation correct AND fast (KV cache usable), turning
a ~100s/clip no-cache eval into normal cached generation.

Usage: `import gemma4_cache_patch; gemma4_cache_patch.apply()` BEFORE loading the model.
"""

import transformers.models.gemma4.modeling_gemma4 as _g4

_orig_create_causal_mask_mapping = _g4.create_causal_mask_mapping


def _patched_create_causal_mask_mapping(
    config,
    inputs_embeds,
    attention_mask,
    past_key_values,
    position_ids,
    mm_token_type_ids=None,
    pixel_values=None,
    is_training=False,
    is_first_iteration=None,
    **kwargs,
):
    if is_first_iteration is None:
        if past_key_values is None:
            is_first_iteration = True
        else:
            try:
                seqlen = past_key_values.get_seq_length()
            except Exception:
                seqlen = 0
            is_first_iteration = (seqlen == 0) or (pixel_values is not None)
    return _orig_create_causal_mask_mapping(
        config,
        inputs_embeds,
        attention_mask,
        past_key_values,
        position_ids,
        mm_token_type_ids=mm_token_type_ids,
        pixel_values=pixel_values,
        is_training=is_training,
        is_first_iteration=is_first_iteration,
        **kwargs,
    )


def apply():
    _g4.create_causal_mask_mapping = _patched_create_causal_mask_mapping
    return True
