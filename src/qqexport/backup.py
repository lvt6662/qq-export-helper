# -*- coding: utf-8 -*-
r"""手机 QQ 消息备份库（`nt_data/msgbackup/temp/*.db`）的只读解析器。

事实依据（本仓库 `qq-export-probe/` 下的取证脚本，见报告）
----------------------------------------------------------------
这些库是 **手机端** QQ 导出的消息备份，PC 端 QQ 在导入时把它们解压到
`nt_data/msgbackup/temp/` 再读入 `nt_msg.db`。它们是 **未加密的 SQLite**，
但消息正文 `msg.extensionData` / `res.extraData` 是 **密文**。

库结构（445 个文件全部一致）::

    android_metadata(locale TEXT)
    ex_info(_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, value TEXT)
        name='key_meta'   value = 128 个十六进制字符（= 64 字节 ASCII hex）
        name='db_version' value = '1'
    msg(_id INTEGER PRIMARY KEY AUTOINCREMENT, chatType INTEGER, chatUin TEXT,
        extensionData BLOB, extraData BLOB, msgRandom INTEGER, msgSeq INTEGER,
        msgTime INTEGER, msgType INTEGER)
    res(_id INTEGER PRIMARY KEY AUTOINCREMENT, extraData BLOB, filePath TEXT,
        fileSize INTEGER, msgRandom INTEGER, msgSeq INTEGER, msgSubType INTEGER,
        msgType INTEGER)

字段语义（已用真实数据验证）

* ``msgTime``   —— UNIX **秒**。
* `msgRandom` —— 64 位随机数（实测 0 .. 9223308472496285369），每行唯一。
* ``msgSeq``    —— 该会话内的递增序号（同一文件里可能重复/有空洞）。
* ``msgType``   —— 1 = 普通短消息（extensionData 平均 ~490 字节），
                   2 = 富文本/带媒体的消息（平均 ~1300 字节；
                   **所有**带 `res` 附件行的 `msg` 行都是 2）。
* ``chatType``  —— 备份导出的编号：1 = 单聊，3 = 群聊
                   （注意 NT 自己库里的编号是 私聊=1/群聊=2/频道=4/
                     公众号=103/企业客服=102/临时会话=100）。
* `chatUin`   —— 大写十六进制的不透明令牌，长度 48（99.3%）或 32（0.7%）
                   字符，即 24 / 16 字节：每个文件内逐行唯一，两个文件之间
                   交集为 0，因此 **不包含** 对端号码。聊天身份由 **文件名**
                   `<decimal-id>_<chatType>.db` 承载。
* ``msg.extraData`` —— 恒为 NULL（列存在但从不写值）。
* ``res``       —— 通过 ``(msgRandom, msgSeq)`` 与 ``msg`` 关联；
                   ``filePath`` 是 URL 编码的安卓路径
                   （如 `/storage/emulated/0/Android/data/com.tencent.mobileqq/...`）。

加密状态（重要）：**没有任何一种密码被证明可以解开 extensionData**
----------------------------------------------------------------
PROVEN CIPHER = **NONE**（不存在被证实可用的算法/密钥）。
``extensionData`` / ``res.extraData`` **无法离线解密**。

下面是本次取证（``_probe/p7``–``_probe/p21``，脚本与输出都在本仓库
``qq-exporter/_probe/``）得到的**直接证据**：

1. **不是压缩、不是编码**：600 个真实密文上 zlib / raw-deflate /
   gzip / bz2 / lzma（偏移 0..63 全部尝试）**零命中**；472 字节样本的
   字节直方图 256 个值里出现 250 个，chi-square = 202.0（df = 255，
   即均匀随机）⇒ 是**真加密**，不是 deflate/熵编码。
2. **不是重复密钥 XOR / 无扩散流密码**：等长密文两两异或后，
   272–376 字节里只出现 0–2 个零字节、最长零串 1 ⇒ 无固定周期。
3. **不是 ECB**（任意 64 位分组密码）：4242 个密文共 389,909 个
   8 字节块，**重复 0 次** ⇒ 密文分组不会重复，分组模式被排除。
4. **分组长度 = 8 字节**：密文长度直方图（mod 16）= {8 字节余数: 2181,
   0: 2061}，即**全部**是 8 的倍数 ⇒ 分组密码的分组大小是 8 字节
   （DES/3DES/CAST5/Blowfish/TEA/XTEA/SEED/RC5 一类），
   **不是** AES 的 16 字节分组（AES-CBC/CTR 产生不了 8 字节粒度）。
5. **``key_meta`` 不是密钥**：689,591 次暴力尝试（193 种派生 ×
   AES-128/192/256 ECB/CBC/CTR/OFB/CFB/GCM、3DES、CAST5、SM4、
   ChaCha20、RC4、XTEA(16/32 轮)、XXTEA × 11 种 IV × 3 种负载偏移，
   判据为“能解出可解析 protobuf / 能解压 / 有中文 / 可打印比高”，
   并且**带解压判据的正控**）——最好成绩 430 分，与随机字节的
   噪声同级，**没有一次**命中。
6. **本机没有密钥材料**：注册表 ``Software\Tencent\QQNT`` 只有版本号；
   ``D:\QQ`` 顶层无 json/txt/dat/cfg；``nt_data\msgbackup\`` 下
   **只有** ``temp\*.db``（445 个），没有任何 key/配置/元数据文件；
   ``nt_data\mmkv`` 只有两个 crc 摘要文件；整棵 QQ 数据树扫描
   ``key_meta`` / 16 字节可打印串 / 32–64 位 hex 串，除图片/表情
   噪声外**零命中**。
7. **``key_meta`` 只有一个全局值**：扫描到的 416 个备份库里
   ``ex_info.key_meta`` **完全相同**（同一个 128 hex 串），0 个库
   有不同值 ⇒ 它不是“每个库一把钥匙”，而是一张**兑换凭证**。
8. **密钥必须来自服务端**：PC 端 ``wrapper.node``（9.9.25-42941）
   里 msgbackup 模块的日志串给出完整链路——
   ``ParseRemoteMsgdb RequestEncrptyKey sigLen:{}``、
   ``GetRemoteMsgKey``、``RequestDecryptKeyForFileMeta``、
   ``OnRecvEncryptKeyAndFileMeta``、``HandlerReq/RespDecryptKey``、
   ``DoDecryptKey``、``secretEncryptKey``、``kEncryptKey``、
   ``ECDH pub key info: cipher_ver:{}, key_ver:{}``、
   ``trpc.login.ecdh.EcdhService.SsoOIDB0x916b``，
   以及 ``oidb_0xcde_request.cc`` —— 客户端在解析远端备份库时
   **向服务器申请解密密钥**（OIDB 0xcde / ECDH 会话密钥），
   真正的 32 字节密钥由服务端下发后经 PBKDF2 派生给
   ``DecryptBuf(business_type, version, iv, ciphertext)``。
   离线没有这一步 ⇒ 没有密钥。

因此本模块 **只导出可读的明文元数据**（时间、会话类型、消息条数、
序号、附件清单/大小/路径）；正文解密需要服务端下发的密钥（或在
客户端进程里 hook ``DecryptBuf`` / ``GetRemoteMsgKey`` 抓取密钥），
见 :func:`decrypt_extension` 与 :func:`decrypt_extension_data`。

2026-10-20 第二轮取证（脚本见 ``qq-export-probe/p5``–``p24``，完整报告见
``qq-export-probe/BACKUP_FINDINGS.md``）新增的硬证据
----------------------------------------------------------------
在 ``1001313006_1.db`` 的**全部 1475 行**（1,132,744 字节）上：

* 合并字节分布的 Shannon 熵 = **7.99984 bit/byte**（上限 8.0），
  256 个字节值全部出现（最少 4214 次、最多 4613 次），
  **chi-square = 249.6（df = 255，期望 255 +/- 23）** => 与均匀随机不可区分。
* **0 / 1475** 条密文能解析成结构完整的 Protobuf；
  偏移 0..64 上“看起来像 protobuf tag”的命中率 ~37%，
  正好等于随机字节的命中率（(4/8)x(255/256)~0.372）=> 没有 tag 结构。
* 长度直方图：全部是 **8 的倍数**（min 376 / max 4656），
  mod 16 = {0: 574, 8: 901} => 8 字节分组的 CBC/CTR，或
  “8 字节对齐的明文 + 16 字节 GCM tag”；**排除** 16 字节 CBC 的纯密文。
* 70,346 个 16 字节块**全部互不相同** => 排除任何固定密钥的 ECB。
* 等长密文两两异或，零字节比例 0.00-0.01（随机水平）=> 排除固定密钥流。
* 同长度（488/496/504）的密文**前缀互不相同**（526/231/83 个各自 526/231/83
  个不同的前 8/16 字节）=> nonce/IV 是**逐行**的，不是逐库的。
* 密钥猎取（本机）：扫描 QQ 数据树 14,229 个文件找
  ``com_tencent_msgbackup`` / ``SecretKey`` / ``encrypt_key_`` **零命中**；
  扫描运行中的 6 个 ``QQ.exe``（3.55 GB 内存）找 **AES 密钥调度表**
  （OpenSSL ``AES_KEY`` 布局，p16 自检通过）**只找到 1 个 16 字节密钥**
  （与备份无关），**没有任何 32 字节（= AES-256）调度表**
  => 备份密钥当前**不在内存里**（备份发生在 2026-10-19 18:39-19:15，
  之后 QQ 重启过）。

因此本模块提供了**带上下文的解密接口**（:class:`BackupKeyContext`、
:func:`decode_extension_data`）：一旦拿到服务端下发的 32 字节密钥与 IV，
同一套候选 framing（ECB/CBC/CTR/OFB/CFB/GCM，IV 取自上下文或密文头尾）
加上严格判据（必须解出可解析的 NT 消息体）就能直接用，无需再改代码。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import os
import re
import sqlite3
import struct
import urllib.parse

__all__ = [
    "OfflineDecryptionUnavailable",
    "DecryptionFailed",
    "BackupKeyContext",
    "BackupMessage",
    "MsgBackupDb",
    "MsgBackupStore",
    "iter_backup_dbs",
    "read_backup_dir",
    "default_backup_root",
    "decrypt_extension",
    "decrypt_extension_data",
    "decode_extension_data",
    "message_segments",
    "message_text",
    "CHAT_TYPE_PRIVATE",
    "CHAT_TYPE_GROUP",
]

#: 备份导出里 chatType=1 表示单聊。
CHAT_TYPE_PRIVATE = 1
#: 备份导出里 chatType=3 表示群聊。
CHAT_TYPE_GROUP = 3

_NAME_RE = re.compile(r"^(\d+)_(\d+)\.db$")

#: 已知的 msgbak key_meta（445 个库完全相同，见 ex_info 表）。
KNOWN_KEY_META = (
    "51880cc04a82515a348976a9da5f439f7cc8855187c7006fb14fe153c0340880911"
    "b67c9e5b2d4773b37bc230ea4245f4a5af35eb7ccb7d0fb5e51587f561092"
)


class OfflineDecryptionUnavailable(RuntimeError):
    """请求离线解密 ``extensionData`` 时抛出：缺少服务端下发的密钥。"""


def default_backup_root() -> str:
    """返回默认的消息备份临时目录（不存在时返回路径字符串）。"""
    base = os.path.expandvars(r"%USERPROFILE%\Documents\Tencent Files")
    if not os.path.isdir(base):
        # 退回到当前用户目录下的标准 QQ 路径
        base = os.path.join(
            os.path.expanduser("~"), "Documents", "Tencent Files"
        )
    hits = []
    if os.path.isdir(base):
        for acc in os.listdir(base):
            cand = os.path.join(
                base, acc, "nt_qq", "nt_data", "msgbackup", "temp"
            )
            if os.path.isdir(cand):
                hits.append(cand)
    if not hits:
        return os.path.join(
            base, "<uin>", "nt_qq", "nt_data", "msgbackup", "temp"
        )
    return hits[0]


def _connect_ro(path: str) -> sqlite3.Connection:
    """只读（且 immutable）打开一个 SQLite 文件，不会在磁盘上产生任何写入。"""
    uri = "file:" + path.replace("\\", "/").replace(":", "%3A") + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


class MsgBackupDb:
    """一个手机 QQ 消息备份库（``<id>_<chatType>.db``）。"""

    def __init__(self, path: str):
        self.path = path
        self.name = os.path.basename(path)
        m = _NAME_RE.match(self.name)
        self.chat_id = m.group(1) if m else None
        self.file_chat_type = int(m.group(2)) if m else None
        self._conn = _connect_ro(path)
        self._ex_info = {}
        try:
            for name, value in self._conn.execute("select name, value from ex_info"):
                self._ex_info[name] = value
        except BaseException:
            # 构造失败时不能漏掉连接（Windows 上会把文件一直占着）
            self._conn.close()
            raise

    # -- 基础信息 ---------------------------------------------------------
    @property
    def key_meta(self):
        """OIDB 0xcde 的兑换凭证（128 hex 字符），**不是** 密钥。"""
        return self._ex_info.get("key_meta")

    @property
    def db_version(self):
        return self._ex_info.get("db_version")

    @property
    def chat_type(self):
        """会话类型：1 = 单聊，3 = 群聊（与文件名后缀一致）。"""
        row = self._conn.execute(
            "select chatType from msg limit 1"
        ).fetchone()
        if row and row[0] is not None:
            return int(row[0])
        return self.file_chat_type

    @property
    def message_count(self) -> int:
        return self._conn.execute("select count(*) from msg").fetchone()[0]

    @property
    def resource_count(self) -> int:
        return self._conn.execute("select count(*) from res").fetchone()[0]

    @property
    def time_range(self):
        """(最早, 最晚) 的 UNIX 秒时间戳。"""
        row = self._conn.execute(
            "select min(msgTime), max(msgTime) from msg"
        ).fetchone()
        if not row or row[0] is None:
            return (None, None)
        return (int(row[0]), int(row[1]))

    # -- 行读取 ------------------------------------------------------------
    def iter_messages(self, limit: int | None = None):
        """按 (msgTime, msgSeq) 顺序产出消息行（字典）。"""
        sql = (
            "select _id, chatType, chatUin, extensionData, extraData,"
            " msgRandom, msgSeq, msgTime, msgType from msg"
            " order by msgTime, msgSeq"
        )
        if limit:
            sql += " limit %d" % int(limit)
        for row in self._conn.execute(sql):
            yield _msg_row_to_dict(row)

    def iter_resources(self, limit: int | None = None):
        """产出资源（附件）行（字典）。"""
        sql = (
            "select _id, extraData, filePath, fileSize, msgRandom, msgSeq,"
            " msgSubType, msgType from res"
        )
        if limit:
            sql += " limit %d" % int(limit)
        for row in self._conn.execute(sql):
            yield _res_row_to_dict(row)

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _msg_row_to_dict(row) -> dict:
    (
        _id,
        chat_type,
        chat_uin,
        extension_data,
        extra_data,
        msg_random,
        msg_seq,
        msg_time,
        msg_type,
    ) = row
    return {
        "id": _id,
        "chat_type": chat_type,
        "chat_uin": chat_uin,
        "extension_data": extension_data,
        "extra_data": extra_data,
        "msg_random": msg_random,
        "msg_seq": msg_seq,
        "msg_time": msg_time,
        "msg_datetime": decode_msg_time(msg_time),
        "msg_type": msg_type,
    }


def _res_row_to_dict(row) -> dict:
    (
        _id,
        extra_data,
        file_path,
        file_size,
        msg_random,
        msg_seq,
        msg_sub_type,
        msg_type,
    ) = row
    return {
        "id": _id,
        "extra_data": extra_data,
        "file_path": file_path,
        "file_path_decoded": urllib.parse.unquote(file_path) if file_path else file_path,
        "file_size": file_size,
        "msg_random": msg_random,
        "msg_seq": msg_seq,
        "msg_sub_type": msg_sub_type,
        "msg_type": msg_type,
    }


def decode_msg_time(ts):
    """UNIX 秒 → ``datetime``（UTC）；脏数据返回 ``None`` 而不是中断整次导出。"""
    if ts is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def iter_backup_dbs(root: str | None = None):
    """遍历目录下所有 ``<id>_<chatType>.db`` 备份库。"""
    root = root or default_backup_root()
    if not os.path.isdir(root):
        return
    for name in sorted(os.listdir(root)):
        if _NAME_RE.match(name):
            yield MsgBackupDb(os.path.join(root, name))


def decrypt_extension(data: bytes, key_meta: str) -> bytes:
    """离线解密 ``msg.extensionData`` —— **目前无法做到**。

    ``key_meta`` 只是 OIDB ``0xcde`` 的兑换凭证，不是密钥；真正的
    32 字节密钥由服务端下发（PC 端 ``VKeyManager::GetSecretKey`` →
    ``FetchSecretKey`` → OIDB 0xcde），解密走 ``DecryptBuf(business_type,
    version, iv, ciphertext)``，内部用 PBKDF2 派生 AES-GCM-256/AES-CBC 密钥。
    本机没有任何明文缓存（``nt_db`` 下全部是 SQLCipher 加密库），
    51,200 次派生×密码组合暴力尝试也没有解出可解析的 protobuf。

    Raises:
        OfflineDecryptionUnavailable: 始终抛出，说明缺少什么才能解密。
    """
    raise OfflineDecryptionUnavailable(
        "extensionData 无法离线解密：key_meta 只是 OIDB 0xcde 的兑换凭证，"
        "真正的密钥（32 字节，VKeyManager::GetSecretKey -> FetchSecretKey -> "
        "OIDB 0xcde）由服务端下发。需要在已登录的会话里请求该密钥，"
        "或在 QQ 进程内 hook DecryptBuf(key, iv, version, business_type, "
        "ciphertext) 抓取密钥/IV 后再解密。"
    )


# ──────────────────────────────────────────────────────────────────────────
# 离线解密尝试（严格判据）
# ──────────────────────────────────────────────────────────────────────────

#: 明文里必须出现的字段号（NT 消息体 MsgBody / MsgContent）。
_MSG_BODY_FIELDS = frozenset({
    40800, 45001, 45002, 45101, 45402, 45405, 45411, 45412, 45812, 80900,
    40090, 40093,
})

_TEA_DELTA = 0x9E3779B9


def _readable_text(raw) -> bool:
    """这段字节是不是**可读文本**（严格 UTF-8 + 几乎全可打印）。"""
    if not raw:
        return False
    data = bytes(raw)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text.strip():
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return printable / len(text) >= 0.9 and len(text.strip()) >= 1


def _looks_like_msg_body(data) -> bool:
    """严格判据：``data`` 是不是一个 NT 消息体（Protobuf）。

    必须同时满足：

    1. 能解出 Protobuf 字段（``parse_wire`` 非空）；
    2. 出现的字段号落在已知消息体字段集合里（40800/45002/45101/…，
       它们的 tag 是 2~3 字节定长序列，随机字节几乎撞不上）；
    3. 内容可读：某字段值是**严格 UTF-8 且几乎全可打印**的文本，
       或出现 ``45002``（内容类型）且取值在 1..18。

    这条判据同时用于“是否真的解开了”——见 :func:`decrypt_extension_data`。
    """
    if not data:
        return False
    raw = bytes(data)
    if len(raw) < 4:
        return False
    try:
        from . import body as _body

        fields = _body.parse_wire(raw)
    except Exception:  # noqa: BLE001
        return False
    if not fields:
        return False
    for f in fields:
        if f.wire == 2 and f.number == 40800:
            # MsgBody → 嵌套 MsgContent（45002 内容类型 / 45101 文本）
            for s in _body.parse_wire(f.value):
                if s.wire == 2 and _readable_text(s.value):
                    return True
                if s.wire == 0 and s.number == 45002 and 1 <= int(s.value) <= 18:
                    return True
        if f.wire == 2 and f.number in _MSG_BODY_FIELDS and _readable_text(f.value):
            return True
        if f.wire == 0 and f.number == 45002 and 1 <= int(f.value) <= 18:
            return True
    return False


def _candidate_keys(key_meta: str):
    """从 ``key_meta`` 派生候选密钥（名称, 字节）。"""
    if not key_meta:
        return []
    text = str(key_meta)
    try:
        h = bytes.fromhex(text)
    except ValueError:
        h = b""
    a = text.encode("utf-8", "ignore")
    out = []

    def add(name, blob):
        if blob and (name, blob) not in out:
            out.append((name, blob))

    for nm, raw in (("hex", h), ("ascii", a)):
        if not raw:
            continue
        for i in (0, 16, 32, 48):
            if len(raw) >= i + 16:
                add("%s[%d:%d]" % (nm, i, i + 16), raw[i:i + 16])
        if len(raw) >= 32:
            add("%s[0:32]" % nm, raw[:32])
        add("md5(%s)" % nm, hashlib.md5(raw).digest())
        add("sha1(%s)" % nm, hashlib.sha1(raw).digest())
        add("sha256(%s)[:16]" % nm, hashlib.sha256(raw).digest()[:16])
        add("sha256(%s)[:32]" % nm, hashlib.sha256(raw).digest()[:32])
    for salt in (b"", a, h, b"msgbackup", b"oi_symmetry"):
        for iters in (1000, 100000):
            add("pbkdf2(ascii,%d,%d,16)" % (len(salt), iters),
                hashlib.pbkdf2_hmac("sha256", a, salt, iters, 16))
            add("pbkdf2(ascii,%d,%d,32)" % (len(salt), iters),
                hashlib.pbkdf2_hmac("sha256", a, salt, iters, 32))
    return out


def _tea_block(block, key, rounds=32):
    """TEA 解密单个 8 字节分组（小端）。"""
    v0, v1 = struct.unpack("<2I", block)
    k = struct.unpack("<4I", key[:16].ljust(16, b"\0"))
    s = (_TEA_DELTA * rounds) & 0xFFFFFFFF
    for _ in range(rounds):
        v1 = (v1 - (((v0 << 4) + k[2]) ^ (v0 + s) ^ ((v0 >> 5) + k[3]))) & 0xFFFFFFFF
        v0 = (v0 - (((v1 << 4) + k[0]) ^ (v1 + s) ^ ((v1 >> 5) + k[1]))) & 0xFFFFFFFF
        s = (s - _TEA_DELTA) & 0xFFFFFFFF
    return struct.pack("<2I", v0, v1)


def _xtea_block(block, key, rounds=32):
    """XTEA 解密单个 8 字节分组（小端）。"""
    v0, v1 = struct.unpack("<2I", block)
    k = struct.unpack("<4I", key[:16].ljust(16, b"\0"))
    s = (_TEA_DELTA * rounds) & 0xFFFFFFFF
    for _ in range(rounds):
        v1 = (v1 - (((v0 << 4 ^ v0 >> 5) + v0) ^ (s + k[(s >> 11) & 3]))) & 0xFFFFFFFF
        s = (s - _TEA_DELTA) & 0xFFFFFFFF
        v0 = (v0 - (((v1 << 4 ^ v1 >> 5) + v1) ^ (s + k[s & 3]))) & 0xFFFFFFFF
    return struct.pack("<2I", v0, v1)


def _xxtea_dec(data, key):
    """XXTEA 整体解密（变长，32 位字数组）。"""
    key32 = key[:32].ljust(32, b"\0")
    k = struct.unpack("<8I", key32)
    n = len(data) // 4
    if n < 2:
        return b""
    v = list(struct.unpack("<%dI" % n, data[: n * 4]))
    rounds = 6 + 52 // n
    s = (rounds * _TEA_DELTA) & 0xFFFFFFFF
    y = v[0]
    for _ in range(rounds):
        e = (s >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = v[p - 1]
            v[p] = (v[p] - ((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4)))
                           ^ ((s ^ y) + (k[(p & 3) ^ e] ^ z)))) & 0xFFFFFFFF
            y = v[p]
        z = v[n - 1]
        v[0] = (v[0] - ((((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4)))
                       ^ ((s ^ y) + (k[e] ^ z)))) & 0xFFFFFFFF
        y = v[0]
        s = (s - _TEA_DELTA) & 0xFFFFFFFF
    return struct.pack("<%dI" % n, *v)


def _rc4(key, data):
    S = list(range(256))
    j = 0
    klen = len(key) or 1
    for i in range(256):
        j = (j + S[i] + key[i % klen]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(byte ^ S[(S[i] + S[j]) & 0xFF])
    return bytes(out)


def _cbc8_dec(ct, key, block_fn, iv):
    """8 字节分组的 CBC 解密。"""
    out = bytearray()
    prev = (iv[:8] or b"\0" * 8)
    for i in range(0, len(ct) - 7, 8):
        blk = ct[i:i + 8]
        dec = block_fn(blk, key)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = blk
    return bytes(out)


def _decrypt_attempts(payload, key, ivsrc):
    """产出 ``(算法描述, 明文候选)``。``ivsrc`` 是原始密文（IV 可能在里面）。"""
    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher,
            algorithms,
            modes,
        )
    except Exception:  # noqa: BLE001 - 没有 cryptography 就只剩 TEA 系
        Cipher = algorithms = modes = None

    # cryptography 49 起 OFB/CFB/TripleDES 搬到 decrepit 模块
    ofb_mode = cfb_mode = None
    des3_alg = cast5_alg = None
    try:
        from cryptography.hazmat.decrepit.ciphers import modes as _dmodes
        from cryptography.hazmat.decrepit.ciphers import algorithms as _dalgs

        ofb_mode = getattr(_dmodes, "OFB", None)
        cfb_mode = getattr(_dmodes, "CFB", None)
        des3_alg = getattr(_dalgs, "TripleDES", None)
        cast5_alg = getattr(_dalgs, "CAST5", None)
    except Exception:  # noqa: BLE001 - 老版本没有 decrepit
        pass
    if modes is not None:
        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            ofb_mode = ofb_mode or getattr(modes, "OFB", None)
            cfb_mode = cfb_mode or getattr(modes, "CFB", None)
            des3_alg = des3_alg or getattr(algorithms, "TripleDES", None)
            cast5_alg = cast5_alg or getattr(algorithms, "CAST5", None)

    if Cipher is not None:
        iv16 = (
            ("zero", b"\0" * 16),
            ("blob", ivsrc[:16]),
            ("bloblast", ivsrc[-16:]),
            ("blob8", ivsrc[8:24]),
            ("key", key[:16]),
        )
        iv8 = (
            ("zero", b"\0" * 8),
            ("blob", ivsrc[:8]),
            ("blob8", ivsrc[8:16]),
            ("key", key[:8]),
        )
        algos = []
        if len(key) >= 16:
            algos.append(("aes-128", algorithms.AES(key[:16])))
        if len(key) >= 24:
            algos.append(("aes-192", algorithms.AES(key[:24])))
        if len(key) >= 32:
            algos.append(("aes-256", algorithms.AES(key[:32])))
        for aname, algo in algos:
            for ivname, iv in iv16:
                for mname, mk in (("cbc", modes.CBC), ("ctr", modes.CTR),
                                  ("ofb", ofb_mode), ("cfb", cfb_mode)):
                    try:
                        dec = Cipher(algo, mk(iv)).decryptor()
                        yield ("%s-%s-%s" % (aname, mname, ivname),
                               dec.update(payload) + dec.finalize())
                    except Exception:  # noqa: BLE001
                        continue
            for ivname, iv in (("zero12", b"\0" * 12), ("blob12", ivsrc[:12])):
                try:
                    dec = Cipher(algo, modes.GCM(iv)).decryptor()
                    yield ("%s-gcm-%s" % (aname, ivname),
                           dec.update(payload) + dec.finalize())
                except Exception:  # noqa: BLE001
                    continue
        if len(key) >= 24 and des3_alg is not None:
            for ivname, iv in iv8:
                try:
                    dec = Cipher(des3_alg(key[:24]), modes.CBC(iv)).decryptor()
                    yield ("des3-cbc-%s" % ivname, dec.update(payload) + dec.finalize())
                except Exception:  # noqa: BLE001
                    continue
        if len(key) >= 16 and cast5_alg is not None:
            for ivname, iv in iv8:
                try:
                    dec = Cipher(cast5_alg(key[:16]), modes.CBC(iv)).decryptor()
                    yield ("cast5-cbc-%s" % ivname, dec.update(payload) + dec.finalize())
                except Exception:  # noqa: BLE001
                    continue

    for rounds in (16, 32):
        for ivname, iv in ((b"zero", b"\0" * 8), ("blob", ivsrc[:8]),
                           ("blob8", ivsrc[8:16]), ("key", key[:8])):
            yield ("tea%d-cbc-%s" % (rounds, ivname),
                   _cbc8_dec(payload, key, _tea_block, iv))
            yield ("xtea%d-cbc-%s" % (rounds, ivname),
                   _cbc8_dec(payload, key, _xtea_block, iv))
    if len(key) >= 32:
        yield ("xxtea", _xxtea_dec(payload, key[:32]))
    yield ("rc4", _rc4(key, payload))


def decrypt_extension_data(blob, key_meta) -> bytes | None:
    """离线解密 ``msg.extensionData``；解不开就返回 ``None``（不抛异常）。

    **结论：PROVEN CIPHER = NONE。** 在离线状态下没有任何算法/密钥
    组合能解开真实备份库里的 ``extensionData`` —— ``key_meta`` 只是
    OIDB ``0xcde`` 的兑换凭证，真正的 32 字节密钥由服务端下发
    （见模块 docstring 的完整证据链）。因此对真实数据本函数
    **恒返回 ``None````。

    这里保留候选尝试有两个用途：

    1. 判据本身是严格的（必须解出可解析的 NT 消息体 Protobuf），
       以后一旦拿到正确密钥（hook ``DecryptBuf`` / ``GetRemoteMsgKey``
       抓到 key+iv），把同一套派生与判据接上就能直接用；
    2. 测试里用“正控”证明判据不是空转（见 ``tests/test_backup.py``）。

    Args:
        blob: ``msg.extensionData`` / ``res.extraData`` 的原始字节。
        key_meta: ``ex_info`` 表里 ``name='key_meta'`` 的值（128 hex 字符）。

    Returns:
        解密后的明文字节；**无法解密时返回 ``None``**。
    """
    if not blob or not key_meta:
        return None
    raw = bytes(blob)
    for _kname, key in _candidate_keys(str(key_meta)):
        for offset in (0, 8, 16):
            payload = raw[offset:]
            if len(payload) < 16:
                continue
            for _cname, plain in _decrypt_attempts(payload, key, raw):
                if _looks_like_msg_body(plain):
                    return plain
    return None


# ──────────────────────────────────────────────────────────────────────────
# 目录级读取器
# ──────────────────────────────────────────────────────────────────────────

class MsgBackupStore:
    """消息备份目录（``nt_data/msgbackup/temp``）的只读读取器。

    离线能拿到的**明文**：时间、会话类型、消息类型、序号、附件路径/大小；
    离线拿不到的：消息正文（``extensionData`` 是密文）。

    用法::

        store = MsgBackupStore(r"C:\\\\Users\\\\<用户名>\\\\Documents\\Tencent Files\\"
                               r"10001\\nt_qq\\nt_data\\msgbackup\\temp")
        for msg in store.iter_messages():
            print(msg["msg_time"], msg["chat_type"], msg["msg_type"],
                  msg["text"], len(msg["raw"] or b""))
    """

    def __init__(self, msgbackup_dir: str):
        root = os.path.abspath(str(msgbackup_dir))
        # 也接受 msgbackup 父目录（里面是 temp/）
        temp = os.path.join(root, "temp")
        if os.path.isdir(temp) and not any(
            _NAME_RE.match(n) for n in os.listdir(root)
        ):
            root = temp
        if not os.path.isdir(root):
            raise FileNotFoundError("消息备份目录不存在: %s" % msgbackup_dir)
        self.msgbackup_dir = root
        self.db_paths = [
            os.path.join(root, name)
            for name in sorted(os.listdir(root))
            if _NAME_RE.match(name)
        ]
        self._key_meta = None
        self._key_meta_loaded = False

    # -- 元信息 ------------------------------------------------------------
    @property
    def key_meta(self):
        """``ex_info`` 里 ``key_meta`` 的值（兑换凭证，不是密钥）。"""
        if not self._key_meta_loaded:
            self._key_meta_loaded = True
            for path in self.db_paths:
                try:
                    conn = _connect_ro(path)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    row = conn.execute(
                        "select value from ex_info where name='key_meta'"
                    ).fetchone()
                except Exception:  # noqa: BLE001
                    row = None
                finally:
                    conn.close()
                if row and row[0]:
                    self._key_meta = row[0]
                    break
        return self._key_meta

    @property
    def chat_count(self) -> int:
        return len(self.db_paths)

    def counts(self) -> dict:
        """每个库的 (消息数, 附件数)。"""
        out = {}
        for path in self.db_paths:
            conn = _connect_ro(path)
            try:
                msgs = conn.execute("select count(*) from msg").fetchone()[0]
                ress = conn.execute("select count(*) from res").fetchone()[0]
            except Exception:  # noqa: BLE001
                msgs = ress = 0
            finally:
                conn.close()
            out[os.path.basename(path)] = (msgs, ress)
        return out

    # -- 行读取 ------------------------------------------------------------
    def iter_messages(self, db=None, limit=None, decrypt=False):
        """产出消息字典（**按库、库内按时间排序**）。

        每个字典至少包含::

            {
              "msg_time": 1737113429,   # UNIX 秒
              "chat_uin": "3F2A...（48 个大写 hex 字符，对端身份的密文令牌）",
              "chat_type": 1,           # 1=单聊 3=群聊
              "msg_type": 2,            # 1=普通短消息 2=富文本/带媒体
              "text": None,             # 正文：离线无法解密，恒为 None
              "raw": b"...",            # 原始 extensionData 密文
              ...                        # 另外还有 msg_seq/msg_random/attachments 等
            }

        ``decrypt=True`` 时会用 ``self.key_meta`` 尝试解密（结果是
        ``None``——离线没有密钥；见模块 docstring）。
        """
        paths = list(self.db_paths) if db is None else [db]
        emitted = 0
        for path in paths:
            conn = _connect_ro(path)
            try:
                res_index = {}
                for r in conn.execute(
                    "select extraData, filePath, fileSize, msgRandom, msgSeq,"
                    " msgSubType, msgType from res"
                ):
                    res_index.setdefault((r[3], r[4]), []).append(r)
                sql = (
                    "select _id, chatType, chatUin, extensionData, extraData,"
                    " msgRandom, msgSeq, msgTime, msgType from msg"
                    " order by msgTime, msgSeq"
                )
                for row in conn.execute(sql):
                    if limit is not None and emitted >= limit:
                        return
                    emitted += 1
                    yield self._row_to_dict(path, row, res_index, decrypt)
            finally:
                conn.close()

    def iter_resources(self, db=None):
        """产出附件（``res`` 表）字典。"""
        paths = list(self.db_paths) if db is None else [db]
        for path in paths:
            conn = _connect_ro(path)
            try:
                for row in conn.execute(
                    "select _id, extraData, filePath, fileSize, msgRandom,"
                    " msgSeq, msgSubType, msgType from res"
                ):
                    yield _res_row_to_dict(row) | {"db": os.path.basename(path)}
            finally:
                conn.close()

    def _row_to_dict(self, path, row, res_index, decrypt=False):
        (_id, chat_type, chat_uin, extension_data, extra_data,
         msg_random, msg_seq, msg_time, msg_type) = row
        attachments = []
        for r in res_index.get((msg_random, msg_seq), ()):
            attachments.append({
                "file_path": r[1],
                "file_path_decoded": urllib.parse.unquote(r[1]) if r[1] else r[1],
                "file_size": r[2],
                "msg_sub_type": r[5],
                "msg_type": r[6],
            })
        plain = None
        if decrypt and extension_data:
            # 有正确密钥时才会返回明文；离线状态恒为 None（见模块 docstring）。
            plain = decrypt_extension_data(extension_data, self.key_meta)
        return {
            "msg_time": int(msg_time) if msg_time is not None else None,
            "msg_datetime": decode_msg_time(msg_time),
            "chat_uin": chat_uin,
            "chat_type": int(chat_type) if chat_type is not None else None,
            "msg_type": int(msg_type) if msg_type is not None else None,
            "text": plain,          # 离线无法解密正文
            "raw": bytes(extension_data) if extension_data else None,
            "msg_seq": msg_seq,
            "msg_random": msg_random,
            "extra_data": bytes(extra_data) if extra_data else None,
            "attachments": attachments,
            "db": os.path.basename(path),
            "id": _id,
        }

    def __repr__(self):
        return "<MsgBackupStore %s (%d dbs)>" % (self.msgbackup_dir, len(self.db_paths))


# ──────────────────────────────────────────────────────────────────────────
# 带上下文的解密 API（拿到服务端密钥后即可直接用）
# ──────────────────────────────────────────────────────────────────────────

#: 说明“为什么离线解不开”的完整提示。
NO_KEY_MESSAGE = (
    "extensionData 无法离线解密：ex_info.key_meta 只是 OIDB 0xcde 的兑换凭证"
    "（128 个 hex 字符），真正的 32 字节密钥由服务端下发（PC 端 "
    "VKeyManager::GetSecretKey -> FetchSecretKey -> oidb_0xcde_request_impl -> "
    "DecryptBuf(business_type, version, iv, ciphertext)）。本机磁盘/MMVK/内存中"
    "均未找到该密钥（见 qq-export-probe/BACKUP_FINDINGS.md）。\n"
    "拿到密钥后请用：decode_extension_data(blob, key_meta, "
    "BackupKeyContext(key=..., iv=...))。"
)


class DecryptionFailed(RuntimeError):
    """给了密钥，但没能解出符合判据（可解析 NT 消息体）的明文。"""


@dataclasses.dataclass
class BackupKeyContext:
    """解密 ``extensionData`` 所需的上下文。

    Attributes:
        key: 服务端下发的密钥（16/24/32 字节；字符串时按 hex 解析）。
        iv: 与之配套的 IV/Nonce（``DecryptBuf`` 的 ``ivStr`` 参数）。
        version: 密钥版本号（``DecryptBuf`` 的 ``version``）。
        business_type: 业务类型（``DecryptBuf`` 的 ``business_type``）。
        key_meta: 该备份库的 ``ex_info.key_meta``（兑换凭证，仅供记录）。
    """

    key: bytes
    iv: bytes | None = None
    version: int | None = None
    business_type: int | None = None
    key_meta: str | None = None

    def __post_init__(self):
        self.key = _parse_key_material(self.key)
        if self.iv is not None:
            self.iv = bytes(self.iv)
        if self.key and len(self.key) not in (16, 24, 32):
            raise ValueError(
                "密钥长度必须是 16/24/32 字节，实际 %d" % len(self.key)
            )


def _parse_key_material(value) -> bytes:
    """把 key 归一化成字节：hex 串 → 原始字节；其它 → utf-8。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    text = str(value)
    try:
        if len(text) in (32, 48, 64) and re.fullmatch(r"[0-9a-fA-F]+", text):
            return bytes.fromhex(text)
    except ValueError:
        pass
    return text.encode("utf-8")


