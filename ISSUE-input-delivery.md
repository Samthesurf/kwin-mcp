# kwin-mcp input delivery: silent drops and element mis-targeting (0.5.x)

Found during a real UI-automation session on **this machine** (Wayland, KWin,
Plasma session, Electron target app). Everything below is backed by journal
evidence, D-Bus probes, and screenshots captured in the session on
2026-08-28. This file is the handoff brief: what is broken, how to reproduce
it, and the recommended fixes.

## TL;DR

| # | Symptom | Severity | Fix direction |
|---|---|---|---|
| 1 | `click` (coordinate mode) returns `ok: true` but input never lands on unfocused Wayland windows | high | verify delivery, or fail honestly |
| 2 | `click_element` hits the wrong element (and later, none) on Electron/CSD windows | high | prefer AT-SPI `DoAction` over synthesized pointer events at AX-derived pixels |
| 3 | Electron apps expose an **empty** AT-SPI tree unless the app opts in | docs / DX | document or detect and hint |

Issue 2's fix (issue 1 disappears with it): stop synthesizing pointer events
for element clicks; resolve the element's `Component` bounds and invoke its
AT-SPI **Action.DoAction(index)** ("press"/"click") directly. Coordinates
then never enter the picture for semantic clicks.

## Environment where this was observed

- Arch Linux, kernel 7.1.9-arch1-2, Wayland + KWin (Plasma 6), 1366x768
- kwin-mcp 0.5.0 (installed binary path, a3c3869)
- Target app: Electron 44 (`fingerprint-app`, CSD header bar, window at
  x=89 y=0, size 1188x801), accessibility enabled via
  `app.setAccessibilitySupportEnabled(true)` in the app itself
- Comparison tool that worked every time: CDP
  (`--remote-debugging-port=9222`, `Input.dispatchMouseEvent`)

## Issue 1: coordinate click reports success, silently drops

### What happened

Two `click(window_id, x, y)` calls into the Electron window returned
`{"ok": true}`:

- click at (482, 607) targeting the app's "Cancel scan" button
- click at (290, 607) targeting "Enroll"

Neither delivered. Proof, three independent ways:

1. The target app's renderer stayed in "Verifying Index..." state and never
   reset (verified by screenshot + CDP DOM read).
2. The fingerprint daemon journal shows the D-Bus side effect of that
   button (`VerifyStop`) **never arrived**:
   ```
   Aug 28 02:43:04 open-fprintd: DEBUG:root:Claim
   Aug 28 02:43:04 open-fprintd: DEBUG:root:VerifyStart
   ... (no VerifyStop line, ever)
   ```
3. The device stayed claimed by the app (a direct D-Bus `Claim` probe from
   outside returned `AlreadyInUse`) until the app was killed.

A third click at (389, 578), taken **verbatim from the AX tree's own
center** for the Cancel button, also did not deliver.

### Why (mechanism)

On Wayland, clients cannot inject input into surfaces they do not own;
whether a virtual-pointer event lands depends on the compositor routing it
to the focused surface under a real cursor position. KWin will move a
virtual pointer, but if the target window is not focused (or the pointer
ends up over a different surface, e.g. the Hermes window on top), the
events die. The bug in kwin-mcp is not the drop itself: it is returning
`ok: true` with **no delivery verification**. A tool that reports success
for input that never happened is worse than one that fails.

### Recommended fix

Any of these, in increasing order of strictness:

1. Focus-first discipline: `activate(window_id)` before every coordinate
   click (documented behavior change + internal auto-focus), or
2. Post-check: after click, cheaply verify the effect when possible
   (e.g. cursor position actually moved, or window focus state), and
3. Minimum: change the contract. If delivery could not be confirmed, return
   `ok: false, reason: "input-not-delivered (window not focused?)"` instead
   of `ok: true`.

