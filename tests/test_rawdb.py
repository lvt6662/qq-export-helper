"""交叉验证 rawdb.PlainDb 与 sqlite3 读同一张表的结果是否一致。

QQ 明文库头里的 reserved 字段是 48，但数据实际按 usable=page_size-80 排布，
sqlite3 因此读不了群消息表。这里用 c2c_msg_table（改 byte20=80 后 sqlite3 能读）
做逐行比对，验证自写 b-tree 解析器的正确性。
"""

from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qqexport import rawdb  # noqa: E402

PLAIN = os.environ.get("QQEXPORT_PLAIN_DB", os.path.join(os.environ.get("QQEXPORT_PROBE_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "qq-export-probe")), "nt_msg_plain_r80.db"))


def _patch_reserved(path: str, value: int, old: int) -> None:
    with open(path, "r+b") as fh:
        fh.seek(20)
        cur = fh.read(1)[0]
        if cur != old:
            fh.seek(20)
            fh.write(bytes([value]))
            fh.flush()


def main() -> int:
    if not os.path.exists(PLAIN):
        print("SKIP: 明文库不存在", PLAIN)
        return 0
    with open(PLAIN, "rb") as fh:
        fh.seek(20)
        orig = fh.read(1)[0]
    print("原 reserved =", orig)
    _patch_reserved(PLAIN, 80, orig)

    conn = sqlite3.connect("file:" + PLAIN.replace("\\", "/") + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for table in ("c2c_msg_table", "group_msg_table"):
            print("=" * 60)
            print("表:", table)
            try:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            except sqlite3.Error as exc:
                print("  sqlite3 PRAGMA 失败:", exc)
                cols = []
            print("  列数:", len(cols), cols[:14], "...")

            sql_rows = []
            try:
                sql_rows = conn.execute(
                    f'SELECT rowid, * FROM "{table}" ORDER BY rowid LIMIT 40'
                ).fetchall()
            except sqlite3.Error as exc:
                print("  sqlite3 读取失败:", exc)

            raw_rows = []
            try:
                with rawdb.PlainDb(PLAIN) as db:
                    print("  rawdb usable =", db.usable, " reserved =", db.reserved)
                    for i, row in enumerate(db.iter_rows(table)):
                        raw_rows.append(row)
                        if i >= 39:
                            break
            except Exception as exc:  # noqa: BLE001
                print("  rawdb 读取失败:", type(exc).__name__, exc)

            print(f"  sqlite3 行数={len(sql_rows)} rawdb 行数={len(raw_rows)}")
            if not sql_rows or not raw_rows:
                continue
            bad = 0
            for srow, rrow in zip(sql_rows, raw_rows):
                for name in cols:
                    sv = srow[name]
                    rv = rrow.get(name)
                    if isinstance(sv, str) and isinstance(rv, bytes):
                        rv = rv.decode("utf-8", "replace")
                    if sv != rv:
                        bad += 1
                        if bad <= 5:
                            print(f"    差异 列={name} sqlite3={sv!r} rawdb={rv!r}")
                        break
            print("  逐行逐列差异条数:", bad)
    finally:
        conn.close()
        _patch_reserved(PLAIN, orig, 80)
        print("已恢复 reserved =", orig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
