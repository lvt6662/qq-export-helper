"""recover.py —— 从明文库里**恢复** ``group_msg_table`` 的表数据。

背景（取证结论，脚本见 ``qq-export-probe/probe_group_scan.py`` 等）：

QQ NT 的 ``nt_msg.db`` 加密解开后是标准 SQLite 库，但 ``group_msg_table``
这张表的**表 b-tree 指针链是坏的**——``sqlite_master`` 里的 rootpage 指向的
物理页根本不是它的 b-tree 根，整表扫描会报::

    sqlite3.DatabaseError: database disk image is malformed

而它的**覆盖索引**（``group_msg_table_idx40027_40058`` 等）却是好的，所以
``SELECT count(*) FROM group_msg_table`` 能返回正确的 2,882,289 行。

本模块的思路因此是**绕开坏掉的表 b-tree，改从叶页把数据捞出来**：

1. 走完好的索引，取出该表全部 rowid（实测 2.1 秒）；
2. 顺序扫描明文库里所有 ``0x0D`` 表叶子页，把 cell 解析出来，用 rowid
   集合筛出属于该表的记录，并把 ``(rowid, 页号, cell 指针)`` 记到一张小表里；
3. 按 rowid 升序回读明文库里的原始记录，写进一份**结构正常的新库**
   （``*.group.db``），表名/列名与原表一致，于是上层所有 SQL
   （``GROUP BY`` / ``ORDER BY`` / ``WHERE 40030 = ?``）都能原样工作。

实测：3.1 GB 明文库全扫约 32 秒，命中 2,881,083 / 2,882,289 行。
"""

from __future__ import annotations

import bisect
import mmap
import os
import re
import sqlite3
import struct
import time
from typing import Any, Sequence
from urllib.parse import quote

__all__ = [
    "QQ_RESERVED",
    "PlainReader",
    "recover_group_table",
    "recover_meta",
    "group_db_path",
    "group_db_is_ready",
    "group_row_count",
]


QQ_RESERVED = 80  # QQ 库的 SQLite 头 byte20：每页保留 80 字节
GROUP_TABLE = "group_msg_table"
_BATCH = 20000
#: 表名会直接拼进 SQL：只允许普通标识符，杜绝注入（即使当前调用方都传常量）
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
#: 单个 cell 的载荷上限：损坏库里 payload varint 可能是天文数字，
#: 不设上限会把几十亿字节读进内存（内存炸弹）。
_MAX_PAYLOAD = 64 * 1024 * 1024


# ── 低层工具 ────────────────────────────────────────────────────────────────


def _varint(buf: bytes, off: int) -> tuple[int, int]:
    """读 SQLite 变长整数，返回 ``(值, 新偏移)``。"""
    v = 0
    for i in range(9):
        x = buf[off + i]
        if i == 8:
            return ((v << 8) | x), off + 9
        v = (v << 7) | (x & 0x7F)
        if not (x & 0x80):
            return v, off + i + 1
    raise ValueError("变长整数解析失败")


def _g2(buf: bytes, off: int) -> int:
    return (buf[off] << 8) | buf[off + 1]


def _serial_len(t: int) -> int:
    """记录头里某个 serial type 对应的字节数。"""
    if t <= 4:
        return t
    if t == 5:
        return 6
    if t in (6, 7):
        return 8
    if t in (8, 9):
        return 0  # 常量 0 / 1
    if t >= 12:
        # 偶数 = BLOB，奇数 = TEXT，长度 = (t - 12) // 2
        return (t - 12) // 2
    return 0


def _is_text(t: int) -> bool:
    return t >= 13 and t % 2 == 1


