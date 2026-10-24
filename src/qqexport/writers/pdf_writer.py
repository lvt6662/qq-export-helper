# -*- coding: utf-8 -*-
"""PDF 导出器：中文用 reportlab 内置 CJK 字体 STSong-Light，页面**流式**写盘。

依赖：reportlab（``pip install reportlab``）。缺少依赖时调用会抛
``qqexport.writers.base.PdfUnavailable``，不会让整个程序崩溃。

为什么不用 reportlab 的 ``canvas.Canvas``？
    reportlab 的 ``Canvas`` / ``PDFDocument`` 会把**整份文档**（每一页的内容流）
    留在内存里，直到 ``save()`` 时才一次性写盘 —— 见 ``reportlab/pdfbase/pdfdoc.py``
    的 ``PDFDocument.format()`` / ``SaveToFile``。全量导出时合订本有好几万页，
    那样内存会直接爆掉（页数 × 每页内容流）。

    所以这里换成「**写完一页立刻落盘**」：页面内容流用 Flate 压缩后马上写进文件，
    内存只跟「一页」有关，与消息总数无关（xref 偏移表每页 8 字节，可忽略）。

    绘制方式与 canvas 一致：逐条消息、逐行 ``Tj``，写满一页就翻页，
    每页有页眉（会话名）和页脚（页码）。**不使用** Platypus 的 flowable 列表
    （几十万条 Paragraph 会把内存吃光）。

字体：沿用 reportlab 自带的 Adobe CID 字体 ``STSong-Light``
（``reportlab.pdfbase.cidfonts.UnicodeCIDFont``），**不需要**额外字体文件。
  * 字体字典直接取 reportlab 的 ``CIDFontInfo`` 生成（与 reportlab 自己输出一致）；
  * 正文按 ``/UniGB-UCS2-H`` 编码写成 UTF-16BE 的十六进制串（``<...> Tj``）；
  * 行宽用 ``pdfmetrics.stringWidth`` 量算。

用法::

    from qqexport.writers.pdf_writer import write_chats
    files = write_chats(chats, r"D:\\QQ导出\\pdf")

产物：
  * ``qq_export.pdf`` —— 合订本：标题页（应用名/导出时间/统计/会话目录）
    + 每个会话单独分页 + 消息逐条（时间、发送者、内容），附件用文字描述。
  * ``<类型>_<会话名>.pdf`` —— 单会话 PDF：封面（会话名/类型/消息数/时间范围）
    + 该会话的消息列表。
  * ``index.txt`` —— 会话清单（哪个文件对应哪个会话）。

实现要点：
  * **边遍历边写**。``chats`` 可以是普通 ``list[Chat]``，也可以是
    :class:`qqexport.model.ChatStream` 之类的流式源（``metas()`` / ``__len__`` /
    ``__iter__``）—— 后者只能遍历一遍，所以单会话 PDF 与合订本在**同一次遍历**里写；
  * 单个会话写失败只记录到 ``self.errors``，不中断整体导出；
  * ``index.txt`` 在遍历结束后再写（遍历时只收集几十字节的元数据行）。
"""

from __future__ import annotations

import array
import datetime as _dt
import hashlib
import os
import re
import zlib
from typing import Iterable, NamedTuple

from ..model import Chat
from .base import (
    APP_NAME,
    BaseWriter,
    PdfUnavailable,
    ProgressFn,
    attachment_label,
    chat_display_name,
    chat_type_label,
    message_text,
    report_progress,
    sender_label,
    time_range_text,
)

try:  # pragma: no cover - 依赖探测
    from reportlab.lib.fonts import addMapping
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import cidfonts, pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.pdfdoc import PDFDocument
    from reportlab.pdfbase.pdfdoc import format as _pdf_format

    _HAS_REPORTLAB = True
except Exception:  # noqa: BLE001 - 任何导入异常都视为依赖不可用
    _HAS_REPORTLAB = False

FONT_NAME = "STSong-Light"
PDF_FILENAME = "qq_export.pdf"

# 字体是否已经注册过（含 font family 映射）
_font_ready = False

