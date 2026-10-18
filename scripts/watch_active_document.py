"""Logs AutoCAD's ActiveDocument and the foreground window every 100 ms, only when either changes.

python scripts/watch_active_document.py <seconds> <out.log>
"""

import sys
import time
from pathlib import Path

import pythoncom
import win32com.client
import win32gui

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backends import com_filter  # noqa: E402

pythoncom.CoInitialize()
com_filter.register(10)
app = win32com.client.GetActiveObject("AutoCAD.Application")
end = time.monotonic() + float(sys.argv[1])
last = None
t0 = time.monotonic()
with open(sys.argv[2], "w", encoding="utf-8") as log:
    while time.monotonic() < end:
        try:
            active = str(app.ActiveDocument.Name) if app.Documents.Count else "-"
        except Exception as exc:  # noqa: BLE001
            active = f"ERR {type(exc).__name__}"
        fg = win32gui.GetWindowText(win32gui.GetForegroundWindow())[:60]
        if (active, fg) != last:
            log.write(f"{time.monotonic() - t0:7.2f}s active={active:<22} foreground={fg}\n")
            log.flush()
            last = (active, fg)
        time.sleep(0.1)
com_filter.unregister()
