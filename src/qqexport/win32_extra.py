# -*- coding: utf-8 -*-
"""MISSING WIN32 HELPERS used by the probes / live extractor.

Adds: create_process, enum_pids, wait_for_debug_event, continue_debug_event,
query_module_base, module_path_from_debug_event, get_final_path_name_by_handle,
peb_loader_modules (PEB walk), query_memory, enum_regions.
All ctypes, no third-party imports.
"""
import ctypes
import os

from .win32 import (
    CONTEXT64, DEBUG_EVENT, ERROR_SEM_TIMEOUT, PAGE_READABLE, PROCESS_INFORMATION,
    STARTUPINFOW, kernel32, MEMORY_BASIC_INFORMATION,
    MODULEENTRY32W, MEM_COMMIT, MEM_PRIVATE, read_process_memory,
)

# ---------------------------------------------------------------- extra APIs
kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p

kernel32.Process32FirstW.restype = ctypes.c_bool
kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32.Process32NextW.restype = ctypes.c_bool
kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

kernel32.Module32FirstW.restype = ctypes.c_bool
kernel32.Module32NextW.restype = ctypes.c_bool

kernel32.WaitForDebugEventEx.restype = ctypes.c_bool
kernel32.WaitForDebugEventEx.argtypes = [ctypes.POINTER(DEBUG_EVENT), ctypes.c_uint32]

kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
kernel32.GetFinalPathNameByHandleW.argtypes = [
    ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32
]

kernel32.NtQueryInformationProcess = ctypes.windll.ntdll.NtQueryInformationProcess
kernel32.NtQueryInformationProcess.restype = ctypes.c_uint32
kernel32.NtQueryInformationProcess.argtypes = [
    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
]

kernel32.ReadProcessMemory.restype = ctypes.c_bool
kernel32.VirtualQueryEx.restype = ctypes.c_size_t

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
TH32CS_SNAPHEAPLIST = 0x00000001
TH32CS_SNAPTHREAD = 0x00000004

PROCESS_BASIC_INFORMATION = 0


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


MODULEENTRY32W_full = MODULEENTRY32W  # alias (kept for readability)


# ------------------------------------------------------------------ helpers
def enum_pids():
    """All pids whose main module name ends with .exe (filtered later)."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise OSError("CreateToolhelp32Snapshot err=%d" % ctypes.get_last_error())
    out = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            out.append((pe.th32ProcessID, pe.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snap))
    return out


def enum_modules(pid):
    """System-wide snapshot filtered by pid (sidesteps per-pid snapshot denial)."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        raise OSError("CreateToolhelp32Snapshot err=%d" % ctypes.get_last_error())
    out = []
    try:
        me = MODULEENTRY32W_full()
        me.dwSize = ctypes.sizeof(MODULEENTRY32W_full)
        ok = kernel32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            if me.th32ProcessID == pid:
                out.append((me.modBaseAddr, me.modBaseSize, me.szModule, me.szExePath))
            ok = kernel32.Module32NextW(snap, ctypes.byref(me))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snap))
    return out


def create_process(exe, args=None, debug_flag=0, workdir=None):
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    pi = PROCESS_INFORMATION()
    cmd = None if not args else "%s %s" % (exe, args)
    ok = kernel32.CreateProcessW(
        exe, cmd, None, None, False, debug_flag, None, workdir,
        ctypes.byref(si), ctypes.byref(pi),
    )
    if not ok:
        raise OSError("CreateProcessW err=%d" % ctypes.get_last_error())
    kernel32.CloseHandle(ctypes.c_void_p(pi.hThread))
    return pi.hProcess, pi


def wait_for_debug_event(timeout_ms=1000):
    ev = DEBUG_EVENT()
    ok = kernel32.WaitForDebugEventEx(ctypes.byref(ev), timeout_ms)
    if not ok:
        err = ctypes.get_last_error()
        if err == ERROR_SEM_TIMEOUT:
            return None
        if err == 0:
            return "EXIT"
        raise OSError("WaitForDebugEventEx err=%d" % err)
    return ev


def continue_debug_event(ev, status=0x00010002):
    return kernel32.ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, status)


def module_path_from_debug_event(ev):
    from .win32 import dll_hfile
    h = dll_hfile(ev)
    if not h:
        return ""
    buf = ctypes.create_unicode_buffer(1024)
    n = kernel32.GetFinalPathNameByHandleW(h, buf, 1024, 2)
    if n == 0:
        return ""
    p = buf.value
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return p


