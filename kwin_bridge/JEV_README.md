# Jev integration (kwin-mcp)

TypeSafe's Jev ("System One" model) served via OpenRouter's Decisions API.
Not an LLM: answers typed questions with calibrated probabilities, in
hundreds of milliseconds, without generating text.

OpenRouter endpoint:

    POST https://openrouter.ai/api/alpha/decisions
    Authorization: Bearer ${OPENROUTER_API_KEY}
    {"model": "~typesafe/jev-latest", "state": {...}, "questions": {...}}

Question types:

| type     | asks                | answer                                  |
|----------|---------------------|-----------------------------------------|
| `noul`   | yes/no              | probability of yes (0..1)               |
| `choice` | pick one option     | pick + per-option probabilities + confidence |
| `score`  | position on a scale | fractional score + confidence           |

Model: pin a concrete id (e.g. `typesafe/jev-1.13-20260917`) for reproducible
thresholds. `~typesafe/jev-latest` always redirects to the newest version.
Set `KWIN_MCP_JEV_MODEL` to override.

## Why this makes kwin-mcp faster

The expensive part of computer use is the per-step LLM round-trip
(observation + reasoning, seconds). Jev replaces just that decision with one
sub-second classify call; the LLM only writes the goal and any free-form
text. This module feeds the existing `get_window_state` AT-SPI tree as the
candidate list, and lets Jev's `choice` pick which element to click or type
into. No screenshots needed: Jev works on the accessibility tree, which
kwin-mcp already produces for every window on KDE Wayland.

## Tools exposed (server.py)

- `jev_act(window_id, goal, values={}, max_steps=8, min_confidence=0.5)`
  Autonomous observe -> decide -> act loop toward a goal. Jev picks the next
  element each step (clickables `e{index}`, editable fields `t{index}`);
  `noul` judges completion; the code executes via the same verified
  `click_element` / `set_value` primitives as manual calls, so the honesty
  contract (DoAction, no fake success) still holds. Values for text fields
  come from the caller in `values` keyed by name or `t{index}`.
- `jev_decide(state, questions, model="")`
  Raw fast classifier: build your own questions, one HTTP call, no state
  walking. Use for routing/classification fast-paths.
- `jev_check(window_id, question)`
  One yes/no probe of the current window surface: "Does the dialog show a
  success message?" Returns `{noul, latency_ms, model}`.

## Calibration (measured 2026-09-18, KDE Plasma on this machine)

- Direct typesafe.ai endpoint (TYPESAFE_API_KEY): warm decisions 393-499 ms
  median on this network; cold first call ~1.3 s (TLS + model warm-up).
  Resolved model: jev-1.13.0. The OpenRouter relay (fallback) measured
  880-3,100 ms for the same question before this module switched direct.
- Both endpoints take the same request shape; the backend auto-selects on
  key presence and `KWIN_MCP_JEV_MODEL` overrides the model id either way.
- Completion `noul` on "done" picks lands 0.72-0.99; the auto-complete
  threshold is therefore 0.7 with the Choice already at `done`.
- Confidence on hard states can drop to 0.61 (still correct action).
- Live e2e on KDE Connect (keep-alive connector):

    | goal            | steps | Jev ms | wall s | status   |
    |-----------------|-------|--------|--------|----------|
    | Refresh Devices | 2     | ~900   | 3.9    | complete |
    | Close Drawer    | 2     | ~970   | 1.0    | complete |

Latency is bounded by OpenRouter network round-trip, not by the model;
direct TypeSafe key (`TYPESAFE_API_KEY`) would cut it further, but requires
waitlist/tier access.

## Escalation contract (what the host LLM should do)

`jev_act` stops and reports rather than guessing:

- `needs_review`: Jev picked `stuck`, picked a vanished element, confidence
  fell below the floor, or the executor failed.
- `needs_value`: a text field was chosen but no value in `values` matches.
  Retry with `values={"<field name>": "text"}`.
- `max_steps`: keep looping if the goal genuinely needs more hops, or treat
  as not-achievable on this surface.

In all three the host can fall back to its normal reasoning loop: the
window's AT-SPI tree is unchanged, so `get_window_state` remains the
source of truth.

## Cost / safety

- Input pricing ~$0.042/M tokens; the AT-SPI tree of a typical window is a
  few hundred tokens per step, so a multi-step act is far cheaper than a
  chain of GLM/Claude reason+act round-trips.
- Candidate list is hard-capped at 200 options; Jev accepts 255.
- No new Python dependency: plain `urllib` against the Decisions API.
