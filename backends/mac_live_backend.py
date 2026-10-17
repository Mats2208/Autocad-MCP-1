"""Live AutoCAD for Mac: the ezdxf engine, mirrored into a running AutoCAD.

AutoCAD for Mac has no COM, so there is no ``ComBackend`` for it. Rather than
re-implementing ~110 contract methods in AutoLISP, this backend keeps ezdxf as
the model -- every tool works, with the same results as headless -- and mirrors
that model into a document AutoCAD is displaying (``mcp-live``), through
:mod:`backends.mac_bridge`: AutoLISP evaluated inside the running AutoCAD by its
own debug adapter, with no keyboard and no window focus.

* **Model -> AutoCAD.** Every mutating call bumps a revision counter; a
  debounced background task writes the DXF and replaces the contents of the
  live document (ERASE + PURGE + DXFIN) and zooms to the extents, so what the
  tools draw appears in AutoCAD. The live document is AutoCAD's own empty
  untitled drawing (claimed with a marker) or an ``mcp-live-*`` one; the user's
  own drawings are never erased.
* **No focus, ever, in the background.** Opening a document needs the OS, and
  the OS brings AutoCAD to the foreground, so the mirror never does it: with no
  live drawing active it waits and retries. ``system_launch`` is the explicit
  call that may open one and show AutoCAD.
* **AutoCAD -> model.** ``system_launch`` ("attach") DXFOUTs what AutoCAD shows
  and loads it into the model, so edits made by hand are picked up. If AutoCAD
  is not running it is launched, in the background.
* **Edits made by hand are never overwritten silently.** The bridge fingerprints
  the live document after each push; if it differs before the next one the push
  is refused (``live.conflict`` in ``system_status``) until the operator attaches.
* ``system_run_lisp`` / ``system_run_command`` run in AutoCAD itself.

Opt-in with ``AUTOCAD_MCP_BACKEND=mac``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from backends.base import CapabilityMap, FeatureCapability
from backends.ezdxf_backend import EzdxfBackend
from backends.mac_bridge import BridgeError, MacBridge, NoLiveDocument, lisp_string

log = logging.getLogger("autocad-mcp.mac")

#: Seconds without a further mutation before the mirror is pushed.
DEFAULT_DEBOUNCE = 1.0

#: Seconds between silent checks while no live drawing is active in AutoCAD.
WAIT_RETRY = 10.0


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


class MacLiveBackend(EzdxfBackend):
    """ezdxf model + a live AutoCAD for Mac window."""

    def __init__(self) -> None:
        super().__init__()
        self.bridge = MacBridge()
        self._debounce = float(os.environ.get("AUTOCAD_MCP_MAC_DEBOUNCE", DEFAULT_DEBOUNCE))
        self._autozoom = _flag("AUTOCAD_MCP_MAC_AUTOZOOM", True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rev = 0  # bumped by every mutation (worker thread)
        self._pushed_rev = -1  # -1: nothing shown in AutoCAD yet
        self._signature: str | None = None  # of the live document after our last push
        self._push_task: asyncio.Task | None = None
        self._push_lock = asyncio.Lock()
        self._conflict: str | None = None
        self._waiting: str | None = None  # no live drawing is active in AutoCAD
        self._last_error: str | None = None
        self._pushes = 0
        self._dirty_in_autocad = False  # run_lisp / run_command may have changed it

    # -- identity ----------------------------------------------------------------

    @property
    def name(self) -> str:
        return "autocad-mac"

    def capabilities(self) -> CapabilityMap:
        base = super().capabilities()
        features = dict(base.features)
        features["dwg"] = FeatureCapability(True, "autocad_wblock")
        features["dwg_write"] = FeatureCapability(True, "autocad_wblock")
        features["live_application"] = FeatureCapability(
            True, "mac_bridge", reason="ezdxf_model_mirrored_into_autocad_for_mac"
        )
        return CapabilityMap(backend=self.name, features=features)

    # -- lifecycle ---------------------------------------------------------------

    async def connect(self) -> None:
        await super().connect()
        self._loop = asyncio.get_running_loop()
        if not self.bridge.app_path:
            raise BridgeError("No AutoCAD for Mac found under /Applications/Autodesk.")
        # Nothing is sent to AutoCAD yet: the debug session is opened on the first
        # call that needs it, and closed again a few seconds after the last one.
        target = self.bridge.install()
        log.info("Mac bridge ready (%s); driving %s", target, self.bridge.app_name)

    async def aclose(self) -> None:
        """Server shutdown hook (see ``server.autocad_lifespan``): detach cleanly.

        AutoCAD keeps a debug session that was not disconnected marked as in use
        until it is restarted, so this must run on every normal exit.
        """
        await self.disconnect()

    async def disconnect(self) -> None:
        if self._push_task and not self._push_task.done():
            self._push_task.cancel()
        await self.bridge.close()
        await super().disconnect()

    # -- mirroring ---------------------------------------------------------------

    def _mark_dirty(self):
        super()._mark_dirty()
        self._rev += 1
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._schedule_push)

    def _schedule_push(self) -> None:
        if self._push_task is None or self._push_task.done():
            self._push_task = asyncio.ensure_future(self._debounced_push())

    async def _debounced_push(self) -> None:
        try:
            while True:
                seen = self._rev
                await asyncio.sleep(self._debounce)
                if self._rev == seen:
                    break
            while True:
                out = await self.flush()
                if not out.get("waiting"):
                    break
                # No live drawing is active (the operator is in another tab). Nothing
                # can be done in the background but wait; each check is silent.
                await asyncio.sleep(WAIT_RETRY)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the mirror must never take a tool call down
            self._last_error = str(exc)
            log.warning("live mirror failed: %s", exc)

    async def _write_model(self, path: Path) -> None:
        """Save the model to ``path`` under the document lock (an empty one if none)."""

        def _sync():
            if self._doc is not None:
                self._doc.saveas(str(path))
                return
            import ezdxf

            ezdxf.new("R2010").saveas(str(path))

        await self._async(_sync)

    async def flush(self, force: bool = False) -> dict:
        """Show the model in AutoCAD now. Returns the outcome."""
        force = force or _flag("AUTOCAD_MCP_MAC_FORCE_PUSH", False)
        async with self._push_lock:
            if self._rev == self._pushed_rev and not force:
                return {"ok": True, "pushed": False}
            rev = self._rev
            snapshot = self.bridge.dir / "push.dxf"
            try:
                self.bridge.dir.mkdir(parents=True, exist_ok=True)
                await self._write_model(snapshot)
                state = await self.bridge.ensure_live_document()
                # A freshly claimed drawing, or the first push, has nothing to compare.
                expect = None if (state == "claimed" or self._pushed_rev < 0) else self._signature
                status, detail = (await self.bridge.push(snapshot, expect, force, self._autozoom))[
                    :2
                ]
            except NoLiveDocument as exc:
                self._waiting = str(exc)
                return {"ok": False, "waiting": True, "error": str(exc)}
            except BridgeError as exc:
                self._last_error = str(exc)
                return {"ok": False, "error": str(exc)}
            self._waiting = None
            if status == "conflict":
                self._conflict = detail
                return {
                    "ok": False,
                    "pushed": False,
                    "conflict": True,
                    "error": (
                        "The live drawing in AutoCAD was edited by hand since the last sync, so "
                        "the model was NOT pushed. system_launch pulls AutoCAD's version into the "
                        "model (discarding the model changes not yet shown); "
                        "AUTOCAD_MCP_MAC_FORCE_PUSH=true overwrites AutoCAD instead."
                    ),
                }
            if status != "ok":
                self._last_error = f"{status}: {detail}"
                return {"ok": False, "error": self._last_error}
            self._signature = detail
            self._pushed_rev = rev
            self._conflict = None
            self._last_error = None
            self._pushes += 1
            return {"ok": True, "pushed": True}

    async def view_zoom_extents(self) -> dict:
        out = await self.flush()
        try:
            await self.bridge.ensure_live_document()
            await self.bridge.call('(command "_.ZOOM" "_E")')
        except BridgeError as exc:
            return {"ok": False, "applied": False, "error": str(exc)}
        return {"ok": True, "applied": True, "sync": out}

    async def view_zoom_window(self, x1, y1, x2, y2) -> dict:
        await self.flush()
        try:
            await self.bridge.ensure_live_document()
            await self.bridge.call(
                f'(command "_.ZOOM" "_W" (list {float(x1)} {float(y1)}) '
                f"(list {float(x2)} {float(y2)}))"
            )
        except BridgeError as exc:
            return {"ok": False, "applied": False, "error": str(exc)}
        return {"ok": True, "applied": True}

    async def view_screenshot(self, overlay_handles: bool = False) -> bytes | None:
        """AutoCAD's own render of the live document (``PNGOUT``), not matplotlib's.

        Handle labels exist only in the headless renderer, so ``overlay_handles``
        keeps using it; so does any failure to reach AutoCAD.
        """
        if overlay_handles:
            return await super().view_screenshot(overlay_handles)
        try:
            await self.flush()
            await self.bridge.ensure_live_document()
            target = self.bridge.dir / "view.png"
            target.unlink(missing_ok=True)
            await self.bridge.call(f'(command "_.PNGOUT" {lisp_string(str(target))} "")')
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not target.exists():
                await asyncio.sleep(0.2)
            if target.exists():
                await asyncio.sleep(0.2)  # let AutoCAD finish writing
                return target.read_bytes()
        except BridgeError as exc:
            self._last_error = str(exc)
            log.warning("AutoCAD screenshot failed (%s); using the headless render", exc)
        return await super().view_screenshot(overlay_handles)

    # -- DWG: AutoCAD writes it -------------------------------------------------

    async def drawing_export_dwg(self, path, version="R2018") -> dict:
        """A real DWG, written by AutoCAD itself (``-WBLOCK *``: the whole drawing).

        ``-WBLOCK`` leaves the live document's name alone, which ``SAVEAS`` would
        not. It always writes AutoCAD's current format, so ``version`` is checked
        and the format actually written is read back from the file.
        """
        from backends.contracts.refs import DWG_VERSIONS

        if version not in DWG_VERSIONS:
            raise ValueError(f"DWG version must be one of {DWG_VERSIONS}, got {version!r}")
        target = Path(path)
        self._refuse_path_held_elsewhere("drawing_export_dwg", str(target))
        await self.flush()
        await self.bridge.ensure_live_document()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        await self.bridge.call(f'(command "_.-WBLOCK" {lisp_string(str(target))} "*")')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not target.exists():
            await asyncio.sleep(0.2)
        if not target.exists():
            raise BridgeError(f"AutoCAD did not write {target}")
        await asyncio.sleep(0.3)
        written = {
            b"AC1032": "R2018",
            b"AC1027": "R2013",
            b"AC1024": "R2010",
            b"AC1021": "R2007",
            b"AC1018": "R2004",
            b"AC1015": "R2000",
        }.get(target.read_bytes()[:6], "unknown")
        return {
            "ok": True,
            "path": str(target),
            "version": written,
            "requested_version": version,
            "bytes": target.stat().st_size,
            "backend": self.name,
        }

    async def drawing_save_as(self, path: str, fmt: str = "dxf") -> dict:
        if fmt.lower() == "dwg" or str(path).lower().endswith(".dwg"):
            out = await self.drawing_export_dwg(path)
            return {"ok": True, "path": out["path"], "format": "dwg", "version": out["version"]}
        return await super().drawing_save_as(path, fmt)

    # -- AutoCAD -> model --------------------------------------------------------

    async def _launch_autocad(self, visible: bool = True) -> bool:
        """Start AutoCAD if it is not running. True if launched.

        ``visible=False`` asks LaunchServices to hide it (``-g -j``); AutoCAD may still
        raise itself while it starts.
        """
        if self.bridge.autocad_pid() is not None:
            return False
        flags = ["-a"] if visible else ["-g", "-j", "-a"]
        subprocess.run(["open", *flags, str(self.bridge.app_path)], check=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.bridge.autocad_pid() is not None:
                await asyncio.sleep(20)  # the command line is not ready the moment the process is
                return True
            await asyncio.sleep(1)
        raise BridgeError("AutoCAD did not start within 120 s.")

    async def system_launch(self, visible: bool = True, open_path: str | None = None) -> dict:
        """Launch AutoCAD if needed and attach to the live drawing.

        This is the one call allowed to open a document, which brings AutoCAD to the
        foreground. If the live drawing already holds work it is pulled into the
        model; if it is new or empty the model is shown there instead.
        """
        launched = await self._launch_autocad(visible)
        if open_path is not None:
            await self.drawing_open(open_path)
            self._rev += 1
        await self._write_model(self.bridge.live_dxf)
        async with self._push_lock:
            state = await self.bridge.ensure_live_document(open_if_needed=True)
        self._waiting = None
        pulled_from = None
        if state == "present" and self._pushed_rev >= 0:
            async with self._push_lock:
                pulled = self.bridge.dir / "pull.dxf"
                pulled.unlink(missing_ok=True)
                status, detail = (await self.bridge.pull(pulled))[:2]
                if status != "ok" or not pulled.exists():
                    raise BridgeError(f"AutoCAD did not hand the drawing over: {status} {detail}")
                kept = self.bridge.dir / "pulled-model.dxf"
                shutil.copyfile(pulled, kept)
            result = await EzdxfBackend.drawing_open(self, str(kept))
            # The model now equals AutoCAD's drawing: nothing to push, nothing to lose.
            self._signature = detail
            self._pushed_rev = self._rev
            self._conflict = None
            self._dirty_in_autocad = False
            pulled_from = "mcp-live (AutoCAD)"
            document = result.get("name")
        else:
            await self.flush(force=True)  # new or empty drawing: show the model there
            document = "mcp-live (AutoCAD)"
        version = await self.bridge.call('(getvar "ACADVER")')
        return {
            "launched": launched,
            "attached": True,
            "version": version.strip('"'),
            "document": document,
            "live_document": state,
            "pulled_from": pulled_from,
        }

    # -- real AutoLISP -----------------------------------------------------------

    async def system_run_lisp(self, expression) -> dict:
        await self.flush()
        try:
            await self.bridge.ensure_live_document()
            out = await self.bridge.call(str(expression))
        except BridgeError as exc:
            return {"ok": False, "error": str(exc)}
        self._dirty_in_autocad = True
        return {
            "ok": True,
            "result": out,
            "note": "Ran in AutoCAD. The model is not updated: call system_launch to pull the drawing.",
        }

    async def system_run_command(self, command) -> dict:
        words = str(command).split()
        if not words:
            return {"ok": False, "error": "empty command"}
        await self.flush()
        form = "(progn (command " + " ".join(lisp_string(w) for w in words) + ") T)"
        try:
            await self.bridge.ensure_live_document()
            await self.bridge.call(form)
        except BridgeError as exc:
            return {"ok": False, "error": str(exc)}
        self._dirty_in_autocad = True
        return {
            "ok": True,
            "note": "Ran in AutoCAD. The model is not updated: call system_launch to pull the drawing.",
        }

    # -- status ------------------------------------------------------------------

    async def system_status(self) -> dict:
        status = await super().system_status()
        status["backend"] = self.name
        status["capabilities"] = [*status.get("capabilities", []), "live_autocad_mac"]
        status["live"] = {
            "application": self.bridge.app_name,
            "autocad_running": self.bridge.autocad_pid() is not None,
            "document": self.bridge.live_dxf.name,
            "bridge_dir": str(self.bridge.dir),
            "shown_in_autocad": self._pushed_rev >= 0,
            "waiting_for_live_document": self._waiting,
            "pushes": self._pushes,
            "model_ahead_of_autocad": self._rev != self._pushed_rev,
            "conflict": self._conflict is not None,
            "autocad_changed_by_lisp_or_command": self._dirty_in_autocad,
            "last_error": self._last_error,
        }
        return status
