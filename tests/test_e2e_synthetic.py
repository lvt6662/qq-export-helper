# -*- coding: utf-8 -*-
"""端到端合成回归：加密的 nt_msg.db → 剥头 → 解密 → 读会话 → 解析消息。

真实 ``nt_msg.db`` 的运行时口令目前拿不到（需要从 QQ 进程里提取），
所以这里用 ``sqlcipher3`` 造一个「同格式」的加密库来跑通整条链路：

* 与 QQ NT 相同的 SQLCipher 参数与 PRAGMA 顺序
  （``cipher_page_size → key → kdf_iter → cipher_hmac_algorithm → cipher_kdf_algorithm``）
* 前面拼 1024 字节的 NTQQ 私有头
* 与真实库同构的 ``c2c_msg_table`` / ``group_msg_table``
* 40800 消息体是真的 Protobuf 字节（复用 ``make_fixture`` 的编码器）

这样就能验证 decrypt.py → proto.py → db.py 三层的对接是否正确。

直接运行：``python tests\\test_e2e_synthetic.py``
"""

from __future__ import annotations

import os
import sqlite3
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, _HERE)

import make_fixture as FIX  # noqa: E402
from qqexport import db, decrypt  # noqa: E402
from test_decrypt import _prepend_qq_header, _scratch, _sqlcipher3  # noqa: E402

KEY = "SYNTHETIC_KEY_16"


def _build_encrypted_msg_db(path: str, key: str) -> str:
    """造一个「加密版 nt_msg.db」：结构与 make_fixture 一致，只是被 SQLCipher 加密了。"""
    mod = _sqlcipher3()
    conn = mod.connect(path, isolation_level=None)
    try:
        # 顺序必须与官方 1.decrypt.py 一致，否则派生出的密钥对不上
        conn.execute("PRAGMA cipher_page_size = 4096;")
        conn.execute("PRAGMA key = '%s';" % key.replace("'", "''"))
        conn.execute("PRAGMA kdf_iter = 4000;")
        conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA1;")
        conn.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512;")
        conn.execute(FIX._C2C_DDL)
        conn.execute(FIX._GROUP_DDL)
        conn.executemany(
            "INSERT INTO c2c_msg_table (%s) VALUES (%s)"
            % (FIX._C2C_COLS, FIX._ph(12)),
            FIX._c2c_rows(),
        )
        conn.executemany(
            "INSERT INTO group_msg_table (%s) VALUES (%s)"
            % (FIX._GROUP_COLS, FIX._ph(13)),
            FIX._group_rows(),
        )
    finally:
        conn.close()
    return path


def _dump(path: str) -> dict:
    """把库里的消息行导成可比较的字典。"""
    conn = sqlite3.connect(path)
    try:
        out = {}
        for table in ("c2c_msg_table", "group_msg_table"):
            rows = conn.execute(
                'SELECT "40001", "40011", "40013", "40030", "40033", "40050", "40800" '
                'FROM %s ORDER BY "40001";' % table
            ).fetchall()
            out[table] = rows
        return out
    finally:
        conn.close()


def test_decrypt_is_lossless():
    """解密后的明文库与原始明文库逐行一致（含 40800 二进制体）。"""
    with _scratch() as td:
        plain_src = FIX.make_fixture(os.path.join(td, "src.db"))
        enc = _build_encrypted_msg_db(os.path.join(td, "enc.db"), KEY)
        full = _prepend_qq_header(enc, os.path.join(td, "nt_msg.db"))

        out = decrypt.decrypt_database(
            full, KEY, os.path.join(td, "plain.db"), progress=lambda _m: None
        )
        assert os.path.isfile(out)
        assert _dump(out) == _dump(plain_src)


