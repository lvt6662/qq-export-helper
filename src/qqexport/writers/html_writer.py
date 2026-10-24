# -*- coding: utf-8 -*-
"""HTML 导出器：自包含单文件，仿聊天软件观感。

依赖：仅标准库（无外部资源，CSS 全部内联，不引图片/CDN）。

用法::

    from qqexport.writers.html_writer import write_chats
    files = write_chats(chats, r"D:\\QQ导出\\html")

产物：
  * ``<类型>_<会话名>.html`` —— 每个会话一个自包含 HTML：
    - 左侧（对方）与右侧（我）气泡左右对齐、底色不同；
    - 按日期在消息中间插入日期分隔条；
    - 图片/视频/文件消息显示占位框 + 文件名 + 大小（不嵌 base64，避免文件爆炸）。
  * ``index.html`` —— 列出所有会话的链接与统计。

实现要点（全量导出友好）：
  * **单遍遍历**：``chats`` 可以是普通 ``list[Chat]``，也可以是流式源
    （:class:`qqexport.model.ChatStream` 或 ``_CountingSource``），
    会话只遍历一次，写完一个立刻处理下一个。
  * **不持有会话对象**：写完一个会话后只留下几行「元数据」用于最后写 ``index.html``。
  * **index 后写**：``index.html`` 在遍历完之后才写。
"""

from __future__ import annotations

import html
import os
from collections import namedtuple
from typing import Iterable

from ..model import Chat
from .base import (
    APP_NAME,
    BaseWriter,
    ProgressFn,
    attachment_label,
    chat_display_name,
    chat_type_label,
    human_size,
    message_text,
    quote_filename,
    report_progress,
    sender_label,
    time_range_text,
)

CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: #ededed;
  color: #1f2329;
  font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", "Hiragino Sans GB", Arial, sans-serif;
  font-size: 14px;
  line-height: 1.6;
}
.wrap { max-width: 900px; margin: 24px auto 48px; background: #f5f5f5; border-radius: 10px;
        box-shadow: 0 1px 8px rgba(0, 0, 0, .14); overflow: hidden; }
