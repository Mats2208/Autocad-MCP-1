"""A COM message filter for the STA thread that talks to AutoCAD: rejected calls are retried, not lost.

AutoCAD refuses an incoming COM call (``RPC_E_CALL_REJECTED``) whenever its main thread is busy: a command or a
LISP routine running, a regen, a document switch, a modal prompt, the window being restored or dragged. Without
a client-side ``IMessageFilter`` COM gives up at once and pywin32's late binding turns the refusal into a bare
``AttributeError: <unknown>.Count`` or ``AddLine.ObjectName``: a write fails half-way through a tool and the
operator is told to keep AutoCAD in front, which is backwards. With this filter COM itself waits and retries the
call (``RetryRejectedCall``) until AutoCAD takes it or ``budget_s`` runs out.

pywin32 does not wrap ``CoRegisterMessageFilter``, so the filter is a minimal COM object built with ctypes: an
``IMessageFilter`` vtable (IUnknown + 3 methods) whose callbacks stay referenced for as long as it is registered.
It touches nothing but COM: no window is activated, no key or mouse event is sent, focus never moves.

Usage, on the thread that called ``CoInitialize`` (a filter belongs to its apartment):

    register(budget_s=60.0)    # in the executor's initializer, after CoInitialize
    unregister()               # in the teardown, before CoUninitialize
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import (
    POINTER,
    WINFUNCTYPE,
    Structure,
    byref,
    c_long,
    c_ubyte,
    c_ulong,
    c_ushort,
    c_void_p,
)

#: RetryRejectedCall's dwRejectType: the callee is busy and asks to be called again later.
SERVERCALL_RETRYLATER = 2
#: HandleInComingCall's answer: process the incoming call (we are a client; nothing calls in normally).
SERVERCALL_ISHANDLED = 0
#: MessagePending's answer: let the message through to the default processing and keep waiting.
PENDINGMSG_WAITDEFPROCESS = 2
#: RetryRejectedCall's "give up" answer.
CANCEL = 0xFFFFFFFF

S_OK = 0
E_NOINTERFACE = -2147467262  # 0x80004002


class GUID(Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]

    def __eq__(self, other):  # pragma: no cover - only used on Windows
        return bytes(self) == bytes(other)


def _guid(d1: int, d2: int, d3: int, d4: bytes) -> GUID:
    return GUID(d1, d2, d3, (c_ubyte * 8)(*d4))


IID_IUnknown = _guid(0x00000000, 0x0000, 0x0000, b"\xc0\x00\x00\x00\x00\x00\x00\x46")
IID_IMessageFilter = _guid(0x00000016, 0x0000, 0x0000, b"\xc0\x00\x00\x00\x00\x00\x00\x46")

_QueryInterface = WINFUNCTYPE(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
_AddRef = WINFUNCTYPE(c_ulong, c_void_p)
_Release = WINFUNCTYPE(c_ulong, c_void_p)
_HandleInComingCall = WINFUNCTYPE(c_ulong, c_void_p, c_ulong, c_void_p, c_ulong, c_void_p)
_RetryRejectedCall = WINFUNCTYPE(c_ulong, c_void_p, c_void_p, c_ulong, c_ulong)
_MessagePending = WINFUNCTYPE(c_ulong, c_void_p, c_void_p, c_ulong, c_ulong)


class _VTable(Structure):
    _fields_ = [
        ("QueryInterface", _QueryInterface),
        ("AddRef", _AddRef),
        ("Release", _Release),
        ("HandleInComingCall", _HandleInComingCall),
        ("RetryRejectedCall", _RetryRejectedCall),
        ("MessagePending", _MessagePending),
    ]


class _Object(Structure):
    _fields_ = [("lpVtbl", POINTER(_VTable))]


class RetryFilter:
    """One IMessageFilter object. ``retry_delay_ms(tick_count, reject_type)`` is the policy, testable alone."""

    def __init__(self, budget_s: float = 60.0, pause_ms: int = 150):
        self.budget_ms = int(budget_s * 1000)
        self.pause_ms = int(pause_ms)
        self.retries = 0  # how many rejected calls COM retried through this filter
        self._refs = 1
        self._vtable = _VTable(
            _QueryInterface(self._query_interface),
            _AddRef(self._add_ref),
            _Release(self._release),
            _HandleInComingCall(self._handle_incoming),
            _RetryRejectedCall(self._retry_rejected),
            _MessagePending(self._message_pending),
        )
        self._object = _Object(ctypes.pointer(self._vtable))

    @property
    def pointer(self) -> int:
        return ctypes.addressof(self._object)

    def retry_delay_ms(self, tick_count: int, reject_type: int) -> int:
        """Milliseconds before COM retries the call, or CANCEL. Only a busy callee is waited for, and only while
        the call is younger than the budget; a call the callee rejected outright fails at once."""
        if reject_type != SERVERCALL_RETRYLATER or tick_count >= self.budget_ms:
            return CANCEL
        return self.pause_ms

    # --- the COM methods (called by ole32 on this thread) ---

    def _query_interface(self, this, riid, ppv):
        if riid and (
            bytes(riid.contents) == bytes(IID_IUnknown)
            or bytes(riid.contents) == bytes(IID_IMessageFilter)
        ):
            ppv[0] = this
            self._refs += 1
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _add_ref(self, this):
        self._refs += 1
        return self._refs

    def _release(self, this):
        self._refs = max(1, self._refs - 1)  # the Python owner keeps the last reference
        return self._refs

    def _handle_incoming(self, this, call_type, caller, tick_count, info):
        return SERVERCALL_ISHANDLED

    def _retry_rejected(self, this, callee, tick_count, reject_type):
        delay = self.retry_delay_ms(tick_count, reject_type)
        if delay != CANCEL:
            self.retries += 1
        return delay

    def _message_pending(self, this, callee, tick_count, pending_type):
        return PENDINGMSG_WAITDEFPROCESS


_local = threading.local()


def register(budget_s: float = 60.0, pause_ms: int = 150) -> RetryFilter | None:
    """Registers a RetryFilter for the calling thread's STA apartment; returns it (None off Windows or on
    failure, which leaves COM's default behaviour: immediate rejection)."""
    if sys.platform != "win32":
        return None
    flt = RetryFilter(budget_s, pause_ms)
    previous = c_void_p()
    hr = ctypes.windll.ole32.CoRegisterMessageFilter(c_void_p(flt.pointer), byref(previous))
    if hr != S_OK:
        return None
    _local.filter = flt
    _local.previous = previous
    return flt


def unregister() -> None:
    """Restores the apartment's previous filter (normally none) and drops ours."""
    flt = getattr(_local, "filter", None)
    if flt is None or sys.platform != "win32":
        return
    old = c_void_p()
    ctypes.windll.ole32.CoRegisterMessageFilter(_local.previous, byref(old))
    _local.filter = None


def current() -> RetryFilter | None:
    """The filter registered on this thread, if any."""
    return getattr(_local, "filter", None)
