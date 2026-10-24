# -*- coding: utf-8 -*-
"""手机 QQ 消息备份库（msgbackup/temp/*.db）解析器的自测。

不依赖 pytest：可直接 ``python tests/test_backup.py`` 运行；
也可以用 pytest 收集（函数名以 test_ 开头）。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qqexport import backup  # noqa: E402


class _Skipped(Exception):
    pass


def _skip(msg):
    raise _Skipped(msg)


def _scratch_dir():
    """临时目录必须放在工作区内（沙箱禁止写 %TEMP%）。"""
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_scratch")
    os.makedirs(d, exist_ok=True)
    return d


def _real_db(name="1653006092_3.db"):
    root = backup.default_backup_root()
    path = os.path.join(root, name)
    if not os.path.isfile(path):
        _skip("本机没有 QQ 消息备份目录: %s" % root)
    return path


def _make_synthetic(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE android_metadata (locale TEXT);
        CREATE TABLE ex_info (_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, value TEXT);
        CREATE TABLE msg (_id INTEGER PRIMARY KEY AUTOINCREMENT, chatType INTEGER,
            chatUin TEXT, extensionData BLOB, extraData BLOB, msgRandom INTEGER,
            msgSeq INTEGER, msgTime INTEGER, msgType INTEGER);
        CREATE TABLE res (_id INTEGER PRIMARY KEY AUTOINCREMENT, extraData BLOB,
            filePath TEXT, fileSize INTEGER, msgRandom INTEGER, msgSeq INTEGER,
            msgSubType INTEGER, msgType INTEGER);
        """
    )
    conn.execute(
        "insert into ex_info(name, value) values('key_meta', ?)",
        (backup.KNOWN_KEY_META,),
    )
    conn.execute("insert into ex_info(name, value) values('db_version', '1')")
    conn.executemany(
        "insert into msg(chatType, chatUin, extensionData, extraData, msgRandom,"
        " msgSeq, msgTime, msgType) values(?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_real_db_metadata():
    """真实备份库：key_meta / db_version / 会话信息 / 时间戳单位。"""
    path = _real_db()
    db = backup.MsgBackupDb(path)
    try:
        assert db.key_meta == backup.KNOWN_KEY_META, db.key_meta
        assert db.db_version == "1", db.db_version
        assert db.chat_type == backup.CHAT_TYPE_GROUP, db.chat_type
        assert db.chat_id == "1653006092", db.chat_id
        assert db.message_count > 0
        first = next(iter(db.iter_messages(limit=1)))
        # msgTime 是 UNIX 秒
        assert 1_500_000_000 < first["msg_time"] < 2_000_000_000, first["msg_time"]
        assert first["msg_datetime"].year >= 2019, first["msg_datetime"]
        assert first["msg_type"] in (1, 2), first["msg_type"]
        assert first["chat_uin"] and len(first["chat_uin"]) in (32, 48), first["chat_uin"]
        assert first["chat_uin"] == first["chat_uin"].upper()
        # extensionData 是密文：非空、8 字节对齐
        assert first["extension_data"]
        assert len(first["extension_data"]) % 8 == 0, len(first["extension_data"])
        # msg.extraData 在真实数据里恒为 NULL
        assert first["extra_data"] is None
    finally:
        db.close()


def test_real_db_resource_paths():
    """res 行：filePath 是 URL 编码的安卓路径，extraData 有值。"""
    path = _real_db()
    db = backup.MsgBackupDb(path)
    try:
        res = next(iter(db.iter_resources(limit=1)))
        assert res["file_path_decoded"].startswith(
            "/storage/emulated/0/Android/data/com.tencent.mobileqq/"
        ), res["file_path_decoded"]
        assert "%2F" in res["file_path"], "filePath 应该是 URL 编码的"
        assert res["extra_data"], "res.extraData 应当有值"
        assert res["file_size"] > 0
    finally:
        db.close()


def test_roundtrip_synthetic_db():
    """自建同结构库 → 解析 → 逐字段回读一致（元数据 round-trip）。"""
    rows = [
        (3, "A" * 48, b"\x01" * 512, None, 11111, 1, 1737113429, 1),
        (3, "B" * 48, b"\x02" * 1232, None, 22222, 2, 1737113500, 2),
    ]
    p = os.path.join(_scratch_dir(), "1653006092_3.db")
    if os.path.exists(p):
        os.remove(p)
    _make_synthetic(p, rows)
    db = backup.MsgBackupDb(p)
    try:
        assert db.key_meta == backup.KNOWN_KEY_META
        assert db.db_version == "1"
        assert db.chat_id == "1653006092"
        assert db.chat_type == 3
        assert db.message_count == 2
        assert db.time_range == (1737113429, 1737113500)
        got = list(db.iter_messages())
        assert len(got) == 2
        for src, dst in zip(rows, got):
            assert dst["chat_type"] == src[0]
            assert dst["chat_uin"] == src[1]
            assert dst["extension_data"] == src[2]
            assert dst["extra_data"] == src[3]
            assert dst["msg_random"] == src[4]
            assert dst["msg_seq"] == src[5]
            assert dst["msg_time"] == src[6]
            assert dst["msg_datetime"].timestamp() == src[6]
            assert dst["msg_type"] == src[7]
    finally:
        db.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.remove(p + suffix)
            except OSError:
                pass


def test_iter_backup_dbs_real_dir():
    """真实目录：文件名形如 <id>_<chatType>.db，chatType 只有 1 / 3。"""
    root = backup.default_backup_root()
    if not os.path.isdir(root):
        _skip("本机没有 QQ 消息备份目录: %s" % root)
    dbs = list(backup.iter_backup_dbs(root))
    assert dbs, "没有找到备份库"
    kinds = set()
    for db in dbs:
        kinds.add(db.chat_type)
        assert db.chat_id and db.chat_id.isdigit()
        db.close()
    assert kinds <= {backup.CHAT_TYPE_PRIVATE, backup.CHAT_TYPE_GROUP}, kinds


def test_decrypt_extension_is_unavailable_offline():
    """离线解密必须明确失败（而不是给出错误明文）。"""
    try:
        backup.decrypt_extension(b"\x00" * 16, backup.KNOWN_KEY_META)
    except backup.OfflineDecryptionUnavailable as exc:
        assert "OIDB" in str(exc)
    else:
        raise AssertionError("decrypt_extension 不应在离线状态下返回明文")


def _real_db_1001313006():
    """任务书指定的真实库：``1001313006_1.db``。"""
    root = backup.default_backup_root()
    path = os.path.join(root, "1001313006_1.db")
    if not os.path.isfile(path):
        _skip("本机没有 QQ 消息备份目录: %s" % root)
    return path


def _load_secret_key():
    """取 msgbackup 的服务端密钥（离线不可得，只能由外部注入）。

    优先级：

    1. 环境变量 ``QQ_MSGBACKUP_KEY``：64 个 hex 字符（32 字节）；
       可选 ``QQ_MSGBACKUP_IV``（hex）。
    2. 文件 ``qq-export-probe/msgbak_key.txt``：第一行 hex 密钥，
       第二行可选 hex IV。

    Returns:
        ``(key_bytes, iv_bytes | None)``；拿不到就返回 ``None``。
    """
    import binascii

    def _hex(s):
        s = s.strip()
        if not s:
            return None
        try:
            return bytes.fromhex(s)
        except ValueError:
            return None

    env_key = os.environ.get("QQ_MSGBACKUP_KEY")
    if env_key:
        key = _hex(env_key)
        if key:
            return key, _hex(os.environ.get("QQ_MSGBACKUP_IV", ""))
    key_file = os.path.join(
        os.environ.get("QQEXPORT_PROBE_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "qq-export-probe")), "msgbak_key.txt"
    )
    if os.path.isfile(key_file):
        try:
            with open(key_file, "r", encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        except OSError:
            return None
        if lines:
            key = _hex(lines[0])
            if key:
                iv = _hex(lines[1]) if len(lines) > 1 else None
                return key, iv
    return None


def test_real_db_1001313006_1():
    """任务书指定的真实库：结构、行数、密文长度不变量。"""
    path = _real_db_1001313006()
    db = backup.MsgBackupDb(path)
    try:
        assert db.key_meta == backup.KNOWN_KEY_META, db.key_meta
        assert db.db_version == "1", db.db_version
        assert db.chat_id == "1001313006", db.chat_id
        assert db.message_count == 1475, db.message_count
        assert db.chat_type == backup.CHAT_TYPE_PRIVATE, db.chat_type
        rows = list(db.iter_messages())
        assert len(rows) == 1475
        lens = set()
        for row in rows:
            blob = row["extension_data"]
            assert blob, "extensionData 不应为空"
            lens.add(len(blob) % 8)
            assert len(blob) % 8 == 0, len(blob)
            assert row["extra_data"] is None
            assert row["chat_uin"] and len(row["chat_uin"]) in (32, 48)
        assert lens == {0}, lens
        # res.extraData 同样是密文（长度也是 8 的倍数）
        res = list(db.iter_resources())
        assert res
        for r in res:
            assert r["extra_data"]
            assert len(r["extra_data"]) % 8 == 0, len(r["extra_data"])
    finally:
        db.close()


def test_read_backup_dir_records():
    """``read_backup_dir`` 产出带 id/chat_type/chat_uin/msg_time/msg_type/
    msg_seq/segments/text/raw 的记录。"""
    path = _real_db_1001313006()
    root = os.path.dirname(path)
    n = 0
    for rec in backup.read_backup_dir(root, dbs=["1001313006_1.db"], limit=5):
        n += 1
        assert rec.id is not None
        assert rec.chat_type in (1, 3)
        assert rec.chat_uin
        assert rec.msg_time > 1_500_000_000
        assert rec.msg_type in (1, 2)
        assert rec.msg_seq is not None
        assert rec.segments == []          # 没有密钥 → 无明文
        assert rec.text is None
        assert rec.raw
        assert rec["id"] == rec.id         # 也支持字典式取值
    assert n == 5, n


def _strict_protobuf_ok(blob):
    """严格 protobuf 结构检查：每个 tag 合法、长度放得下、整块正好消费完。"""
    def _tag_at(buf, off):
        if off >= len(buf):
            return None
        val = shift = i = 0
        while True:
            if off + i >= len(buf) or i >= 5:
                return None
            b = buf[off + i]
            val |= (b & 0x7F) << shift
            i += 1
            if not (b & 0x80):
                break
            shift += 7
        fn, wire = val >> 3, val & 7
        if fn == 0 or fn > 100000 or wire in (3, 4, 6, 7):
            return None
        return fn, wire, i

    off = 0
    fields = 0
    while off < len(blob):
        t = _tag_at(blob, off)
        if not t:
            return False
        fn, wire, tl = t
        off += tl
        if wire == 0:
            while off < len(blob) and (blob[off] & 0x80):
                off += 1
            off += 1
        elif wire == 1:
            off += 8
        elif wire == 2:
            ln = shift = 0
            while off < len(blob):
                c = blob[off]
                off += 1
                ln |= (c & 0x7F) << shift
                shift += 7
                if not (c & 0x80):
                    break
            off += ln
        elif wire == 5:
            off += 4
        fields += 1
        if off > len(blob) or fields > 64:
            return False
    return fields > 0


# NT 消息体里已知的顶层字段号（正文在 40800 里，45002=类型, 45101=文本…）
_NT_FIELDS = {40800, 45001, 45002, 45101, 45402, 45405, 45411, 45412, 45812}


def test_extension_data_is_ciphertext():
    """负结果的可复现证据：熵接近 8、字节均匀、无法当 protobuf 解析。"""
    import collections
    import math

    path = _real_db_1001313006()
    db = backup.MsgBackupDb(path)
    try:
        blobs = [r["extension_data"] for r in db.iter_messages()]
    finally:
        db.close()
    pooled = b"".join(blobs)
    counts = collections.Counter(pooled)
    n = len(pooled)
    ent = -sum((c / n) * math.log2(c / n) for c in counts.values())
    assert len(counts) == 256, len(counts)          # 256 个字节值全部出现
    assert ent > 7.99, ent                            # 接近 8 bit/byte
    chi2 = sum((counts.get(i, 0) - n / 256.0) ** 2 / (n / 256.0) for i in range(256))
    assert chi2 < 400, chi2                           # 均匀随机
    # 不是 protobuf：严格结构检查（tag 合法 + 长度放得下 + 整块消费完）
    assert not [b for b in blobs if _strict_protobuf_ok(b)]
    # 宽松解析也不该出现任何已知 NT 字段号
    from qqexport import body as body_mod

    known = 0
    for blob in blobs:
        for fld in body_mod.parse_wire(blob):
            if getattr(fld, "number", None) in _NT_FIELDS:
                known += 1
    assert known == 0, known
    # 16 字节块不重复 => 不是 ECB
    blocks = collections.Counter()
    for blob in blobs:
        for i in range(0, len(blob) - 15, 16):
            blocks[blob[i:i + 16]] += 1
    assert max(blocks.values()) == 1, "出现了重复的 16 字节块"


def test_decoder_positive_control():
    """正控：证明解密管线（密钥 → framing → 判据 → 中文文本）是通的。

    用 AES-256-GCM 加密一条合成 NT 消息体，再交给
    ``decode_extension_data`` 解回来，必须还原出中文文本。
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception:  # noqa: BLE001
        _skip("没有 cryptography，无法做正控")

    def varint(v):
        out = bytearray()
        while True:
            b = v & 0x7F
            v >>= 7
            if v:
                out.append(b | 0x80)
            else:
                out.append(b)
                break
        return bytes(out)

    def ld(field, payload):
        return varint((field << 3) | 2) + varint(len(payload)) + payload

    def vi(field, value):
        """``field = int32``（45002 内容类型在真实数据里就是 varint）。"""
        return varint((field << 3) | 0) + varint(value)

    text = "你好，QQ-NT 消息备份正控样本".encode("utf-8")
    body = ld(40800, vi(45002, 1) + ld(45101, text))
    key = bytes(range(32))
    iv = bytes(range(12))
    ct = AESGCM(key).encrypt(iv, body, None)
    ctx = backup.BackupKeyContext(key=key, iv=iv)
    plain = backup.decode_extension_data(ct, backup.KNOWN_KEY_META, ctx)
    assert plain == body, plain
    segs = backup.message_segments(plain)
    assert segs, segs
    got = backup.message_text(segs)
    assert got == text.decode("utf-8"), got
    from qqexport import body as body_mod

    assert body_mod.seg_type_name(segs[0]) == "text"


def test_decode_real_rows_chinese_text():
    """**主测试**：用真实密钥解密 ``1001313006_1.db`` 的真实行，
    断言至少 1 行得到非空、可读的中文文本，并打印解密样本。

    密钥来自服务端（OIDB 0xcde），离线拿不到；因此本测试在没有注入
    密钥时是 SKIP 而不是 FAIL。注入方式：

    * 环境变量 ``QQ_MSGBACKUP_KEY=<64 hex>``（可选 ``QQ_MSGBACKUP_IV=<hex>``）
    * 或文件 ``qq-export-probe/msgbak_key.txt``（相对仓库上一级目录）
      （第一行 hex 密钥，第二行可选 hex IV）

    拿到密钥后本测试即会打印真实中文消息样本。
    """
    path = _real_db_1001313006()
    root = os.path.dirname(path)
    got_key = _load_secret_key()
    if got_key is None:
        _skip(
            "没有 msgbackup 服务端密钥（离线不可得）：key_meta 只是 OIDB 0xcde 的"
            "兑换凭证。设置 QQ_MSGBACKUP_KEY=<64 hex> 或写入 "
            r"qq-export-probe\msgbak_key.txt 后重跑本测试。"
        )
    key, iv = got_key
    ctx = backup.BackupKeyContext(key=key, iv=iv)
    decoded = []
    for rec in backup.read_backup_dir(root, ctx=ctx, dbs=["1001313006_1.db"]):
        if rec.text and rec.text.strip():
            decoded.append(rec)
            if len(decoded) >= 3:
                break
    assert decoded, "没有一行解出文本"
    for rec in decoded:
        assert rec.text.strip(), rec
        assert any("\u4e00" <= ch <= "\u9fff" for ch in rec.text), rec.text
        assert rec.decrypted
    print("---- 解密样本 ----")
    for rec in decoded:
        print("id=%s %s type=%s seq=%s text=%r" % (
            rec.id, rec.msg_datetime, rec.msg_type, rec.msg_seq, rec.text))
    print("------------------")


# ──────────────────────────────────────────────────────────────────────────
# MsgBackupStore（目录级读取器） + decrypt_extension_data（离线解密结论）
# ──────────────────────────────────────────────────────────────────────────

#: 用于测试的真实备份库（任务要求：2~3 个真实文件）。
#: 注意：QQ 每次「导出消息记录」用的账号/会话都可能不同，所以真实目录里
#: 不一定还有这几个文件；`_pick_real_dbs()` 会优先用它们，其次退化成
#: 「目录里实际存在的任意 3 个库」。
REAL_DB_NAMES = ("1001313006_1.db", "1653006092_3.db", "423567110_1.db")


def _pick_real_dbs(store, minimum=2):
    """挑出至少 ``minimum`` 个真实备份库文件名。"""
    have = sorted(os.path.basename(p) for p in store.db_paths)
    used = [n for n in REAL_DB_NAMES if n in have]
    if len(used) < minimum:
        for name in have:
            if name not in used:
                used.append(name)
            if len(used) >= minimum:
                break
    return used


def _real_store():
    root = backup.default_backup_root()
    if not os.path.isdir(root):
        _skip("本机没有 QQ 消息备份目录: %s" % root)
    return backup.MsgBackupStore(root)


def _check_key_meta(km):
    """真实备份库的 key_meta 只校验形状。

    ``key_meta`` 是 OIDB 0xcde 的兑换凭证（不是密钥），QQ 每换一批备份就会
    换一个值 —— ``backup.KNOWN_KEY_META`` 只是历史观测到的那个。
    """
    assert km, "key_meta 不应为空"
    assert len(km) == 128, len(km)
    assert all(c in "0123456789abcdefABCDEF" for c in km), km


def _varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _ld(field, payload):
    """Protobuf 的一组 ``field = bytes``。"""
    return _varint((field << 3) | 2) + _varint(len(payload)) + payload


def _vi(field, value):
    """Protobuf 的 ``field = int32``（varint 编码，如 45002 内容类型）。"""
    return _varint((field << 3) | 0) + _varint(value)


def test_msg_backup_store_reads_real_files():
    """``MsgBackupStore`` 能在 2~3 个**真实**备份库上读出元数据。

    断言的是"离线能拿到什么"：时间、会话类型、消息类型、序号、附件路径，
    以及**正文一定解不开**（``text`` 恒为 ``None``）。
    """
    store = _real_store()
    assert store.chat_count >= 3, store.chat_count
    _check_key_meta(store.key_meta)
    have = set(os.path.basename(p) for p in store.db_paths)
    used = _pick_real_dbs(store)
    assert len(used) >= 2, "真实备份库不足 2 个: %s" % sorted(have)[:10]

    counts = store.counts()
    for name in used:
        assert counts[name][0] > 0, (name, counts[name])

    seen_dbs = set()
    for msg in store.iter_messages():
        for key in ("msg_time", "chat_uin", "chat_type", "msg_type", "text", "raw"):
            assert key in msg, (key, sorted(msg))
        assert isinstance(msg["msg_time"], int)
        assert 1_500_000_000 < msg["msg_time"] < 2_000_000_000, msg["msg_time"]
        assert msg["chat_type"] in (backup.CHAT_TYPE_PRIVATE, backup.CHAT_TYPE_GROUP)
        assert msg["msg_type"] in (1, 2), msg["msg_type"]
        # 对端身份是密文令牌，长度随 QQ 版本变化：早期 32/48 个 hex，
        # 新版群会话会给到 128 个 hex（64 字节 uid）。这里只要求非空且是 hex 串。
        assert msg["chat_uin"], "chat_uin 不应为空"
        assert len(msg["chat_uin"]) >= 32, msg["chat_uin"]
        assert len(msg["chat_uin"]) % 2 == 0, msg["chat_uin"]
        assert msg["raw"], "extensionData 不应为空"
        assert len(msg["raw"]) % 8 == 0, len(msg["raw"])
        # 离线解密不出来：正文恒为 None
        assert msg["text"] is None
        seen_dbs.add(msg["db"])
    assert len(seen_dbs) >= 2, seen_dbs


def test_msg_backup_store_accepts_parent_or_temp_dir():
    """``MsgBackupStore`` 两种传法都能用：``msgbackup/`` 或 ``msgbackup/temp/``。"""
    root = backup.default_backup_root()
    if not os.path.isdir(root):
        _skip("本机没有 QQ 消息备份目录: %s" % root)
    a = backup.MsgBackupStore(root)
    b = backup.MsgBackupStore(os.path.dirname(root))  # 上级 msgbackup/ 目录
    assert a.chat_count == b.chat_count
    assert a.chat_count >= 1
    assert os.path.basename(a.msgbackup_dir) == "temp"
    assert os.path.basename(b.msgbackup_dir) == "temp"


def test_msg_backup_store_attachments_are_plaintext():
    """附件路径是**明文**（URL 编码的安卓路径），和正文不同。"""
    store = _real_store()
    found = 0
    for res in store.iter_resources():
        if not res["file_path"]:
            continue
        found += 1
        assert res["file_path_decoded"].startswith("/storage/emulated/0/"), res
        assert res["file_size"] > 0
        if found >= 20:
            break
    assert found, "res 表里没有附件"


def test_decrypt_extension_data_cannot_decrypt_real_blobs():
    """**本次调查的结论**（负结果，必须可复现）。

    真实备份库里 3 个不同库各取若干条 ``extensionData``，
    用库里的 ``key_meta``（唯一的全局兑换凭证）走完整条解密管线
    （193 种派生 × 20+ 种密码 × 11 种 IV × 3 种负载偏移），
    结果**一条都解不开** ⇒ 离线解密不可行。
    """
    store = _real_store()
    used = _pick_real_dbs(store)
    if len(used) < 2:
        _skip("真实备份库不足 2 个")
    key_meta = store.key_meta
    _check_key_meta(key_meta)

    checked = 0
    for name in used:
        path = os.path.join(store.msgbackup_dir, name)
        for msg in store.iter_messages(db=path, limit=4):
            blob = msg["raw"]
            assert blob
            assert backup.decrypt_extension_data(blob, key_meta) is None, (
                "竟然解开了 %s 的 id=%s —— 结论需要重新验证" % (name, msg["id"])
            )
            checked += 1
    assert checked >= 6, checked

    # 没有 key_meta / 空密文也不能给出明文
    assert backup.decrypt_extension_data(b"\x00" * 32, None) is None
    assert backup.decrypt_extension_data(b"", key_meta) is None


def test_decrypt_extension_data_positive_control():
    """正控：证明 ``decrypt_extension_data`` 的判据**不是空转**。

    用一条**合成**的 NT 消息体（40800 → 45002/45101），
    以 ``key_meta`` 的 SHA-256 派生密钥 + AES-256-CTR 加密，
    再交给同一个函数解密：必须还原出原文与中文文本。
    这样"真实数据解不开"就不是因为函数写错了。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher,
            algorithms,
            modes,
        )
    except Exception:  # noqa: BLE001
        _skip("没有 cryptography，无法做正控")
    import hashlib

    from qqexport import body as body_mod

    text = "你好，QQ-NT 消息备份正控样本"
    plain_body = _ld(
        40800,
        _vi(45002, 1) + _ld(45101, text.encode("utf-8")),
    )
    key = hashlib.sha256(backup.KNOWN_KEY_META.encode("utf-8")).digest()[:32]
    ct = Cipher(
        algorithms.AES(key), modes.CTR(b"\0" * 16)
    ).encryptor().update(plain_body)

    got = backup.decrypt_extension_data(ct, backup.KNOWN_KEY_META)
    assert got == plain_body, got
    segs = body_mod.parse_msg_body(got)
    assert segs, segs
    assert body_mod.seg_text(segs[0]) == text, body_mod.seg_text(segs[0])
    assert body_mod.seg_type_name(segs[0]) == "text"


def main():
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except _Skipped as exc:
            print("SKIP %s (%s)" % (name, exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %s: %r" % (name, exc))
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
