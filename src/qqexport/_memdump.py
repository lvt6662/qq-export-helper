# -*- coding: utf-8 -*-
"""dump_proc_mem2.py -- address aware dump of a process' readable memory.

Unlike dump_proc_mem.py (which concatenates the regions and therefore loses
the virtual addresses) this dumper records the base address of every readable
region, so that pointers inside the image can be followed afterwards.

Output:
    <out.bin>   concatenated region data
    <out.map>   JSON list of [base_address, size, file_offset]

Usage:
    python dump_proc_mem2.py <pid> <out.bin>
"""
import ctypes
import json
import os
import sys
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}

k32.OpenProcess.restype = wintypes.HANDLE
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.ReadProcessMemory.restype = wintypes.BOOL


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def dump(pid, out_path):
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    max_addr = 0x7FFFFFFF0000
    total = 0
    regions = []
    with open(out_path, "wb") as out:
        while addr < max_addr:
            n = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                   ctypes.sizeof(mbi))
            if not n:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize
            prot = mbi.Protect
            if (mbi.State == MEM_COMMIT and not (prot & PAGE_GUARD)
                    and not (prot & PAGE_NOACCESS) and (prot & 0xFF) in READABLE):
                off = 0
                while off < size:
                    want = min(4 << 20, size - off)
                    buf = ctypes.create_string_buffer(want)
                    got = wintypes.DWORD(0)
                    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf,
                                               want, ctypes.byref(got))
                    if ok and got.value:
                        data = buf.raw[:got.value]
                        regions.append([base + off, len(data), total])
                        out.write(data)
                        total += len(data)
                    off += want
            addr = base + size
            if size == 0:
                break
    k32.CloseHandle(h)
    with open(out_path + ".map", "w", encoding="utf-8") as fh:
        json.dump(regions, fh)
    print("dumped %d bytes in %d regions -> %s" % (total, len(regions), out_path))
    return regions


if __name__ == "__main__":
    dump(int(sys.argv[1]), sys.argv[2])
