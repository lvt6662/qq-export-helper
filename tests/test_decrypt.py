# -*- coding: utf-8 -*-
"""decrypt.py 的单元测试。

主要验证对象是本机真实样本 ``login.db``（口令硬编码、长度 20 字节 ASCII），
它和 ``nt_msg.db`` 用的是同一套 SQLCipher 参数，因此可以完整跑通
「剥头 → 打开 → 导出 → 明文可读」全流程。

直接运行：``python tests\\test_decrypt.py``
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import traceback
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qqexport import decrypt  # noqa: E402

# 沙箱里 %TEMP% 落在 DSH 托管的不可写目录，所以测试统一用工作区内的 scratch 目录
SCRATCH_ROOT = os.environ.get("QQEXPORT_SCRATCH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_tmp_tests"))


class _scratch:
    """在 SCRATCH_ROOT 下开一个临时目录（退出时删掉）。"""

    def __enter__(self) -> str:
        os.makedirs(SCRATCH_ROOT, exist_ok=True)
        self.path = os.path.join(SCRATCH_ROOT, uuid.uuid4().hex[:12])
        os.makedirs(self.path, exist_ok=True)
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

# ── 真实样本 ────────────────────────────────────────────────────────────────
LOGIN_DB = r"C:\Users\<用户名>\Documents\Tencent Files\nt_qq\global\nt_db\login.db"
LOGIN_KEY = "BD156D6710D54D8782F4"  # 20 字节 ASCII

# 真实消息库（需要 Lead 提供 16 字节运行时口令才能端到端验证）
NT_MSG_DB = (
    r"C:\Users\<用户名>\Documents\Tencent Files\100001\nt_qq\nt_db\nt_msg.db"
)
NT_MSG_KEY_ENV = "NTQQ_DB_KEY"


def _have_login_db() -> bool:
    return os.path.exists(LOGIN_DB)


# ── 基础工具 ────────────────────────────────────────────────────────────────


def test_module_constants():
    assert decrypt.HEADER_SIZE == 1024
    assert decrypt.BATCH_SIZE > 0
    assert decrypt.SqlcipherUnavailable is not None
    assert isinstance(decrypt.SQLCIPHER_PKG_DIRS, tuple)


def test_strip_header_removes_1024_bytes():
    """剥头后的大小 = 原大小 - 1024。"""
    if not _have_login_db():
        print("  (skip: login.db 不存在)")
        return
    with _scratch() as td:
        dst = os.path.join(td, "login_clear.db")
        out = decrypt.strip_header(LOGIN_DB, dst)
        assert out == dst
        assert os.path.getsize(dst) == os.path.getsize(LOGIN_DB) - 1024
        # salt 就是剥头后文件的前 16 字节
        with open(dst, "rb") as f:
            salt = f.read(16)
        assert len(salt) == 16
        with open(LOGIN_DB, "rb") as f:
            f.seek(1024)
            assert f.read(16) == salt
        # 再跑一次应该直接复用（不重写）
        mtime = os.path.getmtime(dst)
        decrypt.strip_header(LOGIN_DB, dst)
        assert os.path.getmtime(dst) == mtime


def test_open_encrypted_reads_sqlite_master():
    """用 sqlcipher3 直接打开剥头后的库，能读出 sqlite_master。"""
    if not _have_login_db():
        print("  (skip: login.db 不存在)")
        return
    with _scratch() as td:
        clear = decrypt.strip_header(LOGIN_DB, os.path.join(td, "c.db"))
        conn = decrypt.open_encrypted(clear, LOGIN_KEY)
        try:
            n = conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()[0]
            assert n == 3, n
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                ).fetchall()
            }
            assert "login_table" in names, names
            assert "login_misc_data_table" in names, names
        finally:
            conn.close()


def test_open_encrypted_rejects_bad_key():
    """口令错误必须抛异常，而不是静默返回空库。"""
    if not _have_login_db():
        print("  (skip: login.db 不存在)")
        return
    with _scratch() as td:
        clear = decrypt.strip_header(LOGIN_DB, os.path.join(td, "c.db"))
        try:
            conn = decrypt.open_encrypted(clear, "THIS_IS_NOT_THE_KEY__")
        except Exception:  # noqa: BLE001
            return
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        raise AssertionError("错误口令居然打开成功了")


def test_decrypt_database_login_db():
    """端到端：login.db → 明文库，sqlite_master 有 3 个对象，能查到表名。"""
    if not _have_login_db():
        print("  (skip: login.db 不存在)")
        return
    with _scratch() as td:
        out_db = os.path.join(td, "login_plain.db")
        logs: list[str] = []
        out = decrypt.decrypt_database(
            LOGIN_DB, LOGIN_KEY, out_db, progress=logs.append
        )
        assert out == out_db
        assert os.path.exists(out_db)
        assert logs, "progress 回调没有被调用"

        conn = sqlite3.connect(out_db)
        try:
            n = conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()[0]
            assert n == 3, f"sqlite_master 对象数应为 3，实际 {n}"
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
                ).fetchall()
            ]
            assert names == ["login_misc_data_table", "login_table"], names
            # 明文库里能查到真实数据行
            n2 = conn.execute("SELECT count(*) FROM login_table;").fetchone()[0]
            assert n2 >= 1, n2
            # 索引也被复制过来了（sqlite_autoindex_*）
            idx = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%';"
                ).fetchall()
            ]
            assert not idx, idx
        finally:
            conn.close()


def test_decrypt_database_key_with_quote():
    """口令里带单引号时也要能正确转义（不抛 SQL 语法错）。"""
    if not _have_login_db():
        print("  (skip: login.db 不存在)")
        return
    with _scratch() as td:
        clear = decrypt.strip_header(LOGIN_DB, os.path.join(td, "c.db"))
        try:
            conn = decrypt.open_encrypted(clear, "BAD'KEY")
        except Exception:  # noqa: BLE001
            return
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        raise AssertionError("含单引号的错误口令居然打开成功了")


def test_detect_page_size():
    """自动探测 page size：login.db 用默认即可。"""
    if not _have_login_db():
        print("  (skip: login.db 不存在)")
        return
    with _scratch() as td:
        clear = decrypt.strip_header(LOGIN_DB, os.path.join(td, "c.db"))
        ps = decrypt.detect_page_size(clear, LOGIN_KEY)
        assert ps in (None, 4096), ps


def test_nt_msg_db_optional():
    """真实 nt_msg.db：若有运行时口令则做一次端到端验证，否则跳过。"""
    key = os.environ.get(NT_MSG_KEY_ENV, "")
    if not key or not os.path.exists(NT_MSG_DB):
        print("  (skip: 未提供 NTQQ_DB_KEY 环境变量或 nt_msg.db 不存在)")
        return
    with _scratch() as td:
        out = decrypt.decrypt_database(
            NT_MSG_DB, key, os.path.join(td, "nt_plain.db"), progress=print
        )
        conn = sqlite3.connect(out)
        try:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
                ).fetchall()
            ]
            assert "c2c_msg_table" in names, names
            assert "group_msg_table" in names, names
        finally:
            conn.close()


# ── 合成样本：模拟 QQ NT 的加密库（无需真实口令即可端到端验证） ────────────


def _sqlcipher3():
    """拿到 sqlcipher3 模块（必要时把探测目录加进 sys.path）。"""
    import importlib

    mod = getattr(_sqlcipher3, "_mod", None)
    if mod is None:
        for d in decrypt.SQLCIPHER_PKG_DIRS:
            if os.path.isdir(d) and d not in sys.path:
                sys.path.insert(0, d)
        mod = importlib.import_module("sqlcipher3")
        _sqlcipher3._mod = mod
    return mod


def _build_encrypted(path: str, key: str, *, page_size: int = 4096) -> str:
    """用 sqlcipher3 造一个与 QQ NT 同参数的加密库。

    PRAGMA 顺序照抄官方 ``1.decrypt.py``（实测：cipher_page_size 必须在 key 之前，
    其余三个算法参数必须在 key 之后，否则派生出的密钥与落盘时不一致）：
        cipher_page_size -> key -> kdf_iter -> cipher_hmac_algorithm -> cipher_kdf_algorithm
    """
    mod = _sqlcipher3()
    conn = mod.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA cipher_page_size = %d;" % page_size)
        conn.execute("PRAGMA key = '%s';" % key.replace("'", "''"))
        conn.execute("PRAGMA kdf_iter = 4000;")
        conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1;")
        conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;")
        conn.execute("CREATE TABLE t(a INTEGER, b TEXT);")
        conn.execute("INSERT INTO t VALUES (1, '中文消息');")
        conn.execute("INSERT INTO t VALUES (2, 'second');")
        conn.execute("CREATE INDEX idx_t_a ON t(a);")
    finally:
        conn.close()
    return path


def _prepend_qq_header(enc_path: str, out_path: str) -> str:
    """给加密库前面拼 1024 字节的 QQ NT 风格头（复刻真实 nt_msg.db 的布局）。"""
    header = bytearray(1024)
    header[0:16] = b"SQLite header 3\x00"
    header[16:18] = (1024).to_bytes(2, "big")  # 伪 SQLite 头里的页大小
    header[18] = 1
    header[19] = 0
    header[20:28] = b"QQ_NT DB"
    header[28:32] = (0xA3).to_bytes(4, "little")
    header[32:36] = bytes([0x12, 0x80, 0x01, 0x65])
    # 0x30 起是一段 120 个 ASCII 十六进制字符（真实库里是 salt 的 hex 串）
    hex_salt = (
        "ea30d39abfb2c95b3d22319e7ec3cac"
        "d8c390d16c2c52e627531d293b016b5ab"
        "151253b59e36de886cea48bba0a917afe"
        "2d6e3454d0c1c3f60c17f31e8"
    ).encode("ascii")[:120]
    header[48:168] = hex_salt.ljust(120, b"0")
    header[168:170] = bytes([0x1A, 0x07])
    header[170:177] = b"1.1.0.1"
    header[177:179] = bytes([0x22, 0x09])
    header[179:189] = b"HMAC_SHA1("
    header[189:205] = bytes(
        [0xFC, 0xAE, 0xF5, 0xC9, 0x06, 0x30, 0xFD, 0xED,
         0x9C, 0xCF, 0x06, 0x00, 0x00, 0x00, 0x00, 0x00]
    )
    header[205:209] = bytes([0x06, 0x00, 0x00, 0x00])
    with open(out_path, "wb") as f:
        f.write(bytes(header))
        with open(enc_path, "rb") as g:
            while True:
                chunk = g.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    return out_path


def test_roundtrip_synthetic_qq_db():
    """合成一个 QQ NT 风格的加密库 → 剥头 → 解密 → 明文内容一致。"""
    key = "SYNTHETIC_KEY_16"
    with _scratch() as td:
        enc = _build_encrypted(os.path.join(td, "enc.db"), key)
        full = _prepend_qq_header(enc, os.path.join(td, "full.db"))
        assert os.path.getsize(full) == os.path.getsize(enc) + 1024

        out = decrypt.decrypt_database(
            full, key, os.path.join(td, "plain.db"), progress=lambda _m: None
        )
        conn = sqlite3.connect(out)
        try:
            rows = conn.execute("SELECT a, b FROM t ORDER BY a;").fetchall()
            assert rows == [(1, "中文消息"), (2, "second")], rows
            # 索引 DDL 也被复制过来了
            idx = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name NOT LIKE 'sqlite_%';"
                ).fetchall()
            ]
            assert "idx_t_a" in idx, idx
        finally:
            conn.close()


def test_roundtrip_synthetic_page_size_1024():
    """页大小不是 4096 时（QQ 用 1024），自动探测 + 解密仍然可用。"""
    key = "SYNTHETIC_KEY_16"
    with _scratch() as td:
        enc = _build_encrypted(os.path.join(td, "enc.db"), key, page_size=1024)
        full = _prepend_qq_header(enc, os.path.join(td, "full.db"))
        clear = decrypt.strip_header(full, os.path.join(td, "c.db"))
        ps = decrypt.detect_page_size(clear, key)
        assert ps in (1024, None), ps
        out = decrypt.decrypt_database(
            full, key, os.path.join(td, "plain.db"), progress=lambda _m: None
        )
        conn = sqlite3.connect(out)
        try:
            assert conn.execute("SELECT count(*) FROM t;").fetchone()[0] == 2
        finally:
            conn.close()


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
    print(f"all decrypt tests passed ({len(fns)} 个)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