class PlainReader:
    """以 mmap 方式随机读取明文库的页。"""

    def __init__(self, path: str | os.PathLike[str], page_size: int = 0) -> None:
        self.path = str(path)
        self._fh = open(self.path, "rb")
        try:
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:  # noqa: BLE001 - 空文件等无法 mmap 时别漏文件句柄
            self._fh.close()
            raise
        self._size = len(self._mm)
        head = self._mm[:100]
        self.encoding = "utf-8"
        if len(head) >= 18 and head[:16] == b"SQLite format 3\x00":
            ps = struct.unpack(">H", head[16:18])[0]
            self.page_size = 65536 if ps == 1 else ps
            self.reserved = head[20] if len(head) > 20 else 0
            enc = struct.unpack(">I", head[56:60])[0] if len(head) >= 60 else 0
            self.encoding = {1: "utf-8", 2: "utf-16-le", 3: "utf-16-be"}.get(
                enc, "utf-8"
            )
        else:  # 不是标准 SQLite 头 → 用默认值
            self.page_size = page_size or 4096
            self.reserved = QQ_RESERVED
        if not self.page_size:
            self.page_size = page_size or 4096
        self.page_count = self._size // self.page_size

    @property
    def usable(self) -> int:
        """每页可用字节数（页大小 − 保留区）。"""
        return self.page_size - self.reserved

    def close(self) -> None:
        try:
            self._mm.close()
        finally:
            self._fh.close()

    def __enter__(self) -> "PlainReader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def page(self, number: int) -> bytes | None:
        """取第 ``number`` 页（1 起）的原始字节；越界返回 ``None``。"""
        if number < 1:
            return None
        start = (number - 1) * self.page_size
        if start + self.page_size > self._size:
            return None
        return self._mm[start : start + self.page_size]


