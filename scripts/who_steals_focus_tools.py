"""Like who_steals_focus.py, but one MCP *tool* at a time, through the server over stdio.

Run it with a Python that has the `mcp` client SDK (>= 1.10).
"""

import asyncio
import ctypes
import os
import subprocess
import time
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

EXE = str(Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "autocad-mcp.exe")
TMP = Path.home() / "AppData" / "Local" / "Temp" / "who_steals_focus"
TMP.mkdir(exist_ok=True)


def fg():
    user32 = ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 256)
    return buf.value[:40]


def notepad():
    p = subprocess.Popen(["notepad.exe"])
    for _ in range(40):
        time.sleep(0.1)
        if "Notepad" in fg():
            break
    return p


WALL = [
    {
        "id": "W",
        "axis": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]],
        "thickness": 200,
        "material": "brick",
        "closed": True,
    }
]
STEPS = [
    ("drawing_new", {}),
    ("layer_create", {"name": "MUROS", "color": 7}),
    ("entity_create_line", {"x1": 0, "y1": 0, "x2": 1000, "y2": 0, "layer": "MUROS"}),
    ("arch_wall", {"walls": WALL, "poche": True}),
    ("arch_room", {"name": "SALA", "x": 3000, "y": 2000}),
    ("view_zoom_extents", {}),
    ("view_screenshot", {}),
    ("drawing_save_as", {"path": str(TMP / "tools.dxf")}),
    ("drawing_close", {}),
]


async def main():
    env = {
        **os.environ,
        "AUTOCAD_MCP_BACKEND": "com",
        "TOOL_PACKS": "core,arch",
        "ALLOWED_PATHS": str(TMP),
    }
    procs = []
    async with Client(
        StdioServerParameters(command=EXE, args=[], env=env), read_timeout_seconds=120
    ) as c:
        for name, args in STEPS:
            procs.append(notepad())
            before = fg()
            r = await c.call_tool(name, args)
            time.sleep(1.5)
            after = fg()
            err = (
                " ERROR " + " ".join(getattr(x, "text", "") for x in r.content)[:80]
                if r.is_error
                else ""
            )
            print(
                f"{name:<20} before={before:<20} after={after:<40}{'  <-- STOLE FOCUS' if 'AutoCAD' in after else ''}{err}",
                flush=True,
            )
    for p in procs:
        p.kill()


asyncio.run(main())
