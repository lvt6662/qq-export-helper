# -*- coding: utf-8 -*-
"""Markdown 导出器：结构化、便于再编辑与版本管理。

依赖：仅标准库。

用法::

    from qqexport.writers.markdown_writer import write_chats
    files = write_chats(chats, r"D:\\QQ导出\\markdown")

产物：
  * ``<类型>_<会话名>.md`` —— 一个会话一个文件：
    - ``#`` 标题写会话名，``##`` 按日期分组，``###`` 写具体时刻；
    - 引用/回复类消息（msg_type == 5）用 ``>`` 引用块表示；
    - 附件用 ``-`` 列表表示。
  * ``index.md`` —— 所有会话的清单（锚点链接到各会话文件）。

实现要点（全量导出友好）：
  * **单遍遍历**：``chats`` 可以是普通 ``list[Chat]``，也可以是流式源
    （:class:`qqexport.model.ChatStream` 或 ``_CountingSource``），
    会话只遍历一次，写完一个立刻处理下一个。
  * **不持有会话对象**：写完一个会话后只留下几行「元数据」用于最后写 ``index.md``。
  * **index 后写**：``index.md`` 在遍历完之后才写。
"""

from __future__ import annotations

import os
from collections import namedtuple
from typing import Iterable

from ..model import Chat
from .base import (
    BaseWriter,
    ProgressFn,
    attachment_label,
    chat_display_name,
    chat_type_label,
    is_reply,
    message_text,
    quote_filename,
    report_progress,
    sender_label,
    single_line,
    time_range_text,
)


# index 清单里的一行（元数据，不含 Chat 对象引用）
IndexRow = namedtuple("IndexRow", "name type_label count time_range file_name")


def _md_cell(text) -> str:
    """Markdown 表格单元格：压成一行并转义竖线（否则整行表格会错位）。"""
    return single_line(text).replace("|", "\\|")


def _md_link_text(text) -> str:
    """Markdown 链接文字：除表格转义外，方括号也要转义。"""
    return _md_cell(text).replace("[", "\\[").replace("]", "\\]")


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


def write_chats(chats: Iterable[Chat], out_dir: str, progress: ProgressFn | None = None) -> list[str]:
    """把会话写成 Markdown 文件，返回写出的文件路径列表。"""
    return MarkdownWriter(out_dir).write(chats, progress)


class MarkdownWriter(BaseWriter):
    """Markdown writer 实现。"""

    fmt = "markdown"
    ext = ".md"

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
            fh.write("# %s %s\n\n" % (chat_type_label(chat), single_line(chat_display_name(chat))))
            fh.write("- 会话 ID：`%s`\n" % (getattr(chat, "chat_id", "") or "-"))
            fh.write("- 类型：%s\n" % chat_type_label(chat))
            fh.write("- 消息数：%d\n" % chat.count)
            fh.write("- 时间范围：%s\n\n" % time_range_text(chat))
            fh.write("---\n\n")

            current_date = None
            for msg in chat.messages:
                if msg.date_text != current_date:
                    current_date = msg.date_text
                    fh.write("## %s\n\n" % current_date)
                fh.write("### %s %s\n\n" % (msg.time_text, sender_label(msg)))
                body = message_text(msg) or "（空消息）"
                if is_reply(msg):
                    for line in body.splitlines() or [""]:
                        fh.write("> %s\n" % line)
                    fh.write("\n")
                else:
                    fh.write(body + "\n\n")
                atts = getattr(msg, "attachments", None) or []
                if atts:
                    for att in atts:
                        fh.write("- %s\n" % attachment_label(att))
                    fh.write("\n")

    # -- index ------------------------------------------------------------
    def _write_index(self, rows: Iterable[IndexRow], path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# QQ 聊天记录导出 - 会话清单\n\n")
            fh.write("- 会话数：%d\n" % len(rows))
            fh.write("- 消息总数：%d\n\n" % sum(row.count for row in rows))
            fh.write("| # | 会话 | 类型 | 消息数 | 时间范围 | 文件 |\n")
            fh.write("| --- | --- | --- | --- | --- | --- |\n")
            for i, row in enumerate(rows, start=1):
                name = row.file_name
                fh.write(
                    "| %d | %s | %s | %d | %s | [%s](%s) |\n"
                    % (
                        i,
                        _md_cell(row.name),
                        _md_cell(row.type_label),
                        row.count,
                        _md_cell(row.time_range),
                        _md_link_text(name),
                        quote_filename(name),
                    )
                )
            fh.write("\n")
