"""直接解析 QQ 明文 SQLite 库的原始 b-tree（绕过 sqlite3 的严格校验）。

背景
----
QQ 的 ``nt_msg.db`` 用 SQLCipher 加密，剥掉 1024 字节 QQ 扩展头并逐页解密后
得到「明文库」。但这个明文库有个特殊之处，导致标准 :mod:`sqlite3` 读群消息
表时会报 ``database disk image is malformed``：

* 文件头里的 *reserved bytes per page* 字段与数据页的实际排布不一致 ——
  数据是按 ``usable = page_size - 80`` 排布的（每页末尾 80 字节留给
  SQLCipher 的 IV/HMAC 等），而头部字段可能写的是别的值。
* 群消息表的某些叶子页会让 sqlite3 的 ``accessPayload`` 一致性检查失败。

但数据本身完全正常（自写解析器验证：585,301 个叶子页 / 2,882,289 条记录、
overflow 链与内部节点键值全部自洽）。所以这里自带一个最小 b-tree 读取器：
直接按页解析 cell，自己处理 overflow 链，输出 ``{列名: 值}`` 的字典。

读出来的值与 sqlite3 逐列比对完全一致（文本列 ``str``，二进制列 ``bytes``）。
"""

from __future__ import annotations

import os
import re
import sqlite3
import struct
from typing import Any, Iterator, Sequence
from urllib.parse import quote

__all__ = ["PlainDb", "RawTableError", "table_schema"]

_MAGIC = b"SQLite format 3\x00"

#: 表名会拼进 SQL（PRAGMA / SELECT），只允许普通标识符。
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

#: 单个 cell 的载荷上限：损坏库里 payload varint 可能是天文数字，
#: 不设上限会把几十亿字节读进内存（内存炸弹）。
_MAX_PAYLOAD = 64 * 1024 * 1024

# QQ 的 SQLCipher 每页末尾留 80 字节（IV 16 + HMAC 32 + 其它），解密后数据页
# 的可用区是 page_size - 80。
_QQ_RESERVED = 80


class RawTableError(RuntimeError):
    """原始 b-tree 解析失败。"""


def _varint(buf: bytes, off: int) -> tuple[int | None, int]:
    """读取 SQLite 的大端 varint，返回 ``(值, 新偏移)``；越界返回 ``(None, off)``。"""
    value = 0
    for i in range(9):
        if off + i >= len(buf):
            return None, off
        byte = buf[off + i]
        if i == 8:
            return ((value << 8) | byte), off + 9
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, off + i + 1
    return None, off


def _serial_len(serial: int) -> int:
    """serial type -> 负载字节数（-1 表示非法）。"""
    if serial in (0, 8, 9, 10, 11):
        return 0
    if 1 <= serial <= 4:
        return serial
    if serial == 5:
        return 6
    if serial in (6, 7):
        return 8
    if serial < 12:
        return -1
    return (serial - 12) // 2


def _is_text(serial: int) -> bool:
    """serial type >= 12 时：偶数 = BLOB，奇数 = TEXT。"""
    return serial >= 12 and (serial - 12) % 2 == 1


