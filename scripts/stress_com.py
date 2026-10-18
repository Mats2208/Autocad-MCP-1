"""Stress the COM path the MCP uses (AddLine/AddCircle/AddHatch + .ObjectName) in a scratch drawing, while AutoCAD
is in the background, and count failures by kind, with (`con`) and without (`sin`) backends.com_filter.

    python scripts/stress_com.py sin 400
    python scripts/stress_com.py con 400
"""

from __future__ import annotations

import collections
import sys
import time
from pathlib import Path

import pythoncom
import pywintypes
import win32com.client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backends import com_filter  # noqa: E402


def pt(x, y):
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(x), float(y), 0.0])


def objs(items):
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, items)


def main(mode: str, n: int) -> None:
    pythoncom.CoInitialize()
    flt = com_filter.register(budget_s=60) if mode == "con" else None
    app = win32com.client.GetActiveObject("AutoCAD.Application")
    doc = app.Documents.Add()
    time.sleep(2)
    ms = doc.ModelSpace
    fails = collections.Counter()
    ok = 0
    t0 = time.monotonic()
    for i in range(n):
        for label, call in (
            (
                "AddLine.ObjectName",
                lambda i=i: ms.AddLine(pt(i * 10, 0), pt(i * 10, 100)).ObjectName,
            ),
            ("AddHatch", lambda i=i: _hatch(ms, i)),
            ("ModelSpace.Count", lambda: ms.Count),
        ):
            try:
                call()
                ok += 1
            except pywintypes.com_error as exc:
                fails[f"{label}: com_error {exc.args[0]:#x}"] += 1
            except AttributeError as exc:
                fails[f"{label}: AttributeError {exc}"] += 1
    dt = time.monotonic() - t0
    print(
        f"mode={mode} calls_ok={ok} fails={sum(fails.values())} in {dt:.1f}s retries={flt.retries if flt else '-'}"
    )
    for k, v in fails.most_common(8):
        print("   ", v, k)
    for _ in range(40):
        try:
            doc.Close(False)
            break
        except (pywintypes.com_error, AttributeError):
            time.sleep(0.5)
    com_filter.unregister()
    pythoncom.CoUninitialize()


def _hatch(ms, i):
    c = ms.AddCircle(pt(i * 10, 200), 4.0)
    h = ms.AddHatch(1, "SOLID", True)
    h.AppendOuterLoop(objs([c]))
    h.Evaluate()
    return h.ObjectName


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 300)
