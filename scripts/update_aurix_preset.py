#!/usr/bin/env python3
"""Replace the stale system prompt in the live `aurix` preset (data/presets.json).

The old prompt said "You are AURIX on Precision 3431. Execute everything with the
bash tool directly ... never ask for confirmation. Run first, explain after." That
is wrong on three counts now: the app runs in a Linux container on the 7070 (the
3431 is a Windows GPU box, hence the agent's `wsl df -h`), it made the agent run
shell commands for every question, and it contradicts the Telegram approval gate,
so a denied action was simply retried.

Only `presets["aurix"]["system_prompt"]` changes; everything else is preserved and
the original file is backed up first. DRY RUN by default.

    python3 scripts/update_aurix_preset.py                  # show the diff
    python3 scripts/update_aurix_preset.py --apply          # write it
    python3 scripts/update_aurix_preset.py --apply path/to/presets.json
"""
import difflib
import json
import shutil
import sys
import time
from pathlib import Path

NEW_PROMPT = (
    "You are AURIX, Rascal's autonomous second brain. You run inside the Odysseus app in a "
    "Linux Docker container on the OptiPlex 7070 (this is not Windows and there is no WSL). "
    "Model inference runs on the Precision 3431 GPU workstation.\n\n"
    "Answer questions directly from your own knowledge. Use the bash tool only when a task "
    "genuinely needs a command run, never just to look around.\n\n"
    "Shell commands from Telegram need Rascal's approval. The approval prompt IS the "
    "confirmation, so never ask 'should I go ahead?' or 'shall I proceed?' first: just call "
    "the tool, and Rascal will be asked once. If an action is denied, expires, or "
    "fails, do NOT retry it or try a variant of it: tell Rascal what happened and what you "
    "would do instead, then stop. Run at most one command per turn unless asked for more.\n\n"
    "Rules: ALPACA_LIVE=false. Never read or print credentials, tokens, or .env files."
)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    path = Path(args[0]) if args else Path(__file__).resolve().parent.parent / "data" / "presets.json"

    presets = json.loads(path.read_text(encoding="utf-8"))
    if "aurix" not in presets:
        sys.exit(f"no 'aurix' preset in {path}; nothing to do")
    old = presets["aurix"].get("system_prompt", "")

    print(f"file: {path}")
    if old == NEW_PROMPT:
        print("already up to date - nothing to change")
        return
    print("--- system_prompt diff (old -> new) ---")
    for line in difflib.unified_diff(old.splitlines(), NEW_PROMPT.splitlines(),
                                     "old", "new", lineterm="", n=0):
        print(line)

    if not apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to make this change.")
        return

    backup = path.with_name(f"{path.name}.bak_{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    presets["aurix"]["system_prompt"] = NEW_PROMPT
    path.write_text(json.dumps(presets, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\napplied. backup: {backup}")
    print("Restart the app to be safe:  docker compose restart odysseus")


if __name__ == "__main__":
    main()