def table_schema(path: str | os.PathLike[str], table: str) -> tuple[int, list[str], int | None]:
    """返回 ``(rootpage, 列名列表, INTEGER PRIMARY KEY 列下标)``。

    只读 ``sqlite_master`` 与 ``PRAGMA table_info``（这两处在 QQ 库里始终正常）。
    """
    if not _IDENT_RE.match(table or ""):
        raise ValueError("非法的表名：%r" % (table,))
    uri = "file:" + quote(str(path).replace("\\", "/"), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        row = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row:
            raise RawTableError(f"库里没有表 {table!r}")
        root = int(row[0])
        cols: list[str] = []
        pk: int | None = None
        for cid, name, _ctype, _notnull, _dflt, is_pk in conn.execute(
            f'PRAGMA table_info("{table}")'
        ):
            cols.append(str(name))
            if is_pk:
                pk = int(cid)
        return root, cols, pk
    finally:
        conn.close()


class PlainDb:
    """QQ 明文库的原始读取器。"""

    def __init__(self, path: str | os.PathLike[str], *, usable: int | None = None):
        self.path = str(path)
        self._fh = open(self.path, "rb")
        self.page_size = 4096
        self.reserved = 0
        self.encoding = "utf-8"
        try:
            self._read_header()
        except Exception:  # noqa: BLE001 - 头部非法时别漏文件句柄
            self._fh.close()
            raise
        if usable:
            self.usable = usable
        else:
            # 探测阶段先以页大小作为可用区上界：_collect_leaves/_max_cell_end
            # 的边界检查会读取 self.usable，此时真实值尚未推断出来。
            self.usable = self.page_size
            self.usable = self._detect_usable()

    # ── 基础 ──────────────────────────────────────────────────────────────────

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass

    def __enter__(self) -> "PlainDb":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _read_header(self) -> None:
        head = self._fh.read(100)
        if not head.startswith(_MAGIC):
            raise RawTableError(f"不是 SQLite 数据库: {self.path}")
        raw = struct.unpack(">H", head[16:18])[0]
        self.page_size = 65536 if raw == 1 else raw
        self.reserved = head[20]
        enc = struct.unpack(">I", head[56:60])[0]
        self.encoding = {1: "utf-8", 2: "utf-16-le", 3: "utf-16-be"}.get(enc, "utf-8")

    def page(self, number: int) -> bytes | None:
        """按 1 起始的页号取整页内容；越界返回 ``None``。"""
        if number < 1:
            return None
        self._fh.seek((number - 1) * self.page_size)
        data = self._fh.read(self.page_size)
        if len(data) < self.page_size:
            return None
        return data

    # ── 可用区推断 ────────────────────────────────────────────────────────────

    def _candidate_usables(self) -> list[int]:
        cands = [
            self.page_size - _QQ_RESERVED,
            self.page_size - self.reserved,
            self.page_size - 48,
            self.page_size,
        ]
        out: list[int] = []
        for c in cands:
            if 200 < c <= self.page_size and c not in out:
                out.append(c)
        return out

    def _leaf_pages(self, limit: int) -> list[int]:
        """取一张大表的叶子页样本（优先 group_msg_table）。"""
        for table in ("group_msg_table", "c2c_msg_table"):
            try:
                root, _cols, _pk = table_schema(self.path, table)
            except Exception:  # noqa: BLE001
                continue
            leaves: list[int] = []
            self._collect_leaves(root, leaves, limit=limit)
            if leaves:
                return leaves
        return []

    def _cell_end(self, data: bytes, ptr: int, usable: int) -> int:
        """按候选 usable 估算一个 cell 在本页的结束位置（0 表示无法解析）。"""
        payload, off2 = _varint(data, ptr)
        if payload is None:
            return 0
        _rowid, off3 = _varint(data, off2)
        if _rowid is None:
            return 0
        max_local = usable - 35
        min_local = ((usable - 12) * 32 // 255) - 23
        if payload <= max_local:
            return off3 + payload
        k = min_local + ((payload - min_local) % (usable - 4))
        local = k if k <= max_local else min_local
        return off3 + local + 4

    def _max_cell_end(self, leaves: Sequence[int], usable: int) -> int:
        """样本叶子页里 cell 结束位置的最大值（用于推断 usable）。"""
        best = 0
        for number in leaves:
            data = self.page(number)
            if data is None:
                continue
            off = 100 if number == 1 else 0
            if off + 5 > len(data) or data[off] != 0x0D:
                continue
            ncell = struct.unpack(">H", data[off + 3 : off + 5])[0]
            # 损坏页的 ncell 可能被改大：先确认指针数组没有越界，
            # 否则下面的切片会悄悄返回短数据、struct.unpack 抛未包装的异常。
            if 8 + 2 * ncell > usable or off + 8 + 2 * ncell > len(data):
                continue
            for i in range(ncell):
                try:
                    ptr = struct.unpack(">H", data[off + 8 + 2 * i : off + 10 + 2 * i])[0]
                    if ptr + 4 > usable:
                        continue
                    end = self._cell_end(data, ptr, usable)
                except (struct.error, IndexError):
                    continue
                if end > best:
                    best = end
        return best

    def _detect_usable(self) -> int:
        """推断真实可用区。

        逐个候选值按该 usable 的 cell 布局规则计算样本页里 cell 的结束位置，
        取第一个「所有 cell 都落在可用区内」的候选值。
        """
        cands = self._candidate_usables()
        if len(cands) == 1:
            return cands[0]
        try:
            leaves = self._leaf_pages(limit=3000)
            if not leaves:
                return cands[0]
        except (RawTableError, struct.error, IndexError):
            # 坏库的 b-tree 结构不可信，退回最保守的候选值，别让构造对象就崩。
            return cands[0]
        for cand in sorted(cands):
            try:
                cell_end = self._max_cell_end(leaves, cand)
            except (RawTableError, struct.error, IndexError):
                continue
            if cell_end and cell_end <= cand:
                return cand
        return cands[0]

    def _collect_leaves(self, root: int, out: list[int], limit: int = 0) -> None:
        """收集 root 子树的叶子页号（按 b-tree 顺序）。"""
        stack = [root]
        visited: set[int] = set()
        while stack:
            number = stack.pop()
            if number < 1 or number in visited:
                continue
            visited.add(number)
            # 页号成环是损坏库的典型表现；给一个硬上限，避免 100% CPU 死循环。
            if len(visited) > 200000:
                raise RawTableError("b-tree 页数异常，疑似数据库损坏")
            data = self.page(number)
            if data is None:
                continue
            off = 100 if number == 1 else 0
            if off + 8 > len(data):
                continue
            kind = data[off]
            if kind == 0x0D:
                out.append(number)
                if limit and len(out) >= limit:
                    return
                continue
            if kind != 0x05:
                continue
            ncell = struct.unpack(">H", data[off + 3 : off + 5])[0]
            # 内部页：8 字节头 + 4 字节最右子页 + 2*ncell 指针数组
            if 12 + 2 * ncell > self.usable or off + 12 + 2 * ncell > len(data):
                continue
            children = []
            for i in range(ncell):
                ptr = struct.unpack(">H", data[off + 12 + 2 * i : off + 14 + 2 * i])[0]
                if ptr + 4 > self.usable or ptr + 4 > len(data):
                    continue
                children.append(struct.unpack(">I", data[ptr : ptr + 4])[0])
            children.append(struct.unpack(">I", data[off + 8 : off + 12])[0])
            stack.extend(reversed(children))

    # ── cell 解析 ────────────────────────────────────────────────────────────

    def _parse_cell(
        self, data: bytes, ptr: int, usable: int
    ) -> tuple[int, int, int, list[int], int, int | None, int]:
        """解析一个叶子 cell。

        返回 ``(payload_size, rowid, header_size, serial_types, local_size,
        overflow_page, data_start)``。

        注意：SQLite 的 payload **包含记录头**（serial type 列表），
        ``local_size`` 也是按 payload 总长算的；溢出页指针位于
        ``cell 起点 + local_size``（不是 payload 数据起点）。
        """
        payload, off2 = _varint(data, ptr)
        if payload is None:
            raise RawTableError("cell 负载长度解析失败")
        rowid, off3 = _varint(data, off2)
        if rowid is None:
            raise RawTableError("cell rowid 解析失败")
        header, hoff = _varint(data, off3)
        if header is None or header <= 0:
            raise RawTableError("记录头长度解析失败")
        types: list[int] = []
        pos = hoff
        end = off3 + header
        while pos < end:
            t, pos = _varint(data, pos)
            if t is None:
                raise RawTableError("serial type 解析失败")
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
                raise RawTableError("overflow 指针越界")
            ovfl = struct.unpack(">I", data[off3 + local : off3 + local + 4])[0]
        return payload, rowid, header, types, local, ovfl, data_start

    def _read_payload(self, data: bytes, ptr: int, usable: int) -> tuple[bytes, int, list[int]] | None:
        try:
            payload, rowid, header, types, local, ovfl, data_start = self._parse_cell(
                data, ptr, usable
            )
        except RawTableError:
            return None
        if payload < 0 or payload > _MAX_PAYLOAD:
            return None
        on_page = local - header
        if on_page < 0:
            return None
        body = bytearray(data[data_start : data_start + on_page])
        if ovfl:
            per_page = usable - 4
            remaining = payload - local
            number = ovfl
            guard = 0
            while number and remaining > 0 and guard < 200000:
                guard += 1
                page = self.page(number)
                if page is None:
                    return None
                take = min(remaining, per_page)
                body += page[4 : 4 + take]
                remaining -= take
                number = struct.unpack(">I", page[0:4])[0]
            if remaining > 0:
                return None
        return bytes(body), rowid, types

    def _decode_record(
        self, body: bytes, types: Sequence[int], rowid: int, pk_index: int | None
    ) -> list[Any]:
        values: list[Any] = []
        pos = 0
        for t in types:
            n = _serial_len(t)
            if n < 0:
                values.append(None)
                pos += 0
                continue
            chunk = body[pos : pos + n]
            pos += n
            if t == 0:
                values.append(None)
            elif t == 8:
                values.append(0)
            elif t == 9:
                values.append(1)
            elif t >= 12 and _is_text(t):
                values.append(chunk.decode(self.encoding, "replace"))
            elif t >= 12:
                values.append(chunk)
            else:
                values.append(int.from_bytes(chunk, "big", signed=True))
        if pk_index is not None:
            while len(values) <= pk_index:
                values.append(None)
            values[pk_index] = rowid
        return values

    # ── 对外接口 ──────────────────────────────────────────────────────────────

    def iter_rows(
        self,
        table: str,
        *,
        columns: Sequence[str] | None = None,
        limit: int = 0,
    ) -> Iterator[dict[str, Any]]:
        """按 b-tree 顺序遍历一张表的全部行。

        :param table: 表名
        :param columns: 只取这些列（``None`` 表示全部）
        :param limit: 最多返回多少行（0 = 不限）
        """
        root, cols, pk = table_schema(self.path, table)
        leaves: list[int] = []
        self._collect_leaves(root, leaves)
        want = None if columns is None else set(columns)
        emitted = 0
        for number in leaves:
            data = self.page(number)
            if data is None:
                continue
            off = 100 if number == 1 else 0
            if off + 5 > len(data) or data[off] != 0x0D:
                continue
            ncell = struct.unpack(">H", data[off + 3 : off + 5])[0]
            if 8 + 2 * ncell > self.usable or off + 8 + 2 * ncell > len(data):
                continue
            for i in range(ncell):
                ptr = struct.unpack(">H", data[off + 8 + 2 * i : off + 10 + 2 * i])[0]
                if ptr + 4 > self.usable or ptr + 4 > len(data):
                    continue
                try:
                    parsed = self._read_payload(data, ptr, self.usable)
                except (struct.error, IndexError):
                    parsed = None
                if parsed is None:
                    continue
                body, rowid, types = parsed
                values = self._decode_record(body, types, rowid, pk)
                row = {
                    name: (values[idx] if idx < len(values) else None)
                    for idx, name in enumerate(cols)
                }
                if want is None:
                    yield row
                else:
                    yield {name: row.get(name) for name in want}
                emitted += 1
                if limit and emitted >= limit:
                    return
