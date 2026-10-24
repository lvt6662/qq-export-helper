# -*- coding: utf-8 -*-
"""Wire-format scratch pad for the QQ NT message body.

The msg blob is *not* JSON — it is a binary container. This file holds small
helpers used while reverse-engineering its layout, so the findings survive in
the repo instead of living only in my scratch notes.
"""
import struct


def read_varint(buf, off):
    """Read a SQLite-style varint (1-9 bytes, big-endian 7-bit groups)."""
    result = 0
    for i in range(8):
        if off + i >= len(buf):
            raise ValueError("varint truncated")
        b = buf[off + i]
        result = (result << 7) | (b & 0x7F)
        if not (b & 0x80):
            return result, off + i + 1
    # 9th byte contributes all 8 bits
    if off + 8 >= len(buf):
        raise ValueError("varint truncated")
    result = (result << 8) | buf[off + 8]
    return result, off + 9


def hexdump(data, base=0, width=16):
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i : i + width]
        hexs = " ".join("%02x" % b for b in chunk)
        text = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk)
        lines.append("%08x  %-*s  |%s|" % (base + i, width * 3 - 1, hexs, text))
    return "\n".join(lines)
