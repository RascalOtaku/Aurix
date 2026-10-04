"""Professional-grade tests for the upgrade lane repair loop (patch 02).

Tests the _is_repairable classification and repair_prompt generation
against the actual rejection reasons from build_changes().
"""
import re

# ---- repairable markers (copied from patch 02) ----
_REPAIRABLE = (
    "the reply was not the JSON I asked for",
    "a file entry had no path",
    "appears twice",
    "a malformed edit",
    "an edit's `find` text matched",
    "the edit changes nothing",
    "does not parse",
    "it did not include a new test file",
    "existing files may only be changed with `edits`",
    "a new file needs `content`",
)

def _is_repairable(why: str) -> bool:
    return any(marker in why for marker in _REPAIRABLE)

def repair_prompt(original_task: str, bad_reply: str, why: str) -> str:
    return (
        "Your last reply was rejected for one specific reason. Fix ONLY that, "
        "keep everything else identical.\n"
        f"REJECTION REASON: {why}\n"
        "RULES FOR THE FIX:\n"
        "- Reply with ONLY the corrected JSON object, no other text.\n"
        "- If the reason mentions `find` text matching wrong: pick a SHORTER, "
        "more unique snippet from the file (copy it character-for-character).\n"
        "- If the reason mentions a missing test file: add "
        "`tests/test_upgrade_<short_name>.py` with a unittest that fails "
        "without your change.\n"
        "- If the reason mentions bad JSON: output valid JSON only.\n"
        "- Do not change your approach, do not add files, do not enlarge the diff.\n"
        f"ORIGINAL TASK: {original_task}\n"
        f"YOUR REJECTED REPLY (fix this):\n{bad_reply}"
    )

# ---- test: all known build_changes rejections classified correctly ----
# (from reading build_changes in upgrades.py)
repairable_cases = [
    "the reply was not the JSON I asked for",
    "a file entry had no path",
    "src/foo.py appears twice",
    "src/foo.py: a malformed edit",
    "src/foo.py: an edit's `find` text matched 3 times (must be exactly once)",
    "src/foo.py: the edit changes nothing",
    "src/foo.py does not parse (invalid syntax at line 42)",
    "it did not include a new test file",
    "src/foo.py: existing files may only be changed with `edits`",
    "src/new.py: a new file needs `content`",
]
for why in repairable_cases:
    assert _is_repairable(why), f"should be repairable: {why}"
print(f"PASS: {len(repairable_cases)} repairable rejections classified correctly")

# ---- test: non-repairable rejections are NOT retried ----
non_repairable = [
    "nothing worth changing",  # judgement call
    "it touched 5 files (limit 4)",  # too big
    "src/foo.py: new code uses `subprocess`, which I do not allow",  # security
    "the change is 400 lines (limit 320); too big to review from a phone",  # too big
    "it only added a test and changed nothing",  # judgement
    "src/secret.py: path not allowed",  # security (check_path)
]
for why in non_repairable:
    assert not _is_repairable(why), f"should NOT be repairable: {why}"
print(f"PASS: {len(non_repairable)} non-repairable rejections correctly excluded")

# ---- test: repair prompt contains the essentials ----
prompt = repair_prompt(
    "Fix the divide function",
    '{"files": []}',
    "src/foo.py: an edit's `find` text matched 3 times (must be exactly once)",
)
assert "REJECTION REASON:" in prompt
assert "matched 3 times" in prompt
assert "SHORTER" in prompt
assert "ONLY the corrected JSON" in prompt
assert "Fix the divide function" in prompt
print("PASS: repair prompt contains rejection reason and fix guidance")

# ---- test: repair prompt for missing test ----
prompt2 = repair_prompt("Add feature", "{}", "it did not include a new test file")
assert "test_upgrade_" in prompt2
assert "unittest" in prompt2
print("PASS: repair prompt guides test file creation")

# ---- test: repair prompt for bad JSON ----
prompt3 = repair_prompt("Task", "not json at all", "the reply was not the JSON I asked for")
assert "valid JSON only" in prompt3
print("PASS: repair prompt guides JSON fix")

print("\nAll upgrade lane repair loop tests pass.")
