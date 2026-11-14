"""Hermetic unit test for the ChatRequest boundary contract
(app.contracts.ChatRequest).

A turn arrives as EITHER free text OR a chip tap — exactly one. The validator
enforces this so an ambiguous or empty request never reaches the agent. This is
a strict-schema invariant (CLAUDE.md #5): the contract defines behavior.

No DB, no LLM.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts import ChatRequest, ChipIntent


def test_both_message_and_chip_intent_rejected():
    """INVARIANT (boundary): supplying BOTH message and chip_intent is ambiguous
    and must raise — guards against a client sending conflicting inputs."""
    with pytest.raises(ValidationError):
        ChatRequest(pid=101, message="hello", chip_intent=ChipIntent.lab_trend)


def test_neither_message_nor_chip_intent_rejected():
    """INVARIANT (boundary): supplying NEITHER input is an empty turn and must
    raise — guards against a no-op request reaching the agent."""
    with pytest.raises(ValidationError):
        ChatRequest(pid=101)


def test_blank_whitespace_message_treated_as_absent():
    """INVARIANT (boundary): a whitespace-only message is not a real turn, so
    with no chip it must raise — guards against blank-string bypass of the
    exactly-one rule."""
    with pytest.raises(ValidationError):
        ChatRequest(pid=101, message="   ")


def test_message_only_ok():
    """INVARIANT (positive path): a free-text turn alone is valid — guards
    against an over-strict validator rejecting the core interface."""
    req = ChatRequest(pid=101, message="Brief me on this patient.")
    assert req.message and req.chip_intent is None


def test_chip_only_ok():
    """INVARIANT (positive path): a chip tap alone is valid — guards the
    suggested-reply accelerant path."""
    req = ChatRequest(pid=101, chip_intent=ChipIntent.explain_care_transition)
    assert req.chip_intent is ChipIntent.explain_care_transition and req.message is None
