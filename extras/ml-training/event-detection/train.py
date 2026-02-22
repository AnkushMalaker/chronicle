#!/usr/bin/env python3
# Event Detection Training - Fine-tune Whisper with LoRA
# ⚠️  NOT INTEGRATED WITH MAIN BACKEND - Use directly from CLI


import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Union

import evaluate
import librosa
import numpy as np
import torch
import tqdm
from datasets import Dataset
from transformers import (
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
    WhisperForConditionalGeneration,
)
from unsloth import FastModel, is_bf16_supported

# Global Constants
DEFAULT_MODEL_ID = "unsloth/whisper-large-v3"
DEFAULT_OUTPUT_DIR = "event_lora_adapter_unsloth"
DEFAULT_TRAIN_MANIFEST = "train.jsonl"
DEFAULT_TARGET_TOKEN = "EVENT_DETECTED"
DEFAULT_SOURCE_TAG = "<event>"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Train Whisper LoRA for Event Detection"
    )
    parser.add_argument(
        "--train_manifest",
        type=str,
        default=DEFAULT_TRAIN_MANIFEST,
        help="Path to training data jsonl",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory for adapter",
    )
    parser.add_argument(
        "--base_model", type=str, default=DEFAULT_MODEL_ID, help="Base Whisper model ID"
    )
    parser.add_argument(
        "--target_token",
        type=str,
        default=DEFAULT_TARGET_TOKEN,
        help="Token to emit for event",
    )
    parser.add_argument(
        "--source_tag",
        type=str,
        default=DEFAULT_SOURCE_TAG,
        help="Tag in text to replace",
    )
    return parser.parse_args()


def prepare_dataset(manifest_path: str, source_tag: str, target_token: str) -> Dataset:
    """Load and normalize training data from jsonl"""
    audio_paths = []
    texts = []

    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"{manifest_path} not found.")

    with open(manifest_path, "r") as f:
        for line in f:
            entry = json.loads(line)
            # Normalize text
            text = entry["text"].replace(source_tag, target_token)

            audio_paths.append(entry["audio"])
            texts.append(text)

            # Oversampling for target keyword (x5 as per spec)
            if target_token in text:
                for _ in range(5):
                    audio_paths.append(entry["audio"])
                    texts.append(text)

    print(f"Loaded {len(audio_paths)} training samples.")

    data = {"audio_path": audio_paths, "text": texts}

    return Dataset.from_dict(data)


@dataclass
class DataCollatorSpeechSeq2SeqWithPadding:
    processor: Any

    def __call__(
        self, features: List[Dict[str, Union[List[int], torch.Tensor]]]
    ) -> Dict[str, torch.Tensor]:
        input_features = [
            {"input_features": feature["input_features"]} for feature in features
        ]
        batch = self.processor.feature_extractor.pad(
            input_features, return_tensors="pt"
        )

        label_features = [{"input_ids": feature["labels"]} for feature in features]
        labels_batch = self.processor.tokenizer.pad(label_features, return_tensors="pt")

        labels = labels_batch["input_ids"].masked_fill(
            labels_batch.attention_mask.ne(1), -100
        )

        if (labels[:, 0] == self.processor.tokenizer.bos_token_id).all().cpu().item():
            labels = labels[:, 1:]

        batch["labels"] = labels

        return batch


def main():
    args = parse_args()

    print(f"Loading model with Unsloth: {args.base_model}")

    # Load model using Unsloth's FastModel
    model, tokenizer = FastModel.from_pretrained(
        model_name=args.base_model,
        dtype=None,  # Auto detection
        load_in_4bit=False,
        auto_model=WhisperForConditionalGeneration,
        whisper_language="English",
        whisper_task="transcribe",
    )

    # Apply LoRA adapters
    model = FastModel.get_peft_model(
        model,
        r=64,
        target_modules=["q_proj", "v_proj"],
        lora_alpha=64,
        lora_dropout=0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=3407,
        use_rslora=False,
        loftq_config=None,
        task_type=None,
    )

    # Configure generation settings
    model.generation_config.language = "<|en|>"
    model.generation_config.task = "transcribe"
    model.config.suppress_tokens = []
    model.generation_config.forced_decoder_ids = None

    # Load dataset
    dataset = prepare_dataset(args.train_manifest, args.source_tag, args.target_token)

    def formatting_prompts_func(example):
        """Process audio and text for training"""
        # Load audio file
        audio_array, sr = librosa.load(example["audio_path"], sr=16000)

        # Extract features
        features = tokenizer.feature_extractor(audio_array, sampling_rate=16000)

        # Tokenize text
        tokenized_text = tokenizer.tokenizer(example["text"])

        return {
            "input_features": features.input_features[0],
            "labels": tokenized_text.input_ids,
        }

    print("Processing dataset...")
    train_data = []
    for example in tqdm.tqdm(dataset, desc="Processing audio"):
        # Errors will bubble up as per spec
        result = formatting_prompts_func(example)
        train_data.append(result)

    print(f"Successfully processed {len(train_data)} samples")

    # Split into train/test
    split_idx = max(1, int(len(train_data) * 0.94))
    train_dataset = train_data[:split_idx]
    test_dataset = train_data[split_idx:]

    print(f"Train samples: {len(train_dataset)}, Test samples: {len(test_dataset)}")

    metric = evaluate.load("wer")

    def compute_metrics(pred):
        pred_logits = pred.predictions[0]
        label_ids = pred.label_ids

        label_ids[label_ids == -100] = tokenizer.pad_token_id

        pred_ids = np.argmax(pred_logits, axis=-1)

        pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
        label_str = tokenizer.batch_decode(label_ids, skip_special_tokens=True)

        wer = 100 * metric.compute(predictions=pred_str, references=label_str)

        return {"wer": wer}

    data_collator = DataCollatorSpeechSeq2SeqWithPadding(processor=tokenizer)

    # Setup trainer
    trainer = Seq2SeqTrainer(
        model=model,
        train_dataset=train_dataset,
        data_collator=data_collator,
        eval_dataset=test_dataset if len(test_dataset) > 0 else None,
        tokenizer=tokenizer.feature_extractor,
        compute_metrics=compute_metrics,
        args=Seq2SeqTrainingArguments(
            per_device_train_batch_size=1,
            gradient_accumulation_steps=4,
            warmup_steps=5,
            max_steps=200,  # Can be exposed as arg if needed, keeping simple for now
            learning_rate=1e-4,
            logging_steps=10,
            optim="adamw_8bit",
            fp16=not is_bf16_supported(),
            bf16=is_bf16_supported(),
            weight_decay=0.001,
            remove_unused_columns=False,
            lr_scheduler_type="linear",
            label_names=["labels"],
            eval_steps=20,
            eval_strategy="steps" if len(test_dataset) > 0 else "no",
            seed=3407,
            output_dir=args.output_dir,
            report_to="none",
        ),
    )

    print("Starting training...")
    trainer.train()

    # Save the model
    print(f"Saving adapter to {args.output_dir}")
    model.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)

    print("Training complete!")


if __name__ == "__main__":
    main()
