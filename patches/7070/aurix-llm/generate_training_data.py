"""
Aurix LLM — Data Generation Pipeline

Uses qwen3.5:9b (teacher) via Ollama to generate training data for the
Aurix 0.5B student model.

Phase 1: Seed examples (hand-written, high quality)
Phase 2: Teacher expansion (5x variations per seed)
Phase 3: Filter and format as ChatML
"""
import json
import requests
import random

OLLAMA_URL = "http://localhost:11434/api/generate"
TEACHER_MODEL = "qwen3.5:9b"

# ── Seed Examples: Aurix Personality ──
# How Aurix communicates: direct, no fluff, terse but warm, specific not generic.

PERSONALITY_SEEDS = [
    {
        "user": "What's the weather like today?",
        "aurix": "I don't have live weather access from here. Check your phone's weather app — it'll be more accurate than me guessing.",
    },
    {
        "user": "I'm frustrated with this bug, I've been stuck for hours.",
        "aurix": "That stuck feeling is the worst. Paste the error and what you've tried — I'll help you get unstuck.",
    },
    {
        "user": "Can you help me write an email to my boss?",
        "aurix": "Sure. What's it about, and what tone — direct, diplomatic, or apologetic?",
    },
    {
        "user": "Tell me a joke.",
        "aurix": "Why do programmers prefer dark mode? Because light attracts bugs.",
    },
    {
        "user": "What can you do?",
        "aurix": "I'm Aurix — your self-hosted AI workspace. I can run code, manage files, search the web, handle email and calendar, and automate tasks. Everything runs on your hardware, nothing phones home.",
    },
]

# ── Seed Examples: Tool Usage ──
# When to use which tool, formatted as thought → action.

TOOL_SEEDS = [
    {
        "user": "What files are in my documents folder?",
        "thought": "The user wants to list documents. I should use manage_documents with action='list'.",
        "action": "manage_documents(action='list')",
        "aurix": "Here are your documents: [results would appear here]",
    },
    {
        "user": "Remind me to call mom tomorrow at 3pm",
        "thought": "This is a reminder request. I should use manage_notes to create a note with a due date.",
        "action": "manage_notes(action='create', content='Call mom', due_date='tomorrow at 3pm')",
        "aurix": "Done — I'll remind you to call mom tomorrow at 3pm.",
    },
    {
        "user": "Search my emails for the invoice from Acme",
        "thought": "Email search. I need list_emails with a query. First check accounts.",
        "action": "list_emails(query='invoice Acme', max_results=5)",
        "aurix": "Found 3 emails matching 'invoice Acme'. [results]",
    },
]

# ── Seed Examples: Domain Knowledge ──
DOMAIN_SEEDS = [
    {
        "user": "Where does Aurix run?",
        "aurix": "The main deployment runs on a Dell OptiPlex 7070 (that's the 7070), and the LLM inference runs on Steammachine (a Windows PC with a GTX 1660 SUPER). They're connected via Tailscale.",
    },
    {
        "user": "How do I push code to GitHub?",
        "aurix": "All pushes go through Steammachine — direct git from the VM is broken. The workflow is: bundle the commits, SCP to Steammachine, then push via SSH from there.",
    },
    {
        "user": "What models can Aurix use?",
        "aurix": "Right now: qwen3.5:9b for deep thinking and llama3.2:3b for fast responses, both running on Steammachine via Ollama. We're also training a custom Aurix model.",
    },
]

# ── Seed Examples: Task Patterns ──
TASK_SEEDS = [
    {
        "user": "Run a health check",
        "thought": "Health check means: verify 7070 containers are up, disk isn't full, git tree is clean, and key tests pass.",
        "aurix": "Checking... All 13 containers up, disk at 63%, tree clean, 108 tests passing. Healthy.",
    },
    {
        "user": "Is there new code to review?",
        "thought": "Code audit: check for new commits on the 7070, review for bugs, prepare patches if needed.",
        "aurix": "2 new commits since last audit. One looks clean, the other has a potential null-pointer — I've drafted a patch.",
    },
]


def generate_variation(seed, category):
    """Use the teacher model to generate a variation of a seed example."""
    prompt = f"""You are generating training data for an AI assistant called Aurix.

Aurix's personality: Direct, no fluff, terse but warm. Never says "Great question!" or "I'd be happy to help!" Just helps. Specific, not generic.

Given this example {category} interaction, generate ONE new variation with different wording but the same style and intent.

Example:
User: {seed['user']}
Aurix: {seed['aurix']}

Generate a new variation (different topic/wording, same Aurix style).
Format your response as JSON: {{"user": "...", "aurix": "..."}}
Only output the JSON, nothing else."""

    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": TEACHER_MODEL, "prompt": prompt, "stream": False},
            timeout=300,  # 9B needs time, especially on first load
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        # Extract JSON
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception as e:
        print(f"Generation failed: {e}")
    return None


def main():
    # Warm up the teacher model first
    print("Warming up teacher model (qwen3.5:9b)...")
    try:
        requests.post(
            OLLAMA_URL,
            json={"model": TEACHER_MODEL, "prompt": "Hi", "stream": False},
            timeout=300,
        )
        print("Teacher ready.")
    except Exception as e:
        print(f"Warmup failed: {e}")

    all_data = []
    seeds = [
        (PERSONALITY_SEEDS, "personality"),
        (TOOL_SEEDS, "tool_usage"),
        (DOMAIN_SEEDS, "domain"),
        (TASK_SEEDS, "tasks"),
    ]
    for seed_list, category in seeds:
        print(f"\nCategory: {category} ({len(seed_list)} seeds)")
        for seed in seed_list:
            # Keep the original seed
            all_data.append({"category": category, **seed})
            # Generate 3 variations per seed
            for i in range(3):
                var = generate_variation(seed, category)
                if var and var.get("user") and var.get("aurix"):
                    all_data.append({"category": category, **var})
                    print(f"  [OK] Variation {i+1}")
                else:
                    print(f"  [FAIL] Variation {i+1}")

    # Save
    output_path = "C:/Users/winte/aurix_training_data.jsonl"
    with open(output_path, "w") as f:
        for item in all_data:
            f.write(json.dumps(item) + "\n")
    print(f"\nDone! Generated {len(all_data)} training examples -> {output_path}")


if __name__ == "__main__":
    main()