def test_read_chats_from_decrypted_db():
    """解密后的库能被 db.py 正确解析成会话与消息。"""
    with _scratch() as td:
        enc = _build_encrypted_msg_db(os.path.join(td, "enc.db"), KEY)
        full = _prepend_qq_header(enc, os.path.join(td, "nt_msg.db"))
        out = decrypt.decrypt_database(
            full, KEY, os.path.join(td, "plain.db"), progress=lambda _m: None
        )
        chats = db.read_chats(out, my_uin=FIX.MY_UIN)
        by_id = {c.chat_id: c for c in chats}
        assert set(by_id) == {"10001", "10002", "88012345"}, sorted(by_id)

        # ── 单聊 10001 ──
        c = by_id["10001"]
        assert not c.is_group
        assert c.name == "张小明", c.name
        assert len(c.messages) == 9, len(c.messages)
        m0 = c.messages[0]
        assert m0.text == "你好，世界", m0.text
        assert m0.is_mine is True
        assert m0.direction == 1
        # 对方发的图片 → 附件 kind=image
        m1 = c.messages[1]
        assert m1.is_mine is False
        assert m1.direction == 0
        assert m1.attachments and m1.attachments[0].kind == "image", m1.attachments
        assert m1.attachments[0].name == "photo.jpg"
        # 我发的文件 → kind=file
        m3 = c.messages[3]
        assert m3.attachments and m3.attachments[0].kind == "file"
        assert m3.attachments[0].name == "需求说明.docx"
        assert m3.attachments[0].size == 51200
        # 我发的语音 → kind=audio（与 writers 的 ATTACHMENT_KIND_LABELS 对齐）
        m4 = c.messages[4]
        assert m4.attachments and m4.attachments[0].kind == "audio", m4.attachments
        # 对方发的回复
        m5 = c.messages[5]
        assert "回复" in m5.content, m5.content
        # 对方发的视频
        m6 = c.messages[6]
        assert m6.attachments and m6.attachments[0].kind == "video", m6.attachments
        # 系统消息：40013=3 → 不是我发的
        m8 = c.messages[8]
        assert m8.is_mine is False
        assert "系统消息" in m8.content, m8.content

        # ── 群聊 88012345 ──
        g = by_id["88012345"]
        assert g.is_group is True
        assert len(g.messages) == 7, len(g.messages)
        # 群名片优先
        assert g.messages[0].sender == "王强(研发小王)", g.messages[0].sender
        # 群里的文件
        gm = g.messages[2]
        assert gm.attachments and gm.attachments[0].name == "会议纪要.docx"
        # 群里的表情
        assert g.messages[5].attachments[0].kind == "sticker"
        # 通话记录
        assert "通话" in g.messages[6].content, g.messages[6].content

        # ── 另一个单聊 10002 ──
        assert by_id["10002"].name == "李婷"
        assert len(by_id["10002"].messages) == 1


def test_read_chats_one_stop_with_key():
    """``db.read_chats(加密库, key=...)`` 一步到位：内部自动解密再读。"""
    with _scratch() as td:
        enc = _build_encrypted_msg_db(os.path.join(td, "enc.db"), KEY)
        full = _prepend_qq_header(enc, os.path.join(td, "nt_msg.db"))
        chats = db.read_chats(full, key=KEY, my_uin=FIX.MY_UIN, reuse=False)
        assert len(chats) == 3, [c.chat_id for c in chats]
        total = sum(len(c.messages) for c in chats)
        assert total == 9 + 1 + 7, total
        # 明文库里不应该有 40030=0 的系统消息被当成会话
        assert all(c.chat_id != "0" for c in chats)


def test_read_chat_by_id():
    """``db.read_chat`` 按会话 id 取单个会话。"""
    with _scratch() as td:
        enc = _build_encrypted_msg_db(os.path.join(td, "enc.db"), KEY)
        full = _prepend_qq_header(enc, os.path.join(td, "nt_msg.db"))
        out = decrypt.decrypt_database(
            full, KEY, os.path.join(td, "plain.db"), progress=lambda _m: None
        )
        c = db.read_chat(out, "10001", my_uin=FIX.MY_UIN)
        assert c is not None and c.chat_id == "10001"
        assert len(c.messages) == 9
        assert db.read_chat(out, "不存在", my_uin=FIX.MY_UIN) is None


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
    print(f"all e2e tests passed ({len(fns)} 个)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
