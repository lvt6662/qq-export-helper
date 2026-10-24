# -*- coding: utf-8 -*-
"""qqexport.win32 — minimal ctypes Win32 bindings used by the QQ key extractor.

This module is intentionally dependency-free (stdlib ctypes only) so it can be
embedded in the final self-extracting .exe without any packaging friction.
"""
import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

# ---------------------------------------------------------------- constants
DEBUG_ONLY_THIS_PROCESS = 0x00000002
DEBUG_PROCESS = 0x00000001

CREATE_PROCESS_DEBUG_EVENT = 3
CREATE_THREAD_DEBUG_EVENT = 2
EXCEPTION_DEBUG_EVENT = 1
EXIT_PROCESS_DEBUG_EVENT = 5
EXIT_THREAD_DEBUG_EVENT = 4
LOAD_DLL_DEBUG_EVENT = 6
UNLOAD_DLL_DEBUG_EVENT = 7
OUTPUT_DEBUG_STRING_EVENT = 8
RIP_EVENT = 9

EXCEPTION_BREAKPOINT = 0x80000003
EXCEPTION_SINGLE_STEP = 0x80000004
EXCEPTION_GUARD_PAGE = 0x80000001
EXCEPTION_ACCESS_VIOLATION = 0xC0000005

DBG_CONTINUE = 0x00010002
DBG_EXCEPTION_NOT_HANDLED = 0x80010001
DBG_REPLY_LATER = 0x40010001

PROCESS_CREATE_THREAD = 0x0002
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_ALL_ACCESS = 0x001FFFFF

THREAD_GET_CONTEXT = 0x0008
THREAD_SET_CONTEXT = 0x0010
THREAD_QUERY_INFORMATION = 0x0040
THREAD_ALL_ACCESS = 0x001FFFFF

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010

CONTEXT_AMD64 = 0x00100000
CONTEXT_CONTROL = (CONTEXT_AMD64 | 0x1)
CONTEXT_INTEGER = (CONTEXT_AMD64 | 0x2)
CONTEXT_SEGMENTS = (CONTEXT_AMD64 | 0x4)
CONTEXT_FLOATING_POINT = (CONTEXT_AMD64 | 0x8)
CONTEXT_DEBUG_REGISTERS = (CONTEXT_AMD64 | 0x10)
CONTEXT_ALL = (
    CONTEXT_CONTROL
    | CONTEXT_INTEGER
    | CONTEXT_SEGMENTS
    | CONTEXT_FLOATING_POINT
    | CONTEXT_DEBUG_REGISTERS
)
# What GetThreadContext actually needs from a debugger (no FPU segment data).
CONTEXT_DEBUGGER_AVAILABLE = CONTEXT_CONTROL | CONTEXT_INTEGER | CONTEXT_DEBUG_REGISTERS
# ... plus the debug registers, which are needed to program hardware
# breakpoints (DR0-DR3) on a thread.
CONTEXT_DEBUGGER_FULL = CONTEXT_CONTROL | CONTEXT_INTEGER | CONTEXT_DEBUG_REGISTERS

ERROR_SEM_TIMEOUT = 121

WAIT_EVENT_TIMEOUT_MS = 10000
CREATE_DEBUG_BP_TIMEOUT_MS = 60000
TERMINATE_BP_TIMEOUT_MS = 60000


