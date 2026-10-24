# -*- coding: utf-8 -*-
"""群名 / 昵称解析的单元测试。

背景：QQ 的消息表里**没有群名字段**（``group_msg_table`` 只有「发送者的群名片」
40090），群名保存在 ``recent_contact_v3_table`` 的 ``40094`` 列里。
本测试造一个带联系人表的假库，验证 ``db.contact_names`` 与 ``db.list_chats``
能正确取到群名 / 对方昵称。

运行::

    python tests\\test_contacts.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import traceback
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
for _p in (_SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from qqexport import db  # noqa: E402

# 与 make_fixture.py 保持一致的最小表结构
_C2C_DDL = """
CREATE TABLE c2c_msg_table (
    "40001" INTEGER PRIMARY KEY, "40011" INTEGER, "40012" INTEGER,
    "40013" INTEGER, "40020" TEXT, "40021" TEXT, "40027" INTEGER,
    "40030" INTEGER, "40033" INTEGER, "40050" INTEGER, "40093" TEXT,
    "40800" BLOB
)
"""
_GROUP_DDL = """
CREATE TABLE group_msg_table (
    "40001" INTEGER PRIMARY KEY, "40011" INTEGER, "40012" INTEGER,
    "40013" INTEGER, "40020" TEXT, "40021" TEXT, "40027" INTEGER,
    "40030" INTEGER, "40033" INTEGER, "40050" INTEGER, "40090" TEXT,
    "40093" TEXT, "40800" BLOB
)
"""
# 真实库里 recent_contact_v3_table 有几十列，这里只留用得上的几列
_CONTACT_DDL = """
CREATE TABLE recent_contact_v3_table (
    "40021" TEXT, "40030" INTEGER, "40033" INTEGER,
    "40093" TEXT, "40094" TEXT, "40095" TEXT
)
"""

BASE_TS = 1_700_000_000
MY_UIN = "100001"

# 群会话：群号 -> (群名 40094, 我在该群的昵称 40093, 群名片 40090)
GROUPS = {
    1001: ("哈工大铁路协会", "無夕.", "安喆(千夜)"),
    1002: ("", "", ""),                      # 本地没缓存群名 -> 应退化成群号
    1003: ("", "安徒生", "11739"),           # 40094 空、40093 是**我的昵称** -> 不能当群名
}
# 单聊：对方 QQ -> (昵称 40094, 我设的备注名 40093)
C2C = {
    2001: ("辣椒酱", "辣椒酱"),
    2002: ("metroren", "我愿变成一颗恒星"),
    2003: ("", "只剩备注"),
}


def _scratch() -> str:
    base = os.path.join(os.path.dirname(_HERE), "_tmp_tests")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "contacts_" + uuid.uuid4().hex[:8])
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _make_db(path: str) -> str:
    """造一个带联系人表的假明文库。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute(_C2C_DDL)
        conn.execute(_GROUP_DDL)
        conn.execute(_CONTACT_DDL)
        rowid = 0
        for gid, (name, nick, card) in GROUPS.items():
            for i in range(3):
                rowid += 1
                conn.execute(
                    'INSERT INTO group_msg_table ("40001","40011","40012","40013",'
                    '"40020","40021","40027","40030","40033","40050","40090","40093") '
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rowid, 2, 1, 0, "u_sender", "u_sender", 1, gid, 12345,
                     BASE_TS + rowid, card, nick),
                )
            # 群行：40030 = 0，40021 = 群号
            conn.execute(
                'INSERT INTO recent_contact_v3_table ("40021","40030","40033",'
                '"40093","40094","40095") VALUES (?,?,?,?,?,?)',
                (str(gid), 0, 12345, nick, name, ""),
            )
        rowid = 0
        for peer, (nick, remark) in C2C.items():
            for i in range(2):
                rowid += 1
                conn.execute(
                    'INSERT INTO c2c_msg_table ("40001","40011","40012","40013",'
                    '"40020","40021","40027","40030","40033","40050","40093") '
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rowid, 2, 1, 0, "u_peer", "u_peer", 1, peer, 0,
                     BASE_TS + 100 + rowid, nick),
                )
            # 单聊行：40030 = 对方 QQ，40021 = 以 u_ 开头的 uid
            conn.execute(
                'INSERT INTO recent_contact_v3_table ("40021","40030","40033",'
                '"40093","40094","40095") VALUES (?,?,?,?,?,?)',
                ("u_peer_%d" % peer, peer, 0, remark, nick, ""),
            )
        conn.commit()
    finally:
        conn.close()
    return path


# ── 测试 ─────────────────────────────────────────────────────────────────────


def test_contact_names_split_group_and_c2c():
    path = os.path.join(_scratch(), "fake.db")
    try:
        _make_db(path)
        conn = db.open_plain_db(path)
        try:
            groups, c2cs = db.contact_names(conn)
            # 群：key 是群号
            assert groups.get("1001") == "哈工大铁路协会", groups
            # 本地没缓存群名的群不该有名字
            assert "1002" not in groups, groups
            # 关键语义：群行的 40093 是「我在该群的昵称」，不能当群名
            assert "1003" not in groups, groups
            # 单聊：key 是对方 QQ
            assert c2cs.get("2001") == "辣椒酱", c2cs
            assert c2cs.get("2002") == "metroren", c2cs
            # 昵称为空时回退到备注名
            assert c2cs.get("2003") == "只剩备注", c2cs
        finally:
            conn.close()
    finally:
        _cleanup(os.path.dirname(path))


