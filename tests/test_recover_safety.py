# -*- coding: utf-8 -*-
"""恢复库 / 明文缓存 / 原始解析器的安全性回归。

覆盖审计里发现的几个「静默出错」问题：

* 不同账号的 ``nt_msg.db`` 同名不同目录 → 明文缓存文件必须不同（曾共用
  ``plain/nt_msg.plain.db``，B 账号会读到 A 账号的会话）。
* ``recover_meta`` 不能因为读元信息就创建 0 字节库；``group_db_is_ready``
  不能把空库/0 行库当有效恢复库。
* 恢复中途失败不能先删掉上一份可用的恢复库；改名失败必须抛错而不是把
  旧库当新库返回。
* 损坏库里的超大 payload varint 不能触发内存炸弹；截断的 varint 不能让
  ``parse_wire`` 抛异常；深层嵌套的 protobuf 不能 RecursionError。
* 打开非 SQLite 文件失败时不能漏文件句柄。

直接运行：``python tests\\test_recover_safety.py``
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, _HERE)

import make_fixture as FIX  # noqa: E402
from qqexport import body, db, rawdb, recover  # noqa: E402
from test_decrypt import _prepend_qq_header  # noqa: E402
from test_e2e_synthetic import KEY, _build_encrypted_msg_db  # noqa: E402

_SCRATCH = os.path.join(_HERE, "_scratch", "recover_safety")


def _fresh(name: str) -> str:
    path = os.path.join(_SCRATCH, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def _varint(n: int) -> bytes:
    """最小 SQLite varint 编码（测试用）。"""
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def _make_plain_group_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            'CREATE TABLE group_msg_table ('
            '"40001" INTEGER PRIMARY KEY, "40030" INTEGER, '
            '"40050" TEXT, "40800" BLOB)'
        )
        conn.executemany(
            "INSERT INTO group_msg_table VALUES (?,?,?,?)",
            [(1, 88012345, "hello", b"x"), (2, 88012345, "world", b"y")],
        )
        conn.commit()
    finally:
        conn.close()


def test_plain_cache_paths_unique() -> None:
    p1 = db.plain_db_path(os.path.join(_SCRATCH, "a", "nt_msg.db"))
    p2 = db.plain_db_path(os.path.join(_SCRATCH, "b", "nt_msg.db"))
    assert os.path.basename(p1) != os.path.basename(p2), (p1, p2)
    assert p1 == db.plain_db_path(os.path.join(_SCRATCH, "a", "nt_msg.db"))
    # 两个账号的目录不同 → 缓存名必须不同，否则会串号
    assert p1 != p2


def test_cache_isolation_end_to_end() -> None:
    root = _fresh("cache")
    os.makedirs(os.path.join(root, "a"))
    os.makedirs(os.path.join(root, "b"))
    enc_a = _build_encrypted_msg_db(os.path.join(root, "a", "enc.db"), KEY)
    db_a = _prepend_qq_header(enc_a, os.path.join(root, "a", "nt_msg.db"))

    orig_c2c, orig_group = FIX._c2c_rows, FIX._group_rows
    FIX._c2c_rows = lambda: [r for r in orig_c2c() if r[7] == 10002]
    FIX._group_rows = lambda: []
    try:
        enc_b = _build_encrypted_msg_db(os.path.join(root, "b", "enc.db"), KEY)
        db_b = _prepend_qq_header(enc_b, os.path.join(root, "b", "nt_msg.db"))
    finally:
        FIX._c2c_rows, FIX._group_rows = orig_c2c, orig_group

    cache = os.path.join(root, "plain")
    os.makedirs(cache, exist_ok=True)
    old_cache = db.PLAIN_CACHE_DIR
    db.PLAIN_CACHE_DIR = cache
    try:
        p1 = db.decrypt_to_plain(db_a, KEY)
        p2 = db.decrypt_to_plain(db_b, KEY)
        ids_a = sorted(c.chat_id for c in db.read_chats(p1, my_uin=FIX.MY_UIN))
        ids_b = sorted(c.chat_id for c in db.read_chats(p2, my_uin=FIX.MY_UIN))
    finally:
        db.PLAIN_CACHE_DIR = old_cache
    assert os.path.abspath(p1) != os.path.abspath(p2), (p1, p2)
    assert ids_b == ["10002"], ids_b
    assert "88012345" in ids_a, ids_a


def test_recover_meta_missing_does_not_create() -> None:
    root = _fresh("meta_missing")
    missing = os.path.join(root, "nope.db")
    assert recover.recover_meta(missing) == {}
    assert not os.path.exists(missing), "recover_meta 不应该创建文件"


def test_group_db_is_ready_rejects_empty() -> None:
    root = _fresh("ready_empty")
    plain = os.path.join(root, "nt_msg.db")
    with open(plain, "wb") as fh:
        fh.write(b"x" * 4096)
    out = recover.group_db_path(plain)
    with open(out, "wb"):
        pass
    assert recover.group_db_is_ready(plain) is False


def test_group_db_is_ready_rejects_zero_rows_meta() -> None:
    root = _fresh("ready_zero")
    plain = os.path.join(root, "nt_msg.db")
    with open(plain, "wb") as fh:
        fh.write(b"x" * 4096)
    out = recover.group_db_path(plain)
    conn = sqlite3.connect(out)
    try:
        conn.execute(
            "CREATE TABLE __qqexport_recover_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT INTO __qqexport_recover_meta VALUES ('recovered_rows', '0')"
        )
        conn.commit()
    finally:
        conn.close()
    assert recover.group_db_is_ready(plain) is False


def test_recover_group_table_rejects_same_path() -> None:
    root = _fresh("same_path")
    plain = os.path.join(root, "nt_msg.db")
    with open(plain, "wb") as fh:
        fh.write(b"x" * 4096)
    try:
        recover.recover_group_table(plain, plain, reuse=False)
    except ValueError as exc:
        assert "不能与明文库相同" in str(exc)
    else:
        raise AssertionError("应该拒绝把恢复库写到明文库路径上")


def test_recover_group_table_keeps_old_db_when_replace_fails() -> None:
    root = _fresh("replace_fail")
    plain = os.path.join(root, "nt_msg.db")
    _make_plain_group_db(plain)
    out = recover.group_db_path(plain)
    with open(out, "wb") as fh:
        fh.write(b"OLD-GOOD-DB")

    orig_replace = recover.os.replace

    def boom(src, dst):
        raise OSError(32, "file in use")

    recover.os.replace = boom
    try:
        try:
            recover.recover_group_table(plain, out, reuse=False)
        except RuntimeError as exc:
            assert "恢复库写入失败" in str(exc)
        else:
            raise AssertionError("改名失败应该抛 RuntimeError")
    finally:
        recover.os.replace = orig_replace
        tmp = out + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
    with open(out, "rb") as fh:
        assert fh.read() == b"OLD-GOOD-DB", "旧的恢复库不能被提前删掉"


def test_parse_cell_rejects_huge_payload() -> None:
    data = bytearray(4096)
    ptr = 100
    payload = _varint(1 << 40)  # 1TB，明显超上限
    data[ptr : ptr + len(payload)] = payload
    try:
        recover._parse_cell(None, bytes(data), ptr, 4096)
    except recover._CellError as exc:
        assert "载荷长度异常" in str(exc)
    else:
        raise AssertionError("超大 payload 应该被拒绝")


def test_body_deep_nesting_no_recursion() -> None:
    blob = b""
    for _ in range(12):
        blob = b"\x0a" + _varint(len(blob)) + blob
    fields = body.parse_wire(blob)
    folded = body.fields_to_dict(fields)  # 不能 RecursionError
    assert isinstance(folded, dict)


def test_parse_wire_truncated_varint_no_raise() -> None:
    assert body.parse_wire(b"\x0a\xff") == []
    assert body.parse_wire(b"\x08") == []
    assert body.parse_wire(b"\x0a\x05ab") == []


def test_reject_bad_table_name() -> None:
    root = _fresh("bad_table")
    plain = os.path.join(root, "nt_msg.db")
    _make_plain_group_db(plain)
    for bad in ("group_msg_table; DROP TABLE x", 'a"b', "1abc", "", "a b"):
        try:
            recover.recover_group_table(plain, None, table=bad, reuse=False)
        except ValueError as exc:
            assert "非法的表名" in str(exc), (bad, exc)
        else:
            raise AssertionError("应拒绝表名 %r" % bad)
        try:
            rawdb.table_schema(plain, bad)
        except ValueError as exc:
            assert "非法的表名" in str(exc), (bad, exc)
        else:
            raise AssertionError("rawdb 应拒绝表名 %r" % bad)


def test_plain_db_closes_handle_on_bad_header() -> None:
    root = _fresh("bad_header")
    bad = os.path.join(root, "not_sqlite.db")
    with open(bad, "wb") as fh:
        fh.write(b"not a sqlite database" * 10)
    try:
        rawdb.PlainDb(bad)
    except rawdb.RawTableError:
        pass
    else:
        raise AssertionError("非 SQLite 文件应该报 RawTableError")
    # Windows 上句柄没关的话这里会删不掉
    os.remove(bad)


def main() -> int:
    shutil.rmtree(_SCRATCH, ignore_errors=True)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        name = fn.__name__
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print("FAIL %s: %s" % (name, exc))
            traceback.print_exc()
        else:
            print("ok   %s" % name)
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
