# -*- coding: utf-8 -*-
"""proto.py 的单元测试 —— 自造 protobuf 字节，验证纯 Python 解码器。

直接运行：``python tests\\test_proto.py``
"""

from __future__ import annotations

import struct
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qqexport import proto  # noqa: E402


# ── 造字节的小工具（与 tests/test_body.py 同风格）────────────────────────────


def enc_varint(n: int) -> bytes:
    """把非负整数编码成 varint。"""
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field(number: int, wire: int, payload: bytes) -> bytes:
    """造一个 protobuf 字段。

    wire=0 时 payload 必须是已经编好的 varint；wire=2 时 payload 是长度前缀
    之后的内容（长度由本函数补上）。
    """
    if wire == 2:
        return enc_varint((number << 3) | 2) + enc_varint(len(payload)) + payload
    return enc_varint((number << 3) | wire) + payload


def vi(number: int, value: int) -> bytes:
    """造一个 varint 字段。"""
    return field(number, 0, enc_varint(value))


def ld(number: int, payload: bytes) -> bytes:
    """造一个 length-delimited 字段。"""
    return field(number, 2, payload)


def msg(*parts: bytes) -> bytes:
    """把若干字段拼成一个 message。"""
    return b"".join(parts)


# ── 基础 ────────────────────────────────────────────────────────────────────


def test_varint_roundtrip():
    for n in (0, 1, 127, 128, 300, 16384, 2**31, 2**53, 2**63 - 1):
        assert proto._read_varint(enc_varint(n), 0) == (n, len(enc_varint(n))), n


def test_signed_int32():
    """负数 int32 用 10 字节 varint 编码，必须解回负数。"""
    payload = enc_varint(((-5) + (1 << 64)) % (1 << 64))
    data = msg(vi(45405, 0)[:-0] or b"")
    blob = msg(field(45411, 0, enc_varint(((-5) + (1 << 64)) % (1 << 64))))
    d = proto.decode_message(blob, "MsgContent")
    assert d["img_width"] == -5, d


def test_unknown_field_kept():
    blob = msg(vi(45002, 1), ld(999999, b"\x01\x02"))
    d = proto.decode_message(blob, "MsgContent")
    assert "_unknown" in d
    assert d["_unknown"][0][0] == 999999


def test_defaults_filled():
    blob = msg(vi(45002, 1))
    d = proto.decode_message(blob, "MsgContent", defaults=True)
    assert d["text"] == ""
    assert d["img_width"] == 0
    assert d["filesize"] == 0
    assert d["md5_raw"] == b""
    assert d["text_fallback"] == []


def test_only_filter():
    blob = msg(vi(45002, 1), ld(45101, "你好".encode("utf-8")), vi(45411, 100))
    d = proto.decode_message(blob, "MsgContent", only=("text",))
    assert d == {"text": "你好"}, d


# ── 40800 消息体 ───────────────────────────────────────────────────────────


def test_decode_text_segment():
    inner = msg(vi(45001, 12345), vi(45002, 1), ld(45101, "你好，世界".encode("utf-8")))
    outer = ld(40800, inner)
    d = proto.decode_40800(outer)
    assert len(d["content"]) == 1
    c = d["content"][0]
    assert c["content_type"] == 1
    assert c["text"] == "你好，世界"
    assert c["msg_id"] == 12345


def test_parse_40800_segments():
    inner = msg(vi(45002, 1), ld(45101, "hi".encode("utf-8")))
    outer = ld(40800, inner) + ld(40800, inner)
    segs = proto.parse_40800(outer)
    assert len(segs) == 2, segs
    s = segs[0]
    assert s.field_no == 40800
    assert s.name == "content"
    assert s.kind == proto.KIND_TEXT
    assert s.seg_text() == "hi"
    assert s.seg_type_name() == "文本"
    assert s.summarize() == "hi"


def test_parse_40800_bare_content():
    """没有外层包装时降级成单段。"""
    inner = msg(vi(45002, 1), ld(45101, "bare".encode("utf-8")))
    segs = proto.parse_40800(inner)
    assert len(segs) == 1
    assert segs[0].seg_text() == "bare"


def test_image_segment():
    inner = msg(
        vi(45002, 2),
        vi(45411, 800),
        vi(45412, 600),
        vi(45405, 12345),
        ld(45402, "photo.jpg".encode("utf-8")),
        ld(45406, bytes(range(16))),
        ld(45812, r"C:\cache\photo.jpg".encode("utf-8")),
    )
    seg = proto.parse_40800(ld(40800, inner))[0]
    assert seg.kind == proto.KIND_IMAGE
    assert seg.seg_type_name() == "图片"
    assert seg.summarize() == "[图片]"
    assert seg.seg_filename() == "photo.jpg"
    assert seg.seg_filesize() == 12345
    assert seg.seg_local_path() == r"C:\cache\photo.jpg"
    assert seg.seg_md5() == bytes(range(16)).hex()


