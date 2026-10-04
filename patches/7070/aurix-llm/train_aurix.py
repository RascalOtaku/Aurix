"""
Aurix LLM — QLoRA Fine-tuning

Fine-tunes Qwen2.5-0.5B-Instruct on Aurix-specific training data.
Uses 4-bit quantization + LoRA for VRAM efficiency on GTX 1660 SUPER (6GB).

Usage:
    py -3.12 train_aurix.py

Requirements:
    pip install torch transformers peft trl datasets accelerate bitsandbytes
"""
import json
import torch
from datasets import Dataset
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    TrainingArguments,
    BitsAndBytesConfig,
)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from trl import SFTTrainer

# ── Config ──
BASE_MODEL = "C:/Users/winte/aurix-llm/base-model"
DATA_PATH = "C:/Users/winte/aurix_training_data.jsonl"
OUTPUT_DIR = "C:/Users/winte/aurix-llm/aurix-0.5b"

# QLoRA config
LORA_R = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]

# Training hyperparameters
LEARNING_RATE = 2e-4
BATCH_SIZE = 4
GRAD_ACCUM = 4  # effective batch = 16
EPOCHS = 3
MAX_SEQ_LEN = 2048


def format_chatml(example):
    """Format training example as ChatML for Qwen2.5-Instruct."""
    messages = [
        {"role": "system", "content": "You are Aurix, a self-hosted AI assistant. You are direct, concise, and helpful. You never use filler phrases like 'Great question!' or 'I'd be happy to help!' You just help."},
        {"role": "user", "content": example["user"]},
        {"role": "assistant", "content": example["aurix"]},
    ]
    # Manual ChatML formatting (Qwen2.5 style)
    text = ""
    for m in messages:
        text += f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n"
    return {"text": text}


def main():
    print("=" * 60)
    print("Aurix LLM — QLoRA Fine-tuning")
    print("=" * 60)

    # ── Load tokenizer ──
    print("\n[1/5] Loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, trust_remote_code=True)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    # ── Load model with 4-bit quantization ──
    print("[2/5] Loading base model (4-bit)...")
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=True,
    )
    model = prepare_model_for_kbit_training(model)

    # ── Apply LoRA ──
    print("[3/5] Applying LoRA adapters...")
    lora_config = LoraConfig(
        r=LORA_R,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        target_modules=TARGET_MODULES,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    # ── Load and format dataset ──
    print("[4/5] Loading training data...")
    with open(DATA_PATH) as f:
        raw_data = [json.loads(line) for line in f]
    print(f"  Loaded {len(raw_data)} examples")
    dataset = Dataset.from_list(raw_data)
    dataset = dataset.map(format_chatml, remove_columns=dataset.column_names)
    # 90/10 train/eval split
    split = dataset.train_test_split(test_size=0.1, seed=42)
    train_ds, eval_ds = split["train"], split["test"]
    print(f"  Train: {len(train_ds)}, Eval: {len(eval_ds)}")

    # ── Training ──
    print("[5/5] Starting training...")
    training_args = TrainingArguments(
        output_dir=OUTPUT_DIR,
        num_train_epochs=EPOCHS,
        per_device_train_batch_size=BATCH_SIZE,
        gradient_accumulation_steps=GRAD_ACC,
        learning_rate=LEARNING_RATE,
        fp16=True,
        logging_steps=10,
        save_steps=100,
        eval_strategy="steps",
        eval_steps=50,
        save_total_limit=3,
        load_best_model_at_end=True,
        optim="paged_adamw_8bit",
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        report_to="none",
    )
    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        tokenizer=tokenizer,
        max_seq_length=MAX_SEQ_LEN,
        dataset_text_field="text",
        packing=False,
    )
    trainer.train()

    # ── Save ──
    print("\nSaving LoRA adapters...")
    model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)
    print(f"\n✅ Done! Model saved to {OUTPUT_DIR}")
    print("\nNext: Merge LoRA into base and export to Ollama with:")
    print("  1. Merge: see merge_lora.py")
    print("  2. Convert to GGUF and create Ollama Modelfile")


if __name__ == "__main__":
    main()
