"""基准测试：rawdb 与 sqlite3 在真实 QQ 明文库上的读取速度与完整性。"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qqexport import rawdb  # noqa: E402

PLAIN = os.environ.get("QQEXPORT_PLAIN_DB", os.path.join(os.environ.get("QQEXPORT_PROBE_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "qq-export-probe")), "nt_msg_plain_r80.db"))
N = 200_000


def main() -> int:
    if not os.path.exists(PLAIN):
        print("SKIP: 明文库不存在", PLAIN)
        return 0
    with rawdb.PlainDb(PLAIN) as db:
        print("usable =", db.usable)
        t0 = time.time()
        n = 0
        for row in db.iter_rows("group_msg_table", limit=N):
            n += 1
        dt = time.time() - t0
        print(f"rawdb group: {n} 行 / {dt:.1f}s -> {n/max(dt,0.001):.0f} 行/s")
        est = 2_882_289 / max(n / max(dt, 0.001), 1)
        print(f"  预计全表耗时: {est/60:.1f} 分钟")

    conn = sqlite3.connect("file:" + PLAIN.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        t0 = time.time()
        try:
            cur = conn.execute(
                'SELECT rowid, "40030", "40050", "40013" FROM "group_msg_table" LIMIT ?', (N,)
            )
            n = 0
            for _ in cur:
                n += 1
            dt = time.time() - t0
            print(f"sqlite3 group: {n} 行 / {dt:.1f}s -> {n/max(dt,0.001):.0f} 行/s")
        except sqlite3.Error as exc:
            print("sqlite3 group 失败:", exc)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
