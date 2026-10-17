"""AutoCAD for Mac: the live backend and its AutoLISP bridge.

AutoCAD for Mac has no COM, so ``MacLiveBackend`` keeps ezdxf as the model and
mirrors it into a running AutoCAD through ``backends/mac_bridge.py``. These tests
need no AutoCAD: the bridge is replaced by a recorder, which is exactly the seam
the backend is written against. What they pin is the behaviour that protects the
operator:

* nothing is sent to AutoCAD until a call needs it (a debug session left open
  locks AutoCAD until it is restarted);
* edits made by hand are never overwritten silently;
* only the server's own ``mcp-live`` document is ever pushed into;
* the adapter that kills its parent is never started as a child of this process.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys

import pytest

from backends import mac_bridge
from backends.mac_bridge import (
    BRIDGE_VERSION,
    BridgeError,
    MacBridge,
    NoLiveDocument,
    find_autocad_app,
    lisp_string,
    parse_lisp_strings,
)
from backends.mac_live_backend import MacLiveBackend


class FakeBridge:
    """Records what the backend asks AutoCAD to do."""

    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.live_dxf = tmp_path / "mcp-live.dxf"
        self.app_name = "AutoCAD 2027"
        self.app_path = tmp_path / "AutoCAD 2027.app"
        self.calls: list[str] = []
        self.pushes: list[tuple] = []
        self.states = ["claimed"]  # what ensure_live_document answers, then "present"
        self.ensure_calls: list[bool] = []
        self.signature = "1|A|0,0|1,1"
        self.next_push = ("ok", "2|B|0,0|2,2")

    def install(self):
        return self.dir / "mcp_bridge.lsp"

    def autocad_pid(self):
        return 4242

    async def call(self, form, timeout=None):
        self.calls.append(form)
        return '"ok"'

    async def ensure_live_document(self, open_if_needed=False, prepare=None):
        self.ensure_calls.append(open_if_needed)
        state = self.states.pop(0) if self.states else "present"
        if state == "absent":
            if not open_if_needed:
                raise NoLiveDocument("no live drawing is active")
            return "opened"
        return state

    async def push(self, path, expect, force, zoom):
        self.pushes.append((expect, force, zoom))
        return list(self.next_push)

    async def pull(self, path):
        import ezdxf

        ezdxf.new("R2010").saveas(str(path))
        return ["ok", "3|C|0,0|3,3"]

    async def close(self):
        self.calls.append("close")


@pytest.fixture
def backend(tmp_path):
    b = MacLiveBackend()
    b.bridge = FakeBridge(tmp_path)
    b._debounce = 0.05
    return b


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_lisp_string_escapes_quotes_and_backslashes():
    assert lisp_string('a"b\\c') == '"a\\"b\\\\c"'


def test_parse_lisp_strings_round_trips_what_the_bridge_prints():
    assert parse_lisp_strings('("conflict" "3|6E|0,0|1,1")') == ["conflict", "3|6E|0,0|1,1"]
    assert parse_lisp_strings('("a\\"b")') == ['a"b']


def test_bridge_version_follows_the_lisp_text():
    # A copy of the bridge already loaded in AutoCAD from an older server must
    # not be trusted: the version is a fingerprint of the text, not a counter.
    assert len(BRIDGE_VERSION) == 8
    assert (
        mac_bridge.hashlib.sha1(mac_bridge._BRIDGE_LISP.encode())
        .hexdigest()
        .startswith(BRIDGE_VERSION)
    )


def test_the_bridge_lisp_only_erases_the_live_document():
    text = mac_bridge._BRIDGE_LISP
    guard = text.index("(not (mcp:live-p))")
    assert guard < text.index('"_.ERASE"'), "ERASE must come after the live-document guard"
    assert "@STEM@" in text  # the stem is substituted from LIVE_DOC_STEM at install time


@pytest.mark.skipif(sys.platform != "darwin", reason="reads /Applications")
def test_find_autocad_app_never_picks_the_plot_style_editor():
    app = find_autocad_app()
    if app is not None:
        assert app.name.startswith("AutoCAD 20") and "Plot" not in app.name


def test_find_autocad_app_override(tmp_path, monkeypatch):
    fake = tmp_path / "AutoCAD 2099.app"
    fake.mkdir()
    monkeypatch.setenv("AUTOCAD_MCP_MAC_APP", str(fake))
    assert find_autocad_app() == fake


def test_the_helpers_are_sent_inline_and_never_loaded_from_a_file():
    """A ``load`` of any file outside AutoCAD's trusted paths raises a modal
    "File Loading - Security Concern" that blocks the whole session, and AutoCAD
    for Mac does not trust its own Support folder. So nothing is loaded."""
    defs = mac_bridge._BRIDGE_DEFS
    assert "\n" not in defs and "@" not in defs and ";" not in defs
    assert f'(setq mcp:ver "{BRIDGE_VERSION}")' in defs
    assert '"MCP-LIVE*"' in defs
    assert "(load " not in defs and "(load " not in inspect.getsource(mac_bridge.MacBridge)


def test_install_touches_nothing_inside_autocad(tmp_path, monkeypatch):
    app = tmp_path / "AutoCAD 2099.app"
    app.mkdir()
    monkeypatch.setenv("AUTOCAD_MCP_MAC_APP", str(app))
    bridge = MacBridge(tmp_path / "bridge")
    assert bridge.install() == tmp_path / "bridge"
    if sys.platform != "win32":  # Windows has no POSIX permission bits
        assert oct((tmp_path / "bridge").stat().st_mode & 0o777) == oct(0o700)
    assert not hasattr(mac_bridge, "_support_dir")


# ── the adapter must never be a child of the server ──────────────────────────


def _ancestors(pid: int) -> list[int]:
    chain = []
    while pid > 1:
        out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, check=False
        ).stdout.strip()
        if not out:
            break
        pid = int(out)
        chain.append(pid)
    return chain


@pytest.mark.skipif(sys.platform == "win32", reason="posix process model")
@pytest.mark.asyncio
async def test_adapter_is_not_a_descendant_of_the_server(tmp_path):
    """The adapter kills its parent on disconnect, and a client that kills the whole
    process tree on shutdown must not be able to kill it mid-session (AutoCAD would
    stay marked "being used to debug another file" until restarted)."""
    pidfile = tmp_path / "pid"
    script = tmp_path / "fake_adapter"
    script.write_text(f"#!/bin/sh\necho $$ > '{pidfile}'\nsleep 5\n")
    script.chmod(0o755)
    adapter = mac_bridge._Adapter(script, 1, asyncio.get_running_loop())
    try:
        for _ in range(50):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.1)
        child = int(pidfile.read_text())
        chain = _ancestors(child)
        assert chain, "the adapter must have a parent to be killed"
        assert os.getpid() not in chain  # not a descendant: a tree-kill cannot reach it
    finally:
        adapter.disconnect_now()


# ── backend behaviour ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_sends_nothing_to_autocad(backend):
    await backend.connect()
    assert backend.bridge.calls == []
    assert backend.bridge.pushes == []


@pytest.mark.asyncio
async def test_a_burst_of_edits_is_pushed_once(backend):
    await backend.connect()
    await backend.drawing_new()
    for i in range(5):
        await backend.entity_create_circle(i, i, 1)
    await asyncio.sleep(0.4)
    assert len(backend.bridge.pushes) == 1
    assert (backend.bridge.dir / "push.dxf").exists()
    # The file an open document came from is never rewritten under AutoCAD.
    assert not backend.bridge.live_dxf.exists()
    status = await backend.system_status()
    assert status["backend"] == "autocad-mac"
    assert status["live"]["shown_in_autocad"] is True
    assert status["live"]["model_ahead_of_autocad"] is False


@pytest.mark.asyncio
async def test_first_push_after_claiming_does_not_check_for_hand_edits(backend):
    await backend.connect()
    await backend.drawing_new()
    await backend.flush()
    assert backend.bridge.pushes[0][0] is None  # nothing to compare with yet
    await backend.entity_create_circle(0, 0, 1)
    await backend.flush()
    assert backend.bridge.pushes[1][0] == "2|B|0,0|2,2"  # the fingerprint of our last push


@pytest.mark.asyncio
async def test_a_hand_edited_live_drawing_is_not_overwritten(backend):
    await backend.connect()
    await backend.drawing_new()
    await backend.flush()
    backend.bridge.next_push = ("conflict", "9|Z|0,0|9,9")
    await backend.entity_create_circle(0, 0, 1)
    out = await backend.flush()
    assert out["ok"] is False and out["conflict"] is True
    status = await backend.system_status()
    assert status["live"]["conflict"] is True
    assert status["live"]["model_ahead_of_autocad"] is True


@pytest.mark.asyncio
async def test_attach_pulls_autocad_into_the_model_and_clears_the_conflict(backend):
    await backend.connect()
    await backend.drawing_new()
    await backend.flush()
    backend.bridge.next_push = ("conflict", "9|Z|0,0|9,9")
    await backend.entity_create_circle(0, 0, 1)
    await backend.flush()

    async def no_launch(visible=True):
        return False

    backend._launch_autocad = no_launch
    out = await backend.system_launch()
    assert out["attached"] is True and out["launched"] is False
    assert out["pulled_from"] == "mcp-live (AutoCAD)"
    status = await backend.system_status()
    assert status["live"]["conflict"] is False
    assert status["live"]["model_ahead_of_autocad"] is False


@pytest.mark.asyncio
async def test_a_bridge_failure_never_fails_the_tool_call(backend):
    await backend.connect()

    async def boom(*a, **k):
        raise BridgeError("The Mac is locked")

    backend.bridge.ensure_live_document = boom
    await backend.drawing_new()
    info = await backend.entity_create_circle(0, 0, 1)  # still works headlessly
    assert info.type == "CIRCLE"
    out = await backend.flush()
    assert out["ok"] is False and "locked" in out["error"]
    assert (await backend.system_status())["live"]["last_error"] == "The Mac is locked"


@pytest.mark.asyncio
async def test_run_lisp_and_run_command_report_that_the_model_is_stale(backend):
    await backend.connect()
    await backend.drawing_new()
    out = await backend.system_run_lisp("(+ 1 2)")
    assert out["ok"] is True
    assert (await backend.system_status())["live"]["autocad_changed_by_lisp_or_command"] is True
    out = await backend.system_run_command("ZOOM E")
    assert out["ok"] is True
    assert any('(command "ZOOM" "E")' in c for c in backend.bridge.calls)


@pytest.mark.asyncio
async def test_empty_run_command_is_refused(backend):
    await backend.connect()
    assert (await backend.system_run_command("   "))["ok"] is False


def test_capability_keys_match_the_headless_engine(backend):
    from backends.ezdxf_backend import EzdxfBackend

    live = backend.capabilities()
    assert live.backend == "autocad-mac"
    assert set(live.features) == set(EzdxfBackend().capabilities().features)
    assert live.features["live_application"].supported is True
    assert live.features["dwg_write"].supported is True


@pytest.mark.asyncio
async def test_dwg_version_is_validated(backend):
    await backend.connect()
    with pytest.raises(ValueError):
        await backend.drawing_export_dwg(str(backend.bridge.dir / "x.dwg"), "R1999")


@pytest.mark.asyncio
async def test_the_background_mirror_never_opens_a_document(backend):
    await backend.connect()
    await backend.drawing_new()
    backend.bridge.states = ["absent"]
    out = await backend.flush()
    assert out["ok"] is False and out["waiting"] is True
    assert backend.bridge.ensure_calls == [False]  # opening needs the OS and steals focus
    assert backend.bridge.pushes == []
    status = await backend.system_status()
    assert "no live drawing" in status["live"]["waiting_for_live_document"]
    assert status["live"]["last_error"] is None


@pytest.mark.asyncio
async def test_the_mirror_retries_silently_until_a_live_drawing_is_active(backend, monkeypatch):
    monkeypatch.setattr("backends.mac_live_backend.WAIT_RETRY", 0.05)
    await backend.connect()
    backend.bridge.states = ["absent", "absent", "claimed"]
    await backend.drawing_new()
    await backend.entity_create_circle(0, 0, 1)
    await asyncio.sleep(0.6)
    assert len(backend.bridge.pushes) == 1
    assert set(backend.bridge.ensure_calls) == {False}
    assert (await backend.system_status())["live"]["waiting_for_live_document"] is None


@pytest.mark.asyncio
async def test_only_system_launch_may_open_a_document(backend):
    await backend.connect()
    await backend.drawing_new()
    backend.bridge.states = ["absent"]

    async def no_launch(visible=True):
        return False

    backend._launch_autocad = no_launch
    out = await backend.system_launch()
    assert out["live_document"] == "opened"
    assert True in backend.bridge.ensure_calls
    assert backend.bridge.pushes, "the model is shown in the freshly opened drawing"
    assert out["pulled_from"] is None  # a new drawing has nothing to pull


@pytest.mark.asyncio
async def test_screenshot_falls_back_to_the_headless_render_when_nothing_is_live(backend):
    await backend.connect()
    await backend.drawing_new()
    await backend.entity_create_circle(0, 0, 1)
    backend.bridge.states = ["absent", "absent"]
    png = await backend.view_screenshot()
    assert png[:4] == b"\x89PNG"
