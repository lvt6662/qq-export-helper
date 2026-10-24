# -*- coding: utf-8 -*-
"""JSON 导出器：结构化、可被程序读回。

依赖：仅标准库（``json``）。

用法::

    from qqexport.writers.json_writer import write_chats
    files = write_chats(chats, r"D:\\QQ导出\\json")

产物：单文件 ``qq_export.json``，结构::

    {
      "app": "QQ导出助手",
      "version": 1,
      "exported_at": "2026-10-20 22:00:00",
      "chats": [ { ...Chat.to_dict()... }, ... ],
      "chat_count": 3,
      "message_count": 19
    }

``chats`` 里每个会话的字段与 ``qqexport.model.Chat.to_dict()`` 完全一致，
因此可以用 ``json.load`` 读回后再构造 ``Chat`` 对象（程序可再读回）。

实现要点（全量导出友好）：
  * **边序列化边写文件**：先写 ``{"app": ..., "chats": [``，然后每个会话
    ``json.dumps`` 完**立刻**写进文件（逗号分隔），最后写 ``]}``。
    绝不像以前那样把每个会话的 JSON 片段攒进一个 ``chunks`` 列表 ——
    290 万条消息时那个列表会吃掉几个 GB。
  * **消息逐条序列化**：一个会话内部也是一条消息一个字典，写完一条就扔掉，
    峰值内存只与「最大的那个会话」有关，而不是与「全部消息」有关。
  * **单遍遍历**：``chats`` 可以是普通 ``list[Chat]``，也可以是流式源
    （:class:`qqexport.model.ChatStream` 或 ``_CountingSource``）。
  * **单个会话失败不影响整体**：失败的会话被跳过（并写进 ``self.errors``），
    已经写出去的部分会回滚，产物仍然是合法 JSON。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Iterable, Sequence

from ..model import Chat
from .base import (
    APP_NAME,
    EXPORT_VERSION,
    BaseWriter,
    ProgressFn,
    report_progress,
)

JSON_FILENAME = "qq_export.json"


def write_chats(chats: Iterable[Chat], out_dir: str, progress: ProgressFn | None = None) -> list[str]:
    """把会话写成单个 JSON 文件，返回写出的文件路径列表。"""
    return JsonWriter(out_dir).write(chats, progress)


def build_export_dict(chats: Sequence[Chat]) -> dict:
    """构造导出对象（内存足够时用；大文件请用 write_chats 的流式实现）。

    注意：本函数会把**全部**会话一次性转成 dict，百万级消息请用
    :func:`write_chats`（边序列化边写文件）。
    """
    chat_dicts: list[dict] = []
    message_count = 0
    for chat in chats:
        chat_dicts.append(chat.to_dict())
        message_count += int(getattr(chat, "count", 0) or 0)
    return {
        "app": APP_NAME,
        "version": EXPORT_VERSION,
        "exported_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "chat_count": len(chat_dicts),
        "message_count": message_count,
        "chats": chat_dicts,
    }


def _total_chats(chats) -> int:
    """会话总数；拿不到长度（普通生成器）时返回 0。"""
    try:
        return len(chats)
    except (TypeError, AttributeError):
        return 0


def _chat_meta(chat) -> dict:
    """取会话的元数据字段（不含 messages），字段与顺序同 ``Chat.to_dict()``。"""
    try:
        data = chat.to_dict(with_messages=False)
    except TypeError:
        # 不支持 with_messages 参数的实现
        data = chat.to_dict()
    data = dict(data or {})
    data.pop("messages", None)  # messages 由我们自己逐条写
    return data


def _json_str(value) -> str:
    """把值转成 JSON 字面量（中文不转义）。"""
    return json.dumps(value, ensure_ascii=False)


class JsonWriter(BaseWriter):
    """JSON writer 实现（流式写盘）。"""

    fmt = "json"
    ext = ".json"

    def write(self, chats: Iterable[Chat], progress: ProgressFn | None = None) -> list[str]:
        path = os.path.join(self.out_dir, JSON_FILENAME)
        exported_at = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 会话总数（流式源也支持）；+1 = 收尾
        total = _total_chats(chats)
        total_out = (total + 1) if total else 0
        done = 0
        written_count = 0
        written_messages = 0
        first = True
        with open(path, "wb") as fh:
            # ---- 文件头：先写 {"app": ..., "chats": [ ----
            header = (
                "{\n"
                '  "app": %s,\n'
                '  "version": %d,\n'
                '  "exported_at": %s,\n'
                '  "chats": [\n'
            ) % (
                _json_str(APP_NAME),
                EXPORT_VERSION,
                _json_str(exported_at),
            )
            fh.write(header.encode("utf-8"))
            for chat in chats:
                # 记下位置：这个会话写失败时要把已经写出去的部分回滚掉
                pos = fh.tell()
                try:
                    meta = _chat_meta(chat)
                except Exception as exc:  # noqa: BLE001 - 单个会话失败不影响其它会话
                    self.errors.append(
                        "会话「%s」序列化失败：%s" % (getattr(chat, "name", "") or "", exc)
                    )
                    continue
                try:
                    if not first:
                        fh.write(b",\n")
                    count = self._write_chat(fh, chat, meta)
                except Exception as exc:  # noqa: BLE001 - 单个会话失败不影响其它会话
                    fh.seek(pos)
                    fh.truncate()
                    self.errors.append(
                        "会话「%s」序列化失败：%s" % (getattr(chat, "name", "") or "", exc)
                    )
                    continue
                first = False
                written_count += 1
                written_messages += count
                done += 1
                report_progress(progress, done, total_out or done, "已导出 %s" % os.path.basename(path))
                chat = None  # 写完立刻放手，别让大群的百万条消息一直挂在内存里
            # ---- 收尾：] + 统计 + } ----
            fh.write(b"\n  ],\n")
            fh.write(('  "chat_count": %d,\n' % written_count).encode("utf-8"))
            fh.write(('  "message_count": %d\n' % written_messages).encode("utf-8"))
            fh.write(b"}\n")
        self.register(path)
        done += 1
        report_progress(progress, done, total_out or done, "已导出 %s" % os.path.basename(path))
        return self.files

    # -- 单个会话 ---------------------------------------------------------
    def _write_chat(self, fh, chat, meta: dict) -> int:
        """写出一个会话的 JSON 片段，返回写出的消息条数。

        会话字段一次性写出，**消息逐条序列化** —— 写完一条就扔掉，
        不会把整个会话的消息字典攒在内存里。
        """
        fh.write(b"    {\n")
        for key, value in meta.items():
            # 每个字段后面都跟逗号 —— 后面一定还有 "messages"
            line = "      %s: %s,\n" % (_json_str(str(key)), _json_str(value))
            fh.write(line.encode("utf-8"))
        # ---- messages：逐条序列化 ----
        fh.write(b'      "messages": [\n')
        count = 0
        first_msg = True
        for msg in getattr(chat, "messages", None) or []:
            chunk = json.dumps(msg.to_dict(), ensure_ascii=False, indent=2)
            chunk = "\n".join("        " + line for line in chunk.splitlines())
            if not first_msg:
                fh.write(b",\n")
            fh.write(chunk.encode("utf-8"))
            first_msg = False
            count += 1
        fh.write(b"\n      ]\n")
        fh.write(b"    }")
        return count
