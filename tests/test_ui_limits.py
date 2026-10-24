# -*- coding: utf-8 -*-
"""大会话限流 / 并发加载 / 数据目录切换的回归测试。

不依赖 pytest：可直接 ``python tests/test_ui_limits.py`` 运行。
"""

import http.client
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qqexport.model import Chat, Message  # noqa: E402
from qqexport.ui import server as srv_mod  # noqa: E402


class _FakeReader:
    """只实现 find_chat 用到的 load_into。"""

    def __init__(self, messages, fail=False):
        self.messages = messages
        self.fail = fail
        self.calls = []

    def load_into(self, chat, limit=None):
        self.calls.append(limit)
        if self.fail:
            raise RuntimeError("模拟读取失败")
        chat.messages = list(self.messages[:limit] if limit else self.messages)

    def close(self):
        pass


def _chat(count=5):
    chat = Chat(chat_id="123", name="测试会话", total_count=count)
    chat.total_count = count
    return chat


def _msgs(count=5):
    return [Message(msg_id=i, time=1700000000 + i, text="m%d" % i) for i in range(count)]


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers={"Host": "127.0.0.1:%d" % port})
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def test_chat_detail_limit_and_truncated():
    """?limit=2 时只返回最近 2 条，并标记 truncated。"""
    srv = srv_mod.QQExportServer()
    chat = _chat(5)
    srv.chats = [chat]
    srv._reader = _FakeReader(_msgs(5))
    base, port = srv.start("127.0.0.1", 0)
    try:
        status, body = _get(port, "/api/chats/123?limit=2")
        assert status == 200, (status, body[:300])
        data = json.loads(body)["chat"]
        assert len(data["messages"]) == 2, data["messages"]
        assert data["truncated"] is True
        assert data["total_count"] == 5
    finally:
        srv.stop()


def test_find_chat_raises_on_load_error():
    """读取失败不能当成空会话返回。"""
    srv = srv_mod.QQExportServer()
    srv.chats = [_chat(5)]
    srv._reader = _FakeReader(_msgs(5), fail=True)
    try:
        srv.find_chat("123", limit=2)
    except RuntimeError as exc:
        assert "读取失败" in str(exc)
    else:
        raise AssertionError("find_chat 应该抛错")


def test_concurrent_load_rejected():
    """并发 /api/load 必须被拒绝，不能交叉覆盖 reader。"""
    srv = srv_mod.QQExportServer()
    assert srv._load_lock.acquire(blocking=False)
    try:
        try:
            srv.load("")
        except RuntimeError as exc:
            assert "正在读取" in str(exc)
        else:
            raise AssertionError("并发 load 应该被拒绝")
    finally:
        srv._load_lock.release()


def test_set_data_root_resets_old_state():
    """手动指定新数据目录后，旧库的会话/口令必须清空。"""
    class _FakeAccounts:
        def __init__(self):
            self.roots = []

        def add_extra_root(self, path):
            self.roots.append(path)
            return True

        def list_accounts(self):
            return [{"uin": "10001", "db_path": "D:/new/nt_msg.db", "db_size": 1,
                     "db_mtime": 1, "has_key": False}]

        def account_roots(self):
            return self.roots

    old = srv_mod._accounts
    srv_mod._accounts = _FakeAccounts()
    srv = srv_mod.QQExportServer()
    srv.db_path = "D:/old/nt_msg.db"
    srv.current_uin = "20002"
    srv._key = "x" * 16
    srv.chats = [_chat(1)]
    srv._reader = _FakeReader(_msgs(1))
    try:
        srv.set_data_root("D:/new")
        assert srv.db_path == "D:/new/nt_msg.db", srv.db_path
        assert srv.current_uin == "10001", srv.current_uin
        assert srv._key == "", "换目录后旧口令必须清空"
        assert srv.chats == [], "换目录后旧会话列表必须清空"
    finally:
        srv_mod._accounts = old


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %s: %s" % (fn.__name__, exc))
        else:
            print("ok   %s" % fn.__name__)
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