# ---------------------------------------------------------------- structures
class CONTEXT64(ctypes.Structure):
    """The AMD64 ``CONTEXT`` layout **as it actually exists on this machine**.

    The offsets below were measured empirically (see ``tests/verify_ctx_layout.py``
    and ``tests/probe_ctx_offsets.py``), not taken from a header:

    * a thread created suspended puts the thread's start address in ``Rcx``
      (offset 0x80) and the thread parameter in ``Rdx`` (offset 0x88) --
      exactly the ``RtlUserThreadStart`` calling convention;
    * at the loader's first breakpoint the debug event's exception address + 1
      (i.e. ``Rip``) lives at offset 0xF8;
    * ``EFlags`` is at 0x44 and ``SegGs``/``SegSs`` at 0x40/0x42 (selector 0x2B).

    Every general purpose register therefore sits 0x18 bytes further down than
    the ``winnt.h`` documentation suggests.  Getting this wrong silently yields
    garbage registers, which is why the values are pinned by assertions below.
    """

    _pack_ = 8
    _fields_ = [
        ("P1Home", ctypes.c_uint64),        # 0x000
        ("P2Home", ctypes.c_uint64),        # 0x008
        ("P3Home", ctypes.c_uint64),        # 0x010
        ("P4Home", ctypes.c_uint64),        # 0x018
        ("P5Home", ctypes.c_uint64),        # 0x020
        ("P6Home", ctypes.c_uint64),        # 0x028
        ("ContextFlags", ctypes.c_uint32),  # 0x030
        ("MxCsr", ctypes.c_uint32),         # 0x034
        ("SegCs", ctypes.c_uint16),         # 0x038
        ("SegDs", ctypes.c_uint16),         # 0x03A
        ("SegEs", ctypes.c_uint16),         # 0x03C
        ("SegFs", ctypes.c_uint16),         # 0x03E
        ("SegGs", ctypes.c_uint16),         # 0x040
        ("SegSs", ctypes.c_uint16),         # 0x042
        ("EFlags", ctypes.c_uint32),        # 0x044
        ("Esp", ctypes.c_uint32),           # 0x048
        ("Eip", ctypes.c_uint32),           # 0x04C
        ("SegCs2", ctypes.c_uint16),        # 0x050
        ("SegDs2", ctypes.c_uint16),        # 0x052
        ("SegEs2", ctypes.c_uint16),        # 0x054
        ("SegFs2", ctypes.c_uint16),        # 0x056
        ("SegGs2", ctypes.c_uint16),        # 0x058
        ("SegSs2", ctypes.c_uint16),        # 0x05A
        ("_Pad0", ctypes.c_uint8 * 28),     # 0x05C - 0x077
        ("Rax", ctypes.c_uint64),           # 0x078
        ("Rcx", ctypes.c_uint64),           # 0x080  <- thread start address
        ("Rdx", ctypes.c_uint64),           # 0x088  <- thread parameter
        ("Rbx", ctypes.c_uint64),           # 0x090
        ("Rsp", ctypes.c_uint64),           # 0x098
        ("Rbp", ctypes.c_uint64),           # 0x0A0
        ("Rsi", ctypes.c_uint64),           # 0x0A8
        ("Rdi", ctypes.c_uint64),           # 0x0B0
        ("R8", ctypes.c_uint64),            # 0x0B8
        ("R9", ctypes.c_uint64),            # 0x0C0
        ("R10", ctypes.c_uint64),           # 0x0C8
        ("R11", ctypes.c_uint64),           # 0x0D0
        ("R12", ctypes.c_uint64),           # 0x0D8
        ("R13", ctypes.c_uint64),           # 0x0E0
        ("R14", ctypes.c_uint64),           # 0x0E8
        ("R15", ctypes.c_uint64),           # 0x0F0
        ("Rip", ctypes.c_uint64),           # 0x0F8
        ("FltSave", ctypes.c_uint8 * 512),  # 0x100
        ("VectorRegister", ctypes.c_uint32 * 26),   # 0x300
        ("VectorControl", ctypes.c_uint32),         # 0x368
        ("DebugControl", ctypes.c_uint64),          # 0x370
        ("LastBranchToRip", ctypes.c_uint64),       # 0x378
        ("LastBranchFromRip", ctypes.c_uint64),     # 0x380
        ("LastExceptionToRip", ctypes.c_uint64),    # 0x388
        ("LastExceptionFromRip", ctypes.c_uint64),  # 0x390
        ("Dr0", ctypes.c_uint64),           # 0x398
        ("Dr1", ctypes.c_uint64),           # 0x3A0
        ("Dr2", ctypes.c_uint64),           # 0x3A8
        ("Dr3", ctypes.c_uint64),           # 0x3B0
        ("Dr4", ctypes.c_uint64),           # 0x3B8
        ("Dr5", ctypes.c_uint64),           # 0x3C0
        ("Dr6", ctypes.c_uint64),           # 0x3C8
        ("Dr7", ctypes.c_uint64),           # 0x3D0
        # remaining XSAVE / reserved area up to sizeof(CONTEXT) == 1232
        ("_Tail", ctypes.c_uint8 * 248),
    ]


