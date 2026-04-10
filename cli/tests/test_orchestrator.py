"""Tests for the orchestrator modules — covers schema validation, circuit breakers,
routing logic, JSON extraction, ledger summary, and path containment.

All tests are runnable without LLM API keys.

    cd cli && python -m pytest tests/ -v
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

# Import from the decomposed modules
from cli.schema import LedgerEntry, SignalEnvelope, VALID_ENTRY_TYPES, VALID_SIGNAL_TYPES
from cli.config import resolve_scope
from cli.parsing import extract_all_json, extract_json, classify_json_object
from cli.review import route_participants, infer_task_type
from cli.breakers import (
    detect_verdict_conflict,
    repetition_breaker_should_fire,
    record_token_usage,
    check_resource_breaker,
    _session_token_usage,
)
from cli.signals import validate_signal_lineage
import cli.ledger as ledger_mod
import cli.config as config_mod
import cli.signals as signals_mod


# ---------- Fixtures ----------


@pytest.fixture()
def tmp_ledger(tmp_path):
    """Provide a temp LEDGER_DIR and patch the module to use it."""
    ledger = tmp_path / "ledger" / "entries"
    ledger.mkdir(parents=True)
    original = ledger_mod.LEDGER_DIR
    ledger_mod.LEDGER_DIR = ledger
    # Also patch config_mod since some functions import LEDGER_DIR from there
    orig_config = config_mod.LEDGER_DIR
    config_mod.LEDGER_DIR = ledger
    yield ledger
    ledger_mod.LEDGER_DIR = original
    config_mod.LEDGER_DIR = orig_config


@pytest.fixture()
def tmp_signals(tmp_path):
    """Provide temp signal dirs and patch the module."""
    inbox = tmp_path / "signal" / "inbox"
    archive = tmp_path / "signal" / "archive"
    inbox.mkdir(parents=True)
    archive.mkdir(parents=True)
    orig_inbox_sig, orig_archive_sig = signals_mod.SIGNAL_INBOX, signals_mod.SIGNAL_ARCHIVE
    orig_inbox_cfg, orig_archive_cfg = config_mod.SIGNAL_INBOX, config_mod.SIGNAL_ARCHIVE
    signals_mod.SIGNAL_INBOX = inbox
    signals_mod.SIGNAL_ARCHIVE = archive
    config_mod.SIGNAL_INBOX = inbox
    config_mod.SIGNAL_ARCHIVE = archive
    yield inbox, archive
    signals_mod.SIGNAL_INBOX = orig_inbox_sig
    signals_mod.SIGNAL_ARCHIVE = orig_archive_sig
    config_mod.SIGNAL_INBOX = orig_inbox_cfg
    config_mod.SIGNAL_ARCHIVE = orig_archive_cfg


def _write_entry(ledger_dir: Path, entry: dict) -> None:
    fname = f"{entry['entry_id']}-{entry['type']}-{entry['author']}.json"
    (ledger_dir / fname).write_text(json.dumps(entry))


def _make_entry(**overrides) -> dict:
    base = {
        "entry_id": "001",
        "timestamp": "2026-01-01T00:00:00Z",
        "author": "test-agent",
        "type": "decision",
        "scope": "scope/test.py",
        "prior_entries": [],
        "summary": "test entry",
        "detail": "",
        "confidence": 0.9,
        "foundation_tag": ["truth"],
    }
    base.update(overrides)
    return base


# ---------- Schema validation ----------


class TestLedgerEntry:
    def test_valid_entry(self):
        e = LedgerEntry(**_make_entry())
        assert e.entry_id == "001"

    def test_invalid_type_rejected(self):
        with pytest.raises(Exception):
            LedgerEntry(**_make_entry(type="bogus"))

    def test_confidence_out_of_range(self):
        with pytest.raises(Exception):
            LedgerEntry(**_make_entry(confidence=1.5))
        with pytest.raises(Exception):
            LedgerEntry(**_make_entry(confidence=-0.1))

    def test_valid_verdict(self):
        e = LedgerEntry(**_make_entry(type="completion", verdict="approve"))
        assert e.verdict == "approve"

    def test_invalid_verdict_rejected(self):
        with pytest.raises(Exception):
            LedgerEntry(**_make_entry(type="completion", verdict="maybe"))

    def test_valid_role_action(self):
        e = LedgerEntry(**_make_entry(role_action="take_orchestrator"))
        assert e.role_action == "take_orchestrator"

    def test_invalid_role_action_rejected(self):
        with pytest.raises(Exception):
            LedgerEntry(**_make_entry(role_action="steal_orchestrator"))


# ---------- JSON extraction ----------


class TestExtractJson:
    def test_fenced_json(self):
        text = 'Some text\n```json\n{"entry_id": "001", "type": "decision"}\n```\nmore'
        result = extract_all_json(text)
        assert len(result) >= 1
        assert result[0]["entry_id"] == "001"

    def test_bare_json(self):
        text = 'prefix {"entry_id": "002"} suffix'
        result = extract_all_json(text)
        assert any(obj.get("entry_id") == "002" for obj in result)

    def test_classify_entry_vs_signal(self):
        entry_obj = {"entry_id": "001", "type": "decision"}
        signal_obj = {"signal_id": "sig-001", "type": "query"}
        assert classify_json_object(entry_obj) == "entry"
        assert classify_json_object(signal_obj) == "signal"


# ---------- Capability routing ----------


class TestRouteParticipants:
    DECLS = [
        {
            "identifier": "agent-code",
            "participation_mode": "active",
            "litellm_model": "x",
            "preferred_tasks": ["code_review"],
            "capability_envelope": {"code_review": 0.9},
        },
        {
            "identifier": "agent-write",
            "participation_mode": "active",
            "litellm_model": "y",
            "preferred_tasks": ["writing_review"],
            "capability_envelope": {"writing_review": 0.85},
        },
        {
            "identifier": "observer",
            "participation_mode": "observer",
            "litellm_model": None,
            "preferred_tasks": [],
        },
    ]

    def test_routes_to_matching_preferred_tasks(self):
        result = route_participants(self.DECLS, "code_review")
        assert [d["identifier"] for d in result] == ["agent-code"]

    def test_routes_writing_to_writing_agent(self):
        result = route_participants(self.DECLS, "writing_review")
        assert [d["identifier"] for d in result] == ["agent-write"]

    def test_falls_back_to_broadcast_on_unknown_type(self):
        result = route_participants(self.DECLS, "unknown_type")
        ids = [d["identifier"] for d in result]
        assert "agent-code" in ids and "agent-write" in ids

    def test_falls_back_to_broadcast_on_none(self):
        result = route_participants(self.DECLS, None)
        assert len(result) == 2  # excludes observer

    def test_excludes_observers(self):
        result = route_participants(self.DECLS, None)
        ids = [d["identifier"] for d in result]
        assert "observer" not in ids


class TestInferTaskType:
    def test_python(self):
        assert infer_task_type("scope/code/foo.py") == "code_review"

    def test_markdown(self):
        assert infer_task_type("docs/README.md") == "writing_review"

    def test_unknown_extension(self):
        assert infer_task_type("data/file.xyz") is None

    def test_no_extension(self):
        assert infer_task_type("Makefile") is None


# ---------- Circuit breakers ----------


class TestConflictDetection:
    def test_same_verdict_no_conflict(self):
        entries = [
            LedgerEntry(**_make_entry(entry_id="001", type="completion", verdict="approve")),
            LedgerEntry(**_make_entry(entry_id="002", type="completion", verdict="approve")),
        ]
        assert detect_verdict_conflict(entries) is False

    def test_different_verdicts_conflict(self):
        entries = [
            LedgerEntry(**_make_entry(entry_id="001", type="completion", verdict="approve")),
            LedgerEntry(**_make_entry(entry_id="002", type="completion", verdict="reject")),
        ]
        assert detect_verdict_conflict(entries) is True

    def test_no_judgment_excluded(self):
        entries = [
            LedgerEntry(**_make_entry(entry_id="001", type="completion", verdict="approve")),
            LedgerEntry(**_make_entry(entry_id="002", type="completion", verdict="no_judgment")),
        ]
        assert detect_verdict_conflict(entries) is False


class TestResourceBreaker:
    def setup_method(self):
        _session_token_usage.clear()

    def test_even_usage_no_fire(self):
        record_token_usage("a", 1000)
        record_token_usage("b", 1000)
        record_token_usage("c", 1000)
        result = check_resource_breaker(
            {"circuit_breakers": {"resource_multiplier": 2.0}}, "s", "h", None
        )
        assert result is None

    def test_skewed_usage_fires(self, tmp_ledger):
        record_token_usage("a", 100)
        record_token_usage("b", 100)
        record_token_usage("c", 20000)
        result = check_resource_breaker(
            {"circuit_breakers": {"resource_multiplier": 2.0}}, "s", "h", None
        )
        assert result is not None
        assert result.type == "failure"
        assert "balance" in result.foundation_tag

    def test_empty_usage_no_fire(self):
        result = check_resource_breaker(
            {"circuit_breakers": {"resource_multiplier": 2.0}}, "s", "h", None
        )
        assert result is None


class TestRepetitionBreaker:
    def test_fires_on_three_unresolved(self, tmp_ledger):
        for i in range(3):
            _write_entry(tmp_ledger, _make_entry(
                entry_id=f"{i+1:03d}", type="failure", author=f"agent-{i}",
            ))
        entries = ledger_mod.entries_for_scope("scope/test.py")
        assert repetition_breaker_should_fire(entries) is True

    def test_does_not_fire_with_repair(self, tmp_ledger):
        for i in range(3):
            _write_entry(tmp_ledger, _make_entry(
                entry_id=f"{i+1:03d}", type="failure", author=f"agent-{i}",
            ))
        # Add a repair linking to one of the failures
        _write_entry(tmp_ledger, _make_entry(
            entry_id="004", type="repair", author="arbiter",
            prior_entries=["001"],
        ))
        entries = ledger_mod.entries_for_scope("scope/test.py")
        # Only 2 unresolved now (002, 003)
        assert repetition_breaker_should_fire(entries) is False


# ---------- Signal lineage validation ----------


class TestSignalLineageValidation:
    def test_missing_lineage_detected(self, tmp_signals):
        env = SignalEnvelope(
            signal_id="sig-test", origin="a", destination="b",
            timestamp="2026-01-01T00:00:00Z", type="query",
            payload={}, context_summary="test", confidence=0.5,
            lineage=["sig-missing-1", "sig-missing-2"],
        )
        missing = validate_signal_lineage(env)
        assert len(missing) == 2

    def test_present_lineage_ok(self, tmp_signals):
        inbox, archive = tmp_signals
        (archive / "sig-exists.json").write_text("{}")
        env = SignalEnvelope(
            signal_id="sig-test", origin="a", destination="b",
            timestamp="2026-01-01T00:00:00Z", type="query",
            payload={}, context_summary="test", confidence=0.5,
            lineage=["sig-exists"],
        )
        missing = validate_signal_lineage(env)
        assert len(missing) == 0


# ---------- Ledger summary ----------


class TestLedgerSummary:
    def test_empty_ledger(self, tmp_ledger):
        result = ledger_mod.summarize_ledger()
        assert "empty" in result.lower()

    def test_failure_preserved_in_full(self, tmp_ledger):
        _write_entry(tmp_ledger, _make_entry(
            entry_id="001", type="failure", detail="important detail here",
        ))
        result = ledger_mod.summarize_ledger()
        assert "important detail here" in result
        assert "**failure**" in result

    def test_completion_compressed_without_active_scope(self, tmp_ledger):
        _write_entry(tmp_ledger, _make_entry(
            entry_id="001", type="completion", verdict="approve",
            detail="should not appear in summary",
        ))
        result = ledger_mod.summarize_ledger(active_scope=None)
        assert "should not appear in summary" not in result

    def test_completion_shown_with_active_scope(self, tmp_ledger):
        _write_entry(tmp_ledger, _make_entry(
            entry_id="001", type="completion", verdict="approve",
        ))
        result = ledger_mod.summarize_ledger(active_scope="scope/test.py")
        assert "**001**" in result  # bold = active scope entry
        assert "verdict=approve" in result


# ---------- Path containment ----------


class TestResolveScope:
    def test_valid_scope(self):
        # scope/code is a valid relative path within ROOT
        # We just test that it doesn't raise for a non-traversal path
        try:
            resolve_scope("scope/code/example_auth.py")
        except ValueError:
            pytest.fail("resolve_scope rejected a valid scope path")

    def test_traversal_rejected(self):
        with pytest.raises(ValueError, match="escapes"):
            resolve_scope("../../etc/passwd")

    def test_dot_dot_in_middle_rejected(self):
        with pytest.raises(ValueError, match="escapes"):
            resolve_scope("scope/../../etc/shadow")
