# -*- coding: utf-8 -*-
"""SQLCipher 加密库解密 —— NT QQ ``nt_msg.db`` 的读取入口。

用法::

    from qqexport.decrypt import decrypt_database

    plain = decrypt_database(r"C:\\...\\nt_msg.db", key16, r"C:\\...\\nt_msg_plain.db")
    print(plain)          # 明文库路径

原理（与官方 ``nt_msg_db_util/1.decrypt.py`` 一致）：

1. NTQQ 的 ``.db`` 文件前面带 1024 字节自定义头，**真正的 SQLCipher 库从
   offset 1024 开始**；先流式剥头得到 ``*.clear.db``。
2. 用 ``sqlcipher3`` 打开剥头后的库，PRAGMA 顺序不能改::

       PRAGMA cipher_page_size = <可选>   # 必须在 key 之前
       PRAGMA key = '...';
       PRAGMA kdf_iter = 4000;
       PRAGMA cipher_hmac_algorithm = HMAC_SHA1;
       PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;

   然后 ``SELECT count(*) FROM sqlite_master`` 触发真正的解密校验。
3. 逐表用 rowid 游标分页导出到输出明文库；遇到损坏页就重连 + 缩小批次，
   单行也坏则 ``rowid += 1`` 跳过；最后把索引 DDL 复制过去。

约束：只依赖标准库 + ``sqlcipher3``（本机预编译包见 :data:`SQLCIPHER_PKG_DIRS`）。
"""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "SqlcipherUnavailable",
    "strip_header",
    "open_encrypted",
    "decrypt_database",
    "detect_page_size",
    "list_encrypted_tables",
    "HEADER_SIZE",
    "BATCH_SIZE",
    "SQLCIPHER_PKG_DIRS",
]


# ── 常量 ────────────────────────────────────────────────────────────────────

HEADER_SIZE = 1024  # NTQQ 自定义头长度（固定值）
BATCH_SIZE = 5000  # 每批读取行数（遇到损坏时自动缩小）

def _sqlcipher_pkg_dirs() -> tuple[str, ...]:
    """``sqlcipher3`` 预编译包的候选目录（可选依赖，纯 Python 解密优先）。

    查找顺序：
    1. 环境变量 ``QQEXPORT_SQLCIPHER_DIR``（指向含 ``sqlcipher3`` 包的目录）；
    2. 仓库根目录下的 ``vendor/sqlcipher3_pkg``（本地放置，不随仓库发布）；
    3. 已安装到当前解释器 site-packages 的 ``sqlcipher3``（由 import 直接命中）。
    """
    dirs: list[str] = []
    env = os.environ.get("QQEXPORT_SQLCIPHER_DIR", "").strip()
    if env:
        dirs.append(env)
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    dirs.append(os.path.join(repo_root, "vendor", "sqlcipher3_pkg"))
    return tuple(d for d in dirs if d)


#: 本机可放置的 sqlcipher3 预编译包目录（Python 3.12 可用）
SQLCIPHER_PKG_DIRS: tuple[str, ...] = _sqlcipher_pkg_dirs()

#: 候选 cipher page size。``None`` 表示不设置该 PRAGMA（用 SQLCipher 默认 4096）。
#: 不同 NT 版本的 nt_msg.db 页大小不一样（实测有 1024 / 4096），因此这里自动探测。
PAGE_SIZE_CANDIDATES: tuple[int | None, ...] = (
    None,
    4096,
    1024,
    2048,
    8192,
    512,
    16384,
    32768,
    65536,
)

#: SQLite 内置表，不需要（也不能）导出
INTERNAL_TABLES = frozenset(
    {
        "sqlite_sequence",
        "sqlite_stat1",
        "sqlite_stat2",
        "sqlite_stat3",
        "sqlite_stat4",
    }
)

ProgressFn = Callable[[str], Any]


class SqlcipherUnavailable(RuntimeError):
    """本机没有可用的 ``sqlcipher3`` 模块。"""


class DecryptError(RuntimeError):
    """解密失败（口令错误 / 库损坏 / 参数不匹配）。"""


# ── sqlcipher3 导入 ─────────────────────────────────────────────────────────


