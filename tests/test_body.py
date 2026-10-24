# -*- coding: utf-8 -*-
"""Sanity tests for the protobuf reader used on the QQ NT message body."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qqexport import body  # noqa: E402


def enc_varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field(number, wire, payload):
    """Build one protobuf field.

    `payload` is the raw value: for wire 0 it must already be a varint, for
    wire 2 it is the length-delimited body (the length is prefixed here).
    """
    if wire == 2:
        return enc_varint((number << 3) | 2) + enc_varint(len(payload)) + payload
    return enc_varint((number << 3) | wire) + payload


def test_varint_roundtrip():
    for n in (0, 1, 127, 128, 300, 16384, 2 ** 31, 2 ** 53):
        v, off = body._read_varint(enc_varint(n), 0)
        assert v == n and off == len(enc_varint(n)), n


def test_parse_wire_basic():
    buf = (
        field(45002, 0, enc_varint(1))  # content_type = 1 (text)
        + field(45101, 2, "你好".encode("utf-8"))
        + field(45405, 0, enc_varint(1234))  # filesize
    )
    fields = body.parse_wire(buf)
    nums = sorted(f.number for f in fields)
    assert nums == [45002, 45101, 45405], nums
    d = body.fields_to_dict(fields)
    assert d[45002] == 1
    text = d[45101]
    if isinstance(text, dict):
        text = text["__raw__"]
    assert text.decode("utf-8") == "你好"
    assert d[45405] == 1234


def test_parse_msg_body():
    inner = field(45002, 0, enc_varint(1)) + field(45101, 2, b"hi")
    outer = field(40800, 2, inner) + field(40800, 2, inner)
    segs = body.parse_msg_body(outer)
    assert len(segs) == 2, segs
    assert body.seg_text(segs[0]) == "hi"
    assert body.seg_type_name(segs[0]) == "text"


def test_repeated_fields_merge():
    inner = field(45402, 2, b"a.txt") + field(45402, 2, b"b.txt")
    d = body.fields_to_dict(body.parse_wire(inner))
    assert isinstance(d[45402], list) and len(d[45402]) == 2, d


def test_truncated_is_safe():
    buf = field(45002, 0, enc_varint(1))[:2]  # cut mid-varint
    assert body.parse_wire(buf) == []


def test_empty_body():
    assert body.parse_msg_body(b"") == []
    assert body.parse_msg_body(None) == []


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all body tests passed")
