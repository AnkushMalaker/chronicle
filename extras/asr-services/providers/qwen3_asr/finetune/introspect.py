"""Dump Qwen3-ASR module structure to choose LoRA target modules (runs on the A100).

Prints the top-level layout of ``model.thinker`` and the unique nn.Linear *leaf names*,
split into "audio/encoder" vs "decoder/text" so we can scope the LoRA target regex to the
Qwen3 text decoder and keep it off the AuT audio encoder (mirrors the gemma4 approach of
adapting the text decoder only).

Loads on CPU (``device_map=None``, no .cuda()) so it can run while the GPU is busy.
"""

import argparse
import re

import torch
from qwen_asr import Qwen3ASRModel


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="Qwen/Qwen3-ASR-0.6B")
    args = p.parse_args()

    wrapper = Qwen3ASRModel.from_pretrained(
        args.model, dtype=torch.bfloat16, device_map=None
    )
    model = wrapper.model
    print("=== model class ===", model.__class__.__name__)
    print("=== thinker children ===")
    for n, _ in model.thinker.named_children():
        print("  ", n)

    # Collect every nn.Linear's full path; classify by which subtree it lives in.
    audio_kw = ("audio", "encoder", "conv", "merger", "proj_audio")
    lin_paths = []
    for name, mod in model.named_modules():
        if mod.__class__.__name__.endswith("Linear"):
            lin_paths.append(name)

    def is_audio(path: str) -> bool:
        return any(k in path.lower() for k in audio_kw)

    decoder = sorted({p.split(".")[-1] for p in lin_paths if not is_audio(p)})
    audio = sorted({p.split(".")[-1] for p in lin_paths if is_audio(p)})
    print("\n=== Linear leaf names: DECODER/text subtree ===", decoder)
    print("=== Linear leaf names: AUDIO/encoder subtree ===", audio)

    # Show a few representative full paths so the regex can be anchored correctly.
    print("\n=== sample decoder Linear paths ===")
    for p_ in [x for x in lin_paths if not is_audio(x)][:8]:
        print("  ", p_)
    print("=== sample audio Linear paths ===")
    for p_ in [x for x in lin_paths if is_audio(x)][:8]:
        print("  ", p_)

    # Candidate regex (q/k/v/o/gate/up/down on the decoder layers, excluding audio).
    cand = r"^(?!.*(audio|encoder)).*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)$"
    n_match = sum(1 for p_ in lin_paths if re.search(cand, p_))
    print(f"\n=== candidate regex matches {n_match} Linear modules ===\n  {cand}")


if __name__ == "__main__":
    main()
