"""Mock-based unit tests for the AT-SPI semantic layer (no real pyatspi, no
real window, no real input). Requires a venv with the core deps installed."""
import sys
import types
import pytest

# ---- minimal pyatspi stand-in so tests run without the system lib ----
STATE_CHECKED, STATE_FOCUSED, STATE_SELECTED, STATE_SHOWING = 1, 2, 3, 4
STATE_VISIBLE, STATE_ENABLED, STATE_SENSITIVE, STATE_EDITABLE = 5, 6, 7, 8
DESKTOP_coords = 0


class _Ext:
    def __init__(s, x, y, w, h):
        s.x, s.y, s.width, s.height = x, y, w, h


class _Component:
    def __init__(s, x, y, w, h):
        s._e = _Ext(x, y, w, h)

    def getExtents(s, coords):
        return s._e


class _StateSet:
    def __init__(s, flags):
        s.flags = set(flags)

    def contains(s, flag):
        return flag in s.flags


class _Action:
    def __init__(s, names):
        s.names = names

    def doAction(s, i):
        return True

    def getName(s, i):
        return s.names[i]

    @property
    def nActions(s):
        return len(s.names)


class _Editable:
    def __init__(s):
        s.val = ""

    def setTextContents(s, v):
        s.val = v
        return True


class _App:
    def __init__(s, pid):
        s._pid = pid

    def get_process_id(s):
        return s._pid


class _Node:
    def __init__(s, role, name, pid, x=0, y=0, w=100, h=20,
                 states=(), actions=(), editable=False, children=()):
        s.role, s.name, s._pid = role, name, pid
        s._comp = _Component(x, y, w, h)
        s._states = _StateSet(states)
        s._actions = _Action(actions)
        s._editable = _Editable() if editable else None
        s._children = list(children)

    def getRoleName(s):
        return s.role

    def queryComponent(s):
        return s._comp

    def getState(s):
        return s._states

    def queryAction(s):
        return s._actions

    def queryEditableText(s):
        if s._editable is None:
            raise Exception("not editable")
        return s._editable

    def getApplication(s):
        return _App(s._pid)

    @property
    def childCount(s):
        return len(s._children)

    def getChildAtIndex(s, i):
        return s._children[i]


def _install_fake_pyatspi():
    send = _Node("push button", "Send", 1547, 500, 400, 60, 28,
                 states=(STATE_ENABLED, STATE_SHOWING), actions=("press",))
    msg = _Node("text entry", "Message", 1547, 500, 440, 300, 24,
                states=(STATE_EDITABLE, STATE_ENABLED), editable=True)
    vol = _Node("slider", "volume", 1547, 500, 480, 200, 20,
                states=(STATE_ENABLED,), editable=True)
    app = _Node("application", "slack", 1547, children=[send, msg, vol])

    class _Desktop:
        def __init__(s):
            s._apps = [app]

        @property
        def childCount(s):
            return len(s._apps)

        def getChildAtIndex(s, i):
            return s._apps[i]

    class _Registry:
        @staticmethod
        def getDesktop(n):
            return _Desktop()

    fake = types.ModuleType("pyatspi")
    for n, v in {"Registry": _Registry, "DESKTOP_coords": DESKTOP_coords,
                 "STATE_CHECKED": STATE_CHECKED, "STATE_FOCUSED": STATE_FOCUSED,
                 "STATE_SELECTED": STATE_SELECTED, "STATE_SHOWING": STATE_SHOWING,
                 "STATE_VISIBLE": STATE_VISIBLE, "STATE_ENABLED": STATE_ENABLED,
                 "STATE_SENSITIVE": STATE_SENSITIVE, "STATE_EDITABLE": STATE_EDITABLE}.items():
        setattr(fake, n, v)
    sys.modules["pyatspi"] = fake
    return send, msg, vol


