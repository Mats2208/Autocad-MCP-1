"""Bridge to a running AutoCAD for Mac -- no keyboard, no focus.

AutoCAD for Mac has no COM, no ActiveX and no AppleScript dictionary. It does
ship ``AutoLispDebugAdapter.app``: the Debug Adapter Protocol (DAP) server the
VS Code AutoLISP extension uses. It attaches to a *running* AutoCAD by process
id and evaluates AutoLISP inside it, in the background, whatever the operator is
doing in another application. That is what this module drives:

    initialize -> attach(processId) -> configurationDone -> evaluate ... -> disconnect

Three facts about the adapter shaped the design (all measured on AutoCAD 2027):

* **It kills its own parent** on ``terminate`` / ``disconnect``. It is therefore
  always started behind a throw-away ``/bin/sh`` in its own session; the MCP
  server is never the adapter's parent.
* **A session that is not disconnected cleanly leaves AutoCAD marked "being used
  to debug another file"** -- every later ``attach`` is refused until AutoCAD is
  restarted. So a session is opened lazily, closed after a short idle time, and
  closed from ``atexit`` and from SIGTERM/SIGHUP.
* ``evaluate`` reports its output as DAP ``output`` events, not in the response,
  so each form is wrapped to print a sentinel line carrying its id and result.

The AutoLISP helpers (live-document guard, push, pull) are **not loaded from a
file**. AutoCAD asks "File Loading - Security Concern" for every ``.lsp`` loaded
from outside its trusted paths -- measured on AutoCAD 2027, its own Support
folder is *not* trusted -- and that modal blocks the whole session. They travel
inside the evaluated expression instead, once per document, so nothing is
written into AutoCAD's folders and no dialog can appear.
"""

from __future__ import annotations

import asyncio
import atexit
import glob
import hashlib
import json
import logging
import os
import plistlib
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

__all__ = [
    "BRIDGE_VERSION",
    "BridgeError",
    "MacBridge",
    "NoLiveDocument",
    "find_autocad_app",
    "lisp_string",
    "parse_lisp_strings",
]

log = logging.getLogger("autocad-mcp.mac")


#: The server's own document inside AutoCAD. The bridge refuses to erase
#: anything in a document that is not named like this.
LIVE_DOC_STEM = "mcp-live"

_BRIDGE_LISP = r""";;; AutoCAD MCP Pro -- AutoCAD for Mac bridge helpers. Sent inline, never loaded.
(setq mcp:ver "@VERSION@")

;; The live document is the one named like @STEM@* or one we claimed by marking it.
(defun mcp:live-p ()
  (or (= (getvar "USERS1") "MCPLIVE")
      (wcmatch (strcase (getvar "DWGNAME")) "@STEM@*")))

;; An untitled, unmodified, empty drawing (AutoCAD's own "Drawing1") costs the
;; operator nothing, so it can be claimed without opening a document.
(defun mcp:claimable-p ()
  (and (= (getvar "DWGTITLED") 0) (= (getvar "DBMOD") 0) (not (ssget "_X"))))

(defun mcp:mark ()
  (setvar "USERS1" "MCPLIVE")
  "marked")

(defun mcp:ensure ()
  (cond ((mcp:live-p) "present")
        ((mcp:claimable-p) (mcp:mark) "claimed")
        (T "absent")))

;; Cheap fingerprint of the drawing: used to notice edits made by hand.
(defun mcp:signature (/ ss)
  (strcat
    (itoa (if (setq ss (ssget "_X")) (sslength ss) 0)) "|"
    (if (entlast) (cdr (assoc 5 (entget (entlast)))) "0") "|"
    (rtos (car (getvar "EXTMIN")) 2 3) "," (rtos (cadr (getvar "EXTMIN")) 2 3) "|"
    (rtos (car (getvar "EXTMAX")) 2 3) "," (rtos (cadr (getvar "EXTMAX")) 2 3)))

;; Replace the live document's contents with a DXF. Never touches any other
;; document, and refuses when the drawing was edited since the last push.
(defun mcp:push (path expect force zoom)
  (cond
    ((not (mcp:live-p)) (list "not-live" (getvar "DWGNAME")))
    ((and expect (not force) (/= expect (mcp:signature)))
     (list "conflict" (mcp:signature)))
    (T
     (command "_.-LAYER" "_T" "*" "_U" "*" "_ON" "*" "")
     (command "_.ERASE" "_ALL" "")
     (command "_.-PURGE" "_A" "*" "_N")
     (command "_.-PURGE" "_A" "*" "_N")
     (command "_.DXFIN" path)
     (if zoom (command "_.ZOOM" "_E"))
     (list "ok" (mcp:signature)))))

(defun mcp:pull (path)
  (if (not (mcp:live-p))
    (list "not-live" (getvar "DWGNAME"))
    (progn
      (command "_.DXFOUT" path "_V" "2018" "16")
      (list "ok" (mcp:signature)))))

;; Run FN, print "<<MCP id OK result>>" or "<<MCP id ERR message>>".
(defun mcp:wrap (id fn / res cmdecho)
  (setq cmdecho (getvar "CMDECHO"))
  (setvar "CMDECHO" 0)
  (setq res (vl-catch-all-apply fn))
  (setvar "CMDECHO" cmdecho)
  (princ (strcat "\n<<MCP " id " "
                 (if (vl-catch-all-error-p res)
                   (strcat "ERR " (vl-catch-all-error-message res))
                   (strcat "OK " (vl-prin1-to-string res)))
                 ">>\n"))
  (princ))
"""

