"""回归测试：PlainDb 不显式传 usable 时也能完成可用区推断。

历史 bug：``__init__`` 里 ``self.usable = usable if usable else self._detect_usable()``
在赋值前就调用探测函数，而 ``_collect_leaves`` / ``_max_cell_end`` 的边界检查会读取
``self.usable``，于是任何含 ``group_msg_table`` / ``c2c_msg_table`` 且 b-tree 有内部页
的真实明文库都会抛 ``AttributeError``，群表恢复链路整体失效。这里用合成库覆盖该路径。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qqexport import rawdb  # noqa: E402

SCRATCH = os.path.join(os.path.dirname(__file__), "_scratch")
ROWS = 3000


def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        # 标准 SQLite 布局：usable = page_size = 4096，用于验证推断不会退化成
        # QQ 的 4016（旧实现把 cell 结束位置算大了，会错误回退到 4016 而丢行）。
        conn.execute("PRAGMA page_size=4096")
        conn.execute(
            'CREATE TABLE "group_msg_table" '
            '("40001" INTEGER PRIMARY KEY, "40030" TEXT, "40050" BLOB)'
        )
        blob = b"x" * 300
        conn.executemany(
            'INSERT INTO "group_msg_table" ("40001", "40030", "40050") VALUES (?, ?, ?)',
            [(i, "12345", blob) for i in range(1, ROWS + 1)],
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    os.makedirs(SCRATCH, exist_ok=True)
    path = os.path.join(SCRATCH, "rawdb_usable_" + uuid.uuid4().hex[:10] + ".db")
    try:
        _make_db(path)
        # 关键断言：不传 usable 也必须能构造成功（旧代码在此抛 AttributeError）。
        with rawdb.PlainDb(path) as db:
            assert db.usable == 4096, db.usable
            rows = list(db.iter_rows("group_msg_table"))
            assert len(rows) == ROWS, len(rows)
            assert rows[0].get("40001") == 1, rows[0]
            assert rows[0].get("40030") == "12345", rows[0]
            assert rows[-1].get("40001") == ROWS, rows[-1]
        print("PASS test_rawdb_usable")
        return 0
    except Exception as exc:  # noqa: BLE001
        print("FAIL test_rawdb_usable:", type(exc).__name__, exc)
        return 1
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
