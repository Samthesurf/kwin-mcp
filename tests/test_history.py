"""Tests for kwin-mcp Computer History.

Mirrors the behavioral contract Cua Driver's computer-history tests assert
(libs/cua-driver/tests/computer_history_*.rs): history is off by default, the
status contract, the query contract (``metadata_only`` / ``model_context_
disclosure``), the hard privacy boundary (no screenshots, typed text, clipboard,
tool args/results, a11y trees, titles, urls, paths), encryption at rest (no
plaintext on disk), and bounded query filters. These run with no desktop and no
real KWin; they exercise the engine directly.

The history engine is process-global, so each test resets it via the public
control surface.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from kwin_bridge import history

ROOT = tempfile.mkdtemp(prefix="kwin-hist-test-")


@pytest.fixture(autouse=True)
def _reset_history():
    # ensure a clean slate before and after every test
    try:
        history.control("delete")
    except Exception:
        pass
    yield
    try:
        history.control("delete")
    except Exception:
        pass


# ── Cua's assert_ready contract ────────────────────────────────────────────────
def test_status_contract_when_ready():
    s = history.status()
    assert s["supported"] is True
    assert s["admitted"] is True
    assert s["enabled"] is False  # off by default
    assert s["paused"] is False
    assert s["encrypted"] is False  # no key until enabled
    assert s["health"] == "disabled"
    assert s["dropped_events"] == 0
    assert s["retention_days"] == 7
    assert s["quota_bytes"] == 100 * 1024 * 1024
    assert s["metadata_only"] is True
    assert s["model_context_disclosure"] is True


def test_enable_reports_ready():
    history.control("enable", root=ROOT)
    s = history.status()
    assert s["enabled"] is True
    assert s["encrypted"] is True
    assert s["profile"] == "kwin-mcp-history-v1"
    assert s["health"] == "ready"
    assert s["dropped_events"] == 0


# ── Cua's query contract (metadata_only / model_context_disclosure) ───────────
def test_query_metadata_only_and_disclosure():
    history.control("enable", root=ROOT)
    history.record_action_completed("click", route="synthetic_events", effect="confirmed")
    history.drain()

    resp = history.query(limit=200)
    assert resp["metadata_only"] is True
    assert resp["model_context_disclosure"] is True
    # we recorded nothing readable beyond the action_completed envelope
    assert any(e["type"] == "kwin-mcp.history.action_completed.v0"
               for e in resp["events"])
    # access record is appended but never returned
    assert all(e["type"] != "kwin-mcp.history.access.v0" for e in resp["events"])


def test_query_bounds_and_ordering():
    history.control("enable", root=ROOT)
    for i in range(5):
        history.record_action_completed("click", route="synthetic_events",
                                         effect="confirmed")
    history.drain()

    # newest `limit` in ascending order
    resp = history.query(limit=3)
    seqs = [e["data"]["sequence"] for e in resp["events"]]
    assert seqs == sorted(seqs)
    assert len(resp["events"]) == 3
    assert seqs[-1] == max(seqs)

    # since_sequence inclusive lower bound
    resp2 = history.query(limit=200, since_sequence=2)
    assert all(e["data"]["sequence"] >= 2 for e in resp2["events"])

    # until_sequence inclusive upper bound
    resp3 = history.query(limit=200, until_sequence=3)
    assert all(e["data"]["sequence"] <= 3 for e in resp3["events"])


def test_query_limit_clamped():
    history.control("enable", root=ROOT)
    for _ in range(10):
        history.record_action_completed("click", route="synthetic_events",
                                         effect="confirmed")
    history.drain()
    resp = history.query(limit=9999)  # above MAX_QUERY_LIMIT
    # 10 action_completed events + the enable control event recorded in this session
    assert len(resp["events"]) == 11
    assert sum(1 for e in resp["events"]
               if e["type"] == "kwin-mcp.history.action_completed.v0") == 10
    # clamp to the cap
    for _ in range(250):
        history.record_action_completed("click", route="synthetic_events",
                                         effect="confirmed")
    history.drain()
    resp2 = history.query(limit=9999)
    assert len(resp2["events"]) == history.MAX_QUERY_LIMIT


# ── Cua's assert_no_private_fields privacy boundary (the hard contract) ────────
FORBIDDEN = {"screenshot", "screenshot_png_b64", "typed_text", "clipboard",
             "raw_arguments", "raw_results", "accessibility_tree", "path",
             "title", "url", "diagnostic"}


def test_privacy_boundary_forbids_private_fields():
    for key in FORBIDDEN:
        bad = {key: "leak"}
        with pytest.raises(history.HistoryError):
            history._assert_no_private_fields(bad)
        nested = {"outer": {key: "leak"}, "list": [{key: "x"}]}
        with pytest.raises(history.HistoryError):
            history._assert_no_private_fields(nested)


def test_action_completed_payload_is_metadata_only():
    history.control("enable", root=ROOT)
    history.record_action_completed(
        "click", route="synthetic_events", effect="confirmed",
        app_name="org.kde.konsole")
    history.drain()
    resp = history.query(limit=10)
    for ev in resp["events"]:
        # recursively forbid any private field anywhere in any event
        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    assert k not in FORBIDDEN, f"private field {k!r} leaked"
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)
        walk(ev)
    # app_name is recorded only as a non-sensitive display class
    completed = next(e for e in resp["events"]
                     if e["type"] == "kwin-mcp.history.action_completed.v0")
    assert completed["data"]["payload"]["application"] == {"display_name": "org.kde.konsole"}
    assert "title" not in completed["data"]["payload"]
    assert "path" not in completed["data"]["payload"]


# ── Encryption at rest: no plaintext on disk ───────────────────────────────────
def test_storage_is_encrypted_no_plaintext():
    history.control("enable", root=ROOT)
    history.record_action_completed("click", route="synthetic_events",
                                    effect="confirmed")
    import time
    time.sleep(0.4)
    chunk = history._chunk_path(ROOT, history._STATE.session_id)
    with open(chunk, "rb") as fh:
        raw = fh.read()
    # raw file is base64 lines; the schema id must never appear in plaintext
    assert b"urn:kwin-mcp:schema:history-event:v0" not in raw
    assert b"action_completed" not in raw
    # but we can round-trip decrypt via the public query
    resp = history.query(limit=10)
    assert any(e["type"] == "kwin-mcp.history.action_completed.v0"
               for e in resp["events"])


def test_query_without_key_returns_empty():
    # never enabled -> no key -> no events
    resp = history.query(limit=50)
    assert resp["events"] == []


# ── Control lifecycle: enable preserves across pause; delete purges ────────────
def test_pause_resume_preserves_store():
    history.control("enable", root=ROOT)
    history.record_action_completed("click", route="synthetic_events",
                                    effect="confirmed")
    history.control("pause")
    assert history.status()["paused"] is True
    # existing history still queryable while paused
    assert len(history.query(limit=10)["events"]) >= 1
    history.control("resume")
    assert history.status()["paused"] is False


def test_delete_purges_ciphertext_and_key():
    history.control("enable", root=ROOT)
    history.record_action_completed("click", route="synthetic_events",
                                    effect="confirmed")
    import time
    time.sleep(0.3)
    chunk = history._chunk_path(ROOT, history._STATE.session_id)
    assert os.path.exists(chunk)
    history.control("delete")
    assert history.status()["enabled"] is False
    assert history.status()["encrypted"] is False
    # store erased
    assert not os.path.exists(chunk)


# ── Decorator preserves signature (FastMCP schema integrity) ───────────────────
def test_record_decorator_preserves_signature():
    import inspect

    @history.record("click", route="synthetic_events")
    def click(x: int = 0, y: int = 0, button: str = "left") -> dict:
        """Click helper."""
        return {"ok": True, "x": x, "y": y}

    sig = inspect.signature(click)
    assert list(sig.parameters) == ["x", "y", "button"]
    assert str(sig.return_annotation) == "dict"
    # docstring preserved (FastMCP uses it for the tool description)
    assert click.__doc__ == "Click helper."


def test_record_decorator_records_around_action():
    history.control("enable", root=ROOT)

    @history.record("type_text", route="trusted_input")
    def type_text(text: str) -> dict:
        return {"ok": True, "typed": len(text)}

    type_text("hello")
    import time
    time.sleep(0.3)
    history.drain()
    resp = history.query(limit=50)
    kinds = [e["data"]["payload"].get("kind") for e in resp["events"]]
    assert "action_started" in kinds
    assert "action_completed" in kinds
    completed = next(e for e in resp["events"]
                     if e["data"]["payload"].get("kind") == "action_completed")
    assert completed["data"]["payload"]["effect"] == "confirmed"
    assert completed["data"]["payload"]["route"] == "trusted_input"


def test_record_decorator_records_failure_effect():
    history.control("enable", root=ROOT)

    @history.record("close_window", route="system_api")
    def close_window(window_id: str) -> dict:
        return {"error": "no such window"}

    close_window("{deadbeef}")
    import time
    time.sleep(0.3)
    history.drain()
    completed = next(e for e in history.query(limit=50)["events"]
                     if e["data"]["payload"].get("kind") == "action_completed")
    assert completed["data"]["payload"]["effect"] == "failed"
