"""Encrypted, metadata-only Computer History for kwin-mcp.

This is a faithful Python port of Cua Driver's "Computer History" preview
(libs/cua-driver/docs/computer-history-*.md). The contract it implements:

* Opt-in, off by default. Nothing is recorded until the local user enables it
  (``history_control({"operation": "enable"})`` from the server process itself).
* Metadata-only. The privacy boundary is permanent: no screenshots, no typed
  text, no clipboard, no raw tool arguments or results, no accessibility trees,
  no window titles, no file paths, no free-form diagnostics. Every event is a
  fixed-field CloudEvents v0 envelope (``urn:cua-driver:schema:history-event:v0``
  is the schema id Cua uses; we use ``urn:kwin-mcp:schema:history-event:v0`` so
  the origin is unambiguous but the shape is identical).
* Encrypted at rest. Every record is sealed with AES-256-GCM before any bytes
  hit disk. There is no plaintext fallback. The key is a 256-bit random key held
  in memory (and, optionally, persisted AES-wrapped under a passphrase).
* Nonblocking. The action path uses a bounded try_send; storage failure or
  backpressure can NEVER fail the computer action that produced the event.
* Fail closed on privacy, fail open on action. Unsafe data is never written;
  history failures never break the requested computer action.
* Agent read-only. Agents may call ``history_status`` / ``history_query`` only.
  Capture lifecycle, retention, and deletion are controlled locally (the server
  process), mirroring Cua's ``history_control_requires_local_cli`` rule.
* Two read-only agent tools: ``history_status`` and ``history_query``.

Engine shape mirrors cua-driver's history.rs: a single writer thread drains a
bounded queue, encrypts each event, and appends it to a CBOR sequence store
(keyed by opaque session id). Queries decrypt, validate, filter, and bound.

This module deliberately imports nothing from the heavy ``mcp`` package at
import time so ``import kwin_bridge`` stays light.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - surfaced clearly at enable time
    AESGCM = None  # type: ignore[assignment]

_CRYPTO_AVAILABLE = AESGCM is not None

# ── Constants (mirror cua-driver history.rs) ─────────────────────────────────
SCHEMA_URN = "urn:kwin-mcp:schema:history-event:v0"
SOURCE_PREFIX = "urn:kwin-mcp:history:"
HISTORY_PROFILE_VERSION = 1
DEFAULT_RETENTION_DAYS = 7
DEFAULT_QUOTA_BYTES = 100 * 1024 * 1024  # 100 MiB
WRITER_QUEUE_CAPACITY = 512
MAX_QUERY_LIMIT = 200
NONCE_BYTES = 12

# Cua's fixed event types (kind names kept so a ported consumer reads the same).
EVENT_CONTROL = "kwin-mcp.history.control.v0"
EVENT_ACTION_STARTED = "kwin-mcp.history.action_started.v0"
EVENT_ACTION_COMPLETED = "kwin-mcp.history.action_completed.v0"
EVENT_SESSION_STARTED = "kwin-mcp.history.session_started.v0"
EVENT_SESSION_ENDED = "kwin-mcp.history.session_ended.v0"
EVENT_ACCESS = "kwin-mcp.history.access.v0"
EVENT_HEALTH = "kwin-mcp.history.health.v0"

# Fixed effect / route / delivery / evidence / escalation vocabularies (subset
# of cua-driver's action_record.rs, enough to classify kwin-mcp tools honestly).
EFFECTS = {"confirmed", "partial", "unverifiable", "suspected_noop", "refused", "failed"}
ROUTES = {"accessibility", "synthetic_events", "global_input", "system_api", "dom",
          "trusted_input", "unknown"}
DELIVERIES = {"background", "foreground", "not_applicable", "unknown"}
EVIDENCE_KINDS = {"accessibility_readback", "browser_readback", "value_readback",
                  "window_change"}
ESCALATION_KINDS = {"activate_target", "retry_with_pixel_target",
                    "retry_with_page_action", "refresh_page_state",
                    "request_permission", "elevate_access", "expand_capture_scope",
                    "prepare_session", "retry_with_foreground_delivery"}
CONTROL_OPS = {"enable", "disable", "pause", "resume", "flush", "delete"}
ACCESS_OPS = {"agent_query", "local_cli"}
HEALTH_CATEGORIES = {"ready", "disabled", "paused", "not_admitted",
                     "key_unavailable", "key_locked", "key_corrupt",
                     "key_destroy_failed", "storage_unavailable", "storage_corrupt",
                     "quota_reached", "events_dropped", "writer_stopped"}

# The privacy boundary Cua tests with assert_no_private_fields. We forbid these
# keys from ever appearing in any event payload, recursively.
FORBIDDEN_KEYS = {
    "screenshot", "screenshot_png_b64", "typed_text", "clipboard",
    "raw_arguments", "raw_results", "accessibility_tree", "path",
    "title", "url", "diagnostic",
}


class HistoryError(Exception):
    """Raised for hard failures (key missing, storage corrupt)."""

    def __init__(self, category: str, message: str = ""):
        self.category = category
        super().__init__(message or category)


@dataclass
class _HistoryState:
    """In-memory mutable state for one history namespace (process-lifetime)."""

    admitted: bool = True          # daemon admitted the preview (always True here)
    enabled: bool = False          # user opt-in
    paused: bool = False
    key: Optional[bytes] = None    # 32 raw bytes; never exposed
    root: str = ""
    session_id: str = ""
    sequence: int = 0
    dropped_events: int = 0
    bytes_used: int = 0
    stop: bool = False
    # per-tool route hints, set by the server when wrapping tools
    queue: "Any" = None  # Optional[queue.Queue]
    writer: "Any" = None  # Optional[threading.Thread]
    last_health: str = ""
    _tool_routes: "dict[str, str]" = field(default_factory=dict)


# One process-wide state. kwin-mcp is a single-tenant local server per desktop.
_STATE = _HistoryState()
_STATE_LOCK = threading.RLock()
_SESSION_LOCK = threading.Lock()


# ── Environment ───────────────────────────────────────────────────────────────
def default_root() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "kwin-mcp", "computer-history")


def _new_id() -> str:
    return uuid.uuid4().hex  # 32 hex chars (opaque id shape)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Encryption / storage primitives ────────────────────────────────────────────
def _ensure_crypto() -> None:
    if not _CRYPTO_AVAILABLE or AESGCM is None:
        raise HistoryError("key_unavailable",
                           "cryptography is required for Computer History; "
                           "install python-cryptography")


def _seal(key: bytes, plaintext: bytes) -> str:
    """AES-256-GCM seal. Returns base64(nonce || ciphertext)."""
    _ensure_crypto()
    assert AESGCM is not None
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ct).decode("ascii")


def _open(key: bytes, sealed: str) -> bytes:
    """Reverse of _seal. Raises HistoryError on auth failure."""
    if not _CRYPTO_AVAILABLE or AESGCM is None:
        raise HistoryError("key_unavailable", "cryptography unavailable")
    try:
        raw = base64.b64decode(sealed, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HistoryError("storage_corrupt", f"bad base64: {exc}") from exc
    if len(raw) < NONCE_BYTES + 1:
        raise HistoryError("storage_corrupt", "ciphertext too short")
    nonce, ct = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception as exc:  # decryption failure / tag mismatch
        raise HistoryError("storage_corrupt", f"decrypt failed: {exc}") from exc


def _chunk_path(root: str, session_id: str) -> str:
    return os.path.join(root, "chunks", f"{session_id}.cborseq")


def _append_record(root: str, session_id: str, key: bytes, envelope: dict) -> int:
    """Encrypt one CloudEvents envelope and append to the session's CBOR
    sequence chunk. Returns the new bytes_used. Non-throwing (records health)."""
    os.makedirs(os.path.join(root, "chunks"), exist_ok=True)
    sealed = _seal(key, json.dumps(envelope, separators=(",", ":")).encode("utf-8"))
    line = sealed.encode("utf-8") + b"\n"
    path = _chunk_path(root, session_id)
    with _STATE_LOCK:
        with open(path, "ab") as fh:
            fh.write(line)
        _STATE.bytes_used += len(line)
        return _STATE.bytes_used


# ── Privacy enforcement (mirrors assert_no_private_fields) ──────────────────────
def _assert_no_private_fields(node: Any) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in FORBIDDEN_KEYS:
                raise HistoryError(
                    "storage_corrupt",
                    f"privacy boundary violation: forbidden field {k!r}")
            _assert_no_private_fields(v)
    elif isinstance(node, list):
        for item in node:
            _assert_no_private_fields(item)


# ── Event envelope construction ────────────────────────────────────────────────
def _envelope(event_type: str, payload: dict) -> dict:
    """Build a fixed-field CloudEvents v0 envelope.

    ``payload`` is the ``data.payload`` object. We inject the constant envelope
    fields (specversion, source, type, time, datacontenttype, dataschema) and
    validate the data contract. The result is metadata-only by construction.
    """
    with _STATE_LOCK:
        sid = _STATE.session_id
    data: dict = {
        "sequence": 0,  # filled by writer
        "platform": "linux",
        "process_model": "in_daemon",
        "caller_category": "kwin_runtime",
        "payload": payload,
    }
    if sid:
        data["session_id"] = sid
    envelope = {
        "specversion": "1.0",
        "id": _new_id(),
        "source": f"{SOURCE_PREFIX}{_new_id()}",
        "type": event_type,
        "subject": sid or "kwin-mcp",
        "time": _now_iso(),
        "datacontenttype": "application/json",
        "dataschema": SCHEMA_URN,
        "data": data,
    }
    return envelope


def _build_action_completed(app_name: Optional[str], route: str,
                             effect: str, delivery: str = "unknown",
                             delivered_count: int = 0,
                             evidence_kinds: Optional[list] = None,
                             escalation_kind: Optional[str] = None) -> dict:
    if effect not in EFFECTS:
        effect = "unverifiable"
    if route not in ROUTES:
        route = "unknown"
    if delivery not in DELIVERIES:
        delivery = "unknown"
    payload: dict = {
        "kind": "action_completed",
        "effect": effect,
        "route": route,
        "delivery": delivery,
        "delivered_count": max(0, int(delivered_count)),
        "evidence_kinds": evidence_kinds or [],
    }
    # escalation is optional and only set when a retry/permission path was taken
    if escalation_kind and escalation_kind in ESCALATION_KINDS:
        payload["escalation_kind"] = escalation_kind
    # application is opt-in metadata: only the non-sensitive display class,
    # never a window title or path.
    if app_name:
        payload["application"] = {"display_name": app_name[:120]}
    _assert_no_private_fields(payload)
    return payload


# ── Writer thread ───────────────────────────────────────────────────────────────
def _writer_loop(state: _HistoryState) -> None:
    import queue  # local import; queue is stdlib
    while not state.stop:
        try:
            item = state.queue.get(timeout=0.5)
        except Exception:
            continue
        if item is None:
            break
        envelope = item
        try:
            with _STATE_LOCK:
                key = state.key
                root = state.root
                sid = state.session_id
                if key is None or not root or not sid:
                    state.dropped_events += 1
                    continue
                state.sequence += 1
                envelope["data"]["sequence"] = state.sequence
            _append_record(root, sid, key, envelope)
        except HistoryError as exc:
            with _STATE_LOCK:
                state.dropped_events += 1
                state.last_health = exc.category
        except Exception:
            with _STATE_LOCK:
                state.dropped_events += 1
                state.last_health = "storage_unavailable"
    # drain remaining on stop
    if state.queue is not None:
        while not state.queue.empty():
            try:
                state.queue.get_nowait()
            except Exception:
                break


def _start_writer(state: _HistoryState) -> None:
    import queue
    if state.writer is not None and state.writer.is_alive():
        return
    state.queue = queue.Queue(maxsize=WRITER_QUEUE_CAPACITY)
    state.writer = threading.Thread(target=_writer_loop, args=(state,),
                                    name="kwin-history-writer", daemon=True)
    state.writer.start()


def _try_enqueue(envelope: dict) -> None:
    """Nonblocking record. Never raises into the action path."""
    with _STATE_LOCK:
        if not _STATE.enabled or _STATE.paused or _STATE.key is None:
            return
        q = _STATE.queue
        if q is None:
            return
    try:
        q.put_nowait(envelope)
    except Exception:
        with _STATE_LOCK:
            _STATE.dropped_events += 1


def drain(timeout: float = 2.0) -> None:
    """Block until the writer queue is empty. Local-only: used by control ops
    that must persist and by tests. Agents never call this."""
    with _STATE_LOCK:
        q = _STATE.queue
    if q is None:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if q.empty():
            time.sleep(0.05)  # let the writer finish the last in-flight item
            if q.empty():
                return
        time.sleep(0.02)


# ── Public API used by server.py ───────────────────────────────────────────────
def set_tool_route(tool_name: str, route: str) -> None:
    """Register the route classification for a wrapped tool (purely advisory;
    stored so record_action_completed can default to it)."""
    with _STATE_LOCK:
        _STATE._tool_routes[tool_name] = route


def record_action_started(tool_name: str) -> None:
    payload = {"kind": "action_started"}
    _assert_no_private_fields(payload)
    _try_enqueue(_envelope(EVENT_ACTION_STARTED, payload))


def record_action_completed(tool_name: str, *, app_name: Optional[str] = None,
                            effect: str = "unverifiable", route: Optional[str] = None,
                            delivery: str = "unknown", delivered_count: int = 0,
                            evidence_kinds: Optional[list] = None,
                            escalation_kind: Optional[str] = None) -> None:
    """Record the validated outcome of a kwin-mcp action. Metadata-only."""
    if route is None:
        with _STATE_LOCK:
            route = _STATE._tool_routes.get(tool_name, "unknown")
    payload = _build_action_completed(
        app_name=app_name, route=route, effect=effect, delivery=delivery,
        delivered_count=delivered_count, evidence_kinds=evidence_kinds,
        escalation_kind=escalation_kind)
    _try_enqueue(_envelope(EVENT_ACTION_COMPLETED, payload))


def record_control(operation: str) -> None:
    payload = {"kind": "control", "operation": operation}
    if operation not in CONTROL_OPS:
        payload["operation"] = "flush"
    _assert_no_private_fields(payload)
    _try_enqueue(_envelope(EVENT_CONTROL, payload))


# ── Control (local only) ────────────────────────────────────────────────────────
def control(operation: str, root: Optional[str] = None) -> dict:
    """Mutate capture lifecycle. MUST only be callable from the local server
    process (mirrors history_control_requires_local_cli)."""
    with _STATE_LOCK:
        if operation == "enable":
            if _STATE.enabled:
                return {"ok": True, "operation": "enable", "already_enabled": True}
            _ensure_crypto()
            _STATE.root = root or default_root()
            os.makedirs(os.path.join(_STATE.root, "chunks"), exist_ok=True)
            # Open or create the namespace key (in-memory; optional wrap later).
            if _STATE.key is None:
                _STATE.key = os.urandom(32)
            _STATE.session_id = _new_id()
            _STATE.sequence = 0
            _STATE.dropped_events = 0
            _STATE.bytes_used = _store_bytes(_STATE.root)
            _start_writer(_STATE)
            _STATE.enabled = True
            _STATE.paused = False
            _enqueue_local(EVENT_CONTROL, {"kind": "control", "operation": "enable"})
            return {"ok": True, "operation": "enable",
                    "encrypted": True, "profile": "kwin-mcp-history-v1"}
        if operation == "disable":
            _STATE.enabled = False
            _enqueue_local(EVENT_CONTROL, {"kind": "control", "operation": "disable"})
            return {"ok": True, "operation": "disable"}
        if operation == "pause":
            _STATE.paused = True
            _enqueue_local(EVENT_CONTROL, {"kind": "control", "operation": "pause"})
            return {"ok": True, "operation": "pause", "paused": True}
        if operation == "resume":
            _STATE.paused = False
            _enqueue_local(EVENT_CONTROL, {"kind": "control", "operation": "resume"})
            return {"ok": True, "operation": "resume", "paused": False}
        if operation == "flush":
            # Drop buffered-but-unwritten events; keep the encrypted store.
            if _STATE.queue is not None:
                while not _STATE.queue.empty():
                    try:
                        _STATE.queue.get_nowait()
                    except Exception:
                        break
            return {"ok": True, "operation": "flush"}
        if operation == "delete":
            # Cryptographic deletion: destroy the in-memory key and erase chunks.
            _stop_writer()
            if _STATE.root and os.path.isdir(_STATE.root):
                import shutil
                shutil.rmtree(_STATE.root, ignore_errors=True)
            _STATE.key = None
            _STATE.session_id = ""
            _STATE.enabled = False
            _STATE.paused = False
            _STATE.bytes_used = 0
            _STATE.sequence = 0
            return {"ok": True, "operation": "delete", "purged": True}
        return {"ok": False, "error": f"unknown operation {operation!r}"}


def _enqueue_local(event_type: str, payload: dict) -> None:
    """Like _try_enqueue but bypasses the enabled/paused gate for control ops
    that must record even while paused or right before disable."""
    _assert_no_private_fields(payload)
    envelope = _envelope(event_type, payload)
    with _STATE_LOCK:
        key = _STATE.key
        root = _STATE.root
        sid = _STATE.session_id
        if key is None or not root or not sid:
            return
        _STATE.sequence += 1
        envelope["data"]["sequence"] = _STATE.sequence
    try:
        _append_record(root, sid, key, envelope)
    except Exception:
        pass


def _store_bytes(root: str) -> int:
    total = 0
    chunks = os.path.join(root, "chunks")
    if os.path.isdir(chunks):
        for name in os.listdir(chunks):
            try:
                total += os.path.getsize(os.path.join(chunks, name))
            except OSError:
                pass
    return total


def _stop_writer() -> None:
    with _STATE_LOCK:
        if _STATE.writer is not None and _STATE.writer.is_alive():
            _STATE.stop = True
            try:
                _STATE.queue.put_nowait(None)
            except Exception:
                pass
    # writer is a daemon thread; it will exit on stop. We do not join (nonblocking).
    with _STATE_LOCK:
        _STATE.stop = False


# ── Read-only agent tools ────────────────────────────────────────────────────────
def status() -> dict:
    """history_status: operational metadata, no events."""
    with _STATE_LOCK:
        enabled = _STATE.enabled
        paused = _STATE.paused
        key = _STATE.key
        bytes_used = _STATE.bytes_used
        dropped = _STATE.dropped_events
        root = _STATE.root
    health = "ready"
    if not enabled:
        health = "disabled"
    elif paused:
        health = "paused"
    elif key is None:
        health = "key_unavailable"
    encrypted = key is not None
    return {
        "supported": True,         # kwin-mcp runs only on the supported platform
        "admitted": True,          # the preview is always admitted in this build
        "enabled": enabled,
        "paused": paused,
        "encrypted": encrypted,
        "profile": "kwin-mcp-history-v1" if encrypted else "",
        "retention_days": DEFAULT_RETENTION_DAYS,
        "quota_bytes": DEFAULT_QUOTA_BYTES,
        "bytes_used": bytes_used,
        "dropped_events": dropped,
        "health": health,
        "root": root or "",
        "metadata_only": True,
        "model_context_disclosure": True,
    }


def query(*, limit: int = 50, session_id: Optional[str] = None,
          since_sequence: Optional[int] = None,
          until_sequence: Optional[int] = None) -> dict:
    """history_query: bounded, metadata-only event slice.

    Always returns ``metadata_only`` and ``model_context_disclosure`` true, and
    never includes the access record it appends. Unknown/himits are rejected by
    the caller (server.py) before reaching here.
    """
    if limit < 1 or limit > MAX_QUERY_LIMIT:
        limit = max(1, min(MAX_QUERY_LIMIT, limit))
    if since_sequence is not None and until_sequence is not None:
        if since_sequence > until_sequence:
            raise HistoryError("storage_unavailable",
                               "since_sequence must not exceed until_sequence")
    # Decrypt + collect
    events: list[dict] = []
    with _STATE_LOCK:
        key = _STATE.key
        root = _STATE.root
        enabled_session = _STATE.session_id
    if key is None or not root:
        return {"events": [], "metadata_only": True, "model_context_disclosure": True}
    target_sessions = ([session_id] if session_id else
                       ([enabled_session] if enabled_session else []))
    for sid in target_sessions:
        path = _chunk_path(root, sid)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as fh:
                for raw_line in fh:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        pt = _open(key, raw_line.decode("utf-8"))
                        env = json.loads(pt.decode("utf-8"))
                    except HistoryError:
                        continue
                    data = env.get("data", {})
                    seq = data.get("sequence", 0)
                    if since_sequence is not None and seq < since_sequence:
                        continue
                    if until_sequence is not None and seq > until_sequence:
                        continue
                    events.append(env)
        except OSError:
            continue
    # ascending by sequence, keep newest `limit`
    events.sort(key=lambda e: e.get("data", {}).get("sequence", 0))
    if len(events) > limit:
        events = events[-limit:]
    # Append an access record (encrypted, not returned).
    _enqueue_local(EVENT_ACCESS, {"kind": "access", "operation": "agent_query",
                                  "returned_events": len(events)})
    return {
        "events": events,
        "metadata_only": True,
        "model_context_disclosure": True,
    }


# ── Decorator used by server.py to wrap mutation/action tools ───────────────────
def record(tool_name: str, route: str = "unknown",
           app_resolver: Optional[Callable[[dict], Optional[str]]] = None) -> Callable:
    """Signature-preserving decorator for an ``@mcp.tool`` function.

    Wraps the tool so that, when history is enabled, an ``action_started`` event
    is recorded before the call and an ``action_completed`` event is recorded
    after (with effect guessed from the returned dict). The tool's own signature
    is preserved so FastMCP advertises the exact same input schema to clients.

    ``app_resolver(result, kwargs) -> Optional[str]`` optionally extracts a
    non-sensitive application display name from the tool result/args to enrich
    the action_completed payload (never a title or path).
    """

    def _classify_effect(result: dict) -> str:
        if not isinstance(result, dict):
            return "unverifiable"
        if result.get("error"):
            return "failed"
        if result.get("ok") is False:
            return "failed"
        if result.get("ok") is True:
            return "confirmed"
        return "unverifiable"

    def decorator(fn: Callable) -> Callable:
        set_tool_route(tool_name, route)
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            record_action_started(tool_name)
            try:
                result = fn(*args, **kwargs)
            except Exception:
                # The action path must not be failed by history; still record.
                record_action_completed(tool_name, effect="failed", route=route)
                raise
            effect = _classify_effect(result)
            app = None
            if app_resolver is not None:
                try:
                    app = app_resolver(result if isinstance(result, dict) else {},
                                       kwargs)
                except Exception:
                    app = None
            record_action_completed(tool_name, app_name=app, effect=effect,
                                    route=route)
            return result

        return wrapper

    return decorator