def test_file_segment():
    inner = msg(
        vi(45002, 3),
        ld(45402, "报表.xlsx".encode("utf-8")),
        vi(45405, 4096),
        ld(45419, "xlsx".encode("utf-8")),
    )
    seg = proto.parse_40800(ld(40800, inner))[0]
    assert seg.kind == proto.KIND_FILE
    assert seg.summarize() == "[文件:报表.xlsx]"
    assert seg.seg_filesize() == 4096


def test_video_segment():
    inner = msg(vi(45002, 2), vi(45415, 15), ld(45402, "v.mp4".encode("utf-8")))
    seg = proto.parse_40800(ld(40800, inner))[0]
    assert seg.kind == proto.KIND_VIDEO
    assert seg.summarize() == "[视频]"


def test_sticker_segment():
    inner = msg(vi(45002, 6), ld(45600, b"\x01\x02\x03"))
    seg = proto.parse_40800(ld(40800, inner))[0]
    assert seg.kind == proto.KIND_STICKER
    assert seg.summarize() == "[表情]"


def test_voice_segment():
    inner = msg(vi(45002, 4), vi(45405, 3000))
    seg = proto.parse_40800(ld(40800, inner))[0]
    assert seg.kind == proto.KIND_VOICE
    assert seg.summarize() == "[语音]"


def test_nested_timestamp_message():
    """45112 是嵌套 message（MsgTimestamp）。"""
    ts = msg(vi(50202, 1700000000))
    inner = msg(vi(45002, 1), ld(45112, ts))
    d = proto.decode_message(inner, "MsgContent")
    assert d["f45112"]["timestamp"] == 1700000000


def test_repeated_text_fallback():
    inner = msg(
        vi(45002, 2),
        ld(45815, "fallback1".encode("utf-8")),
        ld(45815, "fallback2".encode("utf-8")),
    )
    seg = proto.parse_40800(ld(40800, inner))[0]
    assert seg.seg_text() == "fallback1"


def test_recursive_ref_msg():
    """47710 ref_msg 是嵌套的 MsgContent（自引用），不能无限递归。"""
    ref = msg(vi(45002, 1), ld(45101, "被引用的原文".encode("utf-8")))
    inner = msg(vi(45002, 7), ld(47710, ref), ld(47713, "摘要".encode("utf-8")))
    d = proto.decode_message(inner, "MsgContent")
    assert d["ref_msg"]["text"] == "被引用的原文"
    assert d["ref_f47713"] == "摘要"


def test_fixed32_and_fixed64():
    """fixed32 / fixed64 / double 也要能解（虽然 NTQQ 少用）。"""
    schema = {"x": proto.FieldDef(1, "x", "fixed32"), "y": proto.FieldDef(2, "y", "double")}
    blob = field(1, 5, struct.pack("<I", 70000)) + field(2, 1, struct.pack("<d", 1.5))
    d = proto.decode_message(blob, schema)
    assert d["x"] == 70000
    assert abs(d["y"] - 1.5) < 1e-9


def test_truncated_is_safe():
    """截断的数据不能抛异常。"""
    blob = ld(40800, msg(vi(45002, 1), ld(45101, b"abcdefgh")))
    broken = blob[:-3]
    segs = proto.parse_40800(broken)  # 不抛异常即可
    assert isinstance(segs, list)


def test_empty_inputs():
    assert proto.parse_40800(None) == []
    assert proto.parse_40800(b"") == []
    assert proto.decode_message(None, "MsgContent") == {}
    assert proto.parse_40600(None) == {}
    assert proto.parse_40801(None) == []
    assert proto.parse_40900(None) == []


# ── group 附加 proto ────────────────────────────────────────────────────────


def test_group_40600():
    inner = msg(vi(42211, 1700000000123), vi(42261, 7))
    d = proto.parse_40600(ld(40600, inner))
    assert d["status_timestamp"] == 1700000000123
    assert d["marker"] == 7


def test_group_40605():
    d = proto.parse_40605(ld(40605, b""))
    assert d == {}


def test_group_40801():
    inner = msg(vi(41501, 999), vi(41502, 3))
    out = proto.parse_40801(ld(40801, inner) + ld(40801, inner))
    assert len(out) == 2
    assert out[0]["msg_id"] == 999
    assert out[0]["state"] == 3


def test_group_40900():
    inner = msg(
        vi(40001, 8888),
        vi(40011, 2),
        vi(40013, 1),
        vi(40030, 123456),
        vi(40050, 1700000000),
        ld(40800, msg(vi(45002, 1), ld(45101, "hi".encode("utf-8")))),
    )
    out = proto.parse_40900(ld(40900, inner))
    assert len(out) == 1
    c = out[0]
    assert c["msg_id"] == 8888
    assert c["group_id"] == 123456
    assert c["msg_time"] == 1700000000
    assert isinstance(c["message"], list) and len(c["message"]) == 1


# ── 迷你 runner ─────────────────────────────────────────────────────────────


def main() -> int:
    fns = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("ok  ", name)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    print("-" * 60)
    if failed:
        print(f"{failed}/{len(fns)} 个测试失败")
        return 1
    print(f"all proto tests passed ({len(fns)} 个)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