def _freelist_pages(reader: PlainReader) -> set[int]:
    """解析数据库头里的 freelist，返回所有**空闲页**的页号。

    SQLite 删除数据后页面进入 freelist，但页内旧的 ``0x0D`` 叶页头和 cell
    仍然存在。恢复时若不跳过这些页，会把已删除的旧版本当成有效数据。
    """
    pages: set[int] = set()
    header = reader.page(1)
    if header is None or len(header) < 40:
        return pages
    trunk = struct.unpack(">I", header[32:36])[0]
    guard = 0
    while trunk and guard < 100000:
        guard += 1
        if trunk in pages:
            break  # 防御性：freelist 成环
        pages.add(trunk)
        data = reader.page(trunk)
        if data is None or len(data) < 8:
            break
        nxt = struct.unpack(">I", data[0:4])[0]
        n = struct.unpack(">I", data[4:8])[0]
        n = min(n, (len(data) - 8) // 4)
        for i in range(n):
            pages.add(struct.unpack(">I", data[8 + 4 * i : 12 + 4 * i])[0])
        trunk = nxt
    return pages


# ── cell 解析 ──────────────────────────────────────────────────────────────


class _CellError(Exception):
    pass


def _parse_cell(
    reader: PlainReader, data: bytes, ptr: int, usable: int
) -> tuple[int, list[int], bytes]:
    """解析一个表叶子 cell，返回 ``(rowid, serial types, 记录体)``。

    记录体 = 记录头之后的字段内容（**不含**记录头本身）。
    """
    payload, off2 = _varint(data, ptr)
    if payload < 0 or payload > _MAX_PAYLOAD:
        raise _CellError("cell 载荷长度异常: %d" % payload)
    rowid, off3 = _varint(data, off2)
    header, hoff = _varint(data, off3)
    if header <= 0 or header > usable:
        raise _CellError("记录头长度非法")
    types: list[int] = []
    pos = hoff
    end = off3 + header
    while pos < end:
        t, pos = _varint(data, pos)
        types.append(t)
    data_start = off3 + header
    max_local = usable - 35
    min_local = ((usable - 12) * 32 // 255) - 23
    if payload <= max_local:
        local = payload
        ovfl = None
    else:
        k = min_local + ((payload - min_local) % (usable - 4))
        local = k if k <= max_local else min_local
        if off3 + local + 4 > len(data):
            raise _CellError("overflow 指针越界")
        ovfl = struct.unpack(">I", data[off3 + local : off3 + local + 4])[0]
    on_page = local - header
    if on_page < 0:
        raise _CellError("局部数据长度为负")
    body = bytearray(data[data_start : data_start + on_page])
    if ovfl:
        remaining = payload - local
        per_page = usable - 4
        number = ovfl
        guard = 0
        while number and remaining > 0:
            guard += 1
            if guard > 200000:
                raise _CellError("overflow 链过长")
            pg = reader.page(number)
            if pg is None:
                raise _CellError("overflow 页越界")
            take = min(remaining, per_page)
            body += pg[4 : 4 + take]
            remaining -= take
            number = struct.unpack(">I", pg[0:4])[0]
        if remaining > 0:
            raise _CellError("overflow 链不完整")
    return rowid, types, bytes(body)


def _decode_record(
    body: bytes, types: Sequence[int], rowid: int, pk_index: int | None, encoding: str
) -> list[Any]:
    """把记录体还原成列值列表（rowid 别名列用 rowid 顶上）。"""
    values: list[Any] = []
    pos = 0
    for t in types:
        n = _serial_len(t)
        if n == 0:
            if t == 8:
                values.append(0)
            elif t == 9:
                values.append(1)
            else:
                values.append(None)
            continue
        chunk = body[pos : pos + n]
        pos += n
        if _is_text(t):
            values.append(chunk.decode(encoding, "replace"))
        elif t >= 12:
            values.append(chunk)
        else:
            values.append(int.from_bytes(chunk, "big", signed=True))
    if pk_index is not None:
        while len(values) <= pk_index:
            values.append(None)
        values[pk_index] = rowid
    return values


# ── 主流程 ──────────────────────────────────────────────────────────────────


def group_db_path(plain_db: str | os.PathLike[str]) -> str:
    """给定明文库路径，算出恢复库（``*.group.db``）的路径。"""
    base = os.path.basename(str(plain_db))
    stem = base[:-3] if base.endswith(".db") else base
    return os.path.join(os.path.dirname(str(plain_db)), stem + ".group.db")


def group_db_is_ready(plain_db: str | os.PathLike[str],
                      out_db: str | os.PathLike[str] | None = None) -> bool:
    """恢复库是否已经存在、不比明文库旧，且确实恢复出了行。

    ``out_db`` 用于自定义恢复库路径；不传时用默认的 ``<明文库>.group.db``。
    """
    out = str(out_db) if out_db else group_db_path(plain_db)
    if not os.path.isfile(out):
        return False
    try:
        if os.path.getsize(out) <= 0:
            return False
        if os.path.getmtime(out) < os.path.getmtime(str(plain_db)):
            return False
    except OSError:
        return False
    meta = recover_meta(out)
    if meta:
        try:
            if int(meta.get("recovered_rows") or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def recover_meta(path: str | os.PathLike[str]) -> dict[str, str]:
    """读恢复库里的元信息（源表行数、实际恢复行数等）。

    用 ``mode=ro`` 打开：绝不能因为「读元信息」就创建一个 0 字节的库，
    否则后续 ``group_db_is_ready`` 会把空库当成有效恢复库。
    """
    out: dict[str, str] = {}
    uri = "file:" + quote(str(path).replace("\\", "/"), safe="/:") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return out
    try:
        rows = conn.execute(
            "SELECT key, value FROM __qqexport_recover_meta"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    for key, value in rows:
        out[str(key)] = str(value)
    return out


def group_row_count(path: str | os.PathLike[str], table: str = GROUP_TABLE) -> int:
    """恢复库（或任意库）里某张表的行数。"""
    try:
        conn = sqlite3.connect("file:" + quote(str(path).replace("\\", "/"), safe="/:") + "?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        return int(conn.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _table_info(conn: sqlite3.Connection, table: str) -> tuple[list[str], int | None]:
    """返回 ``(列名列表, INTEGER PRIMARY KEY 列下标)``。"""
    cols: list[str] = []
    pk_index: int | None = None
    for row in conn.execute('PRAGMA table_info("%s")' % table):
        name, decl, pk = row[1], row[2], row[5]
        cols.append(name)
        if pk == 1 and "INT" in (decl or "").upper():
            pk_index = len(cols) - 1
    return cols, pk_index


def _check_ident(name: str) -> str:
    """校验会拼进 SQL 的表名，只允许普通标识符。"""
    if not _IDENT_RE.match(name or ""):
        raise ValueError("非法的表名：%r" % (name,))
    return name


def recover_group_table(
    plain_db: str | os.PathLike[str],
    out_db: str | os.PathLike[str] | None = None,
    *,
    table: str = GROUP_TABLE,
    progress=None,
    reuse: bool = True,
) -> str:
    """把明文库里 ``table`` 的数据恢复到一份结构正常的新库。

    :param plain_db: 明文库路径
    :param out_db: 恢复库路径（默认 ``<明文库>.group.db``）
    :param table: 要恢复的表名
    :param progress: 进度回调（参数为一行中文提示）
    :param reuse: 恢复库已存在且不比明文库旧时直接复用
    :returns: 恢复库路径
    """
    plain_db = str(plain_db)
    table = _check_ident(table)
    out = str(out_db) if out_db else group_db_path(plain_db)
    if os.path.abspath(out) == os.path.abspath(plain_db):
        raise ValueError("恢复库路径不能与明文库相同：%s" % out)
    if reuse and group_db_is_ready(plain_db, out):
        if progress:
            progress("已存在恢复库，直接复用：" + os.path.basename(out))
        return out
    if not os.path.isfile(plain_db):
        raise FileNotFoundError(plain_db)

    def say(text: str) -> None:
        if progress:
            progress(text)

    t0 = time.time()
    uri = "file:" + quote(plain_db.replace("\\", "/"), safe="/:") + "?mode=ro"
    src = sqlite3.connect(uri, uri=True)
    try:
        row = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            raise ValueError("明文库里没有表 %s" % table)
        create_sql = row[0]
        cols, pk_index = _table_info(src, table)
        say("正在从索引读取 %s 的行号…" % table)
        rowids = sorted({r[0] for r in src.execute("SELECT rowid FROM %s" % table)})
        say("索引里共有 %d 行（%.1f 秒）" % (len(rowids), time.time() - t0))
    finally:
        src.close()

    loc_db = out + ".loc"
    tmp_out = out + ".tmp"
    # 只清理本次要重建的临时文件；旧的恢复库保留到新库完整写完再原子替换，
    # 这样中途失败/断电也不会把上一份可用数据弄丢。
    for path in (loc_db, tmp_out):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    # ── 第一步：扫叶页，记录 (rowid → 页号/cell 指针) ────────────────────
    loc = sqlite3.connect(loc_db)
    try:
        loc.execute("PRAGMA journal_mode=OFF")
        loc.execute("PRAGMA synchronous=OFF")
        loc.execute(
            "CREATE TABLE loc(rowid INTEGER PRIMARY KEY, pg INTEGER, cell INTEGER)"
        )
    except Exception:  # noqa: BLE001 - 建临时库失败就关掉连接再抛
        loc.close()
        raise
    loc_rows: list[tuple[int, int, int]] = []
    failed = 0
    try:
        with PlainReader(plain_db) as reader:
            usable = reader.usable
            free_pages = _freelist_pages(reader)
            if free_pages:
                say("跳过 %d 个 freelist 空闲页" % len(free_pages))
            last_report = time.time()
            for pg in range(1, reader.page_count + 1):
                if pg in free_pages:
                    continue
                data = reader.page(pg)
                if data is None or data[0] != 0x0D:
                    continue
                ncell = _g2(data, 3)
                # 损坏页里 ncell 可能是任意值：不设上限会越界读指针区
                if ncell < 0 or 8 + 2 * ncell > usable:
                    continue
                for i in range(ncell):
                    try:
                        ptr = _g2(data, 8 + 2 * i)
                    except IndexError:
                        break
                    if ptr < 12 or ptr + 18 > usable:
                        continue
                    try:
                        rowid, _types, _body = _parse_cell(reader, data, ptr, usable)
                    except (_CellError, ValueError, IndexError):
                        failed += 1
                        continue
                    pos = bisect.bisect_left(rowids, rowid)
                    if pos >= len(rowids) or rowids[pos] != rowid:
                        continue
                    loc_rows.append((rowid, pg, ptr))
                    if len(loc_rows) >= _BATCH:
                        loc.executemany("INSERT OR IGNORE INTO loc VALUES (?,?,?)", loc_rows)
                        loc_rows.clear()
                now = time.time()
                if now - last_report > 3:
                    last_report = now
                    say(
                        "扫描明文库 %.0f%%（已定位 %d 行）"
                        % (100.0 * pg / reader.page_count, loc.execute("SELECT count(*) FROM loc").fetchone()[0])
                    )
            if loc_rows:
                loc.executemany("INSERT OR IGNORE INTO loc VALUES (?,?,?)", loc_rows)
                loc_rows.clear()
        loc.commit()
        located = loc.execute("SELECT count(*) FROM loc").fetchone()[0]
        say("已定位 %d / %d 行（%.0f 秒）" % (located, len(rowids), time.time() - t0))

        # ── 第二步：按 rowid 升序回读并写入恢复库 ───────────────────────
        dst = sqlite3.connect(tmp_out)
        try:
            dst.execute("PRAGMA journal_mode=OFF")
            dst.execute("PRAGMA synchronous=OFF")
            dst.execute(create_sql)
            # 会话列表/按会话取消息都要按 40030（群号）过滤 → 建索引
            dst.execute(
                'CREATE INDEX IF NOT EXISTS "idx_recover_peer" ON "%s" ("40030", "40050")'
                % table
            )
            dst.commit()
            placeholders = ",".join("?" * len(cols))
            sql = 'INSERT INTO "%s" (%s) VALUES (%s)' % (
                table,
                ",".join('"%s"' % c for c in cols),
                placeholders,
            )
            batch: list[tuple] = []
            written = 0
            reader = PlainReader(plain_db)
            try:
                usable = reader.usable
                cur = loc.execute("SELECT rowid, pg, cell FROM loc ORDER BY rowid")
                while True:
                    chunk = cur.fetchmany(5000)
                    if not chunk:
                        break
                    for rowid, pg, ptr in chunk:
                        data = reader.page(pg)
                        if data is None:
                            continue
                        try:
                            r2, types, body = _parse_cell(reader, data, ptr, usable)
                        except (_CellError, ValueError, IndexError):
                            continue
                        if r2 != rowid:
                            continue
                        values = _decode_record(
                            body, types, rowid, pk_index, reader.encoding
                        )
                        # SQLite 会省略记录尾部全为 NULL 的列 → 这里补齐
                        if len(values) < len(cols):
                            values.extend([None] * (len(cols) - len(values)))
                        elif len(values) > len(cols):
                            values = values[: len(cols)]
                        batch.append(tuple(values))
                    if len(batch) >= _BATCH:
                        dst.executemany(sql, batch)
                        written += len(batch)
                        batch.clear()
                        say("写入恢复库 %d / %d 行" % (written, located))
                if batch:
                    dst.executemany(sql, batch)
                    written += len(batch)
            finally:
                reader.close()
            dst.commit()
            dst.execute(
                "CREATE TABLE IF NOT EXISTS __qqexport_recover_meta"
                "(key TEXT PRIMARY KEY, value TEXT)"
            )
            dst.executemany(
                "INSERT OR REPLACE INTO __qqexport_recover_meta VALUES (?,?)",
                [
                    ("table", table),
                    ("source_rows", str(len(rowids))),
                    ("recovered_rows", str(written)),
                    ("usable", str(usable)),
                    ("created", str(int(time.time()))),
                ],
            )
            dst.commit()
            say("恢复完成：%d 行（用时 %.0f 秒）" % (written, time.time() - t0))
        finally:
            dst.close()
        # 只有完整写完才改名，避免半成品被当成"已恢复"；
        # 改名失败必须抛错，让上层退回原库，而不是把旧恢复库当新的用。
        try:
            os.replace(tmp_out, out)
        except OSError as exc:
            raise RuntimeError(
                "恢复库写入失败（目标文件可能被占用）：%s" % out
            ) from exc
    finally:
        try:
            loc.close()
        except sqlite3.Error:
            pass
        if os.path.exists(loc_db):
            try:
                os.remove(loc_db)
            except OSError:
                pass  # 临时文件删不掉也无所谓，下次运行会覆盖
    return out
