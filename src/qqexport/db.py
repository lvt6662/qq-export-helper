# -*- coding: utf-8 -*-
"""db.py —— 读取 QQ NT 明文消息库，产出 :mod:`qqexport.model` 的 Chat / Message。

数据来源（``nt_msg.db`` 解密后的 SQLite 库）：
    ``c2c_msg_table``    单聊：40030 = 对方 QQ 号，40033 = 发送者 QQ 号，40093 = 发送者昵称
    ``group_msg_table``  群聊：40030 = 群号，40090 = 发送者群名片，40093 = 发送者默认昵称
    ``40800``           消息体（Protobuf），由 :mod:`qqexport.proto` 解析

方向字段 40013（官方 ``db_docs`` 结论，与任务书里「1=我发 / 2=对方发」不同）：
    ``0`` = 对方发送（收到）、``1`` = 本人发送、``2`` = 本人发送（特殊类型）、
    ``3`` = 系统消息、``4/5`` = 极少见特殊来源；大整数是旧版数据迁移噪音。
    因此 :class:`~qqexport.model.Message` 的 ``direction`` 保存**原始 40013 值**，
    判断「谁发的」请用 ``Message.is_mine``（优先按 ``my_uin`` 比对 40033 发送者 QQ，
    没有 ``my_uin`` 时按 40013 ∈ {1,2,4,5} 判定为我方）。

用法::

    # 1) 已解密的明文库
    conn = db.open_plain_db(plain_path)
    chats = db.load_all(conn, my_uin="100001")

    # 2) 加密库一步到位（内部先解密到缓存目录再读）
    chats = db.read_chats(enc_path, key="16位口令", my_uin="100001")

    # 3) 只取会话列表（不解析消息体，大库上很快）
    conn = db.open_plain_db(plain_path)
    for chat in db.list_chats(conn):
        print(chat.chat_id, chat.name)
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

from . import paths
from . import proto
from .decrypt import decrypt_database, default_out_dir
from .model import (
    MSG_TYPE_CALL,
    MSG_TYPE_CONTACT,
    MSG_TYPE_FILE,
    MSG_TYPE_FORWARD,
    MSG_TYPE_LEGACY_FORWARD,
    MSG_TYPE_REPLY,
    MSG_TYPE_SHORT_VIDEO,
    MSG_TYPE_SYS,
    Attachment,
    Chat,
    ChatStream,
    Message,
    sort_chats,
)

# ── 常量 ─────────────────────────────────────────────────────────────────────

#: 单聊消息表
C2C_TABLE = "c2c_msg_table"
#: 群聊消息表
GROUP_TABLE = "group_msg_table"
#: 最近联系人表 —— **群名 / 对方昵称**的来源
#:
#: 消息表里没有群名字段（``group_msg_table`` 只有「发送者的群名片」40090），
#: 群名保存在最近联系人表的 ``40094`` 列里。
CONTACT_TABLE = "recent_contact_v3_table"

#: 附件 kind 取值（与 writers.ATTACHMENT_KIND_LABELS 对齐）
_KIND_TO_ATTACH = {
    proto.KIND_IMAGE: "image",
    proto.KIND_VIDEO: "video",
    proto.KIND_FILE: "file",
    proto.KIND_VOICE: "audio",
    proto.KIND_STICKER: "sticker",
}

#: 语音/表情等没有文件名时的占位名
_DEFAULT_NAME = {
    "image": "图片",
    "video": "视频",
    "audio": "语音",
    "sticker": "表情",
    "file": "文件",
}


def _cache_dir() -> str:
    """明文库缓存目录。

    明文库有 3 GB 大小，放导出目录里不合适；优先放在应用根目录下的
    ``plain``（根目录由 :mod:`qqexport.paths` 决定：正常是
    ``%LOCALAPPDATA%\\QQExportHelper``，不可写时自动退到 %TEMP% 或
    exe 同目录的 ``runtime``），再不行才退回导出目录。
    """
    candidates: list[str] = []
    try:
        candidates.append(os.path.join(paths.home_dir(), "plain"))
    except Exception:  # noqa: BLE001 - 极端环境下没有可写目录
        pass
    try:
        candidates.append(os.path.join(str(default_out_dir()), "plain"))
    except Exception:  # noqa: BLE001 - 极端环境下没有可写目录
        pass
    candidates.append(
        os.path.join(os.path.expanduser("~"), ".qqexport", "plain")
    )
    for path in candidates:
        if paths.ensure_dir(path):
            return path
    return candidates[0]


#: 解密后的明文库缓存目录
PLAIN_CACHE_DIR = _cache_dir()


# ── SQL ─────────────────────────────────────────────────────────────────────

_C2C_COLUMNS = (
    '"40001" AS msg_id, "40050" AS ts, "40013" AS direction, '
    '"40020" AS sender_uid, "40033" AS sender_qq, "40030" AS peer_qq, '
    '"40021" AS peer_uid, "40011" AS msg_type, "40012" AS subtype, '
    '"40093" AS sender_name, "40800" AS blob'
)

_GROUP_COLUMNS = (
    '"40001" AS msg_id, "40050" AS ts, "40013" AS direction, '
    '"40020" AS sender_uid, "40033" AS sender_qq, "40030" AS peer_qq, '
    '"40021" AS peer_uid, "40011" AS msg_type, "40012" AS subtype, '
    '"40090" AS group_card, "40093" AS sender_name, "40800" AS blob'
)

_C2C_CHAT_SQL = (
    'SELECT "40030" AS peer, COUNT(*) AS cnt, MAX("40050") AS last_time, '
    'MIN(CASE WHEN "40050" > 0 THEN "40050" END) AS first_time '
    'FROM "c2c_msg_table" WHERE "40030" <> 0 GROUP BY "40030"'
)

# 取「最近一条对方消息」的昵称作为会话名（窗口函数，SQLite >= 3.25）
_C2C_NAME_SQL = (
    'SELECT peer, peer_name FROM ('
    ' SELECT "40030" AS peer, "40093" AS peer_name,'
    ' ROW_NUMBER() OVER (PARTITION BY "40030" ORDER BY "40050" DESC) AS rn'
    ' FROM "c2c_msg_table"'
    ' WHERE "40013" = 0 AND "40030" <> 0 AND "40093" IS NOT NULL'
    '   AND TRIM("40093") <> \'\''
    ') WHERE rn = 1'
)

# 退化方案：不支持窗口函数时退化为「任意一条对方昵称」
_C2C_NAME_SQL_FALLBACK = (
    'SELECT "40030" AS peer, MAX("40093") AS peer_name FROM "c2c_msg_table" '
    'WHERE "40013" = 0 AND "40030" <> 0 AND "40093" IS NOT NULL '
    'AND TRIM("40093") <> \'\' GROUP BY "40030"'
)

_GROUP_CHAT_SQL = (
    'SELECT "40030" AS peer, COUNT(*) AS cnt, MAX("40050") AS last_time, '
    'MIN(CASE WHEN "40050" > 0 THEN "40050" END) AS first_time '
    'FROM {ref} WHERE "40030" <> 0 GROUP BY "40030"'
)

#: 最近联系人表的会话名查询。
#:
#: ``40021`` = 会话 id（群聊 = 群号；单聊 = 以 ``u_`` 开头的 uid）
#: ``40030`` = 对方 QQ 号（**单聊有值，群聊为 0** —— 用它区分单聊/群聊）
#: ``40094`` = 群名（群聊）/ 对方昵称（单聊）—— 主力字段
#: ``40093`` = 备注名（单聊）/ 我在该群的昵称（群聊）—— 备用字段
_CONTACT_SQL = (
    'SELECT "40021" AS chat_id, "40030" AS peer, "40094" AS title, "40093" AS alt '
    'FROM "recent_contact_v3_table"'
)

#: 群消息表在 SQL 里的引用方式。
#:
#: QQ 的 nt_msg.db 里 ``group_msg_table`` 的**表 b-tree 指针链是坏的**
#: （``sqlite_master`` 里的 rootpage 指向的物理页并不是它的 b-tree 根），
#: 整表扫描会报 "database disk image is malformed"；但它的**覆盖索引**是好的。
#: 所以产品侧改为：先用 :mod:`qqexport.recover` 把表数据捞出来写成一份结构
#: 正常的新库，再把新库 ``ATTACH`` 进来（别名 ``grp``），群表一律走 ``grp``。
GROUP_REF_MAIN = '"group_msg_table"'
GROUP_REF_ATTACHED = 'grp."group_msg_table"'

#: 挂载恢复库时使用的 schema 别名
GROUP_ATTACH_SCHEMA = "grp"


# ── 底层工具 ─────────────────────────────────────────────────────────────────


def open_plain_db(
    path: str | os.PathLike[str],
    *,
    query_only: bool = True,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """打开**明文**消息库（只读）。

    :param path: 明文 SQLite 文件路径
    :param query_only: 是否以只读方式打开（``ATTACH`` 之前要设成 ``False``）
    :param check_same_thread: 设为 ``False`` 时允许**跨线程**使用这个连接。
        UI 的 HTTP 线程要用到在主线程打开的连接，必须设成 ``False``
        （否则报 "SQLite objects created in a thread can only be used in that
        same thread"）。调用方要自己保证串行访问。
    :returns: ``sqlite3.Connection``（row_factory = sqlite3.Row）
    :raises FileNotFoundError: 文件不存在
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到数据库文件: {p}")
    # 临时目录不可写时，SQLite 的排序/临时表会报 "unable to open database file"
    paths.ensure_temp_dir()
    conn = sqlite3.connect(str(p), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    if query_only:
        try:
            conn.execute("PRAGMA query_only = 1")
        except sqlite3.Error:  # noqa: BLE001 - 老版本 SQLite 不支持时忽略
            pass
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    """库里已有的表名集合。

    读不出表名（库损坏 / 不是 SQLite 文件）时**不吞异常**：上层如果拿到
    空集合会当作「账号里没有聊天记录」，把损坏库伪装成空账号。
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _is_mine(direction: int, sender_qq: int, my_uin: str) -> bool:
    """判断消息是否由我方发出。

    优先用 ``my_uin`` 与发送者 QQ（40033）比对；没有 ``my_uin`` 时按 40013 判定
    （1/2/4/5 = 我方，0/3 = 对方或系统）。
    """
    if my_uin:
        return bool(sender_qq) and str(sender_qq) == str(my_uin)
    return int(direction) in (1, 2, 4, 5)


def _as_int(value: Any, default: int = 0) -> int:
    """宽松地把库里的值转成 int（个别脏值不该让整次导出失败）。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _refine_kind(seg: proto.Segment, msg_type: int) -> str:
    """按官方 ``msgdb/c2c/parser.py`` 的 ``_parse_content`` 规则细分语义类型。"""
    if msg_type == MSG_TYPE_FILE:
        return proto.KIND_FILE
    if msg_type == MSG_TYPE_SHORT_VIDEO:
        return proto.KIND_VIDEO
    if msg_type == MSG_TYPE_CONTACT:
        return proto.KIND_CONTACT
    if msg_type == MSG_TYPE_LEGACY_FORWARD:
        return proto.KIND_LEGACY_FORWARD
    if msg_type == MSG_TYPE_FORWARD:
        return proto.KIND_FORWARD
    if msg_type == MSG_TYPE_SYS:
        return proto.KIND_SYS
    if msg_type == MSG_TYPE_CALL:
        return proto.KIND_CALL
    if msg_type == MSG_TYPE_REPLY:
        return proto.KIND_REPLY

    # msg_type == 2（标准消息）按内部 content_type 细分
    ct = seg.content_type
    value = seg.value
    if ct == 1:
        return proto.KIND_TEXT
    if ct == 2:
        if value.get("img_width") or value.get("img_height"):
            return proto.KIND_IMAGE
        if value.get("f45415") or value.get("video_flag"):
            return proto.KIND_VIDEO
        if value.get("sticker"):
            return proto.KIND_STICKER
        if seg.seg_text():
            return proto.KIND_TEXT
        return proto.KIND_IMAGE
    if ct == 5:
        return proto.KIND_VIDEO
    if ct == 9:
        return proto.KIND_RED_PACKET
    if value.get("sticker"):
        return proto.KIND_STICKER
    if ct == 16:
        return proto.KIND_LEGACY_FORWARD
    if ct == 21:
        return proto.KIND_CALL
    if ct == 6 or ct == 11:
        return proto.KIND_STICKER
    if ct == 4:
        return proto.KIND_VOICE
    if ct == 7:
        return proto.KIND_REPLY
    return seg.kind


def _sys_human_text(raw: Any) -> str:
    """从系统消息的 JSON（80900）里尽量抠出一句人话。"""
    if not raw:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", errors="replace")
    text = str(raw)
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return text.strip()[:200]
    return _find_text(obj)


def _find_text(obj: Any, depth: int = 0) -> str:
    """在 JSON 结构里递归找 ``str``/``text``/``message`` 等人话字段。"""
    if depth > 6:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("str", "text", "message", "msg", "tip", "title", "desc"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for key in ("msg", "data", "content"):
            val = obj.get(key)
            if isinstance(val, (dict, list)):
                found = _find_text(val, depth + 1)
                if found:
                    return found
        for val in obj.values():
            if isinstance(val, (dict, list)):
                found = _find_text(val, depth + 1)
                if found:
                    return found
    elif isinstance(obj, list):
        for val in obj:
            found = _find_text(val, depth + 1)
            if found:
                return found
    return ""


def _ark_prompt(value: Any) -> str:
    """Ark（小程序/分享卡片）消息的正文：从 ``fwd_meta`` 的 JSON 里抠出 prompt/描述。"""
    raw = value.get("fwd_meta") if isinstance(value, dict) else None
    if not raw:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", errors="replace")
    try:
        obj = json.loads(str(raw))
    except (ValueError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    for key in ("prompt", "desc", "title", "text"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return _find_text(obj)


def _summarize(kind: str, seg: proto.Segment) -> str:
    """生成一段人类可读的摘要。"""
    value = seg.value
    if kind == proto.KIND_TEXT:
        return seg.seg_text() or ""
    if kind == proto.KIND_IMAGE:
        return "[图片]"
    if kind == proto.KIND_VIDEO:
        return "[视频]"
    if kind == proto.KIND_VOICE:
        return "[语音]"
    if kind == proto.KIND_FILE:
        name = seg.seg_filename() or value.get("file_ext") or ""
        return f"[文件:{name}]" if name else "[文件]"
    if kind == proto.KIND_STICKER:
        return "[表情]"
    if kind == proto.KIND_REPLY:
        summary = value.get("ref_f47713") or value.get("reply_summary") or ""
        who = value.get("reply_group_nickname") or value.get("nc_nickname_1") or ""
        own = seg.seg_text() or ""
        head = ""
        if summary:
            head = "[回复]" + (f"{who}: " if who else "") + str(summary)
        if own:
            return (head + "\n" + own) if head else own
        return head or "[回复]"
    if kind == proto.KIND_CONTACT:
        nick = (
            value.get("nc_nickname_1")
            or value.get("nc_remark_1")
            or value.get("nc_nickname_2")
            or value.get("nc_remark_2")
            or ""
        )
        return f"[名片]{nick}" if nick else "[名片]"
    if kind == proto.KIND_FORWARD:
        return "[聊天记录]"
    if kind == proto.KIND_LEGACY_FORWARD:
        return "[合并转发]"
    if kind == proto.KIND_CALL:
        desc = value.get("call_desc") or ""
        dur = value.get("call_duration") or 0
        tail = ""
        if desc:
            tail = str(desc)
        elif dur:
            tail = f"{int(dur)}秒"
        return "[通话]" + (f" {tail}" if tail else "")
    if kind == proto.KIND_SYS:
        human = _sys_human_text(value.get("sys_content"))
        return "[系统消息]" + (f" {human}" if human else "")
    if kind == proto.KIND_RED_PACKET:
        return "[红包]"
    if kind == proto.KIND_LOCATION:
        return "[位置]"
    if seg.content_type == 10:  # Ark 应用 / 分享卡片
        prompt = _ark_prompt(value)
        return prompt or "[Ark应用]"
    return f"[{seg.seg_type_name()}]"


def _attachment(kind: str, seg: proto.Segment) -> Attachment | None:
    """把一段媒体内容转成 :class:`~qqexport.model.Attachment`。"""
    attach_kind = _KIND_TO_ATTACH.get(kind)
    if not attach_kind:
        return None
    value = seg.value
    local = str(value.get("local_path") or value.get("ntos_path") or "")
    name = str(value.get("filename") or "")
    if not name and local:
        name = os.path.basename(local.replace("\\", "/"))
    md5 = ""
    raw = value.get("md5_raw")
    if raw:
        try:
            md5 = bytes(raw).hex()
        except (TypeError, ValueError):
            md5 = ""
    try:
        size = int(value.get("filesize") or 0)
    except (TypeError, ValueError):
        size = 0
    url = str(
        value.get("cdn_url_1") or value.get("cdn_url_2") or value.get("cdn_url_3") or ""
    )
    if not name:
        name = _DEFAULT_NAME.get(attach_kind, "文件")
    return Attachment(
        kind=attach_kind,
        name=name,
        size=size,
        local_path=local,
        md5=md5,
        url=url,
    )


def _render(msg_type: int, segments: list[proto.Segment]) -> tuple[int, str, str, list[Attachment]]:
    """把内容段渲染成 ``(content_type, text, content, attachments)``。

    * ``text``    —— 纯文本摘要（列表里显示用）
    * ``content`` —— 完整可读内容（多段用换行拼接）
    """
    if not segments:
        return 0, "", "", []
    ctype = segments[0].content_type
    parts: list[str] = []
    texts: list[str] = []
    attachments: list[Attachment] = []
    for seg in segments:
        kind = _refine_kind(seg, msg_type)
        summary = _summarize(kind, seg)
        if summary:
            parts.append(summary)
        if kind == proto.KIND_TEXT:
            plain = seg.seg_text() or ""
            if plain:
                texts.append(plain)
        elif seg.content_type == 10:  # Ark 应用 / 分享卡片
            prompt = _ark_prompt(seg.value)
            if prompt:
                texts.append(prompt)
        att = _attachment(kind, seg)
        if att is not None:
            attachments.append(att)
    content = "\n".join(parts)
    text = "\n".join(texts)
    if not text:
        text = content
    return ctype, text, content, attachments


def _row_to_message(
    row: sqlite3.Row,
    *,
    is_group: bool,
    my_uin: str = "",
) -> Message:
    """把一行数据库记录转成 :class:`~qqexport.model.Message`。"""
    msg_id = _as_int(row["msg_id"])
    direction = _as_int(row["direction"])
    sender_qq = _as_int(row["sender_qq"])
    msg_type = _as_int(row["msg_type"])
    blob = row["blob"]
    if isinstance(blob, memoryview):
        blob = blob.tobytes()
    segments = proto.parse_40800(blob, only=proto.CORE_FIELDS)
    content_type, text, content, attachments = _render(msg_type, segments)

    if is_group:
        name = str(row["group_card"] or "").strip() or str(row["sender_name"] or "").strip()
    else:
        name = str(row["sender_name"] or "").strip()
    if not name:
        name = str(sender_qq) if sender_qq else "未知"

    return Message(
        msg_id=msg_id,
        time=_as_int(row["ts"]),
        direction=direction,
        sender=name,
        sender_uid=str(row["sender_uid"] or ""),
        msg_type=msg_type,
        content_type=content_type,
        text=text,
        content=content,
        attachments=attachments,
        is_mine=_is_mine(direction, sender_qq, my_uin),
    )


# ── 会话列表 ─────────────────────────────────────────────────────────────────


def _c2c_peer_names(conn: sqlite3.Connection) -> dict[str, str]:
    """单聊会话名 = 最近一条「对方发的」消息里的昵称（40093）。"""
    names: dict[str, str] = {}
    for sql in (_C2C_NAME_SQL, _C2C_NAME_SQL_FALLBACK):
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error:  # noqa: BLE001 - 老 SQLite 不支持窗口函数
            continue
        for row in rows:
            try:
                peer = int(row["peer"] or 0)
            except (TypeError, ValueError):
                continue
            if peer <= 0:
                continue
            label = str(row["peer_name"] or "").strip()
            if label:
                names[str(peer)] = label
        if names:
            break
    return names


def contact_names(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], dict[str, str]]:
    """从最近联系人表取出**群名**与**对方昵称**。

    QQ 的消息表里没有群名字段，``group_msg_table`` 只存「发送者的群名片」（40090）。
    群名在 ``recent_contact_v3_table`` 的 ``40094`` 列里，实测例如::

        群 912346654 -> 「哈工大志愿者群 - 2群」
        群 893787634 -> 「哈工大铁路协会」

    单聊的 ``40094`` 则是对方的昵称（与消息表里取到的昵称一致）。

    :param conn: 明文库连接
    :returns: ``(群名表, 单聊昵称表)``，两个表的 key 分别是群号、对方 QQ 号。
    """
    group_names: dict[str, str] = {}
    c2c_names: dict[str, str] = {}
    if CONTACT_TABLE not in table_names(conn):
        return group_names, c2c_names
    try:
        rows = conn.execute(_CONTACT_SQL).fetchall()
    except sqlite3.Error:  # noqa: BLE001 - 表结构异常时退回「无名字」
        return group_names, c2c_names
    for row in rows:
        title = str(row["title"] or "").strip()
        try:
            peer = int(row["peer"] or 0)
        except (TypeError, ValueError):
            peer = 0
        if peer > 0:
            # 单聊：40094 = 对方昵称，40093 = 我给对方设的备注名
            label = title or str(row["alt"] or "").strip()
            if label:
                c2c_names.setdefault(str(peer), label)
        else:
            # 群聊：40094 = 群名。
            # 注意 40093 在群行里是「**我本人在该群的昵称**」（如「安徒生」），
            # 不是群名，拿来当群名会张冠李戴，所以这里只用 40094。
            chat_id = str(row["chat_id"] or "").strip()
            if title and chat_id:
                group_names.setdefault(chat_id, title)
    return group_names, c2c_names


def list_chats(
    conn: sqlite3.Connection,
    *,
    my_uin: str = "",
    group_ref: str = GROUP_REF_MAIN,
) -> list[Chat]:
    """列出所有会话（**不加载消息体**，大库上很快）。

    会话 id 规则：单聊 = 对方 QQ 号（40030），群聊 = 群号（40030）。
    群名取自 ``recent_contact_v3_table``（消息表里没有群名字段），
    取不到时退化为群号。

    :param conn: 明文库连接
    :param my_uin: 本人 QQ 号（用于群名片/昵称回退判断，可留空）
    :param group_ref: 群消息表在 SQL 里的引用（主库或挂载的恢复库）
    :returns: 按最后一条消息时间倒序排列的 Chat 列表（``messages`` 为空）
    """
    del my_uin  # 列表阶段不需要方向判断
    tables = table_names(conn)
    result: list[tuple[int, Chat]] = []
    group_names, c2c_contact_names = contact_names(conn)

    if C2C_TABLE in tables:
        peer_names = _c2c_peer_names(conn)
        for row in conn.execute(_C2C_CHAT_SQL).fetchall():
            try:
                peer = int(row["peer"] or 0)
            except (TypeError, ValueError):
                continue
            if peer <= 0:
                continue
            label = c2c_contact_names.get(str(peer)) or peer_names.get(str(peer), "")
            chat = Chat(
                chat_id=str(peer),
                name=label or str(peer),
                peer=str(peer),
                is_group=False,
                messages=[],
                source="nt_msg",
                total_count=_as_int(row["cnt"]),
                first_ts=_as_int(row["first_time"]),
                last_ts=_as_int(row["last_time"]),
            )
            result.append((_as_int(row["last_time"]), chat))

    if GROUP_TABLE in tables:
        for row in conn.execute(_GROUP_CHAT_SQL.format(ref=group_ref)).fetchall():
            try:
                gid = int(row["peer"] or 0)
            except (TypeError, ValueError):
                continue
            if gid <= 0:
                continue
            chat = Chat(
                chat_id=str(gid),
                name=group_names.get(str(gid)) or str(gid),
                peer="",
                is_group=True,
                messages=[],
                source="nt_msg",
                total_count=_as_int(row["cnt"]),
                first_ts=_as_int(row["first_time"]),
                last_ts=_as_int(row["last_time"]),
            )
            result.append((_as_int(row["last_time"]), chat))

    result.sort(key=lambda item: item[0], reverse=True)
    return [chat for _, chat in result]


def load_messages(
    conn: sqlite3.Connection,
    chat: Chat,
    *,
    my_uin: str = "",
    group_ref: str = GROUP_REF_MAIN,
    limit: int = 0,
) -> Chat:
    """把某个会话的消息读进 ``chat.messages``（按 40001 升序）。

    :param conn: 明文库连接
    :param chat: 目标会话（``chat_id`` 必须是数字）
    :param my_uin: 本人 QQ 号，用于 ``is_mine``
    :param group_ref: 群消息表在 SQL 里的引用（主库或挂载的恢复库）
    :param limit: 只保留**最近**的 N 条（0 = 不限）。限量会下推到 SQL，
        所以给一个大群限时不必先把 130 万条全读进内存。
    :returns: 同一个 ``chat`` 对象（方便链式调用）
    """
    try:
        key = int(str(chat.chat_id))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"非法的会话 id: {chat.chat_id!r}") from exc
    if chat.is_group:
        table = group_ref
    else:
        table = '"%s"' % C2C_TABLE
    columns = _GROUP_COLUMNS if chat.is_group else _C2C_COLUMNS
    if limit and limit > 0:
        # 倒序取最近 N 条，再翻回来 —— 限量下推到 SQL，省掉全量读
        sql = (
            f'SELECT {columns} FROM {table} WHERE "40030" = ? '
            f'ORDER BY "40001" DESC LIMIT ?'
        )
        rows = list(conn.execute(sql, (key, int(limit))))
        rows.reverse()
    else:
        sql = f'SELECT {columns} FROM {table} WHERE "40030" = ? ORDER BY "40001" ASC'
        # 注意：不要用 .fetchall() / list() —— 那样 sqlite3.Row 与 Message 会
        # 同时驻留，峰值内存翻倍。直接迭代游标，逐行转换。
        rows = conn.execute(sql, (key,))
    messages = [
        _row_to_message(row, is_group=chat.is_group, my_uin=my_uin)
        for row in rows
    ]
    chat.messages = messages
    return chat


def _iter_rows(
    conn: sqlite3.Connection, table: str, columns: str
) -> Iterator[sqlite3.Row]:
    """按 40001 升序流式遍历整张消息表（单次全表扫描）。

    :param table: 表在 SQL 里的引用，可以是 ``'"表名"'`` 或 ``'schema."表名"'``
    """
    sql = f'SELECT {columns} FROM {table} ORDER BY "40001" ASC'
    for row in conn.execute(sql):
        yield row


def load_all(
    conn: sqlite3.Connection,
    *,
    my_uin: str = "",
    limit_per_chat: int = 0,
    group_ref: str = GROUP_REF_MAIN,
) -> list[Chat]:
    """读取全部会话及消息（每张表只扫一遍，按 40001 升序归组）。

    :param conn: 明文库连接
    :param my_uin: 本人 QQ 号，用于 ``is_mine``
    :param limit_per_chat: 每个会话最多保留多少条消息（0 = 不限；保留**最近**的）
    :param group_ref: 群消息表在 SQL 里的引用（主库或挂载的恢复库）
    :returns: 按最后一条消息时间倒序排列的 Chat 列表
    """
    chats = list_chats(conn, group_ref=group_ref)
    index: dict[tuple[bool, str], Chat] = {}
    for chat in chats:
        index[(chat.is_group, chat.chat_id)] = chat
    if not index:
        return []

    tables = table_names(conn)
    buckets: dict[tuple[bool, str], list[Message]] = {}
    for table, is_group, columns in (
        ('"%s"' % C2C_TABLE, False, _C2C_COLUMNS),
        (group_ref, True, _GROUP_COLUMNS),
    ):
        bare = table.split('"')[-2] if table.count('"') >= 2 else table
        if bare not in tables:
            continue
        for row in _iter_rows(conn, table, columns):
            try:
                peer = int(row["peer_qq"] or 0)
            except (TypeError, ValueError):
                continue
            if peer <= 0:
                continue
            key = (is_group, str(peer))
            chat = index.get(key)
            if chat is None:
                continue
            message = _row_to_message(row, is_group=is_group, my_uin=my_uin)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = buckets[key] = []
            bucket.append(message)

    for key, chat in index.items():
        messages = buckets.get(key)
        if not messages:
            chat.messages = []
            continue
        if limit_per_chat and limit_per_chat > 0:
            messages = messages[-limit_per_chat:]
        chat.messages = messages
    return sort_chats(chats)


def iter_chats(
    conn: sqlite3.Connection,
    *,
    my_uin: str = "",
    limit_per_chat: int = 0,
    group_ref: str = GROUP_REF_MAIN,
) -> Iterator[Chat]:
    """**逐个会话地**读出消息（内存占用只与最大的那个会话有关）。

    与 :func:`load_all` 的区别：:func:`load_all` 把所有消息一次性装进内存
    （真实库约 290 万条 → 4GB 以上），本函数每次只把一个会话读进来，
    调用方处理完就可以丢掉，适合「全量导出」这种场景。

    :param conn: 明文库连接
    :param my_uin: 本人 QQ 号，用于 ``is_mine``
    :param limit_per_chat: 每个会话最多保留多少条消息（0 = 不限；保留**最近**的）
    :param group_ref: 群消息表在 SQL 里的引用（主库或挂载的恢复库）
    :returns: 逐个产出带消息的 :class:`~qqexport.model.Chat`
    """
    for chat in list_chats(conn, group_ref=group_ref):
        load_messages(
            conn,
            chat,
            my_uin=my_uin,
            group_ref=group_ref,
            limit=limit_per_chat,
        )
        yield chat


class ChatReader:
    """**保持连接的会话读取器**：元数据一次读出，消息按需逐个会话加载。

    大库上（真实库约 290 万条消息）把消息全读进内存要 4GB 以上，
    所以分成两步：

      1. :meth:`list_chats` —— 只读元数据（会话名、条数、时间范围），很快；
      2. :meth:`load` —— 需要哪个会话的消息才读哪个。

    导出时用 :meth:`stream` 得到一个 :class:`qqexport.model.ChatStream`，
    写器边读边写，内存占用只与「最大的那个会话」有关。
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        key: str | bytes | None = None,
        *,
        my_uin: str = "",
        limit_per_chat: int = 0,
        progress=None,
        reuse: bool = True,
    ) -> None:
        self.my_uin = my_uin
        self.limit_per_chat = limit_per_chat
        plain = str(db_path)
        if key:
            plain = decrypt_to_plain(db_path, key, progress=progress, reuse=reuse)
        self.plain_path = plain
        # UI 的 HTTP 线程要用这个连接 -> 必须允许跨线程；
        # 同时用锁把访问串行化，避免两个线程同时用同一个连接。
        self._lock = threading.RLock()
        self._conn = open_plain_db(
            plain, query_only=False, check_same_thread=False
        )
        self._group_ref = GROUP_REF_MAIN
        self._metas: list[Chat] = []
        try:
            self._group_ref = self._prepare_group_ref(plain, progress, reuse)
            self._metas = list_chats(self._conn, group_ref=self._group_ref)
        except Exception:
            self.close()
            raise

    # -- 内部 -------------------------------------------------------------
    def _prepare_group_ref(self, plain: str, progress, reuse: bool) -> str:
        """决定群消息表走主库还是恢复库，返回 SQL 里的引用。

        已经有恢复库时**直接挂上** —— 省掉在坏表上做整表扫描探测的十几秒
        （实测 13.6 秒，而那个探测最后只会得到 "database disk image is malformed"）。
        没有恢复库才探测主库，探测失败就现场恢复一份。
        """
        return attach_group_db(
            self._conn, plain, progress=progress, reuse=reuse, force=False
        )

    # -- 公开接口 ----------------------------------------------------------
    def list_chats(self) -> list[Chat]:
        """全部会话的**元数据**（不含消息）。"""
        return list(self._metas)

    def load(self, chat: Chat, limit: int | None = None) -> Chat:
        """读出某个会话的消息，**返回一个新对象**。

        刻意不就地填充 ``chat.messages``：:class:`qqexport.model.ChatStream`
        会一直持有那批元数据对象，就地填充等于整库消息都留在内存里
        （实测 290 万条 → 4GB），写器再怎么「写完一个放一个」也没用。

        :param limit: 本次最多读多少条（``None`` 用 Reader 自己的默认值）
        """
        loaded = Chat(
            chat_id=chat.chat_id,
            name=chat.name,
            peer=chat.peer,
            is_group=chat.is_group,
            messages=[],
            source=chat.source,
            total_count=chat.total_count,
            first_ts=chat.first_ts,
            last_ts=chat.last_ts,
        )
        return self.load_into(loaded, limit=limit)

    def load_into(self, chat: Chat, limit: int | None = None) -> Chat:
        """把消息**就地**读进 ``chat.messages`` 并返回它（界面点开某个会话时用）。

        :param limit: 本次最多读多少条；``None`` 表示沿用构造时的 ``limit_per_chat``。
            显式传参而不是改写 ``self.limit_per_chat``，避免一次「限量导出」把
            共享 Reader 永久污染成只读最近 N 条。
        """
        if limit is None:
            limit = self.limit_per_chat
        with self._lock:
            load_messages(
                self._conn,
                chat,
                my_uin=self.my_uin,
                group_ref=self._group_ref,
                limit=limit,
            )
        return chat

    def stream(
        self, chat_ids=None, limit: int | None = None, *, on_close=None
    ) -> ChatStream:
        """得到一个流式会话源（可选只导出指定会话）。

        ``chat_ids`` 的元素可以是会话 id，也可以是 ``(chat_id, is_group)``
        二元组 —— 群号和好友 QQ 号可能撞号，只用 id 会导出/打开错会话。
        """
        metas = self._metas
        if chat_ids:
            wanted_ids = set()
            wanted_pairs = set()
            for item in chat_ids:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    wanted_pairs.add((bool(item[1]), str(item[0])))
                elif isinstance(item, dict):
                    cid = str(item.get("id") or item.get("chat_id") or "")
                    grp = item.get("is_group", item.get("group"))
                    wanted_pairs.add((bool(grp), cid))
                else:
                    wanted_ids.add(str(item))

            def _match(meta: Chat) -> bool:
                if (bool(meta.is_group), str(meta.chat_id)) in wanted_pairs:
                    return True
                return str(meta.chat_id) in wanted_ids

            metas = [c for c in metas if _match(c)]

        def _loader(meta: Chat) -> Chat:
            return self.load(meta, limit=limit)

        return ChatStream(metas, _loader, on_close=on_close)

    def close(self) -> None:
        """关掉底层连接。"""
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def __enter__(self) -> "ChatReader":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def chat_stream(
    db_path: str | os.PathLike[str],
    key: str | bytes | None = None,
    *,
    my_uin: str = "",
    limit_per_chat: int = 0,
    progress=None,
    reuse: bool = True,
) -> "ChatStream":
    """打开消息库，返回一个**流式会话源**（见 :class:`qqexport.model.ChatStream`）。

    先只读会话元数据（很快），消息在 :meth:`ChatStream.iter_chats` 里逐个会话地补上，
    因此内存占用只与「最大的那个会话」有关，而不是整库。

    :param db_path: 加密库或明文库路径
    :param key: 口令；``None``/空串表示 ``db_path`` 已是明文库
    :param my_uin: 本人 QQ 号（用于 ``is_mine``）
    :param limit_per_chat: 每个会话最多保留多少条消息（0 = 不限）
    :param progress: 进度回调
    :param reuse: 加密库时是否复用缓存里的明文库
    :returns: :class:`qqexport.model.ChatStream`
    """
    reader = ChatReader(
        db_path,
        key,
        my_uin=my_uin,
        limit_per_chat=limit_per_chat,
        progress=progress,
        reuse=reuse,
    )
    try:
        # 迭代结束后自动关闭底层连接（否则 Windows 上明文库会被一直占用）
        return reader.stream(on_close=reader.close)
    except BaseException:
        reader.close()
        raise


# ── 高层入口（UI 直接用） ───────────────────────────────────────────────────


def plain_db_path(src_db: str | os.PathLike[str]) -> str:
    """给定加密源库，算出它在缓存目录里对应的明文库路径。

    缓存名里必须带上**源库绝对路径的摘要**：所有账号的消息库都叫
    ``nt_msg.db``（只是目录不同），如果只按文件名缓存，切换账号时
    会直接复用上一个账号的明文库，读到别人的聊天记录。
    """
    stem = Path(src_db).stem or "nt_msg"
    try:
        src_abs = os.path.normcase(os.path.abspath(os.fspath(src_db)))
    except Exception:  # noqa: BLE001 - 极端路径（含非法字符）时退回原串
        src_abs = str(src_db)
    digest = hashlib.sha1(src_abs.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return os.path.join(PLAIN_CACHE_DIR, "%s.%s.plain.db" % (stem, digest))


def decrypt_to_plain(
    src_db: str | os.PathLike[str],
    key: str | bytes,
    *,
    progress=None,
    reuse: bool = True,
) -> str:
    """把加密库解密成缓存目录里的明文库，返回明文库路径。

    :param src_db: 加密的 nt_msg.db
    :param key: 口令（16 字节 ASCII 字符串，也接受 bytes）
    :param progress: 进度回调（参数为一行中文提示）
    :param reuse: 缓存里已有更新的明文库时直接复用
    """
    out = plain_db_path(src_db)
    src_p = Path(src_db)
    if reuse and os.path.isfile(out):
        try:
            if os.path.getmtime(out) >= os.path.getmtime(src_p):
                return out
        except OSError:
            pass
    os.makedirs(PLAIN_CACHE_DIR, exist_ok=True)
    return decrypt_database(src_db, key, out, progress=progress)


def _is_corrupt_error(exc: BaseException) -> bool:
    """判断异常是不是"库结构损坏"（malformed / corrupt）。"""
    text = str(exc).lower()
    return "malformed" in text or "corrupt" in text


def attach_group_db(
    conn: sqlite3.Connection,
    plain_path: str | os.PathLike[str],
    *,
    progress=None,
    reuse: bool = True,
    force: bool = False,
) -> str:
    """把恢复出来的群消息库挂到连接上，返回群表的 SQL 引用。

    QQ 的 nt_msg.db 里 ``group_msg_table`` 的表 b-tree 是坏的（整表扫描报
    "database disk image is malformed"），所以这里先把表数据恢复成一份结构
    正常的新库，再 ``ATTACH`` 进来；群表查询全部改走它。

    :param conn: 明文库连接（不能是 ``query_only`` 状态才能 ATTACH）
    :param plain_path: 明文库路径
    :param progress: 进度回调
    :param reuse: 恢复库已存在时直接复用
    :param force: 已知主库群表损坏时跳过"先探一下主库"的步骤
    :returns: 群表在 SQL 里的引用（挂载成功返回 ``grp."group_msg_table"``）
    """
    from . import recover  # 延迟导入：只有真的要恢复时才加载

    # 已经有恢复库且不比明文库旧 → 直接挂上
    if reuse and recover.group_db_is_ready(plain_path):
        out = recover.group_db_path(plain_path)
        try:
            conn.execute(
                "ATTACH DATABASE ? AS %s" % GROUP_ATTACH_SCHEMA, (out,)
            )
            return GROUP_REF_ATTACHED
        except sqlite3.Error:
            pass

    if not force and _group_table_usable(conn):
        return GROUP_REF_MAIN

    try:
        out = recover.recover_group_table(
            plain_path, progress=progress, reuse=reuse
        )
    except Exception as exc:  # noqa: BLE001 - 恢复失败就退回主库
        if progress:
            progress("群消息表恢复失败（%s），将直接读取原库" % exc)
        return GROUP_REF_MAIN
    meta = recover.recover_meta(out)
    try:
        source_rows = int(meta.get("source_rows") or 0)
        recovered_rows = int(meta.get("recovered_rows") or 0)
    except (TypeError, ValueError):
        source_rows = recovered_rows = 0
    if not recovered_rows:
        # 元信息缺失（老版本/外来库）时不能只看 mtime：实际数一下行数，
        # 空库绝不能挂上去，否则群聊会「恢复成功但一条消息都没有」。
        recovered_rows = recover.group_row_count(out)
        if recovered_rows <= 0:
            if progress:
                progress("恢复库里没有数据，将直接读取原库")
            return GROUP_REF_MAIN
    if source_rows and recovered_rows * 2 < source_rows:
        # 恢复出来的行数太少 → 多半是明文库结构与预期不符，退回主库
        if progress:
            progress(
                "群消息表恢复不完整（%d / %d 行），将直接读取原库"
                % (recovered_rows, source_rows)
            )
        return GROUP_REF_MAIN
    try:
        conn.execute(
            "ATTACH DATABASE ? AS %s" % GROUP_ATTACH_SCHEMA, (out,)
        )
    except sqlite3.Error as exc:  # noqa: BLE001
        if progress:
            progress("挂载恢复库失败（%s），将直接读取原库" % exc)
        return GROUP_REF_MAIN
    return GROUP_REF_ATTACHED


def _group_table_usable(conn: sqlite3.Connection) -> bool:
    """主库里 ``group_msg_table`` 的**表 b-tree** 是否能整表扫描。

    坏掉的库会在这里报 "database disk image is malformed"；好库返回 True。
    """
    try:
        conn.execute(
            'SELECT COUNT(*) FROM "group_msg_table" NOT INDEXED'
        ).fetchone()
    except sqlite3.Error:
        return False
    return True


def read_chats(
    db_path: str | os.PathLike[str],
    key: str | bytes | None = None,
    *,
    my_uin: str = "",
    limit_per_chat: int = 0,
    progress=None,
    reuse: bool = True,
) -> list[Chat]:
    """**UI 的一站式入口**：读出全部会话与消息。

    :param db_path: 加密库或明文库路径
    :param key: 口令；``None``/空串表示 ``db_path`` 已是明文库
    :param my_uin: 本人 QQ 号（用于 ``is_mine``）
    :param limit_per_chat: 每个会话最多保留多少条消息（0 = 不限）
    :param progress: 进度回调
    :param reuse: 加密库时是否复用缓存里的明文库
    :returns: ``list[qqexport.model.Chat]``
    """
    plain = str(db_path)
    if key:
        plain = decrypt_to_plain(db_path, key, progress=progress, reuse=reuse)
    conn = open_plain_db(plain, query_only=False)
    try:
        try:
            return load_all(
                conn, my_uin=my_uin, limit_per_chat=limit_per_chat
            )
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt_error(exc):
                raise
            if progress:
                progress("群消息表结构异常，正在重建群消息表…")
        # 主库的群表 b-tree 是坏的 → 恢复一份出来用
        group_ref = attach_group_db(
            conn, plain, progress=progress, reuse=reuse, force=True
        )
        try:
            conn.execute("PRAGMA query_only = 1")
        except sqlite3.Error:  # noqa: BLE001
            pass
        return load_all(
            conn,
            my_uin=my_uin,
            limit_per_chat=limit_per_chat,
            group_ref=group_ref,
        )
    finally:
        conn.close()


def read_chat(
    db_path: str | os.PathLike[str],
    chat_id: str,
    key: str | bytes | None = None,
    *,
    my_uin: str = "",
    is_group: bool | None = None,
) -> Chat | None:
    """只读一个会话（找不到返回 ``None``）。

    :param is_group: 只看群聊/只看私聊；``None`` 表示不限。
        群号和好友 QQ 号可能撞号，需要精确定位时传它。
    """
    plain = str(db_path)
    if key:
        plain = decrypt_to_plain(db_path, key)
    conn = open_plain_db(plain, query_only=False)
    try:
        try:
            for chat in list_chats(conn):
                if chat.chat_id == str(chat_id) and (
                    is_group is None or chat.is_group == is_group
                ):
                    return load_messages(conn, chat, my_uin=my_uin)
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt_error(exc):
                raise
        group_ref = attach_group_db(conn, plain, force=True)
        try:
            conn.execute("PRAGMA query_only = 1")
        except sqlite3.Error:  # noqa: BLE001
            pass
        for chat in list_chats(conn, group_ref=group_ref):
            if chat.chat_id == str(chat_id) and (
                is_group is None or chat.is_group == is_group
            ):
                return load_messages(
                    conn, chat, my_uin=my_uin, group_ref=group_ref
                )
    finally:
        conn.close()
    return None


def last_plain_db() -> str:
    """缓存目录里最近生成/更新的明文库路径（没有则返回空串）。"""
    try:
        items = [
            os.path.join(PLAIN_CACHE_DIR, name)
            for name in os.listdir(PLAIN_CACHE_DIR)
            if name.endswith(".plain.db")
        ]
    except OSError:
        return ""
    items = [p for p in items if os.path.isfile(p)]
    if not items:
        return ""
    items.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return items[0]


__all__ = [
    "C2C_TABLE",
    "GROUP_TABLE",
    "GROUP_REF_MAIN",
    "GROUP_REF_ATTACHED",
    "PLAIN_CACHE_DIR",
    "attach_group_db",
    "decrypt_to_plain",
    "last_plain_db",
    "list_chats",
    "load_all",
    "load_messages",
    "open_plain_db",
    "plain_db_path",
    "read_chat",
    "read_chats",
    "table_names",
]
