# Aurix LLM — Training Plan

## Direction: Distillation via Data Generation + QLoRA Fine-tuning

**Why not full pre-training?** Requires thousands of GPU hours on datacenter hardware.
**Why not pure distillation?** Complex, needs custom loss functions, marginal gains over data distillation.
**Why this approach?** Practical on 6GB VRAM. Uses the 9B teacher to generate high-quality
training data, then fine-tunes a small student with QLoRA. This is how most successful
"small but capable" models are built.

## Architecture

```
Teacher: qwen3.5:9b (Ollama on Steammachine)
    ↓ generates training data
Student: Qwen2.5-0.5B-Instruct (0.5B params)
    ↓ QLoRA fine-tuning (4-bit base + LoRA adapters)
Result: aurix-0.5b (Aurix personality, tool-aware, fast)
```

**Why 0.5B?**
- Fits in 6GB VRAM with room for training (needs ~4GB for QLoRA)
- Fast inference (~50 tokens/sec on 1660 SUPER)
- Good enough for Aurix's personality and tool-routing tasks
- Can scale to 1.5B later if 0.5B works well

## Training Data (4 categories)

### 1. Aurix Personality (~500 examples)
How Aurix communicates: direct, no fluff, terse but warm. Examples:
- User: "What's the weather?" → Aurix: direct answer, no "Great question!"
- User shares frustration → Aurix: acknowledges specifically, not generically

### 2. Tool Usage (~500 examples)
When and how to use Aurix's tools. Format:
- User request → Thought (which tool) → Tool call → Result → Response

### 3. Aurix Domain Knowledge (~300 examples)
- The 7070 setup, Steammachine, Tailscale network
- Patches, goals, routines
- "Where is X?" "How do I Y?" about the Aurix system

### 4. Task Patterns (~200 examples)
- Health check workflow
- Code audit workflow
- Git push via Steammachine workflow

**Total: ~1500 examples** (good for LoRA fine-tuning a 0.5B model)

## Data Generation Pipeline

1. **Seed prompts**: Hand-write 50 high-quality examples per category
2. **Expand with teacher**: Use qwen3.5:9b to generate variations (5x expansion)
3. **Filter**: Remove low-quality, deduplicate
4. **Format**: ChatML format for Qwen2.5-Instruct

## Training Config

```python
base_model = "Qwen/Qwen2.5-0.5B-Instruct"
quantization = "4-bit (NF4)"
lora_r = 16
lora_alpha = 32
lora_dropout = 0.05
target_modules = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
learning_rate = 2e-4
batch_size = 4
gradient_accumulation = 4  # effective batch 16
epochs = 3
max_seq_length = 2048
```

**VRAM estimate**: 0.5B in 4-bit ≈ 0.3GB + LoRA ≈ 0.1GB + optimizer ≈ 1GB + activations ≈ 1GB = **~2.5GB** ✅ Fits in 6GB

## Evaluation

- **Perplexity** on held-out Aurix conversations (lower = better)
- **Personality match**: Does it talk like Aurix? (human eval)
- **Tool routing accuracy**: Does it pick the right tool? (automated test)
- **Regression**: Doesn't break on general knowledge (MMLU-lite)

## Phases

1. ✅ Environment setup (PyTorch + CUDA on Python 3.12)
2. 🔄 Data generation (teacher generates, we filter)
3. ⏳ Training (QLoRA, ~2-4 hours on 1660 SUPER)
4. ⏳ Evaluation
5. ⏳ Export to Ollama (GGUF) for deployment
6. ⏳ Iterate (more data, 1.5B scale-up if promising)