# --------------------------------------------------------------------------
# 版面（A4，单位 pt）
# --------------------------------------------------------------------------
PAGE_W, PAGE_H = float(A4[0]), float(A4[1])
MARGIN_L = 20 * 2.8346
MARGIN_R = 20 * 2.8346
MARGIN_T = 18 * 2.8346
MARGIN_B = 18 * 2.8346
BODY_W = PAGE_W - MARGIN_L - MARGIN_R  # 正文宽度
BODY_TOP = PAGE_H - MARGIN_T  # 正文第一行的基线
BODY_BOTTOM = MARGIN_B  # 正文最低基线（下面留给页脚）
HEADER_Y = PAGE_H - MARGIN_T + 13  # 页眉基线
FOOTER_Y = MARGIN_B - 22  # 页脚基线

# 字号 / 行距
FS_TITLE = 22.0
FS_SUB = 11.0
FS_H1 = 15.0
FS_META = 9.5
FS_HEAD = 9.0
FS_BODY = 10.5
FS_FOOT = 8.5
LEAD_TITLE = 30.0
LEAD_SUB = 18.0
LEAD_H1 = 22.0
LEAD_META = 14.0
LEAD_HEAD = 12.0
LEAD_BODY = 14.0

# 灰度（0 = 黑，1 = 白）
GRAY_TEXT = 0.0
GRAY_HEAD = 0.6
GRAY_META = 0.4

# 消息行样式：(字号, 灰度, 行距, 左缩进)
STYLE_HEAD = (FS_HEAD, GRAY_HEAD, LEAD_HEAD, 0.0)
STYLE_BODY = (FS_BODY, GRAY_TEXT, LEAD_BODY, 0.0)
STYLE_ATT = (FS_META, GRAY_META, LEAD_META, 14.0)

# 控制字符（正文里可能有）与非 BMP 字符（emoji，UCS-2 放不下）
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NONBMP_RE = re.compile(r"[\U00010000-\U0010FFFF]")


def _require_reportlab() -> None:
    """检查 reportlab 是否可用，不可用抛 PdfUnavailable。"""
    if not _HAS_REPORTLAB:
        raise PdfUnavailable("未安装 reportlab，无法导出 PDF（pip install reportlab）")


def _ensure_font() -> None:
    """注册 CJK 字体（只注册一次）。

    除了 ``pdfmetrics.registerFont``，还必须用 ``reportlab.lib.fonts.addMapping``
    把 CID 字体登记进 reportlab 的 font family 表：否则排版时会调用
    ``ps2tt()`` 去反查 family/bold/italic，直接抛
    ``ValueError: Can't map determine family/bold/italic for stsong-light``。
    这里把 bold/italic 四种组合都映射到同一个 STSong-Light（CID 字体没有粗斜体变体）。
    """
    global _font_ready
    if _font_ready:
        return
    try:
        if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))
        # 顺序：最后写入的 (0, 0) 作为 ps2tt 的默认返回值
        for bold in (1, 0):
            for italic in (1, 0):
                addMapping(FONT_NAME, bold, italic, FONT_NAME)
        _font_ready = True
    except Exception as exc:  # noqa: BLE001
        raise PdfUnavailable("无法注册中文 CJK 字体 %s：%s" % (FONT_NAME, exc))


# --------------------------------------------------------------------------
# 文本处理
# --------------------------------------------------------------------------
def _clean(text) -> str:
    """去掉控制字符；非 BMP 字符（emoji）换成 ``?``（UCS-2 编码放不下）。"""
    s = "" if text is None else str(text)
    if _CTRL_RE.search(s):
        s = _CTRL_RE.sub("", s)
    if _NONBMP_RE.search(s):
        s = _NONBMP_RE.sub("?", s)
    return s


# 字符宽度缓存（单位：千分之一 em）
_W1000: dict[str, float] = {}


def _char_w(ch: str) -> float:
    """单个字符的宽度（千分之一 em）。带缓存，避免每条消息都去问 reportlab。"""
    w = _W1000.get(ch)
    if w is None:
        try:
            w = float(pdfmetrics.stringWidth(ch, FONT_NAME, 1000))
        except Exception:  # noqa: BLE001 - 字体里没有的字符按全宽算
            w = 1000.0
        _W1000[ch] = w
    return w


