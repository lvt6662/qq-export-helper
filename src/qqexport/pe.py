# -*- coding: utf-8 -*-
"""qqexport.pe — read-only PE/.pdata analysis to locate the nt_sqlite3_key_v2 routine.

Ported from the reference PowerShell implementation (QQBackup/qq-win-db-key,
scripts/windows/ntqq/windows_ntqq_get_key.ps1) into pure-Python so the key
extractor can be embedded in the shipped .exe without a PowerShell dependency.
"""
import struct


class Section(object):
    __slots__ = ("name", "va", "vsize", "raw", "rawsize", "characteristics")

    def __init__(self, name, va, vsize, raw, rawsize, characteristics):
        self.name = name
        self.va = va
        self.vsize = vsize
        self.raw = raw
        self.rawsize = rawsize
        self.characteristics = characteristics

    def rva_to_offset(self, rva):
        if self.va <= rva < self.va + self.rawsize:
            return self.raw + (rva - self.va)
        return None

    def offset_to_rva(self, offset):
        if self.raw <= offset < self.raw + self.rawsize:
            return self.va + (offset - self.raw)
        return None


class PEFile(object):
    def __init__(self, data):
        self.data = data
        if len(data) < 0x40 or data[:2] != b"MZ":
            raise ValueError("not an MZ image")
        e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
            raise ValueError("missing PE signature")
        coff = e_lfanew + 4
        machine, nsects, _, _, _, opt_size, _chars = struct.unpack_from("<HHIIIHH", data, coff)
        self.machine = machine
        self.number_of_sections = nsects
        self.optional_header_size = opt_size
        if machine != 0x8664:
            raise ValueError("unexpected machine 0x%X (expected x64)" % machine)
        opt = coff + 20
        magic = struct.unpack_from("<H", data, opt)[0]
        if magic != 0x20B:
            raise ValueError("not a PE32+ image (magic 0x%X)" % magic)
        image_base = struct.unpack_from("<Q", data, opt + 24)[0]
        self.image_base = image_base
        size_of_image = struct.unpack_from("<I", data, opt + 56)[0]
        self.size_of_image = size_of_image
        num_dirs = struct.unpack_from("<I", data, opt + 108)[0]
        self.data_directories = []
        for i in range(num_dirs):
            va, sz = struct.unpack_from("<II", data, opt + 112 + i * 8)
            self.data_directories.append((va, sz))

        self.sections = []
        sect_off = opt + opt_size
        for i in range(nsects):
            off = sect_off + i * 40
            name = data[off : off + 8].rstrip(b"\x00").decode("ascii", "replace")
            vsize, vva, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
            self.sections.append(Section(name, vva, vsize, rawptr, rawsize, 0))

    def section_by_name(self, name):
        for s in self.sections:
            if s.name == name:
                return s
        return None

    def rva_to_offset(self, rva):
        for s in self.sections:
            o = s.rva_to_offset(rva)
            if o is not None:
                return o
        return None

    def offset_to_rva(self, offset):
        for s in self.sections:
            r = s.offset_to_rva(offset)
            if r is not None:
                return r
        return None

    # -------------------------------------------------- string search
    def find_ascii(self, needle, section_name=".rdata"):
        sec = self.section_by_name(section_name)
        if sec is None:
            return None
        window = self.data[sec.raw : sec.raw + sec.rawsize]
        idx = window.find(needle)
        if idx < 0:
            return None
        file_off = sec.raw + idx
        return file_off, sec.va + idx

    # -------------------------------------------------- pdata lookup
    def exports(self):
        """Map exported function RVA -> export name (empty when not exportable)."""
        if len(self.data_directories) < 1:
            return {}
        va, _sz = self.data_directories[0]
        if not va:
            return {}
        off = self.rva_to_offset(va)
        if off is None:
            return {}
        d = self.data
        try:
            num_names = struct.unpack_from("<I", d, off + 24)[0]
            addr_rva = struct.unpack_from("<I", d, off + 28)[0]
            name_rva = struct.unpack_from("<I", d, off + 32)[0]
            ord_rva = struct.unpack_from("<I", d, off + 36)[0]
            a_off = self.rva_to_offset(addr_rva)
            n_off = self.rva_to_offset(name_rva)
            o_off = self.rva_to_offset(ord_rva)
        except struct.error:
            return {}
        if a_off is None or n_off is None or o_off is None:
            return {}
        out = {}
        for i in range(min(num_names, 4096)):
            try:
                nrva = struct.unpack_from("<I", d, n_off + i * 4)[0]
                noff = self.rva_to_offset(nrva)
                if noff is None:
                    continue
                end = d.find(b"\x00", noff)
                name = d[noff:end].decode("ascii", "replace")
                ordv = struct.unpack_from("<H", d, o_off + i * 2)[0]
                frva = struct.unpack_from("<I", d, a_off + ordv * 4)[0]
            except (struct.error, IndexError):
                continue
            if name:
                out.setdefault(frva, name)
        return out

    def function_containing_rva(self, rva):
        """Dichotomic search of the .pdata exception directory (RUNTIME_FUNCTION)."""
        d = self.data_directories[3] if len(self.data_directories) > 3 else (0, 0)
        if not d[1]:
            return None
        pdata = self.section_by_name(".pdata")
        if pdata is None:
            return None
        base = pdata.rva_to_offset(d[0])
        if base is None:
            return None
        count = d[1] // 12
        data = self.data
        lo, hi = 0, count - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            begin, end, _unwind = struct.unpack_from("<III", data, base + mid * 12)
            if rva < begin:
                hi = mid - 1
            elif rva >= end:
                lo = mid + 1
            else:
                return begin
        # linear fallback for tables that are not strictly sorted
        for i in range(count):
            begin, end, _unwind = struct.unpack_from("<III", data, base + i * 12)
            if begin <= rva < end:
                return begin
        return None


def locate_key_v2_function(image_bytes, needle=b"nt_sqlite3_key_v2: db=%p zDb=%s"):
    """Return (functionBeginRVA, leaInstructionRVA, stringRVA) or raise ValueError."""
    pe = PEFile(image_bytes)
    hit = pe.find_ascii(needle)
    if hit is None:
        raise ValueError("log format string not found in .rdata")
    _file_off, string_rva = hit

    text = pe.section_by_name(".text")
    if text is None:
        raise ValueError("no .text section")
    text_bytes = image_bytes[text.raw : text.raw + text.rawsize]

    # scan for `48 8D 05 disp32` (lea reg, [rip+disp32]).
    # The REX byte sits immediately *before* the 0x8D opcode, and the instruction
    # starts at the REX, so the RIP target uses instr_rva (RVA of the REX byte).
    lea_rva = None
    needle_rva = string_rva
    i = 0
    n = len(text_bytes)
    while i + 7 <= n:
        if (
            text_bytes[i] == 0x8D
            and (text_bytes[i + 1] & 0xC7) == 0x05
            and i >= 1
            and 0x40 <= text_bytes[i - 1] <= 0x4F
        ):
            instr_rva = text.va + (i - 1)
            disp = struct.unpack_from("<i", text_bytes, i + 2)[0]
            if instr_rva + 7 + disp == needle_rva:
                lea_rva = instr_rva
                break
        i += 1
    if lea_rva is None:
        raise ValueError("no rip-relative LEA targets the log format string")

    func_rva = pe.function_containing_rva(lea_rva)
    if func_rva is None:
        raise ValueError("no .pdata RUNTIME_FUNCTION contains the LEA instruction")
    return func_rva, lea_rva, string_rva