def _ctx_key_iv(ctx, key_meta: str | None = None):
    """把各种形态的 ``ctx`` 归一化成 ``(key, iv)``；没有密钥时返回 ``(None, None)``。"""
    if ctx is None:
        return None, None
    if isinstance(ctx, BackupKeyContext):
        return (ctx.key or None), ctx.iv
    if isinstance(ctx, (bytes, bytearray, memoryview)):
        return bytes(ctx), None
    if isinstance(ctx, str):
        return _parse_key_material(ctx), None
    if isinstance(ctx, dict):
        key = ctx.get("key") or ctx.get("secret_key") or ctx.get("secretKey")
        iv = ctx.get("iv") or ctx.get("nonce")
        if key is None:
            return None, None
        return _parse_key_material(key), (bytes(iv) if iv else None)
    key = getattr(ctx, "key", None)
    iv = getattr(ctx, "iv", None)
    if key is None:
        return None, None
    return _parse_key_material(key), (bytes(iv) if iv else None)


def _strip_padding(data: bytes) -> bytes:
    """去掉 PKCS#7 / 零填充尾巴（明文按 8 字节对齐）。"""
    out = bytes(data)
    if not out:
        return out
    pad = out[-1]
    if 1 <= pad <= 8 and out.endswith(bytes([pad]) * pad):
        out = out[:-pad]
    return out.rstrip(b"\x00")