.head { padding: 22px 26px; background: #2e2e2e; color: #f2f2f2; }
.head h1 { margin: 0 0 10px; font-size: 20px; font-weight: 600; }
.head .meta { font-size: 13px; opacity: .85; }
.head .meta span { margin-right: 18px; display: inline-block; }
.body { padding: 18px 26px 26px; }
.date-sep { text-align: center; margin: 20px 0 14px; }
.date-sep span { display: inline-block; background: #d4d4d4; color: #fff; font-size: 12px;
                 padding: 3px 14px; border-radius: 12px; }
.row { display: flex; align-items: flex-start; margin: 12px 0; }
.row.mine { flex-direction: row-reverse; }
.avatar { flex: 0 0 auto; width: 38px; height: 38px; border-radius: 5px; margin: 0 10px;
          background: #b6b6b6; color: #fff; font-size: 13px; text-align: center; line-height: 38px; }
.row.mine .avatar { background: #4b8bf5; }
.bubble { max-width: 68%; padding: 9px 13px; border-radius: 8px; background: #ffffff;
          box-shadow: 0 1px 2px rgba(0, 0, 0, .08); word-break: break-word; }
.row.mine .bubble { background: #95ec69; }
.row.sys .bubble { background: #fffbe6; color: #8a6d3b; max-width: 84%; }
.bubble .name { font-size: 12px; color: #8a8a8a; margin-bottom: 2px; }
.row.mine .bubble .name { text-align: right; }
.bubble .text { white-space: pre-wrap; }
.bubble .time { font-size: 11px; color: #9c9c9c; margin-top: 5px; }
.row.mine .bubble .time { text-align: right; }
.attach { margin-top: 8px; }
.file-box { display: inline-block; min-width: 180px; border: 1px dashed #b3b3b3; border-radius: 6px;
            background: rgba(255, 255, 255, .75); padding: 10px 12px; }
.file-box .ico { display: inline-block; min-width: 42px; height: 42px; line-height: 42px; text-align: center;
                 background: #d8d8d8; color: #fff; border-radius: 4px; font-size: 12px; margin-right: 10px; }
.file-box .fname { font-weight: 600; word-break: break-all; }
.file-box .fsize { color: #8a8a8a; font-size: 12px; }
.foot { text-align: center; font-size: 12px; color: #9a9a9a; padding: 18px; }
table.list { width: 100%; border-collapse: collapse; margin-top: 18px; }
table.list th, table.list td { border-bottom: 1px solid #e2e2e2; padding: 10px 8px; text-align: left; font-size: 13px; }
table.list th { background: #fafafa; font-weight: 600; }
table.list a { color: #1a73e8; text-decoration: none; }
table.list a:hover { text-decoration: underline; }
.num { text-align: right; }
"""


def esc(text) -> str:
    """HTML 转义。"""
    return html.escape("" if text is None else str(text), quote=True)


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


def write_chats(chats: Iterable[Chat], out_dir: str, progress: ProgressFn | None = None) -> list[str]:
    """把会话写成 HTML 文件，返回写出的文件路径列表。"""
    return HtmlWriter(out_dir).write(chats, progress)


class HtmlWriter(BaseWriter):
    """HTML writer 实现。"""

    fmt = "html"
    ext = ".html"

    def write(self, chats: Iterable[Chat], progress: ProgressFn | None = None) -> list[str]:
        self.prepare_names(chats)  # 预留原始主名：同名会话加 _2/_3，不互相覆盖
        # 会话总数（流式源也支持）；+1 = index.html
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
        title = "%s %s" % (chat_type_label(chat), chat_display_name(chat))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n")
            fh.write("<meta charset=\"utf-8\">\n")
            fh.write("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n")
            fh.write("<title>%s - QQ聊天记录导出</title>\n" % esc(title))
            fh.write("<style>%s</style>\n</head>\n<body>\n" % CSS)
            fh.write("<div class=\"wrap\">\n")
            # ---- 头部 ----
            fh.write("<div class=\"head\">\n<h1>%s</h1>\n" % esc(title))
            fh.write("<div class=\"meta\">")
            fh.write("<span>类型：%s</span>" % esc(chat_type_label(chat)))
            fh.write("<span>消息数：%d</span>" % chat.count)
            fh.write("<span>时间范围：%s</span>" % esc(time_range_text(chat)))
            fh.write("</div>\n</div>\n")
            # ---- 消息 ----
            fh.write("<div class=\"body\">\n")
            current_date = None
            for msg in chat.messages:
                if msg.date_text != current_date:
                    current_date = msg.date_text
                    fh.write("<div class=\"date-sep\"><span>%s</span></div>\n" % esc(current_date))
                fh.write(self._message_html(chat, msg))
            if not chat.messages:
                fh.write("<div class=\"date-sep\"><span>暂无消息</span></div>\n")
            fh.write("</div>\n")
            fh.write("<div class=\"foot\">由 %s 导出</div>\n" % esc(APP_NAME))
            fh.write("</div>\n</body>\n</html>\n")

    @staticmethod
    def _message_html(chat: Chat, msg) -> str:
        """一条消息的 HTML 片段。"""
        mine = bool(getattr(msg, "is_mine", False)) or getattr(msg, "direction", 0) == 1
        is_sys = getattr(msg, "msg_type", 0) == 17
        if is_sys:
            row_cls = "row sys"
            avatar = "系"
        elif mine:
            row_cls = "row mine"
            avatar = "我"
        else:
            row_cls = "row"
            avatar = (sender_label(msg) or "对")[:1]
        name = sender_label(msg)
        parts = [
            '<div class="%s">' % row_cls,
            '<div class="avatar">%s</div>' % esc(avatar),
            '<div class="bubble">',
        ]
        # 群聊显示昵称；单聊对方不重复显示名字
        if getattr(chat, "is_group", False) or mine or is_sys:
            parts.append('<div class="name">%s</div>' % esc(name))
        body = message_text(msg)
        parts.append('<div class="text">%s</div>' % (esc(body) if body else ""))
        atts = getattr(msg, "attachments", None) or []
        if atts:
            parts.append('<div class="attach">')
            for att in atts:
                parts.append(
                    '<div class="file-box"><span class="ico">%s</span>'
                    '<span class="fname">%s</span><br>'
                    '<span class="fsize">%s</span></div>'
                    % (
                        esc(_kind_cn(att)),
                        esc(att.name or "未命名文件"),
                        esc(human_size(att.size)),
                    )
                )
            parts.append("</div>")
        parts.append('<div class="time">%s</div>' % esc(msg.time_text))
        parts.append("</div></div>")
        return "".join(parts)

    # -- index ------------------------------------------------------------
    def _write_index(self, rows: Iterable[IndexRow], path: str) -> None:
        total_msgs = sum(row.count for row in rows)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n")
            fh.write("<meta charset=\"utf-8\">\n")
            fh.write("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n")
            fh.write("<title>QQ聊天记录导出 - 会话清单</title>\n")
            fh.write("<style>%s</style>\n</head>\n<body>\n" % CSS)
            fh.write("<div class=\"wrap\">\n")
            fh.write("<div class=\"head\"><h1>QQ 聊天记录导出</h1>")
            fh.write("<div class=\"meta\">")
            fh.write("<span>会话数：%d</span>" % len(rows))
            fh.write("<span>消息总数：%d</span>" % total_msgs)
            fh.write("</div></div>\n")
            fh.write("<div class=\"body\">\n")
            fh.write("<table class=\"list\">\n<tr><th>#</th><th>会话</th><th>类型</th>")
            fh.write("<th class=\"num\">消息数</th><th>时间范围</th><th>文件</th></tr>\n")
            for i, row in enumerate(rows, start=1):
                name = row.file_name
                fh.write(
                    "<tr><td>%d</td><td>%s</td><td>%s</td><td class=\"num\">%d</td>"
                    "<td>%s</td><td><a href=\"%s\">%s</a></td></tr>\n"
                    % (
                        i,
                        esc(row.name),
                        esc(row.type_label),
                        row.count,
                        esc(row.time_range),
                        esc(quote_filename(name)),
                        esc(name),
                    )
                )
            fh.write("</table>\n</div>\n")
            fh.write("<div class=\"foot\">由 %s 导出</div>\n" % esc(APP_NAME))
            fh.write("</div>\n</body>\n</html>\n")


def _kind_cn(att) -> str:
    """附件类型中文标签（HTML 占位框用）。"""
    from .base import ATTACHMENT_KIND_LABELS

    return ATTACHMENT_KIND_LABELS.get(str(getattr(att, "kind", "") or "").lower(), "文件")