# sanity-check the measured AMD64 offsets (guards against layout mistakes)
assert CONTEXT64.EFlags.offset == 0x44, CONTEXT64.EFlags.offset
assert CONTEXT64.Rax.offset == 0x78, CONTEXT64.Rax.offset
assert CONTEXT64.Rcx.offset == 0x80, CONTEXT64.Rcx.offset
assert CONTEXT64.Rdx.offset == 0x88, CONTEXT64.Rdx.offset
assert CONTEXT64.Rsp.offset == 0x98, CONTEXT64.Rsp.offset
assert CONTEXT64.R8.offset == 0xB8, CONTEXT64.R8.offset
assert CONTEXT64.R9.offset == 0xC0, CONTEXT64.R9.offset
assert CONTEXT64.Rip.offset == 0xF8, CONTEXT64.Rip.offset
assert CONTEXT64.Dr0.offset == 0x398, CONTEXT64.Dr0.offset
assert CONTEXT64.Dr7.offset == 0x3D0, CONTEXT64.Dr7.offset
assert ctypes.sizeof(CONTEXT64) == 1232, ctypes.sizeof(CONTEXT64)

TRAP_FLAG = 0x100


class DEBUG_EVENT(ctypes.Structure):
    """DEBUG_EVENT — union members are read through accessors to avoid depending
    on ctypes' field-packing of overlapping types."""
    _fields_ = [
        ("dwDebugEventCode", ctypes.c_uint32),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
        ("u", ctypes.c_uint8 * 176),
    ]


U_OFFSET = 16  # offset of the union inside DEBUG_EVENT


def _u32_at(ev, off):
    return ctypes.cast(ctypes.byref(ev, U_OFFSET + off), ctypes.POINTER(ctypes.c_uint32))[0]


def _uptr_at(ev, off):
    return ctypes.cast(ctypes.byref(ev, U_OFFSET + off), ctypes.POINTER(ctypes.c_void_p))[0]


def exception_code(ev):
    """EXCEPTION_DEBUG_INFO.ExceptionCode — DWORD at union offset 0."""
    return _u32_at(ev, 0)


def exception_address(ev):
    """EXCEPTION_RECORD.ExceptionAddress — PVOID at union offset 16.

    Layout (EXCEPTION_RECORD, 152 bytes): ExceptionCode@0, ExceptionFlags@4,
    ExceptionRecord ptr@8, **ExceptionAddress@16**, NumberParameters@24,
    ExceptionInformation[15]@32. DEBUG_EVENT's union starts at offset 16, so
    the address lives at 16+16=32 in the struct. Verified against a raw
    hexdump of a real 0x80000003 event (address 0x00007FFF44BE2239 at [32]).
    """
    return _uptr_at(ev, 16)


def exception_flags(ev):
    """EXCEPTION_RECORD.ExceptionFlags — DWORD at union offset 4."""
    return _u32_at(ev, 4)


def exception_record(ev):
    """EXCEPTION_RECORD.ExceptionRecord — PVOID at union offset 8."""
    return _uptr_at(ev, 8)


def dll_hfile(ev):
    """LOAD_DLL_DEBUG_INFO.hFile — HANDLE at union offset 0."""
    return _uptr_at(ev, 0)


def dll_base(ev):
    """LOAD_DLL_DEBUG_INFO.lpBaseOfDll — LPVOID at union offset 8."""
    return _uptr_at(ev, 8)


def exit_process_code(ev):
    """EXIT_PROCESS_DEBUG_INFO.dwExitCode — DWORD at union offset 0."""
    return _u32_at(ev, 0)


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("th32ModuleID", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("GlblcntUsage", ctypes.c_uint32),
        ("ProccntUsage", ctypes.c_uint32),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", ctypes.c_uint32),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_uint32),
        ("__alignment1", ctypes.c_uint32),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_uint32),
        ("Protect", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("__alignment2", ctypes.c_uint32),
    ]


