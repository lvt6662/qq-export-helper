# -*- coding: utf-8 -*-
"""本轮全量复查修复项的回归测试。

覆盖：
* ``db._refine_kind`` 的 content_type 5/9 映射（5=视频，9=红包）；
* ``model.ChatStream`` 迭代结束后自动关闭底层 Reader；
* ``backup.decode_msg_time`` 对脏时间戳不抛异常；
* ``rawdb.table_schema`` 对含 ``#`` 的路径能正常打开；
* ``recover._freelist_pages`` 能解析出空闲页（恢复时应跳过）。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
for _p in (_SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from qqexport import backup, db, proto, rawdb, recover  # noqa: E402
from qqexport.model import Chat, ChatStream  # noqa: E402


def _scratch() -> str:
    base = os.path.join(os.path.dirname(_HERE), "_tmp_tests")
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, "audit_" + uuid.uuid4().hex[:8])
    os.makedirs(path, exist_ok=True)
    return path


def _seg(ct: int) -> proto.Segment:
    return proto.Segment(
        field_no=40800, name="content", kind="text", value={"content_type": ct}
    )


def test_content_type_mapping() -> None:
    assert db._refine_kind(_seg(5), 2) == proto.KIND_VIDEO
    assert db._refine_kind(_seg(9), 2) == proto.KIND_RED_PACKET
    assert db._refine_kind(_seg(6), 2) == proto.KIND_STICKER


def test_chat_stream_closes_on_iteration_end() -> None:
    closed: list[int] = []
    stream = ChatStream(
        [Chat(chat_id="1")],
        lambda meta: meta,
        on_close=lambda: closed.append(1),
    )
    chats = list(stream)
    assert len(chats) == 1
    assert closed == [1], closed
    stream.close()  # 幂等
    assert closed == [1], closed


def test_chat_stream_closes_on_early_abort() -> None:
    closed: list[int] = []
    stream = ChatStream(
        [Chat(chat_id="1"), Chat(chat_id="2")],
        lambda meta: meta,
        on_close=lambda: closed.append(1),
    )
    it = stream.iter_chats()
    next(it)
    assert closed == [], closed
    it.close()  # 调用方中途放弃，也必须释放连接
    assert closed == [1], closed


def test_decode_msg_time_dirty_values() -> None:
    assert backup.decode_msg_time(None) is None
    assert backup.decode_msg_time("not-a-number") is None
    assert backup.decode_msg_time("9" * 40) is None
    assert backup.decode_msg_time(0) is not None


def test_table_schema_handles_hash_in_path() -> None:
    tmp = _scratch()
    try:
        path = os.path.join(tmp, "a#b.db")
        conn = sqlite3.connect(path)
        try:
            conn.execute('CREATE TABLE "t" ("40001" INTEGER PRIMARY KEY, "x" TEXT)')
            conn.commit()
        finally:
            conn.close()
        root, cols, pk = rawdb.table_schema(path, "t")
        assert root >= 1, root
        assert cols == ["40001", "x"], cols
        assert pk == 0, pk
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_freelist_pages_detected() -> None:
    tmp = _scratch()
    try:
        path = os.path.join(tmp, "free.db")
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA page_size=4096")
            conn.execute("CREATE TABLE t(x BLOB)")
            conn.executemany(
                "INSERT INTO t(x) VALUES (?)", [(b"y" * 800,) for _ in range(4000)]
            )
            conn.commit()
            conn.execute("DELETE FROM t")
            conn.commit()
        finally:
            conn.close()
        reader = recover.PlainReader(path)
        try:
            pages = recover._freelist_pages(reader)
            assert pages, "删除大量数据后 freelist 不应为空"
            assert all(1 <= p <= reader.page_count for p in pages)
        finally:
            reader.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    tests = [
        test_content_type_mapping,
        test_chat_stream_closes_on_iteration_end,
        test_chat_stream_closes_on_early_abort,
        test_decode_msg_time_dirty_values,
        test_table_schema_handles_hash_in_path,
        test_freelist_pages_detected,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("ok", fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL", fn.__name__, "->", type(exc).__name__, exc)
    if failed:
        print("FAILED %d/%d" % (failed, len(tests)))
        return 1
    print("PASS test_audit_fixes (%d)" % len(tests))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