def _load_sqlcipher():
    """导入 ``sqlcipher3.dbapi2``；失败时自动把预编译包目录加入 sys.path 重试。"""
    try:
        return importlib.import_module("sqlcipher3.dbapi2")
    except Exception:  # noqa: BLE001
        pass
    for extra in SQLCIPHER_PKG_DIRS:
        if not os.path.isdir(extra):
            continue
        if extra not in sys.path:
            sys.path.insert(0, extra)
        try:
            return importlib.import_module("sqlcipher3.dbapi2")
        except Exception:  # noqa: BLE001
            continue
    raise SqlcipherUnavailable(
        "找不到 sqlcipher3 模块。已尝试的目录: %s；"
        "请安装 sqlcipher3，或把含 sqlcipher3 包的目录加入 PYTHONPATH。"
        % ", ".join(SQLCIPHER_PKG_DIRS)
    )


def _normalize_key(key: str | bytes) -> str:
    """把口令规范成 str（bytes 按 ASCII / UTF-8 解）。"""
    if isinstance(key, (bytes, bytearray, memoryview)):
        raw = bytes(key)
        for enc in ("ascii", "utf-8"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        raise ValueError(
            "数据库口令是非文本原始字节（%d 字节，十六进制 %s），"
            "当前 SQLCipher 通道只支持文本口令；"
            "请在客户端抓取脚本里改用可打印 ASCII 口令。"
            % (len(raw), raw.hex())
        )
    return str(key)


# ── 步骤 1：剥头 ────────────────────────────────────────────────────────────


def strip_header(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    chunk_size: int = 64 << 20,
    force: bool = False,
) -> str:
    """把源库前 1024 字节的 NTQQ 头剥掉，输出纯 SQLCipher 文件。

    64MB 分块流式拷贝，不会把大文件读进内存。若 ``dst`` 已存在且大小
    正确则直接复用（便于断点续跑），``force=True`` 可强制重写。

    返回 ``dst`` 的路径字符串。
    """
    src_p = Path(src)
    dst_p = Path(dst)
    if not src_p.exists():
        raise FileNotFoundError(f"找不到输入文件: {src_p}")
    total = src_p.stat().st_size
    if total < HEADER_SIZE:
        raise DecryptError(
            f"源文件太小（{total} 字节），不足以包含 {HEADER_SIZE} 字节 NTQQ 头: {src_p}"
        )
    expected = total - HEADER_SIZE
    if dst_p.exists() and not force and dst_p.stat().st_size == expected:
        return str(dst_p)

    dst_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_p.with_name(dst_p.name + ".part")
    with open(src_p, "rb") as fin, open(tmp, "wb") as fout:
        fin.seek(HEADER_SIZE)
        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            fout.write(chunk)
    actual = tmp.stat().st_size
    if actual != expected:
        tmp.unlink(missing_ok=True)
        raise DecryptError(f"写出大小不符：期望 {expected:,}，实际 {actual:,}")
    os.replace(tmp, dst_p)
    return str(dst_p)


# ── 步骤 2：打开加密库 ──────────────────────────────────────────────────────


def open_encrypted(
    path: str | os.PathLike[str],
    key: str | bytes,
    *,
    page_size: int | None = None,
    kdf_iter: int = 4000,
    hmac_algorithm: str = "HMAC_SHA1",
    kdf_algorithm: str = "PBKDF2_HMAC_SHA512",
    verify: bool = True,
):
    """打开 SQLCipher 加密库，返回已通过解密校验的连接。

    ``page_size`` 为 ``None`` 时不设置 ``PRAGMA cipher_page_size``（用默认 4096）。
    口令里的单引号会被转义成 ``''``，不会破坏 PRAGMA 语法。
    """
    sc = _load_sqlcipher()
    conn = sc.connect(str(path), isolation_level=None)
    try:
        if page_size:
            conn.execute("PRAGMA cipher_page_size = %d;" % int(page_size))
        safe_key = _normalize_key(key).replace("'", "''")
        conn.execute("PRAGMA key = '%s';" % safe_key)
        conn.execute("PRAGMA kdf_iter = %d;" % int(kdf_iter))
        conn.execute("PRAGMA cipher_hmac_algorithm = %s;" % hmac_algorithm)
        conn.execute("PRAGMA cipher_kdf_algorithm = %s;" % kdf_algorithm)
        if verify:
            # 触发真正的解密校验：口令错 / 页大小不对都会在这里抛异常
            conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
    except Exception:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    return conn


def detect_page_size(
    path: str | os.PathLike[str],
    key: str | bytes,
    *,
    candidates: Sequence[int | None] = PAGE_SIZE_CANDIDATES,
) -> int | None:
    """探测正确的 ``cipher_page_size``；返回 ``None`` 表示「不设置该 PRAGMA」。

    做法是逐个候选值尝试打开并读 ``sqlite_master``，第一个能读通的就是它。
    页大小不对时 HMAC 校验必然失败，所以这个探测是可靠的。
    """
    last_err: Exception | None = None
    for cand in candidates:
        try:
            conn = open_encrypted(path, key, page_size=cand)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        try:
            conn.execute("SELECT count(*) FROM sqlite_master;").fetchone()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return cand
    raise DecryptError(
        "无法用该口令打开加密库（已尝试的 cipher_page_size: %s）。最后错误: %s"
        % (", ".join("默认" if c is None else str(c) for c in candidates), last_err)
    )


def list_encrypted_tables(conn) -> list[str]:
    """列出加密库里的表名（不含 sqlite_* 内置表）。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in INTERNAL_TABLES]


# ── 步骤 3：逐表导出 ────────────────────────────────────────────────────────


class _Source:
    """加密库句柄封装：记录打开参数，损坏后可以按同样参数重连。"""

    def __init__(self, path: str, key: str, page_size: int | None) -> None:
        self.path = path
        self.key = key
        self.page_size = page_size
        self.conn = None

    def connect(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self.conn = open_encrypted(self.path, self.key, page_size=self.page_size)
        return self.conn

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None


def _table_ddl(conn, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not row or not row[0]:
        raise DecryptError(f"找不到表 {table} 的建表语句")
    ddl = row[0]
    return re.sub(
        r"^CREATE\s+TABLE\s+", "CREATE TABLE IF NOT EXISTS ", ddl, flags=re.IGNORECASE
    )


def _has_rowid(conn, table: str) -> bool:
    try:
        conn.execute(f'SELECT rowid FROM "{table}" LIMIT 1').fetchone()
        return True
    except Exception:  # noqa: BLE001
        return False


def _export_table(
    src: _Source,
    plain: sqlite3.Connection,
    table: str,
    *,
    batch_size: int = BATCH_SIZE,
    progress: ProgressFn | None = None,
) -> tuple[int, int]:
    """把一张表从加密库导出到明文库，返回 ``(成功行数, 跳过损坏次数)``。

    策略：rowid 游标分页（``WHERE rowid > ? ORDER BY rowid LIMIT N``），
    不受随机主键值域影响，始终 O(log N) 定位。遇到损坏页就重连 + 缩小批次；
    单行也坏则 ``rowid += 1`` 跳过。支持断点续跑（从输出库 max(rowid) 继续）。
    """
    enc = src.connect()
    plain.execute(_table_ddl(enc, table))
    plain.commit()

    ncols = len(enc.execute(f'PRAGMA table_info("{table}")').fetchall())
    ph = ",".join("?" * ncols)

    try:
        last_rowid = (
            plain.execute(f'SELECT max(rowid) FROM "{table}"').fetchone()[0] or 0
        )
    except Exception:  # noqa: BLE001
        last_rowid = 0

    total_src = 0
    try:
        total_src = enc.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    except Exception:  # noqa: BLE001
        pass

    ok_rows = 0
    bad_skips = 0
    fail_streak = 0
    batch = batch_size
    t0 = time.time()
    use_rowid = _has_rowid(enc, table)

    def _say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    if not use_rowid:
        # WITHOUT ROWID 表：没有 rowid 游标，退化为一次性全表读
        try:
            rows = enc.execute(f'SELECT * FROM "{table}"').fetchall()
            plain.executemany(
                f'INSERT OR IGNORE INTO "{table}" VALUES ({ph})', rows
            )
            plain.commit()
            ok_rows = len(rows)
        except Exception as exc:  # noqa: BLE001
            raise DecryptError(
                "表 %s 没有 rowid，整表拷贝失败：%s" % (table, exc)
            ) from exc
        _say(f"      表 {table}: {ok_rows:,} 行（无 rowid，直接拷贝）")
        return ok_rows, 0

    while True:
        try:
            rows_with_id = enc.execute(
                f'SELECT rowid, * FROM "{table}" WHERE rowid > ? ORDER BY rowid LIMIT ?',
                (last_rowid, batch),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            # 损坏：重连 + 缩小批次
            try:
                enc.close()
            except Exception:  # noqa: BLE001
                pass
            enc = src.connect()
            if batch == 1:
                bad_skips += 1
                fail_streak += 1
                if fail_streak > 100:
                    raise DecryptError(
                        "表 %s 连续 %d 次读取失败，已中止（疑似数据页损坏）：%s"
                        % (table, fail_streak, exc)
                    ) from exc
                # 尽量跳到下一条真实 rowid，避免在 rowid 空洞上逐个 +1
                try:
                    nxt = enc.execute(
                        f'SELECT min(rowid) FROM "{table}" WHERE rowid > ?',
                        (last_rowid,),
                    ).fetchone()[0]
                except Exception:  # noqa: BLE001
                    nxt = None
                if nxt is not None and int(nxt) > last_rowid:
                    last_rowid = int(nxt)
                    fail_streak = 0
                else:
                    last_rowid += 1  # 跳过这个坏 rowid
                if bad_skips % 50 == 0:
                    _say(
                        f"      ... {ok_rows:,} 行  {bad_skips} 处损坏  {time.time() - t0:.0f}s"
                    )
                batch = min(batch_size, 100)
            else:
                batch = max(1, batch // 4)
            continue

        if not rows_with_id:
            break

        fail_streak = 0
        last_rowid = rows_with_id[-1][0]
        rows = [r[1:] for r in rows_with_id]  # 去掉首列 rowid，还原原始列顺序
        try:
            plain.executemany(f'INSERT OR IGNORE INTO "{table}" VALUES ({ph})', rows)
            plain.commit()
        except Exception:  # noqa: BLE001
            # 单行插入失败（约束冲突等）→ 逐行插入，坏行单独跳过
            for r in rows:
                try:
                    plain.execute(
                        f'INSERT OR IGNORE INTO "{table}" VALUES ({ph})', r
                    )
                except Exception:  # noqa: BLE001
                    bad_skips += 1
            plain.commit()
        ok_rows += len(rows)

        if batch < batch_size:
            batch = min(batch_size, batch * 2)  # 恢复批次大小

        if ok_rows and ok_rows % 500_000 == 0:
            elapsed = time.time() - t0
            rate = ok_rows / elapsed if elapsed else 0
            _say(f"      ... {ok_rows:,}/{total_src:,} 行  ({rate:.0f} 行/s)")

    elapsed = time.time() - t0
    rate = ok_rows / elapsed if elapsed else 0
    _say(
        f"      ✓  {ok_rows:,}/{total_src:,} 行  跳过 {bad_skips} 处  "
        f"({rate:.0f} 行/s)  {elapsed:.0f}s"
    )
    return ok_rows, bad_skips


def _copy_indexes(src: _Source, plain: sqlite3.Connection) -> int:
    """把加密库的索引定义复制到明文库。"""
    enc = src.connect()
    try:
        indexes = enc.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ).fetchall()
    finally:
        try:
            enc.close()
        except Exception:  # noqa: BLE001
            pass
    copied = 0
    for (ddl,) in indexes:
        if not ddl:
            continue
        try:
            plain.execute(ddl)
            copied += 1
        except Exception:  # noqa: BLE001
            pass  # 已存在或依赖表不存在时跳过
    plain.commit()
    return copied


# ── 主入口 ──────────────────────────────────────────────────────────────────


def _default_out_dir() -> Path:
    """默认输出目录：优先用系统临时目录，不可用（沙箱）时退回工作区。

    某些受控环境下 ``%TEMP%`` 指向的目录不可写，这里逐个探测，
    保证 ``decrypt_database(src, key)`` 这种省略 out_db 的调用也能用。
    """
    candidates = [
        Path(tempfile.gettempdir()) / "qqexport",
        Path.cwd() / ".qqexport",
        Path.home() / ".qqexport",
    ]
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / (".write_probe_%d" % os.getpid())
            probe.write_bytes(b"ok")
            probe.unlink()
            return cand
        except Exception:  # noqa: BLE001
            continue
    raise DecryptError("找不到可写的输出目录，请显式传入 out_db")


def default_out_dir() -> Path:
    """默认输出目录（公开接口，供 db.py 决定明文库缓存位置）。"""
    return _default_out_dir()


def _decrypt_with_pure_python(
    src_p: Path,
    out_p: Path,
    key: str | bytes,
    say,
) -> bool:
    """用纯 Python 的 SQLCipher 实现逐页解密；成功返回 True。

    失败（缺少 cryptography / 口令不对 / 库格式特殊）时返回 False，调用方
    会退回官方 sqlcipher3 的逐行导出路径。
    """
    try:
        from . import sqlcipher_py as sp
    except Exception:  # noqa: BLE001
        return False
    if not getattr(sp, "_HAVE_CRYPTOGRAPHY", False):
        return False
    kb = key if isinstance(key, (bytes, bytearray)) else str(key).encode("utf-8")
    try:
        if not sp.check_passphrase(bytes(kb), str(src_p),
                                   header_size=HEADER_SIZE,
                                   page_size=sp.DEFAULT_PAGE_SIZE):
            return False
    except Exception:  # noqa: BLE001
        return False

    say(f"[1/3] 纯 Python 解密 {src_p.name} → {out_p.name} ...")
    tmp_p = out_p.with_name(out_p.name + ".part")
    last = [0.0]

    def _progress(done: int, total: int) -> None:
        import time
        now = time.time()
        if now - last[0] < 1.0 and done != total:
            return
        last[0] = now
        pct = (done * 100 // total) if total else 0
        say(f"      解密中 {done:,}/{total:,} 页 ({pct}%)")

    try:
        sp.decrypt_file(str(src_p), str(tmp_p), bytes(kb),
                        header_size=HEADER_SIZE,
                        page_size=sp.DEFAULT_PAGE_SIZE,
                        progress=_progress)
    except Exception as exc:  # noqa: BLE001
        say(f"      纯 Python 解密失败（{exc}），改用 sqlcipher3 ...")
        try:
            tmp_p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return False
    for suffix in ("-wal", "-shm"):
        side = Path(str(out_p) + suffix)
        if side.exists():
            try:
                side.unlink()
            except Exception:  # noqa: BLE001
                pass
    os.replace(tmp_p, out_p)
    say(f"[完成] 明文库: {out_p}")
    return True


def decrypt_database(
    src_db: str | os.PathLike[str],
    key: str | bytes,
    out_db: str | os.PathLike[str] | None = None,
    *,
    progress: ProgressFn | None = None,
    page_size: int | None = None,
    batch_size: int = BATCH_SIZE,
    keep_clear: bool = True,
) -> str:
    """把 NTQQ 的加密消息库解密成明文 SQLite 库。

    :param src_db:  源库路径（含 1024 字节 NTQQ 头）
    :param key:     口令（16 字节 ASCII 字符串；也接受 bytes）
    :param out_db:  输出明文库路径；``None`` 时自动在临时目录生成
    :param progress: 进度回调，参数是一个人类可读的字符串
    :param page_size: cipher page size；``None`` 表示自动探测
    :param batch_size: 分页导出的每批行数
    :param keep_clear: 是否保留中间的 ``*.clear.db``（保留可断点续跑）
    :returns: 输出明文库路径
    """
    src_p = Path(src_db)
    if not src_p.exists():
        raise FileNotFoundError(f"找不到输入文件: {src_p}")

    if out_db is None:
        # 多个账号的消息库都叫 nt_msg.db，默认名必须带源路径摘要，否则互相覆盖
        digest = hashlib.sha1(
            str(src_p.resolve()).encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        out_p = Path(_default_out_dir()) / ("%s.%s.plain.db" % (src_p.stem, digest))
    else:
        out_p = Path(out_db)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    def _say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    # ── 快路径：纯 Python 逐页解密 ────────────────────────────────────────
    # QQ 自带的 SQLCipher 与官方 sqlcipher3 的 HMAC 计算方式不同，官方模块
    # 读不了 QQ 的库；纯 Python 实现（sqlcipher_py）可以，而且比逐行导出快
    # 两个数量级（3.1GB 库约 12 秒），并且能完整保留原始表结构与索引。
    if _decrypt_with_pure_python(src_p, out_p, key, _say):
        return str(out_p)

    _say(f"[1/3] 剥离 NTQQ 头  {src_p.name} → {out_p.name}.clear.db ...")
    clear_p = out_p.with_name(out_p.name + ".clear.db")
    strip_header(src_p, clear_p)

    _say("[2/3] 验证解密参数 ...")
    resolved_ps = page_size
    if resolved_ps is None:
        resolved_ps = detect_page_size(clear_p, key)
    _say(
        "      cipher_page_size = %s"
        % ("默认(4096)" if resolved_ps is None else str(resolved_ps))
    )
    src = _Source(str(clear_p), _normalize_key(key), resolved_ps)
    try:
        conn = src.connect()
        tables = list_encrypted_tables(conn)
        conn.close()
    except Exception as exc:  # noqa: BLE001
        raise DecryptError(f"解密失败，请检查口令: {exc}") from exc
    _say(f"      密钥正确 ✓  共 {len(tables)} 张表: {', '.join(tables)}")

    _say(f"[3/3] 导出明文数据库 → {out_p} ...")
    # 先写临时文件，全部成功后再原子替换 —— 中途失败/断电不会留下被当作
    # 有效缓存复用的残缺库（decrypt_to_plain 只按 mtime 判断是否可复用）。
    tmp_p = out_p.with_name(out_p.name + ".tmp")
    _tmp_sides = [tmp_p, Path(str(tmp_p) + "-wal"), Path(str(tmp_p) + "-shm")]
    for p in _tmp_sides:
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    total_ok = 0
    total_bad = 0
    try:
        plain = None
        try:
            plain = sqlite3.connect(str(tmp_p))
            plain.execute("PRAGMA journal_mode = WAL;")
            plain.execute("PRAGMA synchronous = NORMAL;")
            plain.execute("PRAGMA cache_size = -65536;")  # 64 MB 页缓存

            for i, table in enumerate(tables, 1):
                _say(f"  [{i}/{len(tables)}] {table}")
                ok, bad = _export_table(
                    src,
                    plain,
                    table,
                    batch_size=batch_size,
                    progress=progress,
                )
                total_ok += ok
                total_bad += bad

            _say("  [索引] 复制索引定义 ...")
            n_idx = _copy_indexes(src, plain)
            _say(f"      复制 {n_idx} 个索引")
            try:
                plain.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:  # noqa: BLE001
                pass
        finally:
            if plain is not None:
                plain.close()
            src.close()

        for suffix in ("-wal", "-shm"):
            side = Path(str(out_p) + suffix)
            if side.exists():
                try:
                    side.unlink()
                except OSError:
                    pass
        os.replace(tmp_p, out_p)
        for p in _tmp_sides[1:]:
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
    except BaseException:
        for p in _tmp_sides:
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        raise

    if not keep_clear:
        try:
            Path(clear_p).unlink()
        except Exception:  # noqa: BLE001
            pass

    _say(
        f"[完成] 总计 {total_ok:,} 行  跳过 {total_bad} 处损坏  输出: {out_p}"
    )
    return str(out_p)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - 命令行入口
    """命令行入口：``python -m qqexport.decrypt <src> <key> [out]``。"""
    import argparse

    ap = argparse.ArgumentParser(description="解密 NTQQ 的 SQLCipher 消息库")
    ap.add_argument("src", help="源库路径（含 1024 字节 NTQQ 头）")
    ap.add_argument("key", help="口令（ASCII）")
    ap.add_argument("out", nargs="?", default=None, help="输出明文库路径")
    ap.add_argument("--page-size", type=int, default=None, help="cipher page size")
    args = ap.parse_args(argv)
    out = decrypt_database(
        args.src, args.key, args.out, progress=print, page_size=args.page_size
    )
    print(out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
