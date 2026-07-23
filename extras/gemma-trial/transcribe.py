"""Gemma 3n audio transcription script."""

import argparse
import time

import soundfile as sf
import torch
from transformers import AutoProcessor, AutoModelForImageTextToText


def transcribe(audio_path: str, prompt: str, model_id: str, max_tokens: int,
               chunk_sec: float = 30.0, max_duration: float = 0, output: str = ""):
    print(f"Loading model: {model_id}")
    t0 = time.time()

    processor = AutoProcessor.from_pretrained(model_id)
    model = AutoModelForImageTextToText.from_pretrained(
        model_id, dtype=torch.bfloat16, device_map="auto"
    )
    print(f"Model loaded in {time.time() - t0:.1f}s")

    # Load audio
    audio, sr = sf.read(audio_path)
    full_duration = len(audio) / sr
    print(f"Audio: {full_duration:.1f}s, sr={sr}")

    # Trim to max_duration if set
    if max_duration > 0:
        samples = int(max_duration * sr)
        audio = audio[:samples]
        print(f"Trimmed to {max_duration:.0f}s")

    duration = len(audio) / sr
    chunk_samples = int(chunk_sec * sr)
    chunks = [audio[i:i + chunk_samples] for i in range(0, len(audio), chunk_samples)]
    print(f"Processing {len(chunks)} chunks of {chunk_sec:.0f}s ({duration:.1f}s total)\n")

    all_text = []
    total_gen_tokens = 0
    total_gen_time = 0

    for i, chunk in enumerate(chunks):
        chunk_dur = len(chunk) / sr
        offset = i * chunk_sec
        print(f"--- Chunk {i+1}/{len(chunks)} [{offset:.0f}s - {offset+chunk_dur:.0f}s] ---")

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "audio", "audio": chunk},
                    {"type": "text", "text": prompt},
                ],
            }
        ]

        inputs = processor.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        )
        inputs = inputs.to(model.device, dtype=model.dtype)
        input_len = inputs["input_ids"].shape[1]

        t1 = time.time()
        outputs = model.generate(**inputs, max_new_tokens=max_tokens)
        gen_tokens = outputs.shape[1] - input_len
        gen_time = time.time() - t1
        total_gen_tokens += gen_tokens
        total_gen_time += gen_time

        result = processor.batch_decode(
            outputs[:, input_len:],
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        )[0]

        print(f"  {gen_tokens} tokens in {gen_time:.1f}s | {result[:100]}...")
        all_text.append(result)

    # Build transcript
    lines = []
    for i, text in enumerate(all_text):
        offset = i * chunk_sec
        lines.append(f"[{offset:.0f}s] {text}")
    transcript = "\n\n".join(lines)

    print("\n" + "=" * 80)
    print(f"TRANSCRIPT ({duration:.0f}s, {total_gen_tokens} tokens in {total_gen_time:.1f}s)")
    print("=" * 80)
    print(transcript)
    print("=" * 80)

    if output:
        with open(output, "w") as f:
            f.write(transcript + "\n")
        print(f"Saved to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Transcribe audio with Gemma 3n")
    parser.add_argument("audio", help="Path to audio file")
    parser.add_argument(
        "--prompt",
        default="Transcribe this audio verbatim with speaker labels. Format each speaker turn as [Speaker N]: text",
    )
    parser.add_argument("--model", default="google/gemma-3n-E4B-it")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--chunk", type=float, default=30.0, help="Chunk duration in seconds")
    parser.add_argument("--max-duration", type=float, default=0, help="Max audio duration to process (0=all)")
    parser.add_argument("-o", "--output", default="", help="Save transcript to file")
    args = parser.parse_args()

    transcribe(args.audio, args.prompt, args.model, args.max_tokens,
               args.chunk, args.max_duration, args.output)
