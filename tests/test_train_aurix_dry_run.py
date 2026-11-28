"""Dry run of patches/7070/aurix-llm/train_aurix.py with the ML libraries stubbed out.

The script used to stop with NameError: GRAD_ACC is not defined (the setting is GRAD_ACCUM) right before training,
after the model had already loaded. This runs main() end to end against stubs, so a misspelled setting fails here
instead of on the GPU box.
"""
import ast
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

SCRIPT = Path(__file__).resolve().parent.parent / "patches" / "7070" / "aurix-llm" / "train_aurix.py"


def test_every_setting_used_in_main_is_defined():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    defined = {t.id for node in tree.body if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Name)}
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    used = {n.id for n in ast.walk(main) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id.isupper()}
    assert used <= defined, f"settings used but never defined: {sorted(used - defined)}"


class _FakeDataset:
    def __init__(self, rows):
        self.rows = rows
        self.column_names = list(rows[0].keys()) if rows else []

    @classmethod
    def from_list(cls, rows):
        return cls(rows)

    def map(self, fn, remove_columns=None):
        return _FakeDataset([fn(r) for r in self.rows])

    def train_test_split(self, test_size, seed):
        cut = max(1, int(len(self.rows) * test_size))
        return {"train": _FakeDataset(self.rows[cut:]), "test": _FakeDataset(self.rows[:cut])}

    def __len__(self):
        return len(self.rows)


def test_main_dry_run_reaches_training(tmp_path, monkeypatch):
    stubs = {name: MagicMock(name=name) for name in ("torch", "transformers", "peft", "trl")}
    stubs["datasets"] = types.SimpleNamespace(Dataset=_FakeDataset)
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location("train_aurix_under_test", SCRIPT)
    train = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(train)

    data = tmp_path / "data.jsonl"
    data.write_text("".join(json.dumps({"user": f"q{i}", "aurix": f"a{i}"}) + "\n" for i in range(20)))
    monkeypatch.setattr(train, "DATA_PATH", str(data))
    monkeypatch.setattr(train, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(train, "BASE_MODEL", str(tmp_path / "base"))

    train.main()

    args = stubs["transformers"].TrainingArguments.call_args.kwargs
    assert args["gradient_accumulation_steps"] == train.GRAD_ACCUM
    assert args["per_device_train_batch_size"] * args["gradient_accumulation_steps"] == 16
    stubs["trl"].SFTTrainer.return_value.train.assert_called_once()
