from __future__ import annotations

import pytest
from app.core.constants import (
    ALLOWED_TRANSITIONS,
    OVERRIDE_TRANSITIONS,
    CandidateStatus,
    is_transition_allowed,
)

ALL_STATUSES = [status.value for status in CandidateStatus]

# Explicit expectation table, written independently of ALLOWED_TRANSITIONS.
EXPECTED = {
    ("received", "needs_review"): True,
    ("received", "needs_resubmission"): True,
    ("received", "forwarded"): True,
    ("received", "shortlisted"): False,
    ("received", "accepted"): False,
    ("needs_review", "received"): True,
    ("needs_review", "forwarded"): False,
    ("needs_resubmission", "received"): True,
    ("needs_resubmission", "needs_review"): False,
    ("forwarded", "shortlisted"): True,
    ("forwarded", "not_shortlisted"): True,
    ("forwarded", "needs_review"): True,
    ("forwarded", "received"): False,
    ("forwarded", "accepted"): False,
    ("shortlisted", "not_shortlisted"): True,
    ("shortlisted", "accepted"): True,
    ("shortlisted", "rejected"): True,
    ("shortlisted", "forwarded"): False,
    ("not_shortlisted", "shortlisted"): True,
    ("not_shortlisted", "rejected"): True,
    ("not_shortlisted", "accepted"): True,
    ("accepted", "received"): False,
    ("accepted", "rejected"): False,
    ("rejected", "accepted"): False,
    ("withdrawn", "received"): False,
    ("anonymised", "received"): False,
    ("deleted", "received"): False,
}


def test_expectation_table_matches_declared_allowed_map():
    allowed = {
        (old, new) for old in ALL_STATUSES for new in ALLOWED_TRANSITIONS[old]
    }
    declared = {pair for pair, ok in EXPECTED.items() if ok}
    assert declared == allowed


@pytest.mark.parametrize("old,new,expected", [(*pair, value) for pair, value in EXPECTED.items()])
def test_declared_transitions(old: str, new: str, expected: bool):
    assert is_transition_allowed(old, new) is expected


def test_no_self_transitions():
    for status in ALL_STATUSES:
        assert not is_transition_allowed(status, status)


@pytest.mark.parametrize("old", ALL_STATUSES)
def test_side_exits_from_every_live_state(old: str):
    assert is_transition_allowed(old, CandidateStatus.WITHDRAWN) is (
        old not in {"withdrawn", "anonymised", "deleted"}
    )
    assert is_transition_allowed(old, CandidateStatus.ANONYMISED) is (
        old not in {"anonymised", "deleted"}
    )
    assert is_transition_allowed(old, CandidateStatus.DELETED) is (old != "deleted")


def test_override_pairs_require_a_reason_by_definition():
    assert (CandidateStatus.SHORTLISTED, CandidateStatus.NOT_SHORTLISTED) in OVERRIDE_TRANSITIONS
    assert (CandidateStatus.NOT_SHORTLISTED, CandidateStatus.SHORTLISTED) in OVERRIDE_TRANSITIONS


def test_terminal_states_have_no_forward_transitions():
    for terminal in ["accepted", "rejected", "withdrawn", "anonymised", "deleted"]:
        forward = {
            new
            for new in ALLOWED_TRANSITIONS[terminal]
            if new not in {"withdrawn", "anonymised", "deleted"}
        }
        assert forward == set()