Note: this is exactly the known class of problem the project already
documents ("Linux automation often reports success while uinput events
drop due to focus/protocol mismatches"). This session is a clean,
journal-backed repro of that class for the coordinate-click path.

## Issue 2: `click_element` mis-targets / silently no-ops on Electron windows

### What happened

1. `click_element(window_id, element_index=1)` where the AX tree said
   element 1 is the app's primary Enroll button (127x35 at AX (189,561)).
   Result: the app entered **verify** mode, which only the Test button
   (element 2, 60px wide at AX (325,561)) can trigger. So the click
   delivered, but on the wrong element.
2. Later, `click_element(window_id, element_index=3)` (Cancel, AX center
   (389,578)) did not deliver at all (claim still held; same probe method
   as issue 1).

### Why (best explanation available)

The AX coordinates returned by `get_window_state` are **not screen-space**
on this setup. Two observations support a transform/offset problem:

- The window sits at x=89 with a CSD title bar; AX x/y values look like
  they are in window-local (or differently-originated) space while the
  click path treats them as global pixels. The first click_element
  succeeded in *delivering* (mouse moved, some button got it), which is
  consistent with "clicked the right coordinate in the wrong space".
- Element indices are unstable across rebuilds: the tree went from 17
  elements (idle) to 18 (scanning), with buttons shifting position as the
  Cancel button appeared. An index captured before a re-render can point
  at a different element after it.

### Recommended fix (this is the good one)

For `click_element` / `perform_action`, stop converting elements to
coordinates and synthesizing pointer events. Instead:

1. Hold a reference to the AT-SPI accessible object, not an index.
2. Prefer its **Action** interface: query `nActions`/`getName(i)` and
   invoke `DoAction(i)` for "press"/"click"/"activate". This is protocol-
   level activation: no cursor, no focus dependency, no coordinate space,
   works on unfocused windows.
3. Only fall back to coordinate synthesis when an element exposes no
   Action, and in that path, add the issue-1 honesty (verify or fail).

This also matches the project's stated design goal: keyboard-first,
semantic automation by role/name instead of pixels. `perform_action`
already exists and claims to prefer the semantic action; the fix is to
make `click_element` route through it (or share its code path) rather than
falling back to pixels.

## Issue 3: Electron apps show an empty AX tree by default

`get_window_state` on the Electron app returned
`{"available": true, "elements": [], "count": 0}` until the **app itself**
called Electron's `app.setAccessibilitySupportEnabled(true)`. After that,
the full tree (17-18 elements) appeared. This is standard Electron
behavior (accessibility tree is built lazily on first AT-SPI client), but
it will look like a kwin-mcp bug to every agent driving an Electron app.

Cheap DX wins:

- `get_window_state` could return a hint when a window's backend is
  available but empty, e.g. `"hint": "tree empty; app may not have
  accessibility enabled (Electron: app.setAccessibilitySupportEnabled(true))"`.
- README/troubleshooting note: for Electron targets, either launch with
  `--force-renderer-accessibility` or set the flag in-app.

## Reproduction recipe (all on this machine)

1. Build/launch the target: `~/Documents/JS_stuff/fingerprint_app`
   (Electron; committed at 26ce653). Launch:
   `./start.sh --remote-debugging-port=9222`
2. `kwin-mcp list-windows` -> find "Fingerprint Enroller" (app_name
   `fingerprint-app`).
3. Coordinate drop repro:
   - `click` at the on-screen center of a visible button in the unfocused
     window (e.g. its "Test" button).
   - Verify delivery via CDP: `node cdp.js eval "document.getElementById('statusMsg').textContent"`
     (status must change) and/or the daemon journal (no side effect = no
     delivery). Result today: no delivery, `ok: true` returned.
4. Element mis-target repro:
   - `get_window_state`, note the index of a button by its AX geometry,
   - `click_element` it,
   - compare the app's observable state (CDP DOM read) against which
     button actually has that geometry. Result today: a different button
     received the click (verify instead of enroll).
5. Working control: `node cdp.js click "#btnTest"` (CDP
   `Input.dispatchMouseEvent`) delivered on **every** attempt.

## Session artifacts (paths, still on disk where kept)

- App repo with the CDP helper and D-Bus probes:
  `~/Documents/JS_stuff/fingerprint_app` (see `cdp.js`, `test_claim.js`,
  `test_dbus.js`, `test_signals*.js`)
- Screenshots from the session: `/tmp/fp-app-shot*.png`,
  `/tmp/fp-app-final.png`
- Daemon evidence: `journalctl -u open-fprintd --since "2026-08-28 02:40"`
  around 02:43:04 (Claim/VerifyStart, no VerifyStop) and 03:02:18-03:02:50
  (CDP clicks: Claim/EnrollStart then EnrollStop/Release, all delivered)