#: Fingerprint of ``_BRIDGE_LISP``: helpers defined in AutoCAD by an older server
#: no longer match and are sent again.
BRIDGE_VERSION = hashlib.sha1(_BRIDGE_LISP.encode()).hexdigest()[:8]

#: The helpers as one line (comments dropped), ready to sit inside ``(progn ...)``.
_BRIDGE_DEFS = (
    " ".join(
        line.strip()
        for line in _BRIDGE_LISP.splitlines()
        if line.strip() and not line.strip().startswith(";")
    )
    .replace("@VERSION@", BRIDGE_VERSION)
    .replace("@STEM@", LIVE_DOC_STEM.upper())
)

_ADAPTER_REL = "Contents/Helpers/AutoLispDebugAdapter.app/Contents/MacOS/AutoLispDebugAdapter"


class BridgeError(RuntimeError):
    """The bridge could not reach AutoCAD or AutoCAD did not answer."""


class NoLiveDocument(BridgeError):
    """AutoCAD's active drawing is not the server's, and none could be claimed.

    Background mirroring never opens a document: only the OS can, and doing so
    brings AutoCAD to the foreground. ``system_launch`` is the explicit way.
    """


def lisp_string(value: str) -> str:
    """Quote ``value`` as an AutoLISP string literal."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_lisp_strings(text: str) -> list[str]:
    """``("ok" "3|...")`` -> ``["ok", "3|..."]`` (the only shape the bridge returns)."""
    return [
        m.replace('\\"', '"').replace("\\\\", "\\")
        for m in re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    ]


def find_autocad_app() -> Path | None:
    """The ``AutoCAD <year>.app`` to drive, or None.

    ``AUTOCAD_MCP_MAC_APP`` overrides the lookup (a path to the ``.app``, or its
    name, ``"AutoCAD 2027"``); otherwise the newest one under
    ``/Applications/Autodesk`` wins.
    """
    override = os.environ.get("AUTOCAD_MCP_MAC_APP", "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            hits = glob.glob(f"/Applications/Autodesk/*/{override}.app")
            path = Path(hits[0]) if hits else path
        return path if path.exists() else None
    # "AutoCAD 2027.app" -- not "AutoCAD Plot Style Editor 2027.app" next to it.
    candidates = sorted(
        p
        for p in glob.glob("/Applications/Autodesk/AutoCAD */AutoCAD *.app")
        if re.fullmatch(r"AutoCAD \d{4}\.app", os.path.basename(p))
    )
    return Path(candidates[-1]) if candidates else None


def screen_locked() -> bool:
    """True while the macOS session is locked (the lock screen is up).

    AutoCAD opens no windows in a locked session, so an attach would hang; the
    bridge says so instead.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, check=False, timeout=5
        ).stdout
        info = plistlib.loads(out)
        entry = info[0] if isinstance(info, list) else info
        return bool(entry.get("IOConsoleLocked") or entry.get("CGSSessionScreenIsLocked"))
    except Exception:  # noqa: BLE001 - an unreadable answer must not block the bridge
        return False


