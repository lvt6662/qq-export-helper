# -*- coding: utf-8 -*-
"""导出器公共基础设施：文件名清洗、体积格式化、消息文本规范化与基类。

依赖：仅标准库（无第三方依赖）。7 种 writer 共用本模块。

用法::

    from qqexport.writers.base import human_size, sanitize_filename
    human_size(12900)            # -> '12.6 KB'
    sanitize_filename('a/b:c*?')  # -> 'abc'  （Windows 非法字符被去掉）

设计要点：
  * 文件名清洗规则与 Windows 一致：非法字符 ``\\ / : * ? " < > |`` 去掉、
    控制字符去掉、末尾空格与点去掉、空名回退 ``未命名会话``、
    超长（>120 字符）截断并追加短 hash 后缀避免重名覆盖。
  * 同名会话（同名同类型）由 ``NameAllocator`` 依次追加 ``_2``、``_3`` 后缀，
    并预留所有会话的原始主名，保证「张三 / 张三 / 张三_2」这类组合互不覆盖。
  * 所有 writer 都不把全部消息拼成巨型字符串，统一按「逐条写出」的方式处理，
    以便支持上万条消息的大会话。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Callable, Iterable, Sequence
from urllib.parse import quote

from ..model import (
    MSG_TYPE_REPLY,
    MSG_TYPE_SYS,
    Attachment,
    Chat,
    ExportResult,
    Message,
)

__all__ = [
    "APP_NAME",
    "EXPORT_VERSION",
    "MAX_NAME_LEN",
    "ProgressFn",
    "WriterError",
    "PdfUnavailable",
    "ExcelUnavailable",
    "human_size",
    "short_hash",
    "sanitize_filename",
    "excel_sheet_title",
    "quote_filename",
    "chat_type_label",
    "chat_display_name",
    "file_stem",
    "NameAllocator",
    "message_text",
    "single_line",
    "sender_label",
    "direction_label",
    "attachment_label",
    "attachments_text",
    "is_reply",
    "time_range_text",
    "ensure_dir",
    "BaseWriter",
    "report_progress",
]

# 应用名 / 导出结构版本（JSON 导出里的 app、version 字段）
APP_NAME = "QQ导出助手"
EXPORT_VERSION = 1

# 文件名最大长度（不含扩展名）
MAX_NAME_LEN = 120

# progress 回调签名: progress(done: int, total: int, msg: str)
ProgressFn = Callable[[int, int, str], None]

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# 附件类型 -> 中文标签
ATTACHMENT_KIND_LABELS = {
    "image": "图片",
    "video": "视频",
    "file": "文件",
    "audio": "语音",
    "sticker": "表情",
    "unknown": "未知",
}


class WriterError(Exception):
    """所有导出相关异常的基类。"""


class PdfUnavailable(WriterError):
    """reportlab 不可用（或 CJK 字体注册失败），无法导出 PDF。"""


class ExcelUnavailable(WriterError):
    """openpyxl 不可用，无法导出 Excel。"""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def human_size(size) -> str:
    """把字节数格式化成人类可读文本，如 12900 -> ``12.6 KB``。"""
    try:
        n = float(size or 0)
    except (TypeError, ValueError):
        return "0 B"
    if n < 0:
        n = 0.0
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    while n >= 1024.0 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    if idx == 0:
        return "%d B" % int(n)
    return "%.1f %s" % (n, units[idx])


def short_hash(text, length: int = 8) -> str:
    """文本的短 sha1 摘要（默认 8 位十六进制），用于文件名去重后缀。"""
    return hashlib.sha1(str(text).encode("utf-8", "replace")).hexdigest()[:length]


def sanitize_filename(name, fallback: str = "未命名会话", max_len: int = MAX_NAME_LEN) -> str:
    """把任意字符串清洗成安全的 Windows 文件名（不含扩展名）。

    规则：去掉非法字符 ``\\ / : * ? " < > |`` 与控制字符；去掉首尾空白；
    去掉末尾的空格与点；空名回退 ``fallback``；超过 ``max_len`` 截断并追加
    短 hash，避免长名截断后互相覆盖。
    """
    s = "" if name is None else str(name)
    s = _CONTROL_CHARS.sub("", s)
    s = _ILLEGAL_CHARS.sub("", s)
    s = s.strip().rstrip(" .")
    if not s:
        s = fallback
    if len(s) > max_len:
        h = short_hash(s)
        cut = max(0, max_len - len(h) - 1)
        s = s[:cut] + "_" + h
    return s


def _clip_utf16(text: str, max_units: int) -> str:
    """按 UTF-16 码元数截断（Excel 的 31 字符上限按 UTF-16 码元算）。

    emoji 等 BMP 外字符在 Excel 里占 2 个字符，只按 Python ``len()`` 截断
    会让「16 个 emoji」这种群名生成 32 字符的 sheet 名，Excel 打开时报错。
    """
    raw = text.encode("utf-16-le")
    if len(raw) <= max_units * 2:
        return text
    return raw[: max_units * 2].decode("utf-16-le", "ignore")


def excel_sheet_title(name, fallback: str = "未命名会话", max_len: int = 31) -> str:
    """生成合法的 Excel 工作表名。

    规则（Excel 的硬性限制）：去掉 ``[]:*?/\\`` 与控制字符；不能以单引号
    开头或结尾；不超过 ``max_len`` 个字符（按 UTF-16 码元计）；不能是保留名
    ``History``。截断之后再补一次去首尾单引号，避免「第 31 个字符恰好是
    单引号」这种截断后才出现的非法名（openpyxl 不校验，Excel 打开时会报错）。
    """
    s = "" if name is None else str(name)
    s = _CONTROL_CHARS.sub("", s)
    s = re.sub(r"[\[\]:*?/\\]", "", s)
    s = s.strip().strip("'").strip()
    if not s:
        s = fallback
    s = _clip_utf16(s, max_len).rstrip("'").rstrip()
    if not s:
        s = _clip_utf16(fallback, max_len).rstrip("'").rstrip() or "Sheet"
    if s.lower() == "history":  # Excel 保留名（用于修订记录），不能建同名 sheet
        s = _clip_utf16(s + "_", max_len)
    return s


def quote_filename(name) -> str:
    """把文件名编码成相对链接（percent-encode 特殊字符）。

    只把空格换成 ``%20`` 是不够的：``#`` 会被浏览器当成锚点、``%`` 会开启
    非法转义、``(``/``)`` 会提前截断 Markdown 链接、``&`` 会被 HTML 转义。
    统一用 :func:`urllib.parse.quote` 全量编码，index 里的链接才可靠。
    """
    return quote(str(name or ""), safe="")


def chat_type_label(chat: Chat) -> str:
    """会话类型中文标签：群聊 / 单聊。"""
    return "群聊" if getattr(chat, "is_group", False) else "单聊"


def chat_display_name(chat: Chat) -> str:
    """会话显示名：优先 name，其次 peer，最后 chat_id。"""
    name = (getattr(chat, "name", "") or "").strip()
    if name:
        return name
    peer = (getattr(chat, "peer", "") or "").strip()
    if peer:
        return peer
    return (getattr(chat, "chat_id", "") or "").strip() or "未命名会话"


def file_stem(chat: Chat) -> str:
    """会话文件主名：``<单聊|群聊>_<会话名>``，自带清洗。"""
    return "%s_%s" % (chat_type_label(chat), sanitize_filename(chat_display_name(chat)))


class NameAllocator:
    """同一目录内的文件名分配器，保证不重名、不互相覆盖。

    规则：
      * 第一次出现的主名原样使用（例如 ``单聊_张三``）；
      * 重名时依次追加 ``_2``、``_3`` 后缀（``单聊_张三_2``、``单聊_张三_3``）；
      * ``reserved`` 是「预留名」集合：所有会话的原始主名都会先被预留，
        这样「张三」的重名后缀 ``_2`` 不会撞上另一个本来就叫「张三_2」的会话。
    """

    def __init__(self, reserved=None) -> None:
        self._used: set[str] = set()
        self._reserved: set[str] = set()
        if reserved:
            self.reserve_many(reserved)

    # -- 预留 ---------------------------------------------------------------
    def reserve(self, *names: str) -> None:
        """预留若干主名（不可被重名后缀占用）。"""
        for name in names:
            if name:
                self._reserved.add(str(name).lower())

    def reserve_many(self, stems) -> None:
        """预留一批主名。"""
        for stem in stems or ():
            if stem:
                self._reserved.add(str(stem).lower())

    # -- 分配 ---------------------------------------------------------------
    def allocate(self, stem: str, key: str = "") -> str:
        """分配一个唯一文件名主名（不含扩展名）。

        ``key`` 仅用于调试/扩展预留，保持向后兼容，当前实现按分配顺序编号。
        """
        base = stem
        if base.lower() in self._used:
            counter = 2
            while True:
                candidate = "%s_%d" % (stem, counter)
                low = candidate.lower()
                if low not in self._used and low not in self._reserved:
                    base = candidate
                    break
                counter += 1
        self._used.add(base.lower())
        return base

    def reset(self) -> None:
        self._used.clear()


# --------------------------------------------------------------------------
# 消息文本规范化
# --------------------------------------------------------------------------
def message_text(msg: Message) -> str:
    """消息正文：优先完整内容 content，回退摘要 text。"""
    text = (getattr(msg, "content", "") or "").strip()
    if not text:
        text = (getattr(msg, "text", "") or "").strip()
    return text


def single_line(text) -> str:
    """把多行文本压成一行（换行 -> ``⏎``），用于 TXT/CSV 等单行场景。"""
    if not text:
        return ""
    return (
        str(text)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", " ⏎ ")
    )


def sender_label(msg: Message) -> str:
    """发送者显示名：我 / 对方昵称 / 系统。"""
    if getattr(msg, "msg_type", 0) == MSG_TYPE_SYS:
        return "系统"
    if getattr(msg, "is_mine", False):
        return "我"
    if getattr(msg, "direction", 0) == 1:  # DIRECTION_OUT
        return "我"
    name = (getattr(msg, "sender", "") or "").strip()
    return name or "对方"


def direction_label(msg: Message) -> str:
    """方向文字：我发出 / 对方发出。"""
    if getattr(msg, "is_mine", False) or getattr(msg, "direction", 0) == 1:
        return "我发出"
    return "对方发出"


def attachment_label(att: Attachment) -> str:
    """附件描述，如 ``[图片] 照片.png (12.6 KB)``。"""
    kind = ATTACHMENT_KIND_LABELS.get(str(getattr(att, "kind", "") or "").lower(), "文件")
    name = (getattr(att, "name", "") or "").strip() or "未命名文件"
    size = getattr(att, "size", 0) or 0
    return "[%s] %s (%s)" % (kind, name, human_size(size))


def attachments_text(msg: Message) -> str:
    """一条消息的所有附件描述，分号连接。"""
    atts = getattr(msg, "attachments", None) or []
    return "; ".join(attachment_label(a) for a in atts)


def is_reply(msg: Message) -> bool:
    """是否引用/回复类消息（msg_type == 5）。"""
    return getattr(msg, "msg_type", 0) == MSG_TYPE_REPLY


def time_range_text(chat: Chat) -> str:
    """会话时间范围文字；空会话返回 ``—``。"""
    msgs = getattr(chat, "messages", None) or []
    if not msgs:
        return "—"
    # 消息是按**消息 ID** 排序的，不是按时间 —— 首尾消息的时间戳未必是
    # 这个会话里最早/最晚的（个别消息的时间戳是 0，会显示成 1970 年）。
    # 所以优先用库里聚合出来的 first_ts / last_ts。
    first = chat.first_time
    last = chat.last_time
    first_text = msgs[0].time_text if not first else _fmt_ts(first)
    last_text = msgs[-1].time_text if not last else _fmt_ts(last)
    return first_text if first_text == last_text else "%s ~ %s" % (first_text, last_text)


def _fmt_ts(ts: int) -> str:
    """时间戳 -> ``YYYY-MM-DD HH:MM:SS``。"""
    import datetime as _dt

    try:
        return _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def ensure_dir(path: str) -> str:
    """确保目录存在，返回绝对路径。"""
    if path:
        os.makedirs(path, exist_ok=True)
    return os.path.abspath(path)


def report_progress(progress: ProgressFn | None, done: int, total: int, msg: str) -> None:
    """安全调用进度回调（回调异常不打断导出）。"""
    if progress is None:
        return
    try:
        progress(int(done), int(total), str(msg))
    except Exception:  # noqa: BLE001 - 进度回调不应影响导出结果
        pass


class BaseWriter:
    """所有 writer 的基类：负责输出目录、文件名分配与进度上报。"""

    fmt: str = ""
    ext: str = ""

    def __init__(self, out_dir: str) -> None:
        self.out_dir = ensure_dir(out_dir)
        self.allocator = NameAllocator()
        self.files: list[str] = []
        # 单个会话写失败的错误信息（不中断整体导出）
        self.errors: list[str] = []

    # -- 路径 -------------------------------------------------------------
    def prepare_names(self, chats) -> None:
        """预留所有会话的原始主名，避免重名后缀撞上别的会话。

        例如同时存在「张三」「张三」「张三_2」三个会话时：
        张三 -> ``单聊_张三``、张三 -> ``单聊_张三_3``、张三_2 -> ``单聊_张三_2``，
        三者互不覆盖。

        ``chats`` 可以是普通的会话列表，也可以是
        :class:`qqexport.model.ChatStream`（流式源）。后者会用它的
        **元数据**来预留名字 —— 否则会把流消费掉，消息就没了。
        """
        metas = getattr(chats, "metas", None)
        names = metas() if callable(metas) else chats
        self.allocator.reserve_many(file_stem(c) for c in (names or ()))

    def reserve_names(self, *names: str) -> None:
        """预留固定文件名（如 ``index``、``qq_export``），防止与会话文件冲突。"""
        self.allocator.reserve(*names)

    def chat_path(self, chat: Chat, ext: str | None = None) -> str:
        """为会话分配一个不重名的文件路径。"""
        stem = self.allocator.allocate(file_stem(chat), getattr(chat, "chat_id", "") or "")
        return os.path.join(self.out_dir, stem + (ext if ext is not None else self.ext))

    def index_path(self, name: str = "index") -> str:
        """index 文件路径。"""
        return os.path.join(self.out_dir, name + self.ext)

    def register(self, path: str) -> str:
        """登记已写出的文件。"""
        self.files.append(path)
        return path

    def safe_write(self, chat: Chat, path: str, func) -> bool:
        """写单个会话；失败只记录错误，不中断整体导出。

        ``func`` 签名为 ``func(chat, path) -> None``。
        """
        try:
            func(chat, path)
        except Exception as exc:  # noqa: BLE001 - 单会话失败不应中断整体导出
            self.errors.append(
                "会话「%s」写入失败(%s)：%s" % (chat_display_name(chat), os.path.basename(path), exc)
            )
            return False
        self.register(path)
        return True

    # -- 接口 -------------------------------------------------------------
    def write(self, chats: Iterable[Chat], progress: ProgressFn | None = None) -> list[str]:
        """把会话写盘，返回写出的文件路径列表。

        :param chats: 会话序列。**可以是迭代器** —— 写器应当**边读边写**，
            不要先把整个序列转成 list（全量导出时数据量是百万级）。
            文件名分配请用 :meth:`prepare_names` 预先做好。
        :param progress: 进度回调 ``progress(done, total, msg)``
        """
        raise NotImplementedError
