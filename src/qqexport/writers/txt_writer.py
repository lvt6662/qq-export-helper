# -*- coding: utf-8 -*-
"""TXT 导出器：纯文本，可读性优先。

依赖：仅标准库。

用法::

    from qqexport.writers.txt_writer import write_chats
    files = write_chats(chats, r"D:\\QQ导出\\txt")

产物：
  * ``<类型>_<会话名>.txt``  —— 每个会话一个文件，文件头写会话名/类型/消息数/时间范围，
    每条消息一行：``[2026-10-20 21:44:20] 我: 你好，世界``，
    附件以 ``[图片] 照片.png (12.6 KB)`` 的形式追加在行尾。
  * ``index.txt`` —— 所有会话的清单（名称、类型、消息数、时间范围、对应文件）。

实现要点（全量导出友好）：
  * **单遍遍历**：``chats`` 可以是普通 ``list[Chat]``，也可以是流式源
    （:class:`qqexport.model.ChatStream` 或 ``_CountingSource``）。
    会话只遍历一次，写完一个立刻处理下一个。
  * **不持有会话对象**：写完一个会话后只留下几行「元数据」用于最后写 ``index.txt``，
    不把 Chat（连带它的几十万条消息）留在内存里。
  * **index 后写**：``index.txt`` 在遍历完之后才写，避免边写边改清单。
"""

from __future__ import annotations

import os
from collections import namedtuple
from typing import Iterable

from ..model import Chat
from .base import (
    BaseWriter,
    ProgressFn,
    attachments_text,
    chat_display_name,
    chat_type_label,
    message_text,
    report_progress,
    sender_label,
    single_line,
    time_range_text,
)

SEPARATOR = "=" * 60

# index 清单里的一行（元数据，不含 Chat 对象引用）
IndexRow = namedtuple("IndexRow", "name type_label count time_range file_name")


def _total_chats(chats) -> int:
    """会话总数；拿不到长度（普通生成器）时返回 0。"""
    try:
        return len(chats)
    except (TypeError, AttributeError):
        return 0


def _index_row(chat: Chat, path: str) -> IndexRow:
    """把会话的关键信息压成一行元数据（之后就可以把 Chat 释放掉）。"""
    return IndexRow(
        name=chat_display_name(chat),
        type_label=chat_type_label(chat),
        # 消息没载入时退回「库里的总条数」
        count=int(getattr(chat, "count", 0) or 0) or int(getattr(chat, "total_count", 0) or 0),
        time_range=time_range_text(chat),
        file_name=os.path.basename(path),
    )


def _header_lines(chat: Chat) -> list[str]:
    """会话文件头。"""
    return [
        SEPARATOR,
        "会话：%s %s" % (chat_type_label(chat), chat_display_name(chat)),
        "类型：%s" % chat_type_label(chat),
        "消息数：%d" % chat.count,
        "时间范围：%s" % time_range_text(chat),
        SEPARATOR,
        "",
    ]


def write_chats(chats: Iterable[Chat], out_dir: str, progress: ProgressFn | None = None) -> list[str]:
    """把会话写成 TXT 文件，返回写出的文件路径列表。"""
    return TxtWriter(out_dir).write(chats, progress)


class TxtWriter(BaseWriter):
    """TXT writer 实现。"""

    fmt = "txt"
    ext = ".txt"

    def write(self, chats: Iterable[Chat], progress: ProgressFn | None = None) -> list[str]:
        self.prepare_names(chats)  # 预留原始主名：同名会话加 _2/_3，不互相覆盖
        # 会话总数（流式源也支持）；+1 = index 文件
        total = _total_chats(chats)
        total_out = (total + 1) if total else 0
        done = 0
        # 只收集元数据行（几百行，量很小），不持有 Chat 对象
        rows: list[IndexRow] = []
        for chat in chats:
            path = self.chat_path(chat)
            if self.safe_write(chat, path, self._write_chat):
                rows.append(_index_row(chat, path))
            done += 1
            report_progress(progress, done, total_out or done, "已导出 %s" % os.path.basename(path))
            chat = None  # 写完立刻放手，别让大群的百万条消息一直挂在内存里
        # index 在遍历完之后再写
        index = self.index_path()
        self._write_index(rows, index)
        self.register(index)
        done += 1
        report_progress(progress, done, total_out or done, "已导出 %s" % os.path.basename(index))
        return self.files

    # -- 单个会话 ---------------------------------------------------------
    def _write_chat(self, chat: Chat, path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for line in _header_lines(chat):
                fh.write(line + "\n")
            for msg in chat.messages:
                fh.write(self._message_line(msg) + "\n")

    @staticmethod
    def _message_line(msg) -> str:
        """一行消息：``[时间] 发送者: 内容``，附件追加在行尾。"""
        line = "[%s] %s: %s" % (msg.time_text, sender_label(msg), single_line(message_text(msg)))
        atts = attachments_text(msg)
        if atts:
            line = "%s | %s" % (line, atts)
        return line

    # -- index ------------------------------------------------------------
    def _write_index(self, rows: Iterable[IndexRow], path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("QQ 聊天记录导出 - 会话清单\n")
            fh.write(SEPARATOR + "\n")
            fh.write("会话数：%d\n\n" % len(rows))
            for i, row in enumerate(rows, start=1):
                fh.write("%d. %s %s\n" % (i, row.type_label, row.name))
                fh.write("   消息数：%d\n" % row.count)
                fh.write("   时间范围：%s\n" % row.time_range)
                fh.write("   文件：%s\n\n" % row.file_name)
