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

# Configuration
MODEL_ID = "unsloth/whisper-large-v3"
OUTPUT_DIR = "sneeze_lora_adapter_unsloth"
TRAIN_MANIFEST = "train.jsonl"


def prepare_dataset():
    """Load training data from jsonl"""
    audio_paths = []
    texts = []

    # Check if file exists
    if not os.path.exists(TRAIN_MANIFEST):
        raise FileNotFoundError(f"{TRAIN_MANIFEST} not found.")

    with open(TRAIN_MANIFEST, "r") as f:
        for line in f:
            try:
                entry = json.loads(line)
                # Normalize text
                text = entry["text"].replace("<sneeze>", "SNEEZE")

                audio_paths.append(entry["audio"])
                texts.append(text)

                # Simple oversampling for target keyword
                if "SNEEZE" in text:
                    for _ in range(5):
                        audio_paths.append(entry["audio"])
                        texts.append(text)
            except Exception as e:
                print(f"Skipping bad line: {e}")

    print(f"Loaded {len(audio_paths)} training samples.")

    data = {"audio_path": audio_paths, "text": texts}

    return Dataset.from_dict(data)


def main():
    print(f"Loading model with Unsloth: {MODEL_ID}")

    # Load model using Unsloth's FastModel
    model, tokenizer = FastModel.from_pretrained(
        model_name=MODEL_ID,
        dtype=None,  # Auto detection
        load_in_4bit=False,  # Set to True for 4bit quantization (lower memory)
        auto_model=WhisperForConditionalGeneration,
        whisper_language="English",
        whisper_task="transcribe",
        # token = "hf_...",  # Use if needed for gated models
    )

    # Apply LoRA adapters using Unsloth (only updates 1-10% of parameters)
    model = FastModel.get_peft_model(
        model,
        r=64,  # Suggested: 8, 16, 32, 64, 128
        target_modules=["q_proj", "v_proj"],
        lora_alpha=64,
        lora_dropout=0,  # 0 is optimized
        bias="none",  # "none" is optimized
        use_gradient_checkpointing="unsloth",  # 30% less VRAM, fits 2x larger batch sizes
        random_state=3407,
        use_rslora=False,
        loftq_config=None,
        task_type=None,  # MUST be None for Whisper
    )

    # Configure generation settings
    model.generation_config.language = "<|en|>"
    model.generation_config.task = "transcribe"
    model.config.suppress_tokens = []
    model.generation_config.forced_decoder_ids = None

    # Load dataset
    dataset = prepare_dataset()

    def formatting_prompts_func(example):
        """Process audio and text for training"""
        try:
            # Load audio file
            audio_array, sr = librosa.load(example["audio_path"], sr=16000)
        except Exception as e:
            print(f"Error loading {example['audio_path']}: {e}")
            return None

        # Extract features using tokenizer's feature extractor
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
        result = formatting_prompts_func(example)
        if result is not None:
            train_data.append(result)

    print(f"Successfully processed {len(train_data)} samples")

    # Split into train/test
    split_idx = max(1, int(len(train_data) * 0.94))
    train_dataset = train_data[:split_idx]
    test_dataset = train_data[split_idx:]

    print(f"Train samples: {len(train_dataset)}, Test samples: {len(test_dataset)}")

    # Setup WER metric for evaluation
    metric = evaluate.load("wer")

    def compute_metrics(pred):
        pred_logits = pred.predictions[0]
        label_ids = pred.label_ids

        # Replace -100 with pad_token_id
        label_ids[label_ids == -100] = tokenizer.pad_token_id

        pred_ids = np.argmax(pred_logits, axis=-1)

        pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
        label_str = tokenizer.batch_decode(label_ids, skip_special_tokens=True)

        wer = 100 * metric.compute(predictions=pred_str, references=label_str)

        return {"wer": wer}

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
            labels_batch = self.processor.tokenizer.pad(
                label_features, return_tensors="pt"
            )

            labels = labels_batch["input_ids"].masked_fill(
                labels_batch.attention_mask.ne(1), -100
            )

            if (
                (labels[:, 0] == self.processor.tokenizer.bos_token_id)
                .all()
                .cpu()
                .item()
            ):
                labels = labels[:, 1:]

            batch["labels"] = labels

            return batch

    data_collator = DataCollatorSpeechSeq2SeqWithPadding(processor=tokenizer)

    # Show memory stats before training
    if torch.cuda.is_available():
        gpu_stats = torch.cuda.get_device_properties(0)
        start_gpu_memory = round(
            torch.cuda.max_memory_reserved() / 1024 / 1024 / 1024, 3
        )
        max_memory = round(gpu_stats.total_memory / 1024 / 1024 / 1024, 3)
        print(f"GPU = {gpu_stats.name}. Max memory = {max_memory} GB.")
        print(f"{start_gpu_memory} GB of memory reserved.")

    # Setup trainer with Seq2SeqTrainer
    trainer = Seq2SeqTrainer(
        model=model,
        train_dataset=train_dataset,
        data_collator=data_collator,
        eval_dataset=test_dataset if len(test_dataset) > 0 else None,
        tokenizer=tokenizer.feature_extractor,
        compute_metrics=compute_metrics,
        args=Seq2SeqTrainingArguments(
            # predict_with_generate=True,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=4,
            warmup_steps=5,
            # num_train_epochs=1,  # Set for full training run
            max_steps=200,
            learning_rate=1e-4,
            logging_steps=10,
            optim="adamw_8bit",
            fp16=not is_bf16_supported(),
            bf16=is_bf16_supported(),
            weight_decay=0.001,
            remove_unused_columns=False,  # Required for PEFT
            lr_scheduler_type="linear",
            label_names=["labels"],
            eval_steps=20,
            eval_strategy="steps" if len(test_dataset) > 0 else "no",
            seed=3407,
            output_dir=OUTPUT_DIR,
            report_to="none",
        ),
    )

    print("Starting training...")
    trainer_stats = trainer.train()

    # Show final memory stats
    if torch.cuda.is_available():
        used_memory = round(torch.cuda.max_memory_reserved() / 1024 / 1024 / 1024, 3)
        used_memory_for_lora = round(used_memory - start_gpu_memory, 3)
        used_percentage = round(used_memory / max_memory * 100, 3)
        lora_percentage = round(used_memory_for_lora / max_memory * 100, 3)
        print(f"{trainer_stats.metrics['train_runtime']} seconds used for training.")
        print(
            f"{round(trainer_stats.metrics['train_runtime']/60, 2)} minutes used for training."
        )
        print(f"Peak reserved memory = {used_memory} GB.")
        print(f"Peak reserved memory for training = {used_memory_for_lora} GB.")
        print(f"Peak reserved memory % of max memory = {used_percentage} %.")
        print(
            f"Peak reserved memory for training % of max memory = {lora_percentage} %."
        )

    # Save the model
    print(f"Saving adapter to {OUTPUT_DIR}")
    model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)

    print("Training complete!")


def run_inference(audio_file: str, model_path: str = OUTPUT_DIR):
    """Run inference with the trained model"""
    from transformers import pipeline

    print(f"Loading model from {model_path}")

    # Load the fine-tuned model
    model, tokenizer = FastModel.from_pretrained(
        model_name=model_path,
        dtype=None,
        load_in_4bit=False,
        auto_model=WhisperForConditionalGeneration,
    )

    # Set model to inference mode
    FastModel.for_inference(model)
    model.eval()

    # Create pipeline
    whisper = pipeline(
        "automatic-speech-recognition",
        model=model,
        tokenizer=tokenizer.tokenizer,
        feature_extractor=tokenizer.feature_extractor,
        processor=tokenizer,
        return_language=True,
        torch_dtype=torch.float16,
    )

    # Transcribe
    result = whisper(audio_file)
    print(f"Transcription: {result['text']}")
    return result


if __name__ == "__main__":
    main()
