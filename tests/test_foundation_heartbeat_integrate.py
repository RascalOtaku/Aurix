"""The integrate lane's open ideas must reach the owner through heartbeat.waiting_for_you().

Covers the 2026-10-14 change: waiting_for_you() gained an 'integrate' section that lists
proposed ideas as adopt/skip lines. Regression risk: a renamed key or a changed open_ideas()
shape silently drops the section (the add() wrapper swallows exceptions), so assert the line.
"""
import os
from unittest import mock

import pytest

from src.foundation import heartbeat, integrate, repos


@pytest.fixture()
def lanes(tmp_path):
    """Point every lane's data at one throwaway project root."""
    with mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(tmp_path)}):
        yield tmp_path


def _write_plan(rid: str, repo: str, ideas: list) -> None:
    owner, name = repo.split("/")
    repos.save({"id": rid, "owner": owner, "repo": name, "created": 1.0, "status": "absorbed"})
    integrate._save_plan({"rid": rid, "repo": repo, "created": 1.0, "announced": True,
                          "status": "planned", "summary": "test plan", "ideas": ideas, "dropped": []})


def _idea(iid: str, status: str = "proposed") -> dict:
    return {"id": iid, "title": "Port the rate limiter", "kind": "upgrade",
            "target": "src/foundation/rate_limiter.py", "spec": "adopt the sliding window",
            "risk": "medium", "license_ok": True, "status": status}


def test_open_ideas_lists_only_proposed(lanes):
    _write_plan("r-test01", "acme/widget", [_idea("i-aaaaaa"), _idea("i-bbbbbb", "skipped")])
    out = integrate.open_ideas()
    assert [i["id"] for i in out] == ["i-aaaaaa"]
    assert out[0]["repo"] == "acme/widget"


def test_waiting_for_you_surfaces_integration_idea(lanes):
    _write_plan("r-test02", "acme/gadget", [_idea("i-cccccc")])
    msg = heartbeat.waiting_for_you()
    assert "🧩" in msg, msg
    assert "acme/gadget" in msg, msg
    assert "adopt i-cccccc" in msg, msg
    assert "skip i-cccccc" in msg, msg


def test_waiting_for_you_quiet_when_nothing_proposed(lanes):
    _write_plan("r-test03", "acme/quiet", [_idea("i-dddddd", "adopted")])
    msg = heartbeat.waiting_for_you()
    assert "🧩" not in msg, msg
