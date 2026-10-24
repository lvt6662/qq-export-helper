"""统一的数据模型：会话(Chat)与消息(Message)。

本模块是全工程的「数据契约」：
  * db.py   —— 从解密后的明文库读出 Chat / Message 列表
  * writers —— 把 Chat / Message 转成 7 种导出格式
  * ui      —— 把 Chat / Message 转成 JSON 交给前端渲染

字段命名刻意保持简单稳定，新增字段必须带默认值，避免破坏既有 writer。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

# 消息类别（对应 QQ 的 msg_type 字段）
MSG_TYPE_TEXT = 2  # 普通文本/图片/视频等标准消息
MSG_TYPE_FILE = 3
MSG_TYPE_REPLY = 5
MSG_TYPE_CONTACT = 6
MSG_TYPE_SHORT_VIDEO = 7
MSG_TYPE_LEGACY_FORWARD = 8
MSG_TYPE_FORWARD = 11
MSG_TYPE_SYS = 17
MSG_TYPE_CALL = 19

# 方向：1 = 我发出，2 = 对方发出（QQ 的 40013 字段）
DIRECTION_OUT = 1
DIRECTION_IN = 2


def _ts_to_datetime(ts: int | None) -> _dt.datetime:
    """QQ 时间戳(秒) -> datetime；0/None 视为纪元。"""
    if not ts:
        return _dt.datetime(1970, 1, 1)
    try:
        return _dt.datetime.fromtimestamp(int(ts))
    except (OverflowError, OSError, ValueError):
        return _dt.datetime(1970, 1, 1)


@dataclass
class Attachment:
    """消息里携带的附件（图片/视频/文件/语音等）。"""

    kind: str = "file"  # image / video / file / audio / sticker / unknown
    name: str = ""
    size: int = 0
    local_path: str = ""
    md5: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "size": self.size,
            "local_path": self.local_path,
            "md5": self.md5,
            "url": self.url,
        }


@dataclass
class Message:
    """一条聊天消息。"""

    msg_id: int = 0
    time: int = 0  # UNIX 秒
    direction: int = 0  # 1 我发出 / 2 对方发出
    sender: str = ""  # 发送者昵称或 QQ 号
    sender_uid: str = ""
    msg_type: int = 0
    content_type: int = 0
    text: str = ""  # 纯文本摘要（用于列表展示）
    content: str = ""  # 完整可读内容（多段用换行拼接）
    attachments: list[Attachment] = field(default_factory=list)
    is_mine: bool = False

    @property
    def dt(self) -> _dt.datetime:
        return _ts_to_datetime(self.time)

    @property
    def time_text(self) -> str:
        return self.dt.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def date_text(self) -> str:
        return self.dt.strftime("%Y-%m-%d")

    @property
    def type_name(self) -> str:
        return {
            MSG_TYPE_TEXT: "文本",
            MSG_TYPE_FILE: "文件",
            MSG_TYPE_REPLY: "回复",
            MSG_TYPE_CONTACT: "名片",
            MSG_TYPE_SHORT_VIDEO: "短视频",
            MSG_TYPE_LEGACY_FORWARD: "合并转发",
            MSG_TYPE_FORWARD: "转发",
            MSG_TYPE_SYS: "系统消息",
            MSG_TYPE_CALL: "音视频通话",
        }.get(self.msg_type, "消息")

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "time": self.time,
            "time_text": self.time_text,
            "date_text": self.date_text,
            "direction": self.direction,
            "is_mine": self.is_mine,
            "sender": self.sender,
            "sender_uid": self.sender_uid,
            "msg_type": self.msg_type,
            "type_name": self.type_name,
            "content_type": self.content_type,
            "text": self.text,
            "content": self.content,
            "attachments": [a.to_dict() for a in self.attachments],
        }


@dataclass
class Chat:
    """一个会话（单聊或群聊）。"""

    chat_id: str = ""  # 唯一标识：单聊=对方 QQ 号，群聊=群号
    name: str = ""  # 昵称/群名
    peer: str = ""  # 对端 QQ 号（群聊为空）
    is_group: bool = False
    messages: list[Message] = field(default_factory=list)
    source: str = "nt_msg"  # 数据来源标记：nt_msg / backup
    #: 这个会话在库里的**消息总条数**。
    #: 消息没载入时（``messages`` 为空）也知道有多少条 —— 大库上先只读元数据，
    #: 列表页要显示条数、导出要算总量，都靠它。
    total_count: int = 0
    #: 库里的首/末消息时间（消息没载入时也知道）
    first_ts: int = 0
    last_ts: int = 0

    @property
    def count(self) -> int:
        return len(self.messages)

    @property
    def first_time(self) -> int:
        # 优先用库里聚合出来的首条时间 —— 消息是按**消息 ID** 排序的，
        # 首尾消息的时间戳未必是真正最早/最晚的（个别消息时间戳是 0）。
        if self.first_ts:
            return self.first_ts
        return self.messages[0].time if self.messages else 0

    @property
    def last_time(self) -> int:
        if self.last_ts:
            return self.last_ts
        return self.messages[-1].time if self.messages else 0

    @property
    def last_time_text(self) -> str:
        ts = self.last_time
        return _ts_to_datetime(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""

    @property
    def preview(self) -> str:
        if not self.messages:
            return ""
        m = self.messages[-1]
        text = (m.content or m.text or "").replace("\n", " ").strip()
        return text[:60]

    def to_dict(self, with_messages: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "chat_id": self.chat_id,
            "name": self.name,
            "peer": self.peer,
            "is_group": self.is_group,
            # 消息没载入时退回「库里的总条数」，列表页才不会显示成 0
            "count": self.count if self.messages else self.total_count,
            "total_count": self.total_count,
            "first_time": self.first_time,
            "last_time": self.last_time,
            "last_time_text": self.last_time_text,
            "preview": self.preview,
            "source": self.source,
        }
        if with_messages:
            data["messages"] = [m.to_dict() for m in self.messages]
        return data


@dataclass
class ExportResult:
    """一次导出的结果汇总。"""

    path: str = ""
    fmt: str = ""
    chats: int = 0
    messages: int = 0
    ok: bool = True
    error: str = ""


class ChatStream:
    """会话源：**先给元数据，再逐个补上消息**。

    全量导出时数据量大到没法一次装进内存 —— 真实库约 290 万条消息，
    全部读成 :class:`Message` 对象要 4GB 以上内存。于是拆成两步：

      * :meth:`metas` —— 只有元数据（会话名、消息条数、时间范围）的会话列表，
        用来预留文件名、显示进度；
      * :meth:`iter_chats` —— 逐个会话地把消息补上，调用方写完一个就可以丢掉一个，
        内存占用只与「最大的那个会话」有关。

    写器（:mod:`qqexport.writers`）同时接受普通的 ``list[Chat]`` 和本对象。
    """

    def __init__(
        self,
        metas: Sequence[Chat],
        loader: Callable[[Chat], Chat],
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._metas: list[Chat] = list(metas)
        self._loader = loader
        self._on_close = on_close
        self._closed = False

    def metas(self) -> list[Chat]:
        """只有元数据的会话列表（``messages`` 为空）。"""
        return self._metas

    def iter_chats(self) -> Iterator[Chat]:
        """逐个会话地产出**带消息**的 :class:`Chat`。"""
        try:
            for meta in self._metas:
                yield self._loader(meta)
        finally:
            # 迭代结束（或调用方中途放弃）后释放底层连接
            self.close()

    def close(self) -> None:
        """关闭底层资源；可重复调用。"""
        if self._closed:
            return
        self._closed = True
        callback = self._on_close
        self._on_close = None
        if callback is not None:
            try:
                callback()
            except Exception:  # noqa: BLE001 - 关闭失败不该影响导出结果
                pass

    def __enter__(self) -> "ChatStream":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __iter__(self) -> Iterator[Chat]:
        return self.iter_chats()

    def __len__(self) -> int:
        return len(self._metas)


def sort_chats(chats: Iterable[Chat]) -> list[Chat]:
    """按最后一条消息时间倒序排列（最近会话在前）。"""
    return sorted(chats, key=lambda c: c.last_time, reverse=True)


__all__ = [
    "Attachment",
    "Chat",
    "ChatStream",
    "ExportResult",
    "Message",
    "sort_chats",
    "DIRECTION_IN",
    "DIRECTION_OUT",
    "MSG_TYPE_CALL",
    "MSG_TYPE_CONTACT",
    "MSG_TYPE_FILE",
    "MSG_TYPE_FORWARD",
    "MSG_TYPE_LEGACY_FORWARD",
    "MSG_TYPE_REPLY",
    "MSG_TYPE_SHORT_VIDEO",
    "MSG_TYPE_SYS",
    "MSG_TYPE_TEXT",
]