# sanity-check the packed layout (guards against ctypes padding surprises)
assert ctypes.sizeof(MEMORY_BASIC_INFORMATION) == 48, ctypes.sizeof(MEMORY_BASIC_INFORMATION)


# ---------------------------------------------------------------- prototypes
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]

kernel32.CloseHandle.restype = ctypes.c_bool
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

kernel32.ReadProcessMemory.restype = ctypes.c_bool
kernel32.ReadProcessMemory.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]

kernel32.WriteProcessMemory.restype = ctypes.c_bool
kernel32.WriteProcessMemory.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]

kernel32.FlushInstructionCache.restype = ctypes.c_bool
kernel32.FlushInstructionCache.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]

kernel32.VirtualProtectEx.restype = ctypes.c_bool
kernel32.VirtualProtectEx.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
]

PAGE_EXECUTE_READWRITE = 0x40
PAGE_READWRITE = 0x04

kernel32.TerminateProcess.restype = ctypes.c_bool
kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

kernel32.DebugActiveProcessStop.restype = ctypes.c_bool
kernel32.DebugActiveProcessStop.argtypes = [ctypes.c_uint32]

kernel32.WaitForDebugEvent.restype = ctypes.c_bool
kernel32.WaitForDebugEvent.argtypes = [ctypes.POINTER(DEBUG_EVENT), ctypes.c_uint32]

kernel32.ContinueDebugEvent.restype = ctypes.c_bool
kernel32.ContinueDebugEvent.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]

kernel32.CreateProcessW.restype = ctypes.c_bool
kernel32.CreateProcessW.argtypes = [
    ctypes.c_wchar_p,
    ctypes.c_wchar_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_bool,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_wchar_p,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]

kernel32.GetThreadContext.restype = ctypes.c_bool
kernel32.GetThreadContext.argtypes = [ctypes.c_void_p, ctypes.POINTER(CONTEXT64)]

kernel32.SetThreadContext.restype = ctypes.c_bool
kernel32.SetThreadContext.argtypes = [ctypes.c_void_p, ctypes.POINTER(CONTEXT64)]

kernel32.OpenThread.restype = ctypes.c_void_p
kernel32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]

kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]

kernel32.Module32FirstW.restype = ctypes.c_bool
kernel32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]

kernel32.Module32NextW.restype = ctypes.c_bool
kernel32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MODULEENTRY32W)]

kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.VirtualQueryEx.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION),
    ctypes.c_size_t,
]

kernel32.GetLastError.restype = ctypes.c_uint32

advapi32.OpenProcessToken.restype = ctypes.c_bool
advapi32.GetTokenInformation.restype = ctypes.c_bool

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
PAGE_READABLE = 0x04 | 0x02 | 0x20 | 0x40


def raise_last_error(prefix):
    err = ctypes.get_last_error()
    raise OSError(err, "%s (WinError %d)" % (prefix, err))


def open_process(pid, access=PROCESS_QUERY_INFORMATION | PROCESS_VM_READ):
    h = kernel32.OpenProcess(access, False, pid)
    if not h:
        raise_last_error("OpenProcess(%d)" % pid)
    return h


def open_thread(thread_id, access=None):
    """Open a debuggee thread, degrading the access mask until it succeeds.

    GetThreadContext only needs THREAD_GET_CONTEXT (and, on Windows 7+,
    THREAD_QUERY_INFORMATION); THREAD_ALL_ACCESS can be denied even for
    processes we own when a non-admin debugger is in play.
    """
    if access is None:
        candidates = (
            THREAD_ALL_ACCESS,
            THREAD_GET_CONTEXT | THREAD_SET_CONTEXT | THREAD_QUERY_INFORMATION,
            THREAD_GET_CONTEXT | THREAD_SET_CONTEXT,
            THREAD_GET_CONTEXT,
            THREAD_QUERY_INFORMATION | THREAD_GET_CONTEXT,
        )
    else:
        candidates = (access,)
    last_err = 0
    for mask in candidates:
        h = kernel32.OpenThread(mask, False, thread_id)
        if h:
            return h
        last_err = ctypes.get_last_error()
    ctypes.set_last_error(last_err)
    raise_last_error("OpenThread(%d)" % thread_id)


