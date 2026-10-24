# -*- coding: utf-8 -*-
"""构造「假的明文消息库」，供 db.py 的单元测试使用。

生成的 SQLite 库结构与 QQ NT 的 ``nt_msg.db`` 一致：
    c2c_msg_table    40001 主键 / 40011 msg_type / 40012 subtype / 40013 方向 /
                     40020 发送者 NT UID / 40021 会话对象 UID / 40030 对方 QQ /
                     40033 发送者 QQ / 40050 时间戳 / 40093 发送者昵称 / 40800 消息体
    group_msg_table  同上，另有 40090 群名片；40030 = 群号
    40800            MsgBody { repeated MsgContent content = 40800 } 的 Protobuf

用法::

    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from make_fixture import make_fixture
    make_fixture(r"D:\\tmp\\fake_msg.db")

命令行::

    python tests\\make_fixture.py <输出.db>
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

# 固定基准时间，保证测试可复现（2026-10-20 20:00:00）
BASE_TIME = 1792536000

#: 本人 QQ 号（测试里的「我」）
MY_UIN = "100001"


# ── Protobuf 编码小工具 ──────────────────────────────────────────────────────


def enc_varint(n: int) -> bytes:
    """把非负整数编码成 Protobuf varint。"""
    if n < 0:
        n += 1 << 64
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field(number: int, wire: int, payload: bytes) -> bytes:
    """拼一个 protobuf 字段（wire: 0=varint, 1=fixed64, 2=length-delimited）。"""
    return enc_varint((number << 3) | wire) + payload


def vi(number: int, value: int) -> bytes:
    """varint 字段。"""
    return field(number, 0, enc_varint(value))


def ld(number: int, payload: bytes) -> bytes:
    """length-delimited 字段（bytes / string / 子消息）。"""
    return field(number, 2, enc_varint(len(payload)) + payload)


def st(number: int, text: str) -> bytes:
    """string 字段。"""
    return ld(number, text.encode("utf-8"))


def body(*contents: bytes) -> bytes:
    """把若干 MsgContent 包成 40800 消息体。"""
    return b"".join(ld(40800, c) for c in contents)


# ── 内容段构造 ───────────────────────────────────────────────────────────────


def seg_text(text: str, *, content_type: int = 1, msg_id: int = 0) -> bytes:
    """文本段（45002=1，45101=text）。"""
    return vi(45001, msg_id) + vi(45002, content_type) + st(45101, text)


def seg_image(
    *,
    filename: str = "photo.jpg",
    width: int = 800,
    height: int = 600,
    size: int = 12345,
    md5: bytes = b"\x01" * 16,
    local_path: str = r"C:\Users\<用户名>\Pictures\photo.jpg",
) -> bytes:
    """图片段（45002=2，带宽高）。"""
    return (
        vi(45002, 2)
        + st(45402, filename)
        + vi(45405, size)
        + ld(45406, md5)
        + vi(45411, width)
        + vi(45412, height)
        + st(45812, local_path)
        + st(45802, "https://cdn.example/photo.jpg")
    )


def seg_file(
    *,
    filename: str = "需求说明.docx",
    size: int = 51200,
    md5: bytes = b"\x02" * 16,
    ext: str = "docx",
) -> bytes:
    """文件段（45002=3）。"""
    return (
        vi(45002, 3)
        + st(45402, filename)
        + vi(45405, size)
        + ld(45406, md5)
        + st(45419, ext)
    )


def seg_voice(*, local_path: str = r"nt_msg\voice\1234.amr", size: int = 3200) -> bytes:
    """语音段（45002=4）。"""
    return vi(45002, 4) + st(45402, "voice.amr") + vi(45405, size) + st(45812, local_path)


def seg_video(*, duration: int = 12) -> bytes:
    """视频段（45002=2 且带 45415 时长）。"""
    return (
        vi(45002, 2)
        + st(45402, "clip.mp4")
        + vi(45405, 204800)
        + ld(45406, b"\x03" * 16)
        + vi(45415, duration)
        + vi(47601, 1)
    )


def seg_reply(text: str, *, summary: str = "你好，世界") -> bytes:
    """回复段（45002=7，带被引用的摘要 47713）。"""
    return vi(45002, 7) + st(45101, text) + st(47713, summary) + st(47705, "张小明")


def seg_contact(*, nickname: str = "李婷") -> bytes:
    """名片段（45002=7 + nc_uid_1/47703）。"""
    return vi(45002, 7) + st(47703, "u_liting") + st(47705, nickname)


def seg_sticker() -> bytes:
    """表情段（45002=6，带 45600 sticker 字节）。"""
    return vi(45002, 6) + ld(45600, b"\x04" * 16)


def seg_forward() -> bytes:
    """聊天记录（45002=16，47901 = fwd_meta JSON）。"""
    meta = json.dumps({"title": "群聊记录", "count": 12}, ensure_ascii=False)
    return vi(45002, 16) + st(47901, meta) + st(47904, "fwd-uuid")


def seg_sys(text: str = "对方撤回了一条消息") -> bytes:
    """系统消息（45002=8，80900 = sys_content JSON）。"""
    payload = json.dumps({"msgType": 5, "str": text}, ensure_ascii=False)
    return vi(45002, 8) + vi(80810, 5) + st(80900, payload)


def seg_call(*, desc: str = "语音通话", duration: int = 65) -> bytes:
    """通话记录（45002=21）。"""
    return vi(45002, 21) + vi(48151, 1) + vi(48152, duration) + st(48153, desc)


# ── 假库构造 ─────────────────────────────────────────────────────────────────

_C2C_DDL = """
CREATE TABLE c2c_msg_table (
    "40001" INTEGER PRIMARY KEY,
    "40011" INTEGER,
    "40012" INTEGER,
    "40013" INTEGER,
    "40020" TEXT,
    "40021" TEXT,
    "40027" INTEGER,
    "40030" INTEGER,
    "40033" INTEGER,
    "40050" INTEGER,
    "40093" TEXT,
    "40800" BLOB
)
"""

_GROUP_DDL = """
CREATE TABLE group_msg_table (
    "40001" INTEGER PRIMARY KEY,
    "40011" INTEGER,
    "40012" INTEGER,
    "40013" INTEGER,
    "40020" TEXT,
    "40021" TEXT,
    "40027" INTEGER,
    "40030" INTEGER,
    "40033" INTEGER,
    "40050" INTEGER,
    "40090" TEXT,
    "40093" TEXT,
    "40800" BLOB
)
"""

# 列顺序（与 DDL 一致）：40001,40011,40012,40013,40020,40021,40027,40030,
#                   40033,40050,40093,40800 / 群聊多一个 40090 群名片
_C2C_COLS = ", ".join(
    f'"{c}"'
    for c in (
        "40001", "40011", "40012", "40013", "40020", "40021", "40027",
        "40030", "40033", "40050", "40093", "40800",
    )
)
_GROUP_COLS = ", ".join(
    f'"{c}"'
    for c in (
        "40001", "40011", "40012", "40013", "40020", "40021", "40027",
        "40030", "40033", "40050", "40090", "40093", "40800",
    )
)


def _c2c_rows() -> list[tuple]:
    """单聊消息行（msg_id, msg_type, subtype, direction, sender_uid, peer_uid,
    40027 本地会话键, 40030 对方 QQ, 40033 发送者 QQ, 40050 时间戳,
    40093 发送者昵称, 40800 消息体）。"""
    peer_uid = "u_zhangxiaoming"
    my_uid = "u_me"
    return [
        # 1 我发的文本
        (
            1, 2, 0, 1, my_uid, peer_uid, 10001, 10001, int(MY_UIN), BASE_TIME + 0,
            "我", body(seg_text("你好，世界", msg_id=1)),
        ),
        # 2 对方发的图片
        (
            2, 2, 0, 0, peer_uid, peer_uid, 10001, 10001, 10001, BASE_TIME + 180,
            "张小明", body(seg_image()),
        ),
        # 3 对方发的文本
        (
            3, 2, 0, 0, peer_uid, peer_uid, 10001, 10001, 10001, BASE_TIME + 300,
            "张小明", body(seg_text("在的，有什么可以帮你？", msg_id=3)),
        ),
        # 4 我发的文件
        (
            4, 3, 0, 1, my_uid, peer_uid, 10001, 10001, int(MY_UIN), BASE_TIME + 720,
            "我", body(seg_file()),
        ),
        # 5 我发的语音
        (
            5, 2, 0, 1, my_uid, peer_uid, 10001, 10001, int(MY_UIN), BASE_TIME + 900,
            "我", body(seg_voice()),
        ),
        # 6 对方发的回复
        (
            6, 5, 0, 0, peer_uid, peer_uid, 10001, 10001, 10001, BASE_TIME + 1200,
            "张小明", body(seg_reply("好的，多谢")),
        ),
        # 7 对方发的视频
        (
            7, 2, 0, 0, peer_uid, peer_uid, 10001, 10001, 10001, BASE_TIME + 1500,
            "张小明", body(seg_video()),
        ),
        # 8 我发的聊天记录
        (
            8, 11, 0, 1, my_uid, peer_uid, 10001, 10001, int(MY_UIN), BASE_TIME + 1800,
            "我", body(seg_forward()),
        ),
        # 9 系统消息（对方撤回）——40013=3
        (
            9, 17, 12, 3, "", peer_uid, 10001, 10001, 0, BASE_TIME + 1860,
            "", body(seg_sys()),
        ),
        # 10 另一个单聊：10002
        (
            10, 2, 0, 0, "u_li", "u_li", 10002, 10002, 10002, BASE_TIME + 60,
            "李婷", body(seg_text("在吗？", msg_id=10)),
        ),
        # 11 系统消息且 40030=0（不该生成会话）
        (
            11, 17, 12, 3, "", "", 0, 0, 0, BASE_TIME + 1900,
            "", body(seg_sys("系统消息")),
        ),
    ]


def _group_rows() -> list[tuple]:
    """群聊消息行（比单聊多一个 40090 群名片）。"""
    gid = 88012345
    return [
        (
            101, 2, 0, 0, "u_wangqiang", str(gid), gid, gid, 100000001,
            BASE_TIME + 1200, "王强(研发小王)", "王强", body(seg_text("大家好，我是新来的")),
        ),
        (
            102, 2, 0, 1, "u_me", str(gid), gid, gid, int(MY_UIN),
            BASE_TIME + 1500, "", "我", body(seg_text("欢迎欢迎，请多指教")),
        ),
        (
            103, 3, 0, 0, "u_liting", str(gid), gid, gid, 100000002,
            BASE_TIME + 1800, "李婷(测试小李)", "李婷",
            body(seg_file(filename="会议纪要.docx", size=81920, md5=b"\x05" * 16)),
        ),
        (
            104, 2, 0, 0, "u_wangqiang", str(gid), gid, gid, 100000001,
            BASE_TIME + 2400, "王强(研发小王)", "王强", body(seg_image(filename="原型图.png")),
        ),
        (
            105, 17, 12, 3, "", str(gid), gid, gid, 0,
            BASE_TIME + 2700, "", "", body(seg_sys("「王强(研发小王)」撤回了一条消息")),
        ),
        (
            106, 2, 0, 0, "u_liting", str(gid), gid, gid, 100000002,
            BASE_TIME + 3000, "李婷(测试小李)", "李婷", body(seg_sticker()),
        ),
        (
            107, 19, 0, 0, "u_wangqiang", str(gid), gid, gid, 100000001,
            BASE_TIME + 3300, "王强(研发小王)", "王强", body(seg_call()),
        ),
    ]


def _ph(n: int) -> str:
    """生成 n 个占位符 ``?,?,?``。"""
    return ",".join(["?"] * n)


def make_fixture(path: str | os.PathLike[str], *, my_uin: str = MY_UIN) -> str:
    """生成假的明文消息库。

    :param path: 输出 SQLite 路径（已存在会被覆盖）
    :param my_uin: 「我」的 QQ 号
    :returns: 输出路径
    """
    p = os.fspath(path)
    if os.path.exists(p):
        os.unlink(p)
    parent = os.path.dirname(os.path.abspath(p))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        conn.execute(_C2C_DDL)
        conn.execute(_GROUP_DDL)
        conn.executemany(
            f"INSERT INTO c2c_msg_table ({_C2C_COLS}) VALUES ({_ph(12)})",
            _c2c_rows(),
        )
        conn.executemany(
            f"INSERT INTO group_msg_table ({_GROUP_COLS}) VALUES ({_ph(13)})",
            _group_rows(),
        )
        conn.commit()
    finally:
        conn.close()
    return p


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - 命令行入口
    argv = list(sys.argv[1:] if argv is None else argv)
    out = argv[0] if argv else os.path.join(os.path.dirname(__file__), "fixture.db")
    path = make_fixture(out)
    conn = sqlite3.connect(path)
    try:
        c2c = conn.execute("SELECT COUNT(*) FROM c2c_msg_table").fetchone()[0]
        grp = conn.execute("SELECT COUNT(*) FROM group_msg_table").fetchone()[0]
    finally:
        conn.close()
    print("已生成假库: %s（单聊 %d 行 / 群聊 %d 行）" % (path, c2c, grp))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