def _frame(message: dict) -> bytes:
    body = json.dumps(message).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class _Adapter:
    """One attached DAP session. Threads read the pipe; the caller is async."""

    def __init__(self, adapter_path: Path, pid: int, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._seq = 0
        # The adapter kills its parent when it disconnects, so its parent must be a
        # throw-away sh (the trailing ``:`` stops that sh from exec-ing it).
        # That sh is started *by another sh that exits at once*, which reparents
        # it to launchd: a client that kills the whole process tree of the server
        # on shutdown (the MCP SDK does) then cannot kill the adapter mid-session
        # and leave AutoCAD marked "being used to debug another file".
        self.proc = subprocess.Popen(
            ["/bin/sh", "-c", '/bin/sh -c \'"$0"; :\' "$0" <&0 &', str(adapter_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.pid = pid
        self.inbox: asyncio.Queue[dict | None] = asyncio.Queue()
        self.closed = False
        self.eof = threading.Event()  # the adapter's stdout closed: it is gone
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        out = self.proc.stdout
        try:
            while True:
                length = None
                while True:
                    line = out.readline()
                    if not line:
                        return
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length"):
                        length = int(line.split(b":")[1])
                if length is None:
                    continue
                body = out.read(length)
                try:
                    self._loop.call_soon_threadsafe(self.inbox.put_nowait, json.loads(body))
                except RuntimeError:
                    return
        except Exception as exc:  # noqa: BLE001 - reader must never raise into the void
            log.debug("adapter reader stopped: %s", exc)
        finally:
            self.eof.set()
            try:
                self._loop.call_soon_threadsafe(self.inbox.put_nowait, None)
            except RuntimeError:  # the loop is already closed: nothing is listening
                pass

    def send(self, command: str, arguments: dict | None = None) -> int:
        self._seq += 1
        message = {"seq": self._seq, "type": "request", "command": command}
        if arguments is not None:
            message["arguments"] = arguments
        self.proc.stdin.write(_frame(message))
        self.proc.stdin.flush()
        return self._seq

    async def next(self, timeout: float) -> dict | None:
        return await asyncio.wait_for(self.inbox.get(), timeout=timeout)

    async def request(self, command: str, arguments: dict | None = None, timeout: float = 15):
        """Send and wait for the matching response; returns (response, events)."""
        seq = self.send(command, arguments)
        events: list[dict] = []
        deadline = time.monotonic() + timeout
        while True:
            message = await self.next(max(0.1, deadline - time.monotonic()))
            if message is None:
                raise BridgeError("The AutoLISP debug adapter exited unexpectedly.")
            if message.get("type") == "response" and message.get("request_seq") == seq:
                return message, events
            log.debug("dap <- %s", json.dumps(message)[:300])
            events.append(message)
            # A refused attach never gets a response -- only these events.
            if message.get("event") in ("dgbfatalerr", "terminated"):
                return {"success": False, "request_seq": seq, "command": command}, events

    def disconnect_now(self) -> None:
        """Synchronous, best-effort clean close (atexit / signal safe)."""
        if self.closed:
            return
        self.closed = True
        try:
            self.send("disconnect", {})
        except Exception:  # noqa: BLE001
            pass
        self.eof.wait(timeout=2.0)
        try:  # the adapter may outlive the sh it just killed
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self.proc.poll()  # reap the launcher sh


class MacBridge:
    """Evaluate AutoLISP inside the AutoCAD for Mac that is running now."""

    def __init__(self, bridge_dir: Path | None = None) -> None:
        raw = os.environ.get("AUTOCAD_MCP_MAC_BRIDGE_DIR", "").strip()
        self.dir = Path(bridge_dir or raw or Path.home() / ".autocad-mcp" / "bridge").expanduser()
        self.app_path = find_autocad_app()
        self.app_name = self.app_path.stem if self.app_path else ""
        self.timeout = float(os.environ.get("AUTOCAD_MCP_MAC_TIMEOUT", "45"))
        #: Seconds the debug session stays attached after the last call.
        self.idle = float(os.environ.get("AUTOCAD_MCP_MAC_IDLE", "4"))
        self._lock = asyncio.Lock()
        self._adapter: _Adapter | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._installed = False
        self._hooks_installed = False

    # -- setup -------------------------------------------------------------------

    @property
    def live_dxf(self) -> Path:
        return self.dir / f"{LIVE_DOC_STEM}.dxf"

    def install(self) -> Path:
        """Create the private bridge folder; return it. Nothing goes into AutoCAD's folders."""
        if not self.app_path:
            raise BridgeError("No AutoCAD for Mac found under /Applications/Autodesk.")
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.dir, 0o700)
        self._installed = True
        return self.dir

    def autocad_pid(self) -> int | None:
        """pid of the running AutoCAD (the app's own executable), or None."""
        if not self.app_path:
            return None
        exe = str(self.app_path / "Contents" / "MacOS") + "/"
        out = subprocess.run(
            ["pgrep", "-f", exe], capture_output=True, text=True, check=False
        ).stdout.split()
        return int(out[0]) if out else None

    # -- session -----------------------------------------------------------------

    def _install_exit_hooks(self) -> None:
        if self._hooks_installed:
            return
        self._hooks_installed = True
        atexit.register(self._emergency_close)
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
                previous = signal.getsignal(sig)

                def _handler(signum, frame, _previous=previous):
                    self._emergency_close()
                    if callable(_previous):
                        _previous(signum, frame)
                    else:
                        signal.signal(signum, signal.SIG_DFL)
                        os.kill(os.getpid(), signum)

                try:
                    signal.signal(sig, _handler)
                except (ValueError, OSError):
                    pass

    def _emergency_close(self) -> None:
        adapter = self._adapter
        if adapter is not None:
            adapter.disconnect_now()

    async def _attach(self) -> _Adapter:
        pid = self.autocad_pid()
        if pid is None:
            raise BridgeError(f"{self.app_name or 'AutoCAD'} is not running.")
        adapter_path = self.app_path / _ADAPTER_REL
        if not adapter_path.exists():
            raise BridgeError(f"AutoLISP debug adapter not found: {adapter_path}")
        self._install_exit_hooks()
        loop = asyncio.get_running_loop()
        adapter = _Adapter(adapter_path, pid, loop)
        self._adapter = adapter
        try:
            await adapter.request("initialize", {"adapterID": "autocad-mcp"})
            response, events = await adapter.request("attach", {"processId": pid})
            fatal = [
                e["body"].get("output", "")
                for e in events
                if e.get("event") in ("dgbfatalerr", "output") and isinstance(e.get("body"), dict)
            ]
            if not response.get("success") or any(f for f in fatal):
                text = next((f for f in fatal if f), "attach refused")
                hint = ""
                if "debug another file" in text:
                    hint = (
                        " Either another MCP server is attached to this AutoCAD (only one "
                        "can be), or a previous session was killed without disconnecting "
                        "and AutoCAD must be quit and reopened once to clear it."
                    )
                raise BridgeError(f"Could not attach to AutoCAD: {text}.{hint}")
            await adapter.request("configurationDone", {})
        except BaseException:
            adapter.disconnect_now()
            self._adapter = None
            raise
        return adapter

    async def _detach(self) -> None:
        adapter, self._adapter = self._adapter, None
        if adapter is not None:
            await asyncio.get_running_loop().run_in_executor(None, adapter.disconnect_now)

    def _arm_idle(self) -> None:
        loop = asyncio.get_running_loop()
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._idle_handle = loop.call_later(
            self.idle, lambda: asyncio.ensure_future(self._idle_close())
        )

    async def _idle_close(self) -> None:
        if self._lock.locked():
            self._arm_idle()
            return
        async with self._lock:
            await self._detach()

    async def close(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        async with self._lock:
            await self._detach()

    # -- the channel -------------------------------------------------------------

    async def call(self, form: str, timeout: float | None = None) -> str:
        """Evaluate one AutoLISP form inside AutoCAD; return its printed result."""
        if not self._installed:
            self.install()
        timeout = timeout or self.timeout
        if screen_locked():
            raise BridgeError(
                "The Mac is locked: AutoCAD opens no windows and cannot answer until the "
                "session is unlocked."
            )
        async with self._lock:
            if self._idle_handle is not None:
                self._idle_handle.cancel()
            if self._adapter is None or self._adapter.eof.is_set():
                self._adapter = None
                await self._attach()
            adapter = self._adapter
            req_id = secrets.token_hex(5)
            # Defined once per document: each one has its own AutoLISP namespace.
            expression = (
                f'(progn (if (/= mcp:ver "{BRIDGE_VERSION}") (progn {_BRIDGE_DEFS})) '
                f'(mcp:wrap "{req_id}" \'(lambda () {form})))'
            )
            pattern = re.compile(r"<<MCP " + req_id + r" (OK|ERR) (.*?)>>", re.S)
            try:
                _, events = await adapter.request(
                    "evaluate", {"expression": expression, "context": "repl"}, timeout=timeout
                )
                seen = "".join(
                    e["body"].get("output", "")
                    for e in events
                    if e.get("event") == "output" and isinstance(e.get("body"), dict)
                )
                deadline = time.monotonic() + timeout
                while not (match := pattern.search(seen)):
                    message = await adapter.next(max(0.1, deadline - time.monotonic()))
                    if message is None:
                        raise BridgeError("The AutoLISP debug adapter exited unexpectedly.")
                    if message.get("event") == "output" and isinstance(message.get("body"), dict):
                        seen += message["body"].get("output", "")
            except TimeoutError as exc:
                await self._detach()
                locked = " The Mac is locked." if screen_locked() else ""
                raise BridgeError(
                    f"AutoCAD did not answer within {timeout:.0f} s.{locked} A dialog or an "
                    "active command may be blocking it."
                ) from exc
            finally:
                if self._adapter is not None:
                    self._arm_idle()
            kind, text = match.group(1), match.group(2).strip()
            if kind == "ERR":
                raise BridgeError(f"AutoLISP error: {text}")
            return text

    # -- typed helpers -----------------------------------------------------------

    async def ensure_live_document(self, open_if_needed: bool = False, prepare=None) -> str:
        """Make sure AutoCAD's active drawing is the server's. Returns how.

        ``"present"``  it already is (named ``mcp-live*`` or marked).
        ``"claimed"``  the active drawing was AutoCAD's own empty untitled one, now ours.
        ``"opened"``   (``open_if_needed`` only) a fresh ``mcp-live-*.dxf`` was opened.

        Opening needs the OS (``(command "_.OPEN")`` evaluated through the adapter
        does nothing, measured) and the OS brings AutoCAD to the foreground, so
        the background mirror never does it: it raises :class:`NoLiveDocument`,
        and only an explicit ``system_launch`` passes ``open_if_needed``.
        """
        state = (await self.call("(mcp:ensure)")).strip('"')
        if state in ("present", "claimed"):
            return state
        if not open_if_needed:
            raise NoLiveDocument(
                "AutoCAD's active drawing is not the live one, and it holds work, so the "
                "model was not shown. Switch to the 'mcp-live' tab, open an empty drawing "
                "(Cmd+N), or call system_launch (it opens the live drawing and brings "
                "AutoCAD to the front)."
            )
        if prepare is not None:
            prepare()
        # A name AutoCAD has not seen: opening an already-open file switches nothing.
        target = self.dir / f"{LIVE_DOC_STEM}-{time.strftime('%H%M%S')}.dxf"
        shutil.copyfile(self.live_dxf, target)
        subprocess.run(["open", "-a", str(self.app_path), str(target)], check=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if (await self.call('(if (mcp:live-p) "yes" "no")')).strip('"') == "yes":
                await self.call("(mcp:mark)")
                return "opened"
        raise BridgeError(f"The live document {target.name} did not become active in AutoCAD.")

    async def push(self, path: Path, expect: str | None, force: bool, zoom: bool) -> list[str]:
        form = (
            f"(mcp:push {lisp_string(str(path))} "
            f"{lisp_string(expect) if expect else 'nil'} {'T' if force else 'nil'} "
            f"{'T' if zoom else 'nil'})"
        )
        return parse_lisp_strings(await self.call(form))

    async def pull(self, path: Path) -> list[str]:
        return parse_lisp_strings(await self.call(f"(mcp:pull {lisp_string(str(path))})"))
