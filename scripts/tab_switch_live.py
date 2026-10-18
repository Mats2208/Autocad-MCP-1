"""Live: the server creates its drawing, then another drawing comes to the front (as when the operator clicks a
tab), then the server draws and saves. Prints where the line and the file went.

    python scripts/tab_switch_live.py <autocad-mcp.exe>     (a Python with the mcp client SDK)
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

EXE = sys.argv[1]
TMP = Path(tempfile.gettempdir()) / "tab_switch_live"
TMP.mkdir(exist_ok=True)
HELPER = Path(__file__).resolve().parents[1] / ".venv" / "Scripts" / "python.exe"
SWITCH = (
    "import pythoncom, win32com.client; pythoncom.CoInitialize(); "
    "app = win32com.client.GetActiveObject('AutoCAD.Application'); "
    "d = app.Documents.Add(); d.Activate(); print(d.Name)"
)
COUNT = (
    "import pythoncom, win32com.client, json; pythoncom.CoInitialize(); "
    "app = win32com.client.GetActiveObject('AutoCAD.Application'); "
    "print(json.dumps({d.Name: d.ModelSpace.Count for d in app.Documents}))"
)


def txt(r):
    return " ".join(getattr(c, "text", "") for c in r.content)


async def main():
    env = {
        **os.environ,
        "AUTOCAD_MCP_BACKEND": "com",
        "TOOL_PACKS": "core",
        "ALLOWED_PATHS": str(TMP),
    }
    async with Client(
        StdioServerParameters(command=EXE, args=[], env=env), read_timeout_seconds=120
    ) as c:
        mine = json.loads(txt(await c.call_tool("drawing_new", {})))["name"]
        other = subprocess.run(
            [str(HELPER), "-c", SWITCH], capture_output=True, text=True
        ).stdout.strip()
        await c.call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 5000, "y2": 0})
        saved = txt(await c.call_tool("drawing_save_as", {"path": str(TMP / "mio.dxf")}))
        counts = json.loads(
            subprocess.run([str(HELPER), "-c", COUNT], capture_output=True, text=True).stdout
        )
        print(f"server drawing={mine} operator's tab={other}")
        print("entities per open drawing:", counts)
        print("save_as:", saved[:160])
        await c.call_tool("drawing_close", {})
    close = (
        "import pythoncom, win32com.client; pythoncom.CoInitialize(); "
        "app = win32com.client.GetActiveObject('AutoCAD.Application'); "
        f"[d.Close(False) for d in list(app.Documents) if d.Name in ({other!r}, 'mio.dxf', {mine!r})]"
    )
    subprocess.run([str(HELPER), "-c", close], capture_output=True, text=True)


asyncio.run(main())