def _handle(h):
    """Normalise a Win32 handle to c_void_p (accepts int or c_void_p)."""
    if isinstance(h, ctypes.c_void_p):
        return h
    return ctypes.c_void_p(h)


def verify_context_layout():
    """Empirically confirm the CONTEXT64 register offsets on this machine.

    A thread created suspended receives its start address in ``Rcx`` and the
    thread parameter in ``Rdx`` (``RtlUserThreadStart`` convention).  We create
    such a thread with known values and look at where they land in the
    captured context.  Returns ``{"Rcx": off, "Rdx": off, "Rip": off}`` or
    raises ``RuntimeError`` when the layout does not match ``CONTEXT64``.
    """
    import struct

    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.CreateThread.restype = ctypes.c_void_p
    lib.CreateThread.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
    ]
    lib.GetThreadContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.GetThreadContext.restype = ctypes.c_bool
    lib.TerminateThread.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.TerminateThread.restype = ctypes.c_bool

    magic = 0x1122334455667788

    def _worker(param):                            # pragma: no cover - never runs
        kernel32.Sleep(20)
        return 0

    proto = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
    fn = proto(_worker)
    start_addr = ctypes.cast(fn, ctypes.c_void_p).value
    tid = ctypes.c_uint32(0)
    handle = lib.CreateThread(None, 0, ctypes.cast(fn, ctypes.c_void_p),
                              ctypes.c_void_p(magic), 0x4, ctypes.byref(tid))
    if not handle:
        raise RuntimeError("CreateThread failed: %d" % ctypes.get_last_error())
    try:
        buf = (ctypes.c_uint8 * ctypes.sizeof(CONTEXT64))()
        struct.pack_into("<I", buf, CONTEXT64.ContextFlags.offset, CONTEXT_DEBUGGER_AVAILABLE)
        if not lib.GetThreadContext(ctypes.c_void_p(handle), ctypes.byref(buf)):
            raise RuntimeError("GetThreadContext failed: %d" % ctypes.get_last_error())
        raw = bytes(bytearray(buf))
        found = {}
        for want in (start_addr, magic):
            for off in range(0, len(raw) - 7, 8):
                if struct.unpack_from("<Q", raw, off)[0] == want:
                    found.setdefault(want, off)
                    break
        start_off = found.get(start_addr)
        param_off = found.get(magic)
        if start_off is None or param_off is None:
            raise RuntimeError("无法在 CONTEXT 中定位线程入口/参数 (start=%s param=%s)"
                               % (start_off, param_off))
        measured = {
            "Rcx": start_off,
            "Rdx": param_off,
            "Rip": start_off + (CONTEXT64.Rip.offset - CONTEXT64.Rcx.offset),
        }
        expected = {
            "Rcx": CONTEXT64.Rcx.offset,
            "Rdx": CONTEXT64.Rdx.offset,
            "Rip": CONTEXT64.Rip.offset,
        }
        if measured != expected:
            raise RuntimeError("CONTEXT 布局与本机不符: 实测 %r, 结构体 %r" % (measured, expected))
        return measured
    finally:
        lib.TerminateThread(ctypes.c_void_p(handle), 0)


def read_process_memory(h_process, address, size):
    buf = (ctypes.c_uint8 * size)()
    nread = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(
        _handle(h_process),
        ctypes.c_void_p(address),
        ctypes.cast(ctypes.pointer(buf), ctypes.c_void_p),
        size,
        ctypes.byref(nread),
    )
    if not ok:
        raise_last_error("ReadProcessMemory(0x%X, %d)" % (address, size))
    return bytes(bytearray(buf[: nread.value]))


def write_process_memory(h_process, address, data):
    # NOTE: ctypes.cast() on a 1-byte array raises "cannot be converted to
    # pointer"; take the address through ctypes.pointer() instead.
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    nwritten = ctypes.c_size_t(0)
    ok = kernel32.WriteProcessMemory(
        _handle(h_process),
        ctypes.c_void_p(address),
        ctypes.cast(ctypes.pointer(buf), ctypes.c_void_p),
        len(data),
        ctypes.byref(nwritten),
    )
    if not ok:
        raise_last_error("WriteProcessMemory(0x%X, %d bytes)" % (address, len(data)))
    kernel32.FlushInstructionCache(_handle(h_process), ctypes.c_void_p(address), len(data))
    return nwritten.value


