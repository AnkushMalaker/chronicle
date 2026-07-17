"""Train in-process and eval WITHOUT save/reload, to localize the loss-0-but-wrong bug.

If in-process teacher-forced loss is ~0 AND generation reproduces -> the bug is in
save/reload (adapter not persisted/loaded correctly).
If in-process loss is ~0 but generation still fails -> teacher-forcing/label issue.
If in-process loss is also high -> the Trainer's reported step loss was the illusion.
"""

import torch
from data import DEFAULT_PROMPT, CosheSample7Dataset, Gemma4AudioCollator
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForMultimodalLM,
    AutoProcessor,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
)

MODEL = "google/gemma-4-E2B-it"
LORA_TARGETS = (
    r".*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)"
)

processor = AutoProcessor.from_pretrained(MODEL)
bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16,
    llm_int8_skip_modules=[
        "model.audio_tower",
        "model.vision_tower",
        "model.embed_audio",
        "model.embed_vision",
        "lm_head",
    ],
)
model = AutoModelForMultimodalLM.from_pretrained(
    MODEL,
    quantization_config=bnb,
    dtype=torch.bfloat16,
    device_map="auto",
    attn_implementation="eager",
)
model.config.use_cache = False
for p in model.parameters():
    p.requires_grad = False
model.gradient_checkpointing_enable(
    gradient_checkpointing_kwargs={"use_reentrant": False}
)
model.enable_input_require_grads()
model = get_peft_model(
    model,
    LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.0,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=LORA_TARGETS,
    ),
)
model.print_trainable_parameters()

ds = CosheSample7Dataset(
    "/data/coshe-eval/sample7", max_seconds=30.0, target_max_chars=540
)
collator = Gemma4AudioCollator(processor)

trainer = Trainer(
    model=model,
    args=TrainingArguments(
        output_dir="/train/out/_inproc",
        per_device_train_batch_size=1,
        num_train_epochs=30,
        learning_rate=2e-4,
        lr_scheduler_type="constant",
        bf16=True,
        logging_steps=20,
        save_strategy="no",
        report_to=[],
        remove_unused_columns=False,
        gradient_checkpointing=False,
    ),
    train_dataset=ds,
    data_collator=collator,
)
trainer.train()

# ---- eval in the SAME process, no save/reload ----
model.eval()
print("\n===== IN-PROCESS EVAL (no save/reload) =====", flush=True)
for item in ds:
    b = collator([item])
    b = {k: v.to(model.device) for k, v in b.items()}
    with torch.inference_mode():
        o = model(**b)
        ng = model(**{k: v for k, v in b.items() if k != "labels"})
    lg = ng.logits[0]
    lb = b["labels"][0]
    p = lg[:-1].argmax(-1)
    g = lb[1:]
    s = g != -100
    n = int(s.sum())
    m = int((p[s] == g[s]).sum())
    print(
        f"  {item['name']:16s} model.loss={float(o.loss):.4f}  argmax-match={m}/{n} "
        f"({100*m/max(n,1):.1f}%)",
        flush=True,
    )

# generation on sample 0 using the SAME collator prompt path
model.config.use_cache = True
item = ds[0]
ptext = processor.apply_chat_template(
    [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": DEFAULT_PROMPT},
                {"type": "audio", "audio": item["audio"]},
            ],
        }
    ],
    tokenize=False,
    add_generation_prompt=True,
)
inp = processor(text=[ptext], audio=[item["audio"]], return_tensors="pt").to(
    model.device
)
inlen = inp["input_ids"].shape[-1]
with torch.inference_mode():
    out = model.generate(**inp, max_new_tokens=256, do_sample=False)
gen = processor.decode(out[0][inlen:], skip_special_tokens=True)
print(f"\n  TARGET[:300]: {item['target'][:300]}", flush=True)
print(f"  GEN[:300]   : {gen[:300]}", flush=True)
