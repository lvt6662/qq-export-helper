# -*- coding: utf-8 -*-
"""CSV 导出器：UTF-8 带 BOM，Excel 打开中文不乱码。

依赖：仅标准库（``csv`` 模块）。

用法::

    from qqexport.writers.csv_writer import write_chats
    files = write_chats(chats, r"D:\\QQ导出\\csv")

产物：
  * ``<类型>_<会话名>.csv`` —— 每个会话一个文件，表头固定为：
    ``会话,类型,消息ID,时间,日期,方向,发送者,消息类型,内容,附件``，
    一条消息一行（行数 = 消息数 + 1）。
  * ``index.csv`` —— 会话级清单（会话,类型,聊天ID,消息数,时间范围,最后消息）。

注意：内容里的换行会被压成 ``⏎``，保证「一条消息 = 一行」，
这样用 Excel / pandas 读回来时行数与消息数严格一致。

实现要点（全量导出友好）：
  * **单遍遍历**：``chats`` 可以是普通 ``list[Chat]``，也可以是流式源
    （:class:`qqexport.model.ChatStream` 或 ``_CountingSource``），
    会话只遍历一次，写完一个立刻处理下一个。
  * **不持有会话对象**：写完一个会话后只留下几行「元数据」用于最后写 ``index.csv``。
  * **index 后写**：``index.csv`` 在遍历完之后才写。
"""

from __future__ import annotations

import csv
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
    direction_label,
    message_text,
    report_progress,
    sender_label,
    single_line,
    time_range_text,
)

# 会话明细表头
HEADERS = [
    "会话",
    "类型",
    "消息ID",
    "时间",
    "日期",
    "方向",
    "发送者",
    "消息类型",
    "内容",
    "附件",
]

# 会话清单表头
INDEX_HEADERS = ["会话", "类型", "聊天ID", "消息数", "时间范围", "最后消息"]

# index 清单里的一行（元数据，不含 Chat 对象引用）
IndexRow = namedtuple("IndexRow", "name type_label chat_id count time_range preview")


def _total_chats(chats) -> int:
    """会话总数；拿不到长度（普通生成器）时返回 0。"""
    try:
        return len(chats)
    except (TypeError, AttributeError):
        return 0


def _index_row(chat: Chat) -> IndexRow:
    """把会话的关键信息压成一行元数据（之后就可以把 Chat 释放掉）。"""
    return IndexRow(
        name=chat_display_name(chat),
        type_label=chat_type_label(chat),
        chat_id=getattr(chat, "chat_id", "") or "",
        # 消息没载入时退回「库里的总条数」
        count=int(getattr(chat, "count", 0) or 0) or int(getattr(chat, "total_count", 0) or 0),
        time_range=time_range_text(chat),
        preview=single_line(getattr(chat, "preview", "") or ""),
    )


def write_chats(chats: Iterable[Chat], out_dir: str, progress: ProgressFn | None = None) -> list[str]:
    """把会话写成 CSV 文件，返回写出的文件路径列表。"""
    return CsvWriter(out_dir).write(chats, progress)


class CsvWriter(BaseWriter):
    """CSV writer 实现（utf-8-sig = 带 BOM）。"""

    fmt = "csv"
    ext = ".csv"

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
                rows.append(_index_row(chat))
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
        # utf-8-sig：写入 UTF-8 BOM，Excel 双击打开不乱码
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\r\n")
            writer.writerow(HEADERS)
            for msg in chat.messages:
                writer.writerow(
                    [
                        chat_display_name(chat),
                        chat_type_label(chat),
                        msg.msg_id,
                        msg.time_text,
                        msg.date_text,
                        direction_label(msg),
                        sender_label(msg),
                        msg.type_name,
                        single_line(message_text(msg)),
                        single_line(attachments_text(msg)),
                    ]
                )

    # -- index ------------------------------------------------------------
    def _write_index(self, rows: Iterable[IndexRow], path: str) -> None:
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, lineterminator="\r\n")
            writer.writerow(INDEX_HEADERS)
            for row in rows:
                writer.writerow(
                    [
                        row.name,
                        row.type_label,
                        row.chat_id,
                        row.count,
                        row.time_range,
                        row.preview,
                    ]
                )