def _wrap(text: str, size: float, max_width: float) -> list[str]:
    """按宽度折行：中文逐字断行，英文尽量在空格处断。

    返回行列表（不含换行符），空段落返回 ``[""]``。
    """
    limit = max_width * 1000.0 / size
    lines: list[str] = []
    for para in text.split("\n"):
        if not para:
            lines.append("")
            continue
        cur: list[str] = []
        cur_w = 0.0
        space_at = -1  # cur 里最后一个空格的下标
        for ch in para:
            w = _char_w(ch)
            if cur and cur_w + w > limit:
                if space_at >= 0 and (len(cur) - space_at) <= 12 and ch.isascii():
                    # 在最近的空格处断行（英文单词尽量不拆开）
                    lines.append("".join(cur[:space_at]).rstrip())
                    cur = cur[space_at + 1:]
                    cur_w = sum(_char_w(c) for c in cur)
                else:
                    lines.append("".join(cur).rstrip())
                    cur = []
                    cur_w = 0.0
                space_at = -1
            if ch == " ":
                space_at = len(cur)
            cur.append(ch)
            cur_w += w
        lines.append("".join(cur).rstrip())
    return lines


def _f(value: float) -> str:
    """PDF 里的数字格式。"""
    return "%.2f" % value


# --------------------------------------------------------------------------
# 字体字典
# --------------------------------------------------------------------------
# 兜底用的字体字典（拿不到 reportlab 的 CIDFontInfo 时用；结构与 reportlab 一致）
_FALLBACK_FONT_OBJECT = (
    b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H"
    b" /DescendantFonts [ << /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light"
    b" /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 0 >> /DW 1000"
    b" /FontDescriptor << /Type /FontDescriptor /FontName /STSongStd-Light /Flags 6"
    b" /FontBBox [-25 -254 1000 880] /ItalicAngle 0 /Ascent 752 /Descent -271"
    b" /CapHeight 737 /StemV 58 >> >> ] >>"
)

_font_object_cache: bytes | None = None


def _font_object() -> bytes:
    """STSong-Light 的字体字典（Type0 / UniGB-UCS2-H），只生成一次。

    直接取 reportlab 的 ``cidfonts.CIDFontInfo``，生成结果与 reportlab 自己
    输出的 PDF 里的字体字典一致（同样带 /W 宽度表）。
    """
    global _font_object_cache
    if _font_object_cache is None:
        data = b""
        try:
            info = dict(cidfonts.CIDFontInfo["STSong-Light"])
            info["Name"] = "/F1"
            info["Encoding"] = "/UniGB-UCS2-H"
            data = _pdf_format(cidfonts.structToPDF(info), PDFDocument())
        except Exception:  # noqa: BLE001 - reportlab 内部结构变了就用兜底字典
            data = b""
        _font_object_cache = data or _FALLBACK_FONT_OBJECT
    return _font_object_cache


def _pdf_string(text: str) -> bytes:
    """PDF 字符串：Unicode 文本写成 UTF-16BE 十六进制串（带 BOM）。"""
    if not text:
        return b"()"
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