def _decrypt_with_key(raw: bytes, key: bytes, iv: bytes | None):
    """用给定密钥穷举候选 framing，产出 ``(描述, 明文)``。

    候选来自 ``wrapper.node`` 里 msgbackup/gpro 的密码学符号
    （``AES_set_encrypt_key`` / ``AesCbcEncrypt`` / ``AES_gcm_256_encrypt`` /
    ``GenerateAesKey`` / ``GenerateAesIV`` / ``CRYPTO_chacha_20``）以及
    密文长度特征（**长度恒为 8 的倍数** ⇒ 8 字节分组的 CBC/CTR，或
    “8 字节对齐明文 + 16 字节 GCM tag”）。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher,
            algorithms,
            modes,
        )
    except Exception:  # noqa: BLE001 - 没有 cryptography 就只剩 TEA/RC4 系
        Cipher = algorithms = modes = None

    # cryptography 49 起 OFB/CFB 搬到 decrepit 模块（先试新位置，避免
    # 触发 CryptographyDeprecationWarning）
    ofb_mode = cfb_mode = None
    try:
        from cryptography.hazmat.decrepit.ciphers import modes as _dmodes

        ofb_mode = getattr(_dmodes, "OFB", None)
        cfb_mode = getattr(_dmodes, "CFB", None)
    except Exception:  # noqa: BLE001 - 老版本 cryptography 没有 decrepit
        pass
    if (ofb_mode is None or cfb_mode is None) and modes is not None:
        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            ofb_mode = ofb_mode or getattr(modes, "OFB", None)
            cfb_mode = cfb_mode or getattr(modes, "CFB", None)

    if Cipher is not None:
        if len(key) == 16:
            algos = [("aes-128", algorithms.AES(key))]
        elif len(key) == 24:
            algos = [("aes-192", algorithms.AES(key))]
        else:
            algos = [("aes-256", algorithms.AES(key))]
        for aname, algo in algos:
            # -- ECB --
            try:
                dec = Cipher(algo, modes.ECB()).decryptor()
                yield ("%s-ecb" % aname, dec.update(raw) + dec.finalize())
            except Exception:  # noqa: BLE001
                pass
            ivs = []
            if iv:
                ivs.append(("ctx", bytes(iv)))
            ivs += [
                ("blob16", raw[:16]),
                ("bloblast16", raw[-16:]),
                ("blob12", raw[:12]),
                ("blob8", raw[:8]),
                ("zero16", b"\x00" * 16),
                ("zero12", b"\x00" * 12),
                ("zero8", b"\x00" * 8),
                ("key16", key[:16]),
            ]
            for ivname, ivv in ivs:
                if len(ivv) == 16:
                    for mname, mk in (("cbc", modes.CBC), ("ctr", modes.CTR),
                                      ("ofb", ofb_mode), ("cfb", cfb_mode)):
                        try:
                            dec = Cipher(algo, mk(ivv)).decryptor()
                            yield ("%s-%s-%s" % (aname, mname, ivname),
                                   dec.update(raw) + dec.finalize())
                        except Exception:  # noqa: BLE001
                            pass
                if len(ivv) in (8, 12):
                    for mname, mk in (("ctr", modes.CTR), ("ofb", ofb_mode),
                                      ("cfb", cfb_mode)):
                        try:
                            dec = Cipher(algo, mk(ivv)).decryptor()
                            yield ("%s-%s-%s" % (aname, mname, ivname),
                                   dec.update(raw) + dec.finalize())
                        except Exception:  # noqa: BLE001
                            pass
            # -- GCM：tag 在尾 / 在头 / iv||tag||ct --
            for ivname, ivv in ivs:
                if len(ivv) not in (12, 16):
                    continue
                for tagpos in ("tail", "head"):
                    if tagpos == "tail":
                        ct, tag = raw[:-16], raw[-16:]
                    else:
                        ct, tag = raw[16:], raw[:16]
                    if len(ct) < 16:
                        continue
                    try:
                        dec = Cipher(algo, modes.GCM(ivv, tag)).decryptor()
                        yield ("%s-gcm-%s-%s" % (aname, ivname, tagpos),
                               dec.update(ct) + dec.finalize())
                    except Exception:  # noqa: BLE001
                        pass

    # -- 无 cryptography 时的退化路径（8 字节分组 / 流密码） --
    for rounds in (16, 32):
        for ivname, ivv in (("ctx", iv or b"\x00" * 8), ("zero8", b"\x00" * 8),
                            ("blob8", raw[:8])):
            if len(ivv) != 8:
                continue
            yield ("tea%d-cbc-%s" % (rounds, ivname),
                   _cbc8_dec(raw, key, _tea_block, ivv))
            yield ("xtea%d-cbc-%s" % (rounds, ivname),
                   _cbc8_dec(raw, key, _xtea_block, ivv))
    if len(key) >= 32:
        yield ("xxtea", _xxtea_dec(raw, key[:32]))
    yield ("rc4", _rc4(key, raw))


def decode_extension_data(blob, key_meta: str | None = None, ctx=None) -> bytes:
    """解密一条 ``msg.extensionData`` / ``res.extraData``。

    Args:
        blob: 密文字节。
        key_meta: 该库的 ``ex_info.key_meta``（兑换凭证，仅作校验/记录）。
        ctx: 解密上下文。可以是 :class:`BackupKeyContext`、原始密钥
            ``bytes``、64 个 hex 字符的 ``str``，或 ``{"key": ..., "iv": ...}``
            字典。

    Returns:
        解出的明文字节（去掉尾部填充）。

    Raises:
        OfflineDecryptionUnavailable: 没有提供密钥（离线状态下的必然结果）。
        DecryptionFailed: 有密钥，但所有候选 framing 都解不出 NT 消息体。
    """
    if not blob:
        return b""
    key, iv = _ctx_key_iv(ctx, key_meta)
    if not key:
        raise OfflineDecryptionUnavailable(NO_KEY_MESSAGE)
    raw = bytes(blob)
    for _name, plain in _decrypt_with_key(raw, key, iv):
        for candidate in (plain, _strip_padding(plain)):
            if _looks_like_msg_body(candidate):
                return candidate
    raise DecryptionFailed(
        "使用了给定密钥，但没能解出符合判据的 NT 消息体（试了 ECB/CBC/CTR/OFB/CFB/GCM "
        "与多种 IV/负载偏移）。若密钥正确，说明 framing 假设仍需修正。"
    )


def message_segments(plain: bytes):
    """把解出的明文解析成 NT 消息体片段（依赖 ``qqexport.body``）。"""
    if not plain:
        return []
    try:
        from . import body as _body
    except Exception:  # noqa: BLE001
        return []
    try:
        return list(_body.parse_msg_body(bytes(plain)))
    except Exception:  # noqa: BLE001
        return []


def message_text(segments) -> str:
    """把消息片段拼成可读文本（依赖 ``qqexport.body.seg_text``）。"""
    if not segments:
        return ""
    try:
        from . import body as _body
    except Exception:  # noqa: BLE001
        return ""
    out = []
    for seg in segments:
        try:
            text = _body.seg_text(seg)
        except Exception:  # noqa: BLE001
            text = None
        if text:
            out.append(text)
    return "".join(out)


@dataclasses.dataclass
class BackupMessage:
    """一条备份消息记录（``read_backup_dir`` 的产出）。"""

    id: int | None = None
    chat_type: int | None = None
    chat_uin: str | None = None
    msg_time: int | None = None
    msg_type: int | None = None
    msg_seq: int | None = None
    segments: list = dataclasses.field(default_factory=list)
    text: str | None = None
    raw: bytes | None = None
    # 附加信息
    msg_datetime: object = None
    msg_random: int | None = None
    extra_data: bytes | None = None
    attachments: list = dataclasses.field(default_factory=list)
    db: str | None = None
    decrypted: bool = False
    error: str | None = None

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def keys(self):
        return list(self.__dataclass_fields__)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def read_backup_dir(path, ctx=None, key_meta: str | None = None, limit=None,
                    dbs=None, with_attachments: bool = True):
    """遍历消息备份目录，产出 :class:`BackupMessage` 记录。

    Args:
        path: 备份目录（``nt_data/msgbackup/temp``，也接受其父目录
            ``nt_data/msgbackup``；单个 ``.db`` 文件也可以）。
        ctx: 解密上下文（见 :func:`decode_extension_data`）。``None`` 时
            只导出明文元数据，正文保持 ``None``。
        key_meta: 覆盖 ``ex_info.key_meta``（默认从库中读）。
        limit: 最多产出多少条记录。
        dbs: 只处理这些文件名（默认全部 ``<id>_<chatType>.db``）。
        with_attachments: 是否把 ``res`` 表挂到记录上。

    Yields:
        :class:`BackupMessage`
    """
    root = os.path.abspath(str(path))
    if os.path.isfile(root):
        db_paths = [root]
        root = os.path.dirname(root)
    else:
        temp = os.path.join(root, "temp")
        if os.path.isdir(temp) and not any(
            _NAME_RE.match(n) for n in os.listdir(root)
        ):
            root = temp
        names = sorted(os.listdir(root)) if os.path.isdir(root) else []
        db_paths = [os.path.join(root, n) for n in names if _NAME_RE.match(n)]
    if dbs:
        wanted = set(dbs)
        db_paths = [p for p in db_paths if os.path.basename(p) in wanted]

    emitted = 0
    for db_path in db_paths:
        conn = _connect_ro(db_path)
        try:
            km = key_meta
            if km is None:
                try:
                    row = conn.execute(
                        "select value from ex_info where name='key_meta'"
                    ).fetchone()
                    km = row[0] if row else None
                except Exception:  # noqa: BLE001
                    km = None
            res_index = {}
            if with_attachments:
                for r in conn.execute(
                    "select extraData, filePath, fileSize, msgRandom, msgSeq,"
                    " msgSubType, msgType from res"
                ):
                    res_index.setdefault((r[3], r[4]), []).append(r)
            sql = (
                "select _id, chatType, chatUin, extensionData, extraData,"
                " msgRandom, msgSeq, msgTime, msgType from msg"
                " order by msgTime, msgSeq"
            )
            for row in conn.execute(sql):
                if limit is not None and emitted >= limit:
                    return
                emitted += 1
                (_id, chat_type, chat_uin, extension_data, extra_data,
                 msg_random, msg_seq, msg_time, msg_type) = row
                attachments = []
                for r in res_index.get((msg_random, msg_seq), ()):
                    attachments.append({
                        "file_path": r[1],
                        "file_path_decoded": (
                            urllib.parse.unquote(r[1]) if r[1] else r[1]
                        ),
                        "file_size": r[2],
                        "msg_sub_type": r[5],
                        "msg_type": r[6],
                    })
                plain = None
                segments = []
                text = None
                error = None
                if extension_data:
                    if ctx is not None:
                        try:
                            plain = decode_extension_data(extension_data, km, ctx)
                            segments = message_segments(plain)
                            text = message_text(segments) or None
                        except (OfflineDecryptionUnavailable,
                                DecryptionFailed) as exc:
                            error = str(exc)
                    else:
                        error = "no key context (offline)"
                yield BackupMessage(
                    id=_id,
                    chat_type=int(chat_type) if chat_type is not None else None,
                    chat_uin=chat_uin,
                    msg_time=int(msg_time) if msg_time is not None else None,
                    msg_type=int(msg_type) if msg_type is not None else None,
                    msg_seq=msg_seq,
                    segments=segments,
                    text=text,
                    raw=bytes(extension_data) if extension_data else None,
                    msg_datetime=decode_msg_time(msg_time),
                    msg_random=msg_random,
                    extra_data=bytes(extra_data) if extra_data else None,
                    attachments=attachments,
                    db=os.path.basename(db_path),
                    decrypted=plain is not None,
                    error=error,
                )
        finally:
            conn.close()
