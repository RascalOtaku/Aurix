"""Structural tests for the triage prototype (no model calls)."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from triage import (
    _parse_yes_no,
    _parse_choice,
    _parse_score,
    _needs_human_guard,
    KINDS,
    ROUTES,
)


def test_parse_yes_no_exact():
    assert _parse_yes_no("YES") == ("YES", 1.0)
    assert _parse_yes_no("no") == ("NO", 1.0)


def test_parse_yes_no_fuzzy():
    d, c = _parse_yes_no("Yes.")
    assert d == "YES" and c < 1.0
    assert _parse_yes_no("maybe")[0] is None


def test_parse_choice_exact():
    assert _parse_choice("HEAVY") == ("HEAVY", 1.0)
    assert _parse_choice("human") == ("HUMAN", 1.0)
    assert _parse_choice("NEITHER") == ("NEITHER", 1.0)
    assert _parse_choice("both")[0] is None


def test_parse_score_exact():
    assert _parse_score("3") == ("3", 1.0)
    assert _parse_score("7")[0] is None
    assert _parse_score("high")[0] is None


def test_human_guard():
    assert _needs_human_guard("Please publish this post to the blog")
    assert _needs_human_guard("Send email to the client now")
    assert not _needs_human_guard("Summarize this status update")


def test_kinds_and_routes():
    assert set(KINDS) == {"yes_no", "choice", "score"}
    assert set(ROUTES) == {"local", "heavy", "human"}


def test_triage_rejects_bad_kind():
    import pytest
    from triage import triage
    with pytest.raises(ValueError):
        triage("hello", "bogus")