# --------------------------------------------------------------------------
# 流式 PDF 写盘器
# --------------------------------------------------------------------------
class _StreamPdf:
    """边画边写盘的 PDF。

    * 内容流按页生成，**写完一页立刻落盘**（Flate 压缩），内存只跟「一页」有关；
    * 只有 xref 偏移表随页数增长（每页 8 字节），与消息总数无关；
    * 对象编号固定：1=Catalog 2=Pages 3=字体 4=Info，5 起每页两个对象
      （内容流 + 页对象）。
    """

    def __init__(self, path: str, title: str = "", author: str = APP_NAME) -> None:
        self.path = path
        self._fh = open(path, "wb")
        self._fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        self._offsets = array.array("q", [0, 0, 0, 0, 0])  # 下标 = 对象号
        self._kids = array.array("i")
        self._next = 5
        self._ops: list[str] = []
        self._run_key = None
        self._run_x = 0.0
        self._run_y = 0.0
        self._page_no = 0
        self._title = title
        self._author = author
        self._closed = False
        #: 下一行的基线 y（由调用方推进）
        self.y = BODY_TOP

    # -- 画布 -------------------------------------------------------------
    def new_page(self, header: str = "") -> None:
        """翻到新的一页：写页眉（会话名）和页脚（页码）。"""
        self._flush_page()
        self._page_no += 1
        self._ops = []
        self._run_key = None
        self.y = BODY_TOP
        if header:
            self.text(MARGIN_L, HEADER_Y, header, FS_FOOT, GRAY_HEAD)
        self.text(MARGIN_L, FOOTER_Y, APP_NAME, FS_FOOT, GRAY_HEAD)
        self.text(PAGE_W / 2.0, FOOTER_Y, "第 %d 页" % self._page_no, FS_FOOT, GRAY_HEAD, center=True)

    def text(
        self,
        x: float,
        y: float,
        s: str,
        size: float = FS_BODY,
        gray: float = GRAY_TEXT,
        center: bool = False,
    ) -> None:
        """在 (x, y) 处写一行文字（基线对齐）。"""
        if not s:
            return
        if center:
            x -= pdfmetrics.stringWidth(s, FONT_NAME, size) / 2.0
        key = (size, gray)
        if self._run_key != key:
            if self._run_key is not None:
                self._ops.append("ET")
            self._ops.append(
                "BT /F1 %s Tf %s %s %s rg 1 0 0 1 %s %s Tm"
                % (_f(size), _f(gray), _f(gray), _f(gray), _f(x), _f(y))
            )
            self._run_key = key
        else:
            # 同一段文字里用相对位移，省得每行都重写矩阵
            self._ops.append("%s %s Td" % (_f(x - self._run_x), _f(y - self._run_y)))
        self._run_x, self._run_y = x, y
        self._ops.append("<%s> Tj" % s.encode("utf-16-be").hex())

    def has_room(self, lead: float) -> bool:
        """当前页还放得下一行（行距 lead）吗？"""
        return self.y - lead >= BODY_BOTTOM

    # -- 落盘 -------------------------------------------------------------
    def _flush_page(self) -> None:
        """把当前页的内容流写进文件。"""
        if not self._ops:
            return
        if self._run_key is not None:
            self._ops.append("ET")
            self._run_key = None
        data = zlib.compress("\n".join(self._ops).encode("latin1", "replace"), 6)
        num = self._next
        self._offsets.append(self._fh.tell())
        self._fh.write(b"%d 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % (num, len(data)))
        self._fh.write(data)
        self._fh.write(b"\nendstream\nendobj\n")
        self._next += 1
        self._kids.append(self._next)
        self._offsets.append(self._fh.tell())
        self._fh.write(
            b"%d 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.4f %.4f]"
            b" /Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>\nendobj\n"
            % (self._next, PAGE_W, PAGE_H, num)
        )
        self._next += 1
        self._ops = []

    def _write_obj(self, num: int, data: bytes) -> None:
        self._offsets[num] = self._fh.tell()
        self._fh.write(b"%d 0 obj\n" % num + data + b"\nendobj\n")

    def close(self) -> None:
        """写完 xref / trailer 并关闭文件。"""
        if self._closed:
            return
        self._flush_page()
        self._write_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        self._write_obj(
            2,
            b"<< /Type /Pages /Kids ["
            + b" ".join(b"%d 0 R" % k for k in self._kids)
            + b"] /Count %d >>" % len(self._kids),
        )
        self._write_obj(3, _font_object())
        self._write_obj(
            4,
            b"<< /Title "
            + _pdf_string(self._title)
            + b" /Author "
            + _pdf_string(self._author)
            + b" /Producer "
            + _pdf_string(APP_NAME + " PDF 导出器")
            + b" /CreationDate ("
            + _dt.datetime.now().strftime("D:%Y%m%d%H%M%S").encode("ascii")
            + b") >>",
        )
        size = self._next
        xref = self._fh.tell()
        self._fh.write(b"xref\n0 %d\n" % size)
        self._fh.write(b"0000000000 65535 f \n")
        for i in range(1, size):
            self._fh.write(b"%010d 00000 n \n" % self._offsets[i])
        digest = hashlib.md5(
            ("%s|%s|%s" % (self._title, self._author, self.path)).encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        self._fh.write(
            b"trailer\n<< /Size %d /Root 1 0 R /Info 4 0 R /ID [<%s><%s>] >>\n"
            b"startxref\n%d\n%%%%EOF\n" % (size, digest.encode("ascii"), digest.encode("ascii"), xref)
        )
        self._fh.close()
        self._closed = True

    def abort(self) -> None:
        """写失败时丢弃半成品。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._fh.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.remove(self.path)
        except OSError:
            pass

    # -- 上下文管理 -------------------------------------------------------
    def __enter__(self) -> "_StreamPdf":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.close()
        else:
            self.abort()
        return False


# --------------------------------------------------------------------------
# 消息 -> 行
# --------------------------------------------------------------------------
def _message_lines(msg) -> list[tuple[str, str]]:
    """一条消息排成若干行：``[(样式名, 文本), ...]``。"""
    lines = [("head", "[%s] %s" % (msg.time_text, sender_label(msg)))]
    body = _clean(message_text(msg))
    if body:
        for ln in _wrap(body, FS_BODY, BODY_W):
            lines.append(("body", ln))
    else:
        lines.append(("body", "（空消息）"))
    for att in getattr(msg, "attachments", None) or []:
        lines.append(("att", "附件：%s" % _clean(attachment_label(att))))
    return lines


def _chat_count(chat) -> int:
    """会话消息条数：消息没载入时用库里的总条数。"""
    try:
        msgs = getattr(chat, "messages", None)
    except Exception:  # noqa: BLE001
        msgs = None
    if msgs:
        return len(msgs)
    try:
        return int(getattr(chat, "total_count", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _chat_time_range(chat) -> str:
    """会话时间范围：消息没载入时用库里的首/末时间。"""
    try:
        msgs = getattr(chat, "messages", None)
    except Exception:  # noqa: BLE001
        msgs = None
    if msgs:
        return time_range_text(chat)
    try:
        first = int(getattr(chat, "first_ts", 0) or 0)
        last = int(getattr(chat, "last_ts", 0) or 0)
    except Exception:  # noqa: BLE001
        return "—"
    if not first and not last:
        return "—"
    try:
        f = _dt.datetime.fromtimestamp(first).strftime("%Y-%m-%d %H:%M:%S")
        l = _dt.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "—"
    return f if f == l else "%s ~ %s" % (f, l)


def _total_chats(chats) -> int:
    """会话总数（流式源也支持）；拿不到长度时返回 0。"""
    try:
        return len(chats)
    except (TypeError, AttributeError):
        return 0


def _metas_of(chats) -> list:
    """会话元数据（只有名字/条数/时间范围，没有消息体）。

    流式源用它的 ``metas()``；普通列表直接用列表本身（**不**复制消息体）。
    """
    getter = getattr(chats, "metas", None)
    if callable(getter):
        return getter()
    if isinstance(chats, (list, tuple)):
        return chats
    return list(chats)


class IndexRow(NamedTuple):
    """index.txt 里的一行（只有元数据，不持有 Chat 对象）。"""

    name: str
    type_label: str
    count: int
    time_range: str
    file_name: str


def _index_row(chat, path: str) -> IndexRow:
    """把会话的关键信息压成一行元数据（之后就可以把 Chat 释放掉）。"""
    return IndexRow(
        name=chat_display_name(chat),
        type_label=chat_type_label(chat),
        count=_chat_count(chat),
        time_range=_chat_time_range(chat),
        file_name=os.path.basename(path),
    )


def write_chats(chats: Iterable[Chat], out_dir: str, progress: ProgressFn | None = None) -> list[str]:
    """把会话写成 PDF 文件，返回写出的文件路径列表。"""
    _require_reportlab()
    _ensure_font()
    return PdfWriter(out_dir).write(chats, progress)


class PdfWriter(BaseWriter):
    """PDF writer 实现（流式：边遍历边写，内存与消息总数无关）。"""

    fmt = "pdf"
    ext = ".pdf"

    def write(self, chats: Iterable[Chat], progress: ProgressFn | None = None) -> list[str]:
        # 依赖与中文字体在此处再确认一次：write_export() 走的是本类，不经过模块级 write_chats()
        _require_reportlab()
        _ensure_font()
        self.prepare_names(chats)  # 预留原始主名：同名会话加 _2/_3，不互相覆盖
        total = _total_chats(chats)
        total_out = (total + 2) if total else 0  # +1 合订本 +1 index
        done = 0

        # 会话元数据（标题页的会话目录用；流式源只读元数据，不碰消息）
        metas = _metas_of(chats)

        # 只收集元数据行（几百行，量很小），不持有 Chat 对象
        rows: list[IndexRow] = []

        # 1) 合订本 + 每会话 PDF：**同一次遍历**里一起写
        #    （流式源只能遍历一遍，不能先写合订本再写单会话）
        combined_path = os.path.join(self.out_dir, PDF_FILENAME)
        combined: _StreamPdf | None = None
        try:
            combined = _StreamPdf(
                combined_path, title="%s 聊天记录导出" % APP_NAME, author=APP_NAME
            )
            try:
                self._write_title_page(combined, metas)
            except Exception as exc:  # noqa: BLE001 - 标题页失败不影响正文
                self.errors.append("合订本标题页生成失败：%s" % exc)
            for chat in chats:
                name = chat_display_name(chat)
                path = ""
                try:
                    path = self.chat_path(chat)
                    # 每会话一个 PDF
                    if self.safe_write(chat, path, self._write_single):
                        rows.append(_index_row(chat, path))
                    # 同一个会话也写进合订本（每会话单独分页）
                    try:
                        combined.new_page(name)
                        self._write_chat_body(combined, chat, name)
                    except Exception as exc:  # noqa: BLE001 - 单个会话失败不影响整体
                        self.errors.append("会话「%s」写入合订本失败：%s" % (name, exc))
                        combined.new_page(name)  # 丢掉半页，重新起一页
                except Exception as exc:  # noqa: BLE001 - 单个会话失败不影响整体
                    self.errors.append("会话「%s」写入失败：%s" % (name, exc))
                done += 1
                report_progress(
                    progress, done, total_out or done, "已导出 %s" % (os.path.basename(path) if path else name)
                )
                chat = None  # 写完立刻放手，别让大群的百万条消息一直挂在内存里
            combined.close()
            self.register(combined_path)
            self.files.insert(0, self.files.pop())  # 合订本排在最前面
            done += 1
            report_progress(progress, done, total_out or done, "已导出 %s" % PDF_FILENAME)
        except Exception as exc:  # noqa: BLE001 - 合订本失败不影响单会话 PDF
            self.errors.append("合订本 %s 生成失败：%s" % (PDF_FILENAME, exc))
            if combined is not None:
                combined.abort()

        # 2) index.txt —— 会话清单（遍历完之后再写）
        index = os.path.join(self.out_dir, "index.txt")
        self._write_index(rows, index)
        self.register(index)
        done += 1
        report_progress(progress, done, total_out or done, "已导出 %s" % os.path.basename(index))
        return self.files

    # -- 合订本标题页 -----------------------------------------------------
    def _write_title_page(self, pdf: _StreamPdf, metas) -> None:
        """标题页：应用名 / 导出时间 / 统计 + 会话目录。"""
        pdf.new_page("QQ 聊天记录导出")
        total_msgs = 0
        for meta in metas:
            total_msgs += _chat_count(meta)
        pdf.y = BODY_TOP - 70
        pdf.text(PAGE_W / 2.0, pdf.y, "QQ 聊天记录导出", FS_TITLE, GRAY_TEXT, center=True)
        pdf.y -= LEAD_SUB + 16
        pdf.text(PAGE_W / 2.0, pdf.y, "由 %s 生成" % APP_NAME, FS_SUB, GRAY_META, center=True)
        pdf.y -= LEAD_SUB + 12
        for line in (
            "导出时间：%s" % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "会话数量：%d" % len(metas),
            "消息总数：%d" % total_msgs,
        ):
            pdf.text(PAGE_W / 2.0, pdf.y, line, FS_SUB, GRAY_META, center=True)
            pdf.y -= LEAD_SUB

        # 会话目录
        pdf.y -= 12
        pdf.text(MARGIN_L, pdf.y, "会话目录", FS_H1, GRAY_TEXT)
        pdf.y -= LEAD_H1 + 4
        for i, meta in enumerate(metas, start=1):
            line = "%d. %s %s    消息数 %d    %s" % (
                i,
                chat_type_label(meta),
                chat_display_name(meta),
                _chat_count(meta),
                _chat_time_range(meta),
            )
            for ln in _wrap(_clean(line), FS_META, BODY_W):
                if not pdf.has_room(LEAD_META):
                    pdf.new_page("QQ 聊天记录导出")
                pdf.text(MARGIN_L, pdf.y, ln, FS_META, GRAY_META)
                pdf.y -= LEAD_META

    # -- 单会话 -----------------------------------------------------------
    def _write_single(self, chat: Chat, path: str) -> None:
        """写一个会话的独立 PDF（封面 + 消息）。"""
        title = "%s %s" % (chat_type_label(chat), chat_display_name(chat))
        pdf = _StreamPdf(path, title=title, author=APP_NAME)
        try:
            # 封面
            pdf.new_page(title)
            pdf.y = BODY_TOP - 110
            pdf.text(PAGE_W / 2.0, pdf.y, title, FS_TITLE, GRAY_TEXT, center=True)
            pdf.y -= LEAD_SUB + 18
            for line in (
                "类型：%s" % chat_type_label(chat),
                "消息数：%d" % _chat_count(chat),
                "时间范围：%s" % _chat_time_range(chat),
                "导出时间：%s" % _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ):
                pdf.text(PAGE_W / 2.0, pdf.y, line, FS_SUB, GRAY_META, center=True)
                pdf.y -= LEAD_SUB
            # 正文
            pdf.new_page(title)
            self._write_chat_body(pdf, chat, title)
            pdf.close()
        except Exception:
            pdf.abort()
            raise

    # -- 会话正文 ---------------------------------------------------------
    def _write_chat_body(self, pdf: _StreamPdf, chat: Chat, header: str) -> None:
        """把一个会话的消息逐条写进 pdf（写满一页自动翻页）。"""
        pdf.text(MARGIN_L, pdf.y, "%s %s" % (chat_type_label(chat), chat_display_name(chat)), FS_H1, GRAY_TEXT)
        pdf.y -= LEAD_H1
        pdf.text(
            MARGIN_L,
            pdf.y,
            "类型：%s    消息数：%d    时间范围：%s"
            % (chat_type_label(chat), _chat_count(chat), _chat_time_range(chat)),
            FS_META,
            GRAY_META,
        )
        pdf.y -= LEAD_META + 6
        try:
            messages = getattr(chat, "messages", None) or []
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("会话消息读取失败：%s" % exc)
        if not messages:
            pdf.text(MARGIN_L, pdf.y, "（本会话暂无消息）", FS_BODY, GRAY_TEXT)
            pdf.y -= LEAD_BODY
            return
        for msg in messages:
            for kind, text in _message_lines(msg):
                size, gray, lead, indent = {
                    "head": STYLE_HEAD,
                    "body": STYLE_BODY,
                    "att": STYLE_ATT,
                }[kind]
                if not pdf.has_room(lead):
                    pdf.new_page(header)
                pdf.text(MARGIN_L + indent, pdf.y, text, size, gray)
                pdf.y -= lead
            pdf.y -= 5  # 两条消息之间留一点白

    # -- index ------------------------------------------------------------
    def _write_index(self, rows: Iterable[IndexRow], path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("QQ 聊天记录导出 - 会话清单\n")
            fh.write("=" * 60 + "\n")
            fh.write("合订本：%s\n" % PDF_FILENAME)
            fh.write("会话数：%d\n\n" % len(rows))
            for i, row in enumerate(rows, start=1):
                fh.write("%d. %s %s\n" % (i, row.type_label, row.name))
                fh.write("   消息数：%d\n" % row.count)
                fh.write("   时间范围：%s\n" % row.time_range)
                fh.write("   文件：%s\n\n" % row.file_name)
