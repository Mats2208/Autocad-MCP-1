"""Stress the document switch: Documents.Add, work on it at once (as drawing_new + arch_wall do), Close.

python scripts/stress_switch.py sin 15
python scripts/stress_switch.py con 15
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
from stress_com import _hatch, pt  # noqa: E402

from backends import com_filter  # noqa: E402


def main(mode: str, n: int) -> None:
    pythoncom.CoInitialize()
    flt = com_filter.register(budget_s=60) if mode == "con" else None
    app = win32com.client.GetActiveObject("AutoCAD.Application")
    fails = collections.Counter()
    ok = 0
    t0 = time.monotonic()
    for i in range(n):
        doc = None
        try:
            doc = app.Documents.Add()
            ms = doc.ModelSpace
            _ = ms.AddLine(pt(0, 0), pt(100, 0)).ObjectName
            _hatch(ms, i)
            _ = ms.Count
            doc.SaveAs(str(Path.home() / "AppData" / "Local" / "Temp" / f"stress_{i}.dxf"), 61)
            ok += 1
        except pywintypes.com_error as exc:
            fails[f"com_error {exc.args[0]:#x} {exc.args[1]}"] += 1
        except AttributeError as exc:
            fails[f"AttributeError {exc}"] += 1
        finally:
            for _ in range(60):
                try:
                    if doc is not None:
                        doc.Close(False)
                    break
                except (pywintypes.com_error, AttributeError):
                    time.sleep(0.5)
    print(
        f"mode={mode} rounds_ok={ok}/{n} in {time.monotonic() - t0:.1f}s retries={flt.retries if flt else '-'}"
    )
    for k, v in fails.most_common(8):
        print("   ", v, k)
    com_filter.unregister()
    pythoncom.CoUninitialize()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 15)