def test_list_chats_uses_group_names():
    path = os.path.join(_scratch(), "fake.db")
    try:
        _make_db(path)
        conn = db.open_plain_db(path)
        try:
            chats = {c.chat_id: c for c in db.list_chats(conn)}
        finally:
            conn.close()
        assert chats["1001"].name == "哈工大铁路协会", chats["1001"].name
        assert chats["1001"].is_group is True
        # 没有群名的群 -> 退化成群号（群号字符串，不是名字）
        assert chats["1002"].name == "1002", chats["1002"].name
        # 40093（我的昵称）绝不能被当成群名
        assert chats["1003"].name == "1003", chats["1003"].name
        # 单聊
        assert chats["2001"].name == "辣椒酱", chats["2001"].name
        assert chats["2001"].is_group is False
    finally:
        _cleanup(os.path.dirname(path))


def test_list_chats_meta_counts_without_messages():
    """元数据阶段就该知道每个会话有多少条消息（消息没载入）。"""
    path = os.path.join(_scratch(), "fake.db")
    try:
        _make_db(path)
        conn = db.open_plain_db(path)
        try:
            chats = {c.chat_id: c for c in db.list_chats(conn)}
        finally:
            conn.close()
        assert chats["1001"].total_count == 3, chats["1001"].total_count
        assert chats["2001"].total_count == 2, chats["2001"].total_count
        # 消息没载入时，to_dict 的 count 也要有值
        assert chats["1001"].to_dict(with_messages=False)["count"] == 3
    finally:
        _cleanup(os.path.dirname(path))


def test_chat_reader_stream_does_not_retain_messages():
    """流式遍历结束后，元数据对象里不能还攥着全部消息。"""
    path = os.path.join(_scratch(), "fake.db")
    try:
        _make_db(path)
        reader = db.ChatReader(path, None, my_uin=MY_UIN)
        try:
            stream = reader.stream()
            n = 0
            for chat in stream.iter_chats():
                assert chat.count >= 0
                n += 1
            assert n == len(GROUPS) + len(C2C), n
            # 关键：元数据对象必须还是「无消息」的
            for meta in stream.metas():
                assert not meta.messages, (
                    "元数据对象里残留了 %d 条消息 —— 会让整库驻留内存"
                    % len(meta.messages)
                )
        finally:
            reader.close()
    finally:
        _cleanup(os.path.dirname(path))


def test_chat_reader_load_returns_new_object():
    """load() 必须返回**新对象**，不就地污染元数据。"""
    path = os.path.join(_scratch(), "fake.db")
    try:
        _make_db(path)
        reader = db.ChatReader(path, None, my_uin=MY_UIN)
        try:
            metas = reader.list_chats()
            meta = metas[0]
            loaded = reader.load(meta)
            assert loaded is not meta, "load() 不应就地修改传入的对象"
            assert loaded.messages, "新对象应当带着消息"
            assert not meta.messages, "原元数据对象不应被填充"
        finally:
            reader.close()
    finally:
        _cleanup(os.path.dirname(path))


def test_contact_names_without_table():
    """没有联系人表的老库不能报错，退化成群号/昵称回退。"""
    path = os.path.join(_scratch(), "old.db")
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute(_C2C_DDL)
            conn.execute(_GROUP_DDL)
            conn.execute(
                'INSERT INTO group_msg_table ("40001","40030","40050") VALUES (1, 777, ?)',
                (BASE_TS,),
            )
            conn.commit()
        finally:
            conn.close()
        conn = db.open_plain_db(path)
        try:
            groups, c2cs = db.contact_names(conn)
            assert groups == {}, groups
            assert c2cs == {}, c2cs
            chats = {c.chat_id: c for c in db.list_chats(conn)}
            assert chats["777"].name == "777", chats["777"].name
        finally:
            conn.close()
    finally:
        _cleanup(os.path.dirname(path))


def test_chat_reader_usable_from_another_thread():
    """连接在主线程打开，要能在别的线程里用。

    UI 的 HTTP 线程就是用主线程（点「加载聊天记录」时）打开的连接去读消息的。
    sqlite3 默认 ``check_same_thread=True``，跨线程用会抛
    "SQLite objects created in a thread can only be used in that same thread"。
    """
    import threading

    path = os.path.join(_scratch(), "fake.db")
    try:
        _make_db(path)
        reader = db.ChatReader(path, None, my_uin=MY_UIN)
        try:
            errors = []
            got = {}

            def work():
                try:
                    chat = reader.load(reader.list_chats()[0])
                    got["count"] = chat.count
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            t = threading.Thread(target=work)
            t.start()
            t.join(30)
            assert not errors, "跨线程读失败: %r" % (errors[0] if errors else None)
            assert got.get("count", 0) > 0, got
        finally:
            reader.close()
    finally:
        _cleanup(os.path.dirname(path))


# ── 运行器 ───────────────────────────────────────────────────────────────────

_CASES = [
    test_contact_names_split_group_and_c2c,
    test_list_chats_uses_group_names,
    test_list_chats_meta_counts_without_messages,
    test_chat_reader_stream_does_not_retain_messages,
    test_chat_reader_load_returns_new_object,
    test_contact_names_without_table,
    test_chat_reader_usable_from_another_thread,
]


def main() -> int:
    failed = 0
    for case in _CASES:
        try:
            case()
            print("ok   %s" % case.__name__)
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL %s" % case.__name__)
            traceback.print_exc()
    print("-" * 60)
    if failed:
        print("%d / %d 个用例失败" % (failed, len(_CASES)))
        return 1
    print("all contact tests passed (%d 个)" % len(_CASES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
