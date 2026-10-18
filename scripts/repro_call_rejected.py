"""Live repro of RPC_E_CALL_REJECTED against a running AutoCAD, with and without backends.com_filter.

    python scripts/repro_call_rejected.py sin   # no filter: the calls made while AutoCAD is busy fail
    python scripts/repro_call_rejected.py con   # with the filter: they wait and succeed

Opens its own new drawing, keeps AutoCAD busy with a LISP loop started by SendCommand from a second STA
thread, and meanwhile reads Documents.Count / ModelSpace.Count and adds a LINE from this thread. Closes the
drawing without saving. Sends no keystrokes and never activates a window.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pythoncom
import pywintypes
import win32com.client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backends import com_filter  # noqa: E402

BUSY_LISP = "(progn (setq i 0) (repeat 60000000 (setq i (1+ i))) (princ))\n"


def busy(doc_name: str, started: threading.Event) -> None:
    pythoncom.CoInitialize()
    com_filter.register(
        budget_s=60
    )  # this thread only sets the stage: it must not fail on a rejection itself
    try:
        app = win32com.client.GetActiveObject("AutoCAD.Application")
        doc = next(d for d in app.Documents if d.Name == doc_name)
        started.set()
        t = time.monotonic()
        doc.SendCommand(BUSY_LISP)
        print(f"  [busy] SendCommand returned after {time.monotonic() - t:.2f} s")
    finally:
        started.set()
        com_filter.unregister()
        pythoncom.CoUninitialize()


def main(mode: str) -> None:
    pythoncom.CoInitialize()
    flt = com_filter.register(budget_s=60) if mode == "con" else None
    print("filter:", "registered" if flt else "none")
    app = win32com.client.GetActiveObject("AutoCAD.Application")
    for d in list(app.Documents):  # a test drawing left behind by an interrupted run
        if d.Name.startswith("Drawing") and d.FullName == "" and d.ModelSpace.Count == 0:
            d.Close(False)
    doc = app.Documents.Add()
    switch = []
    for label, call in (
        ("Documents.Count after Add", lambda: app.Documents.Count),
        ("ActiveDocument.Name after Add", lambda: app.ActiveDocument.Name),
    ):
        t = time.monotonic()
        try:
            switch.append((label, "OK", call(), round(time.monotonic() - t, 2)))
        except (pywintypes.com_error, AttributeError) as exc:
            switch.append((label, "FAIL", repr(exc)[:120], round(time.monotonic() - t, 2)))
    for r in switch:
        print("  ", *r)
    time.sleep(2.0)
    name = doc.Name
    print("test drawing:", name)
    try:
        started = threading.Event()
        th = threading.Thread(target=busy, args=(name, started))
        th.start()
        started.wait()
        time.sleep(1.0)  # let the LISP loop start
        results = []
        for label, call in (
            ("Documents.Count", lambda: app.Documents.Count),
            ("ModelSpace.Count", lambda: doc.ModelSpace.Count),
            (
                "AddLine",
                lambda: (
                    doc.ModelSpace.AddLine(
                        win32com.client.VARIANT(
                            pythoncom.VT_ARRAY | pythoncom.VT_R8, [0.0, 0.0, 0.0]
                        ),
                        win32com.client.VARIANT(
                            pythoncom.VT_ARRAY | pythoncom.VT_R8, [100.0, 0.0, 0.0]
                        ),
                    ).ObjectName
                ),
            ),
        ):
            t = time.monotonic()
            try:
                val = call()
                results.append((label, "OK", val, round(time.monotonic() - t, 2)))
            except (pywintypes.com_error, AttributeError) as exc:
                results.append((label, "FAIL", repr(exc)[:120], round(time.monotonic() - t, 2)))
        th.join()
        for r in results:
            print("  ", *r)
        if flt:
            print("  retries done by the filter:", flt.retries)
    finally:
        for _ in range(20):
            try:
                doc.Close(False)
                break
            except (pywintypes.com_error, AttributeError):
                time.sleep(0.5)
        com_filter.unregister()
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sin")
