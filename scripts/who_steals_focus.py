"""Which AutoCAD COM call brings AutoCAD to the foreground? Before each step a fresh Notepad takes the foreground;
after it, the foreground window is read. No call here moves focus itself.

    python scripts/who_steals_focus.py
"""

import subprocess
import sys
import time
from pathlib import Path

import pythoncom
import win32com.client
import win32gui

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backends import com_filter  # noqa: E402

TMP = Path.home() / "AppData" / "Local" / "Temp" / "who_steals_focus"
TMP.mkdir(exist_ok=True)


def fg() -> str:
    return win32gui.GetWindowText(win32gui.GetForegroundWindow())[:40]


def notepad():
    p = subprocess.Popen(["notepad.exe"])
    for _ in range(40):
        time.sleep(0.1)
        if "Notepad" in fg():
            break
    return p


def pt(x, y):
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(x), float(y), 0.0])


pythoncom.CoInitialize()
com_filter.register(30)
app = win32com.client.GetActiveObject("AutoCAD.Application")
state = {}
steps = [
    ("Documents.Add", lambda: state.update(doc=app.Documents.Add())),
    ("ModelSpace.AddLine", lambda: state["doc"].ModelSpace.AddLine(pt(0, 0), pt(100, 0))),
    ("Layers.Add", lambda: state["doc"].Layers.Add("PRUEBA")),
    ("ZoomExtents", lambda: app.ZoomExtents()),
    ("SaveAs", lambda: state["doc"].SaveAs(str(TMP / "focus.dxf"), 61)),
    ("Documents.Open", lambda: state.update(doc2=app.Documents.Open(str(TMP / "focus.dxf")))),
    ("Activate (other doc)", lambda: state["doc"].Activate()),
    ("Close", lambda: state["doc2"].Close(False)),
    ("Close", lambda: state["doc"].Close(False)),
]
procs = []
for name, step in steps:
    procs.append(notepad())
    before = fg()
    try:
        step()
        err = ""
    except Exception as exc:  # noqa: BLE001
        err = f" ERROR {exc!r}"[:90]
    time.sleep(1.5)
    after = fg()
    print(
        f"{name:<22} before={before:<22} after={after:<40}{'  <-- STOLE FOCUS' if 'AutoCAD' in after else ''}{err}"
    )
for p in procs:
    p.kill()
com_filter.unregister()