def peb_loader_modules(h_process):
    """PEB walk: returns list of dicts with base/name/full path.

    PEB is at (ProcessBasicInformation).PebBaseAddress; Ldr is at PEB+0x18.
    """
    pbi = (ctypes.c_uint8 * 48)()
    ret = ctypes.c_uint32(0)
    st = kernel32.NtQueryInformationProcess(
        ctypes.c_void_p(h_process), PROCESS_BASIC_INFORMATION,
        pbi, 48, ctypes.byref(ret),
    )
    if st != 0:
        raise OSError("NtQueryInformationProcess status=0x%X" % st)
    peb = ctypes.cast(pbi, ctypes.POINTER(ctypes.c_void_p))[2]
    ldr = read_process_memory(h_process, peb + 0x18, 8)
    ldr = int.from_bytes(ldr, "little")
    head = ldr + 0x10  # InLoadOrderModuleList
    cur = read_process_memory(h_process, head, 8)
    cur = int.from_bytes(cur, "little")
    out = []
    seen = 0
    while cur and cur != head and seen < 512:
        # LDR_DATA_TABLE_ENTRY: entry base = link - 0x10 (InLoadOrderLinks @0)
        ent = cur - 0x10
        try:
            dllbase = int.from_bytes(read_process_memory(h_process, ent + 0x30, 8), "little")
            size = int.from_bytes(read_process_memory(h_process, ent + 0x40, 8), "little")
            len1 = int.from_bytes(read_process_memory(h_process, ent + 0x48, 2), "little")
            buf1 = int.from_bytes(read_process_memory(h_process, ent + 0x50, 8), "little")
            len2 = int.from_bytes(read_process_memory(h_process, ent + 0x58, 2), "little")
            buf2 = int.from_bytes(read_process_memory(h_process, ent + 0x60, 8), "little")
            full = read_process_memory(h_process, buf1, len1).decode("utf-16-le", "replace") if len1 and buf1 else ""
            base_nm = read_process_memory(h_process, buf2, len2).decode("utf-16-le", "replace") if len2 and buf2 else ""
            nxt = read_process_memory(h_process, cur, 8)
            out.append(dict(base=dllbase, size=size, name=base_nm, path=full))
            cur = int.from_bytes(nxt, "little")
            seen += 1
        except OSError:
            break
    return out


def query_module_base(h_process):
    """(base, path) of the main executable of the process owning h_process."""
    # image base is at PEB+0x10
    pbi = (ctypes.c_uint8 * 48)()
    ret = ctypes.c_uint32(0)
    st = kernel32.NtQueryInformationProcess(
        ctypes.c_void_p(h_process), PROCESS_BASIC_INFORMATION, pbi, 48, ctypes.byref(ret))
    if st != 0:
        raise OSError("NtQueryInformationProcess status=0x%X" % st)
    peb = ctypes.cast(pbi, ctypes.POINTER(ctypes.c_void_p))[2]
    base = int.from_bytes(read_process_memory(h_process, peb + 0x10, 8), "little")
    ldr = int.from_bytes(read_process_memory(h_process, peb + 0x18, 8), "little")
    head = ldr + 0x10
    e = int.from_bytes(read_process_memory(h_process, head, 8), "little")
    # first InLoadOrder entry is the exe itself
    ent = e - 0x10
    ln = int.from_bytes(read_process_memory(h_process, ent + 0x48, 2), "little")
    bp = int.from_bytes(read_process_memory(h_process, ent + 0x50, 8), "little")
    path = read_process_memory(h_process, bp, ln).decode("utf-16-le", "replace") if ln else ""
    return base, path


def query_memory(h_process, address=0):
    """MEMORY_BASIC_INFORMATION for a region.  h_process may be an int handle
    or an already-wrapped c_void_p."""
    if isinstance(h_process, ctypes.c_void_p):
        h = h_process
    else:
        h = ctypes.c_void_p(h_process)
    mbi = MEMORY_BASIC_INFORMATION()
    n = kernel32.VirtualQueryEx(
        h, ctypes.c_void_p(address),
        ctypes.byref(mbi), ctypes.sizeof(mbi))
    if not n:
        return None
    return mbi


def enum_regions(h_process, readable_only=True):
    addr = 0
    out = []
    while True:
        mbi = query_memory(h_process, addr)
        if not mbi:
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize or 0
        if size <= 0:
            break
        prot = mbi.Protect or 0
        ok = (mbi.State == MEM_COMMIT) and not (prot & 0x100) and not (prot & 0x01)
        if ok and (not readable_only or (prot & 0x04) or (prot & 0x02) or (prot & 0x20) or (prot & 0x40)):
            out.append((base, size, mbi.Type, prot))
        addr = base + size
        if addr >= 0x7FFFFFFFFFFF:
            break
    return out


def read_regions(h_process, regions):
    total = 0
    blobs = []
    for base, size, typ, prot in regions:
        try:
            data = read_process_memory(h_process, base, size)
        except OSError:
            continue
        blobs.append((base, data))
        total += len(data)
    return blobs, total
