"""The COM message filter (backends/com_filter.py) and the promise that nothing here ever leaves AutoCAD.

AutoCAD answers RPC_E_CALL_REJECTED while it switches documents, and longer when it is not the foreground
app: measured on 2026-10-18 (AutoCAD 2027, scripts/stress_switch.py, 15 rounds of Documents.Add + AddLine +
AddHatch + SaveAs + Close with AutoCAD in the background): 12 of 15 rounds failed without the filter, 15 of
15 passed with it (7 retries). These tests pin the retry policy, the registration, and that no module drives
the keyboard, the mouse or the window focus.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from backends import com_filter as cf

ROOT = Path(__file__).resolve().parents[1]


def test_a_busy_callee_is_retried_after_the_pause():
    f = cf.RetryFilter(budget_s=10, pause_ms=150)
    assert f.retry_delay_ms(0, cf.SERVERCALL_RETRYLATER) == 150
    assert f.retry_delay_ms(9_999, cf.SERVERCALL_RETRYLATER) == 150


def test_the_retry_stops_at_the_budget():
    f = cf.RetryFilter(budget_s=10, pause_ms=150)
    assert f.retry_delay_ms(10_000, cf.SERVERCALL_RETRYLATER) == cf.CANCEL


def test_a_call_rejected_outright_is_not_retried():
    f = cf.RetryFilter(budget_s=10)
    assert f.retry_delay_ms(0, 1) == cf.CANCEL  # SERVERCALL_REJECTED


def test_the_com_methods_answer_as_a_client_filter_should():
    f = cf.RetryFilter(budget_s=10)
    assert f._handle_incoming(None, 0, None, 0, None) == cf.SERVERCALL_ISHANDLED
    assert f._message_pending(None, None, 0, 0) == cf.PENDINGMSG_WAITDEFPROCESS
    assert f._retry_rejected(None, None, 100, cf.SERVERCALL_RETRYLATER) == f.pause_ms
    assert f.retries == 1


@pytest.mark.skipif(sys.platform != "win32", reason="COM apartments are Windows only")
def test_it_registers_on_an_sta_thread_and_unregisters():
    pythoncom = pytest.importorskip("pythoncom")
    pythoncom.CoInitialize()
    try:
        flt = cf.register(budget_s=5)
        assert flt is not None and cf.current() is flt
        cf.unregister()
        assert cf.current() is None
    finally:
        pythoncom.CoUninitialize()


def test_the_com_thread_registers_the_filter():
    src = (ROOT / "backends" / "com_backend.py").read_text(encoding="utf-8")
    init = src[src.index("def _com_init") : src.index("def _com_teardown")]
    assert "com_filter.register(" in init
    teardown = src[src.index("def _com_teardown") : src.index("def _apoint")]
    assert "com_filter.unregister()" in teardown


#: Anything that could put a keystroke, a click or the focus anywhere but inside AutoCAD's own API.
FORBIDDEN = re.compile(
    r"\b(SendKeys|SendInput|keybd_event|mouse_event|SetForegroundWindow|BringWindowToTop|SetActiveWindow|"
    r"SetFocus|AppActivate|AttachThreadInput|WM_KEYDOWN|WM_CHAR|pyautogui|pynput|import keyboard|"
    r"from keyboard|import mouse|from mouse)\b"
)


def test_no_module_drives_the_keyboard_the_mouse_or_the_focus():
    """Commands reach AutoCAD through its COM API only (SendCommand included: it is AutoCAD's own command
    queue, not the keyboard), so nothing can land in another application, whatever window is in front."""
    hits = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if (
            rel.parts[0] in ("tests", ".venv", "venv", "build", "dist")
            or "site-packages" in rel.parts
        ):
            continue
        for n, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if FORBIDDEN.search(line) and not line.lstrip().startswith("#"):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert hits == []