def protect_memory(h_process, address, size, protect=PAGE_EXECUTE_READWRITE):
    """Change the page protection of a range inside another process.

    Writing a breakpoint into a code page fails with ERROR_WRITE_PROTECT
    unless the page is temporarily made writable, so the page protection has
    to be relaxed first.
    """
    old = ctypes.c_uint32(0)
    ok = kernel32.VirtualProtectEx(
        _handle(h_process),
        ctypes.c_void_p(address),
        ctypes.c_size_t(size),
        ctypes.c_uint32(protect),
        ctypes.byref(old),
    )
    if not ok:
        raise_last_error("VirtualProtectEx(0x%X)" % address)
    return old.value


def write_code_byte(h_process, address, data):
    """Write into an executable page, relaxing the protection if needed."""
    try:
        return write_process_memory(h_process, address, data)
    except OSError:
        old = protect_memory(h_process, address, 0x1000, PAGE_EXECUTE_READWRITE)
        try:
            return write_process_memory(h_process, address, data)
        finally:
            protect_memory(h_process, address, 0x1000, old)


def find_module_base(pid, module_name):
    """Return (baseAddress, imageSize) of the first loaded module matching."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == ctypes.c_void_p(-1).value or not snap:
        raise_last_error("CreateToolhelp32Snapshot(%d)" % pid)
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(MODULEENTRY32W)
        ok = kernel32.Module32FirstW(snap, ctypes.byref(me))
        wanted = module_name.lower()
        while ok:
            if me.szModule.lower() == wanted:
                return me.modBaseAddr, me.modBaseSize
            ok = kernel32.Module32NextW(snap, ctypes.byref(me))
        return None, None
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snap))


# ------------------------------------------------------------ extra helpers
kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
kernel32.GetFinalPathNameByHandleW.argtypes = [
    ctypes.c_void_p,
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
]

try:
    ntdll = ctypes.WinDLL("ntdll")
except OSError:  # pragma: no cover - ntdll always exists on Windows
    ntdll = None
if ntdll is not None:
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtQueryInformationProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]


def file_path_from_handle(h_file):
    """Resolve an open file handle to its on-disk path (LOAD_DLL events).

    Returns a DOS path such as ``C:\\QQ\\...\\wrapper.node``; an empty string
    when the handle cannot be resolved.  ``VOLUME_NAME_DOS`` (0) is used so the
    result can be opened directly; the NT form is the fallback.
    """
    if not h_file:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    for flags in (0x0, 0x2):  # VOLUME_NAME_DOS, then VOLUME_NAME_NT
        n = kernel32.GetFinalPathNameByHandleW(
            ctypes.c_void_p(h_file), buf, 1024, flags
        )
        if n:
            return buf.value
    return ""


def flush_instruction_cache(h_process, address, size=16):
    kernel32.FlushInstructionCache(
        _handle(h_process), ctypes.c_void_p(address), size
    )


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ExitStatus", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


def mask_being_debugged(h_process):
    """Clear ``PEB.BeingDebugged`` (PEB+0x02) of a debugged child process.

    Only that single byte is touched: PEB+0x68 (NtGlobalFlag) and the
    ProcessHeaps/GdiSharedHandleTable fields must be left alone or the
    process dies immediately.
    """
    if ntdll is None:
        return False
    pbi = _PROCESS_BASIC_INFORMATION()
    ret = ctypes.c_ulong(0)
    status = ntdll.NtQueryInformationProcess(
        _handle(h_process),
        0,  # ProcessBasicInformation
        ctypes.byref(pbi),
        ctypes.sizeof(pbi),
        ctypes.byref(ret),
    )
    if status != 0 or not pbi.PebBaseAddress:
        return False
    try:
        old = read_process_memory(h_process, pbi.PebBaseAddress + 0x02, 1)
        if old and old[0]:
            write_process_memory(h_process, pbi.PebBaseAddress + 0x02, b"\x00")
        return True
    except OSError:
        return False
