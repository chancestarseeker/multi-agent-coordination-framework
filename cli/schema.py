"""Pydantic models and enums for the Multi-Agent Coordination Framework.

This module is a leaf — it imports nothing from other cli modules.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator  # noqa: F401 — re-exported


VALID_ENTRY_TYPES = {
    "decision",
    "attempt",
    "completion",
    "failure",
    "repair",
    "boundary_change",
    "intention_shift",
}

VALID_VERDICTS = {
    "approve",
    "approve_with_conditions",
    "reject",
    "escalate",
    "no_judgment",
}

VALID_ROLE_ACTIONS = {
    "offer_orchestrator",
    "accept_orchestrator",
    "refuse_orchestrator",
    "rotate_orchestrator",
    "stepdown_orchestrator",
}

VALID_SIGNAL_TYPES = {
    "handoff",
    "state_update",
    "boundary_change",
    "query",
    "acknowledgment",
    "error",
}


class SignalEnvelope(BaseModel):
    """Mirrors the Signal Envelope schema in fnd-preamble.md.

    Signals are out-of-band participant-to-participant messages, distinct
    from ledger entries. The orchestrator processes signals via per-type
    handlers; some handlers write ledger entries (e.g., a `query`
    recommending a participant becomes a `decision` entry per
    fnd-participants.md -> Discovery), but the signal envelope itself lives
    in signal/inbox/ -> signal/archive/.
    """

    signal_id: str
    origin: str
    destination: str
    timestamp: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    context_summary: str
    confidence: float
    lineage: list[str] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def _signal_type_in_enum(cls, v: str) -> str:
        if v not in VALID_SIGNAL_TYPES:
            raise ValueError(f"signal type must be one of {sorted(VALID_SIGNAL_TYPES)}")
        return v

    @field_validator("confidence")
    @classmethod
    def _signal_confidence_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")
        return v


class LedgerEntry(BaseModel):
    """Schema mirrors fnd-ledger.md -> Entry Schema.

    Local extension: `verdict` is added as an optional structured field so
    convergent reviewers can express compatible/incompatible judgments
    mechanically. The Conflict circuit breaker compares verdicts on
    completion entries that share a scope.
    """

    entry_id: str
    timestamp: str
    author: str
    type: str
    scope: str
    prior_entries: list[str] = Field(default_factory=list)
    summary: str
    detail: str = ""
    confidence: float
    foundation_tag: list[str] = Field(default_factory=list)
    verdict: str | None = None
    role_action: str | None = None

    @field_validator("type")
    @classmethod
    def _type_in_enum(cls, v: str) -> str:
        if v not in VALID_ENTRY_TYPES:
            raise ValueError(f"type must be one of {sorted(VALID_ENTRY_TYPES)}")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")
        return v

    @field_validator("verdict")
    @classmethod
    def _verdict_in_enum(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in VALID_VERDICTS:
            raise ValueError(f"verdict must be one of {sorted(VALID_VERDICTS)}")
        return v

    @field_validator("role_action")
    @classmethod
    def _role_action_in_enum(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in VALID_ROLE_ACTIONS:
            raise ValueError(f"role_action must be one of {sorted(VALID_ROLE_ACTIONS)}")
        return v