def _stub_window(monkeypatch):
    """Stub the window lookup so the test needs no live, matching window."""
    import kwin_bridge.a11y as a11y
    import kwin_bridge.windows as wmod
    import kwin_bridge.input as inmod
    from kwin_bridge.windows import Window

    stub = Window(window_id="{fake-1111-2222-3333-444455556666}",
                  title="slack", app_name="slack", pid=1547,
                  x=0, y=0, width=1000, height=800, visible=True)
    monkeypatch.setattr(wmod, "is_uuid", lambda c: True)
    monkeypatch.setattr(wmod, "get_window", lambda wid: stub)
    monkeypatch.setattr(wmod, "list_windows", lambda: [stub])
    clicks = []
    monkeypatch.setattr(inmod, "click_window",
                        lambda wid, x, y, button="left", double=False: clicks.append((x, y)))
    return a11y, stub, clicks


def test_semantic_lifecycle(monkeypatch):
    _install_fake_pyatspi()
    import kwin_bridge.a11y as a11y
    monkeypatch.setattr(a11y, "_backend", lambda: "pyatspi")  # force pyatspi path
    a11y, stub, clicks = _stub_window(monkeypatch)

    st = a11y.get_window_state(stub.window_id)
    assert st["available"]
    idx = {e["name"]: e for e in st["elements"]}
    assert set(idx) >= {"Send", "Message", "volume"}
    # index numbering is interactive-only and consistent
    assert idx["Send"]["index"] == 0
    assert idx["Message"]["index"] == 1
    assert idx["volume"]["index"] == 2
    assert idx["Message"]["editable"] is True
    assert "enabled" in idx["Send"]["states"]
    assert idx["Send"]["actions"] == ["press"]

    # semantic search
    btns = a11y.resolve_elements(stub.window_id, role="button")
    assert btns and btns[0].name == "Send"
    msgs = a11y.resolve_elements(stub.window_id, text="message")
    assert msgs and msgs[0].name == "Message"

    # action
    assert a11y.perform_action(stub.window_id, btns[0].index, action="press")["ok"]

    # set value on editable + reject on non-editable
    send, msg, vol = _install_fake_pyatspi()  # rebuild fresh; keep refs
    sv = a11y.set_value(stub.window_id, msgs[0].index, "hello")
    assert sv["ok"]
    bad = a11y.set_value(stub.window_id, btns[0].index, "x")
    assert not bad["ok"]

    # click_element maps to the element center; click_semantic uses text/role
    cl = a11y.click_element(stub.window_id, btns[0].index)
    assert cl["ok"] and cl["center_screen"] == [530, 414]
    assert clicks == [(530, 414)]
    clicks.clear()
    cs = a11y.click_semantic(stub.window_id, role="slider")
    assert cs["ok"] and cs["element"] == 2
    assert clicks == [(600, 490)]


def test_missing_pyatspi_degrades_gracefully(monkeypatch):
    import kwin_bridge.a11y as a11y
    sys.modules.pop("pyatspi", None)
    monkeypatch.setattr(a11y, "_atspi_available", lambda: False)
    monkeypatch.setattr(a11y, "_backend", lambda: None)  # no backend available
    st = a11y.get_window_state("{fake}")
    assert st["available"] is False
    assert a11y.resolve_elements("{fake}", role="button") == []
    assert a11y.perform_action("{fake}", 0)["ok"] is False


def test_dbus_backend_live():
    """On a machine with a reachable at-spi bus, the pure-D-Bus backend works.

    Skips when there is no at-spi socket (e.g. a headless CI box).
    """
    import kwin_bridge.atspi_dbus as adb
    if not adb.available():
        pytest.skip("no at-spi bus available on this host")
    assert adb.available()
    # The registry root must expose at least the accessibility apps.
    apps = list(adb._application_nodes())
    assert len(apps) > 0
    # Every app should resolve a PID + bus name.
    for bus, path, pid, name in apps[:5]:
        assert bus.startswith(":") and path.startswith("/")
        assert pid is None or pid > 0
