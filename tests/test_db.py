# -*- coding: utf-8 -*-
"""db.py 的单元测试：用 ``make_fixture.py`` 造的假明文库验证读取逻辑。

运行::

    python tests\\test_db.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
for _p in (_SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from qqexport import db  # noqa: E402
from make_fixture import MY_UIN, make_fixture  # noqa: E402


# ── 临时目录（沙箱下 %TEMP% 可能不可写，所以放在工作区里） ──────────────────


def _scratch() -> str:
    """在工作区里造一个可写的临时目录。"""
    base = os.path.join(os.path.dirname(_HERE), "_tmp_tests")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "db_" + uuid.uuid4().hex[:8])
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _fixture_db() -> str:
    tmp = _scratch()
    path = make_fixture(os.path.join(tmp, "fake_msg.db"))
    return path


# ── 测试 ─────────────────────────────────────────────────────────────────────


def test_open_plain_db():
    path = _fixture_db()
    try:
        conn = db.open_plain_db(path)
        try:
            names = db.table_names(conn)
            assert db.C2C_TABLE in names
            assert db.GROUP_TABLE in names
        finally:
            conn.close()
    finally:
        _cleanup(os.path.dirname(path))


def test_open_plain_db_missing():
    try:
        db.open_plain_db(os.path.join(os.path.dirname(_HERE), "_不存在的库.db"))
    except FileNotFoundError:
        return
    raise AssertionError("应当抛 FileNotFoundError")


def test_list_chats():
    path = _fixture_db()
    try:
        conn = db.open_plain_db(path)
        try:
            chats = db.list_chats(conn)
        finally:
            conn.close()
        ids = {c.chat_id for c in chats}
        assert ids == {"10001", "10002", "88012345"}, ids
        by_id = {c.chat_id: c for c in chats}
        assert by_id["10001"].is_group is False
        assert by_id["10001"].peer == "10001"
        assert by_id["10001"].name == "张小明", by_id["10001"].name
        assert by_id["88012345"].is_group is True
        assert by_id["88012345"].name == "88012345"
        assert by_id["88012345"].peer == ""
        # 40030=0 的系统消息不该生成会话
        assert "0" not in ids
        # 按最后消息时间倒序
        times = [c.messages for c in chats]
        del times
    finally:
        _cleanup(os.path.dirname(path))


def test_list_chats_sorted_by_time():
    path = _fixture_db()
    try:
        conn = db.open_plain_db(path)
        try:
            chats = db.load_all(conn, my_uin=MY_UIN)
        finally:
            conn.close()
        assert chats[0].chat_id == "88012345", [c.chat_id for c in chats]
        assert chats[-1].chat_id == "10002", [c.chat_id for c in chats]
    finally:
        _cleanup(os.path.dirname(path))


def test_load_all_counts():
    path = _fixture_db()
    try:
        conn = db.open_plain_db(path)
        try:
            chats = db.load_all(conn, my_uin=MY_UIN)
        finally:
            conn.close()
        by_id = {c.chat_id: c for c in chats}
        # 单聊 10001 有 9 条（10002 只有 1 条）
        assert by_id["10001"].count == 9, by_id["10001"].count
        assert by_id["10002"].count == 1
        assert by_id["88012345"].count == 7
        # 消息按 40001 升序
        ids = [m.msg_id for m in by_id["10001"].messages]
        assert ids == sorted(ids), ids
    finally:
        _cleanup(os.path.dirname(path))


def test_message_text_and_direction():
    path = _fixture_db()
    try:
        conn = db.open_plain_db(path)
        try:
            chats = db.load_all(conn, my_uin=MY_UIN)
        finally:
            conn.close()
        by_id = {c.chat_id: c for c in chats}
        msgs = {m.msg_id: m for m in by_id["10001"].messages}

        m1 = msgs[1]
        assert m1.text == "你好，世界", m1.text
        assert m1.content == "你好，世界"
        assert m1.msg_type == 2
        assert m1.content_type == 1
        assert m1.direction == 1
        assert m1.is_mine is True
        assert m1.sender == "我"

        m2 = msgs[2]
        assert m2.direction == 0
        assert m2.is_mine is False
        assert m2.sender == "张小明"
        assert m2.content == "[图片]"
        assert len(m2.attachments) == 1
        att = m2.attachments[0]
        assert att.kind == "image"
        assert att.name == "photo.jpg"
        assert att.size == 12345
        assert att.md5 == "01" * 16
        assert att.local_path.endswith("photo.jpg")

        m3 = msgs[3]
        assert m3.text == "在的，有什么可以帮你？"

        m4 = msgs[4]
        assert m4.msg_type == 3
        assert m4.content == "[文件:需求说明.docx]"
        assert m4.attachments[0].kind == "file"
        assert m4.attachments[0].size == 51200

        m5 = msgs[5]
        assert m5.content == "[语音]"
        assert m5.attachments[0].kind == "audio"

        m6 = msgs[6]
        assert m6.msg_type == 5
        assert "你好，世界" in m6.content
        assert m6.content.startswith("[回复]")

        m7 = msgs[7]
        assert m7.content == "[视频]"
        assert m7.attachments[0].kind == "video"

        m8 = msgs[8]
        assert m8.msg_type == 11
        assert m8.content == "[聊天记录]"

        m9 = msgs[9]
        assert m9.msg_type == 17
        assert m9.direction == 3
        assert m9.is_mine is False
        assert "撤回" in m9.content
    finally:
        _cleanup(os.path.dirname(path))


def test_group_messages_use_group_card():
    path = _fixture_db()
    try:
        conn = db.open_plain_db(path)
        try:
            chats = db.load_all(conn, my_uin=MY_UIN)
        finally:
            conn.close()
        group = {c.chat_id: c for c in chats}["88012345"]
        msgs = {m.msg_id: m for m in group.messages}
        assert msgs[101].sender == "王强(研发小王)", msgs[101].sender
        assert msgs[101].is_mine is False
        assert msgs[102].is_mine is True
        assert msgs[103].attachments[0].name == "会议纪要.docx"
        assert msgs[106].content == "[表情]", msgs[106].content
        assert msgs[107].content.startswith("[通话]")
    finally:
        _cleanup(os.path.dirname(path))


def test_load_messages_single_chat():
    path = _fixture_db()
    try:
        conn = db.open_plain_db(path)
        try:
            chats = db.list_chats(conn)
            target = [c for c in chats if c.chat_id == "10002"][0]
            loaded = db.load_messages(conn, target, my_uin=MY_UIN)
        finally:
            conn.close()
        assert loaded.count == 1
        assert loaded.messages[0].text == "在吗？"
    finally:
        _cleanup(os.path.dirname(path))


def test_load_all_limit_per_chat():
    path = _fixture_db()
    try:
        conn = db.open_plain_db(path)
        try:
            chats = db.load_all(conn, my_uin=MY_UIN, limit_per_chat=2)
        finally:
            conn.close()
        by_id = {c.chat_id: c for c in chats}
        assert by_id["10001"].count == 2
        # 保留的是最近的两条
        assert [m.msg_id for m in by_id["10001"].messages] == [8, 9]
    finally:
        _cleanup(os.path.dirname(path))


def test_is_mine_without_my_uin():
    path = _fixture_db()
    try:
        conn = db.open_plain_db(path)
        try:
            chats = db.load_all(conn)  # 不给 my_uin
        finally:
            conn.close()
        by_id = {c.chat_id: c for c in chats}
        msgs = {m.msg_id: m for m in by_id["10001"].messages}
        assert msgs[1].is_mine is True  # 40013=1
        assert msgs[2].is_mine is False  # 40013=0
        assert msgs[9].is_mine is False  # 40013=3 系统消息
    finally:
        _cleanup(os.path.dirname(path))


def test_read_chats_plain():
    path = _fixture_db()
    try:
        chats = db.read_chats(path, None, my_uin=MY_UIN)
        assert len(chats) == 3
        total = sum(c.count for c in chats)
        assert total == 17, total  # 9 + 1 + 7
    finally:
        _cleanup(os.path.dirname(path))


def test_read_chat_by_id():
    path = _fixture_db()
    try:
        chat = db.read_chat(path, "10001", None, my_uin=MY_UIN)
        assert chat is not None
        assert chat.chat_id == "10001"
        assert chat.count == 9
        assert db.read_chat(path, "不存在的会话", None) is None
    finally:
        _cleanup(os.path.dirname(path))


def test_chat_to_dict():
    path = _fixture_db()
    try:
        chats = db.read_chats(path, None, my_uin=MY_UIN)
        data = chats[0].to_dict()
        assert "chat_id" in data and "messages" in data
        msg_data = data["messages"][0]
        assert "attachments" in msg_data
        assert "is_mine" in msg_data
    finally:
        _cleanup(os.path.dirname(path))


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
    print(f"all db tests passed ({len(fns)} 个)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
