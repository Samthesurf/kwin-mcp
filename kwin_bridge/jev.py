"""Jev integration: fast System One decisions over the AT-SPI tree.

TypeSafe's Jev (served through OpenRouter's Decisions API) answers typed
questions with calibrated probabilities instead of generating text, in a few
hundred milliseconds. kwin-mcp already produces exactly the state Jev eats: a
structured AT-SPI element tree with roles, names, states and actions.

``jev_act`` runs the two-tier agent loop locally:

  1. observe   - a11y.get_window_state; interactive elements become candidate
                 options (clickables -> "e{index}", editable fields ->
                 "t{index}").
  2. decide    - ONE Jev request per step: a Choice over the candidates plus
                 parallel Noul questions ("is the goal already complete?",
                 "is the state stuck?").
  3. act       - the code picks the executor: click_element for clickables,
                 set_value (the caller-provided text) for editable fields.
  4. re-observe and loop until Jev says complete, confidence drops below
     floor (escalate to the calling LLM), or steps run out.

The calling LLM writes the goal; Jev makes every step-level decision. That
replaces a multi-second LLM round-trip per UI step with a sub-second
(probabilistic) answer, and everything free-form (the goal, the text values)
stays on the caller.

Requires OPENROUTER_API_KEY in the environment. No extra Python dependency:
the Decisions API is plain HTTP and we use urllib.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
_DEFAULT_MODEL = "~typesafe/jev-latest"

# Hard safety caps.
_MAX_OPTIONS = 200      # Jev allows 255; leave headroom for sentinels
_MAX_STEPS_DEFAULT = 8
_MIN_CONFIDENCE_DEFAULT = 0.5


class JevUnavailable(RuntimeError):
    """OPENROUTER_API_KEY missing or the decisions API unreachable."""


def greet() -> str:  # pragma: no cover - trivial smoke export
    return "jev module ready"


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise JevUnavailable(
            "OPENROUTER_API_KEY is not set; export it (OpenRouter -> Keys) to "
            "use the jev_* tools"
        )
    return key


class _KeepAliveHTTPS:
    """Persistent HTTPS connection pool (one socket) for the decisions API.

    urllib opens a fresh TLS connection per call (~370 ms of the measured
    1.3 s). One reused socket cuts a warm decision to ~550-750 ms on the
    same network. Thread-safe enough for the MCP server's worker threads;
    on connection errors the socket is dropped and rebuilt once, lazily.
    """

    def __init__(self) -> None:
        self._conn: http.client.HTTPSConnection | None = None
        self._host = urllib.parse.urlparse(_DECISIONS_URL).hostname or "openrouter.ai"
        self._path = urllib.parse.urlparse(_DECISIONS_URL).path

    def _get(self):
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(self._host, timeout=30)
        return self._conn

    def post_json(self, payload: bytes, auth: str,
                  timeout: int = 30) -> tuple[int, dict]:
        """POST once; returns (status, parsed_json). Reconnects on a dead
        socket and retries once; second failure re-raises as OSError."""
        body = payload
        for attempt in (1, 2):
            conn = self._get()
            conn.timeout = timeout
            try:
                conn.request(
                    "POST", self._path, body=body,
                    headers={
                        "Authorization": auth,
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                        "Connection": "keep-alive",
                    })
                resp = conn.getresponse()
                data = resp.read()
                return resp.status, json.loads(data.decode())
            except (TimeoutError, OSError) as exc:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None
                if attempt == 2:
                    raise
                continue
        raise OSError("unreachable")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


_POOL = _KeepAliveHTTPS()


def decide(state, questions: dict, model: str = "", timeout: int = 30) -> dict:
    """Evaluate typed questions about a state in one Jev call.

    state: a string or JSON-able object (anything 32k-token-safe).
    questions: {id: {"type": "noul"|"choice"|"score", ..., "criteria": ...}}
               using the OpenRouter Decisions API question shape.
    Returns {"answers": {...}, "model": <resolved id>, "latency_ms": int}.
    Raises JevUnavailable on missing key / network / HTTP failure.
    Uses a persistent keep-alive connection so warm calls skip TLS setup.
    """
    payload = {
        "model": model or os.environ.get("KWIN_MCP_JEV_MODEL", _DEFAULT_MODEL),
        "state": state,
        "questions": questions,
    }
    body = json.dumps(payload).encode()
    auth = f"Bearer {_api_key()}"
    t0 = time.monotonic()
    try:
        status, result = _POOL.post_json(body, auth, timeout=timeout)
        if status != 200:
            detail = json.dumps(result)[:400]
            raise JevUnavailable(f"decisions API HTTP {status}: {detail}")
    except JevUnavailable:
        raise
    except (json.JSONDecodeError, TimeoutError, OSError) as exc:
        raise JevUnavailable(f"decisions API unreachable: {exc}") from exc
    return {
        "answers": result.get("answers", {}),
        "model": result.get("model", payload["model"]),
        "latency_ms": int((time.monotonic() - t0) * 1000),
    }


# ── Candidate construction ──────────────────────────────────────────────────

def _element_kind(el: dict) -> str:
    """Classify an element dict as clickable, editable, or (None)."""
    role = (el.get("role") or "").lower()
    editable = bool(el.get("editable"))
    has_actions = bool(el.get("actions"))
    if editable and any(k in role for k in ("text", "entry", "edit", "spin")):
        return "editable"
    if editable:
        return "editable"
    if has_actions or "push button" in role or role.endswith("button"):
        return "clickable"
    if any(k in role for k in ("link", "menu item", "list item", "tab",
                               "check box", "radio", "combo", "icon")):
        return "clickable"
    return ""


def _candidate_criteria(elements: list[dict]) -> tuple[dict, dict]:
    """Build {key -> human description} for every usable element.

    Returns (criteria, kind_by_key) with at most _MAX_OPTIONS element entries.
    Keys: 'e{index}' for clickable elements, 't{index}' for editable ones.
    """
    criteria: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for el in elements:
        kind = _element_kind(el)
        if not kind:
            continue
        if kind == "clickable":
            key = f"e{el['index']}"
            desc = f"{el.get('role', '?')} '{el.get('name', '')}' -> click it"
        else:
            key = f"t{el['index']}"
            desc = f"{el.get('role', '?')} '{el.get('name', '')}' -> type text into it"
        criteria[key] = desc
        kinds[key] = kind
        if len(criteria) >= _MAX_OPTIONS:
            break
    return criteria, kinds


def _state_payload(window_state: dict, goal: str, values: dict,
                   recent: list[dict]) -> dict:
    elements = [
        {
            "key": f"e{el['index']}" if _element_kind(el) == "clickable"
            else (f"t{el['index']}" if _element_kind(el) == "editable" else None),
            "role": el.get("role"),
            "name": el.get("name"),
            "states": [s for s in el.get("states", []) if s in
                       ("focused", "checked", "enabled", "editable",
                        "pressed", "selected")],
        }
        for el in window_state.get("elements", [])
        if _element_kind(el)
    ]
    return {
        "goal": goal,
        "provided_values": {str(k): v for k, v in (values or {}).items()},
        "window": {
            "title": window_state.get("window_title", ""),
            "app": window_state.get("app_name",
                                    window_state.get("backend", "")),
        },
        "elements": elements,
        "recent_actions": recent,  # last few decisions, to break loops
    }


def _pick_value(el: dict, values: dict) -> tuple[str, str]:
    """Match an editable element to a caller-provided value.

    Returns (index_key, text). Matches on element index first (caller passed
    {"t7": "..."}), then on name substring. Raises ValueError when nothing
    fits so the caller gets an honest needs_value answer instead of a
    misdirected keystroke.
    """
    key = f"t{el['index']}"
    name = (el.get("name") or "").lower()
    if key in values:
        return key, values[key]
    for k, v in values.items():
        if name and str(k).lower() in name:
            return key, v
    for k, v in values.items():
        if name and name in str(k).lower():
            return key, v
    if len(values) == 1:
        # Ambiguous but the caller gave exactly one string: use it and say so.
        k, v = next(iter(values.items()))
        return key, v
    raise ValueError(
        f"element '{el.get('name')}' needs text but no provided value matches"
    )


def act(window_id: str, goal: str, values: dict | None = None,
        max_steps: int = _MAX_STEPS_DEFAULT,
        min_confidence: float = _MIN_CONFIDENCE_DEFAULT) -> dict:
    """Run the observe -> decide -> act loop toward ``goal`` on one window.

    Jev makes every step decision (click which element / type into which
    field / already done / stuck); the code owns observation, values and
    execution. Returns a trace with one entry per step (latency, choice,
    confidence, execution result).
    """
    from . import a11y  # late import keeps module import cheap

    out: dict = {
        "ok": False, "window_id": window_id, "goal": goal,
        "steps": [], "model": None, "status": "",
    }
    values = values or {}
    recent: list[dict] = []
    cumulative_ms = 0

    for step_no in range(1, max_steps + 1):
        state = a11y.get_window_state(window_id)
        if not state.get("available"):
            out["status"] = "error"
            out["error"] = state.get("error", "AT-SPI unavailable")
            return out
        if not state.get("elements"):
            out["status"] = "error"
            out["error"] = state.get(
                "hint", "window exposes an empty AT-SPI tree")
            return out

        criteria, kinds = _candidate_criteria(state["elements"])
        if not criteria:
            out["status"] = "needs_review"
            out["error"] = "no clickable or editable elements found in tree"
            return out
        criteria.update({"done": "the goal is already met in this state",
                         "stuck": "nothing here can advance the goal"})

        questions = {
            "next": {
                "type": "choice",
                "instructions": (
                    "Work toward `goal` using the listed elements. "
                    "Recent actions already taken are in `recent_actions`; "
                    "do not repeat them. Which single option is next?"
                ),
                "criteria": criteria,
            },
            "complete": {
                "type": "noul",
                "instructions": "Is `goal` already met in this state?",
            },
        }
        dec = decide(_state_payload(state, goal, values, recent),
                     questions)
        out["model"] = dec["model"]
        cumulative_ms += dec["latency_ms"]

        answers = dec["answers"]
        pick = answers.get("next", {}).get("choice", "")
        conf = answers.get("next", {}).get("confidence", 0.0)
        done_p = answers.get("complete", {}).get("noul", 0.0)
        step = {
            "step": step_no, "latency_ms": dec["latency_ms"],
            "choice": pick, "confidence": round(conf, 3),
            "complete_noul": round(done_p, 3),
        }

        if done_p >= 0.8 and pick in ("done", "stuck"):
            step["action"] = "none (goal met)"
            out["steps"].append(step)
            out["ok"] = True
            out["status"] = "complete"
            out["total_jev_ms"] = cumulative_ms
            return out

        c_conf = answers.get("complete", {}).get("noul", 0.0)
        if c_conf >= 0.7 and pick == "done":
            # Jev's Choice and Noul agree the goal is met: trust it.
            step["action"] = "none (goal met, choice+noul agree)"
            out["steps"].append(step)
            out["ok"] = True
            out["status"] = "complete"
            out["total_jev_ms"] = cumulative_ms
            return out

        if pick in ("done", "stuck", "") or pick not in kinds:
            step["action"] = "none"
            out["steps"].append(step)
            out["status"] = "needs_review"
            out["error"] = (f"Jev picked '{pick or 'nothing'}' (conf "
                            f"{conf:.2f}); state ambiguous, escalate to the "
                            "calling LLM")
            out["total_jev_ms"] = cumulative_ms
            return out

        if conf < min_confidence:
            step["action"] = "none"
            out["steps"].append(step)
            out["status"] = "needs_review"
            out["error"] = (f"confidence {conf:.2f} below floor "
                            f"{min_confidence:.2f}; not acting")
            out["total_jev_ms"] = cumulative_ms
            return out

        # Execute with the repo's own verified primitives.
        el_index = int(pick[1:])
        if kinds[pick] == "clickable":
            res = a11y.click_element(window_id, el_index)
            step["action"] = "click_element"
        else:
            el = next((e for e in state["elements"]
                       if e.get("index") == el_index), None)
            if el is None:
                step["action"] = "none"
                step["error"] = f"element {el_index} vanished after decision"
                out["steps"].append(step)
                out["status"] = "needs_review"
                out["total_jev_ms"] = cumulative_ms
                return out
            try:
                _, text = _pick_value(el, values)
            except ValueError as exc:
                step["action"] = "none"
                step["error"] = str(exc)
                out["steps"].append(step)
                out["status"] = "needs_value"
                out["needs"] = f"text for element {el_index} ('{el.get('name')}')"
                out["total_jev_ms"] = cumulative_ms
                return out
            res = a11y.set_value(window_id, el_index, text)
            step["action"] = "set_value"
        step["exec"] = res

        recent.append({"step": step_no, "choice": pick, "action": step["action"],
                       "ok": bool(res.get("ok"))})
        out["steps"].append(step)
        if not res.get("ok"):
            out["status"] = "needs_review"
            out["error"] = (f"executor failed on step {step_no}: "
                            f"{res.get('error')}")
            out["total_jev_ms"] = cumulative_ms
            return out

    out["status"] = "max_steps"
    out["error"] = f"reached max_steps={max_steps} without 'done'"
    out["total_jev_ms"] = cumulative_ms
    return out


def check(window_id: str, question: str) -> dict:
    """Ask Jev one yes/no question about a window's current AT-SPI state.

    Fast completion/verification probes (<1 s class) that do not consume an
    LLM call. Returns {"noul": 0..1, "latency_ms": int, "model": id}.
    """
    from . import a11y
    state = a11y.get_window_state(window_id)
    if not state.get("available"):
        return {"ok": False, "error": state.get("error", "AT-SPI unavailable")}
    compact = [
        f"{el['role']} '{el['name']}' [{','.join(el.get('states', []))}]"
        for el in state.get("elements", [])
    ]
    dec = decide(
        {"window_title": state.get("window_title", ""),
         "elements": compact, "question": question},
        {"answer": {"type": "noul",
                    "instructions": "Answer the `question` about this state."}},
    )
    noul = dec["answers"].get("answer", {}).get("noul")
    if noul is None:
        return {"ok": False, "error": f"unexpected answers: {dec['answers']}"}
    return {"ok": True, "noul": noul, "latency_ms": dec["latency_ms"],
            "model": dec["model"]}
