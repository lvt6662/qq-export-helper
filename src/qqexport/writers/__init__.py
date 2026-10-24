# -*- coding: utf-8 -*-
"""7 种导出格式的统一入口。

支持的格式（key -> 显示名）::

    FORMATS = {
        "txt": "TXT", "html": "HTML", "json": "JSON", "pdf": "PDF",
        "markdown": "Markdown", "csv": "CSV", "excel": "Excel",
    }

对外接口（UI 直接调用）::

    from qqexport.writers import write_export, available_formats, FORMATS, EXTENSIONS

    result = write_export("txt", chats, r"D:\\QQ导出", progress=on_progress)
    # result: model.ExportResult(path, fmt, chats, messages, ok, error)

行为约定：
  * 未知 ``fmt`` 抛 ``ValueError``；
  * PDF 需要 reportlab、Excel 需要 openpyxl，缺少依赖时抛
    ``PdfUnavailable`` / ``ExcelUnavailable``（不会让整个程序崩溃，UI 捕获即可）；
  * 单个会话写失败不会中断整体导出，失败信息会写进 ``ExportResult.error``；
  * 产物写在 ``out_dir/<格式key>/`` 目录下，**「合订本 + 每会话文件」两类并存**：
    合订本（Excel ``qq_export.xlsx``、PDF ``qq_export.pdf``、JSON ``qq_export.json``）
    与每会话一个文件（``<类型>_<会话名><扩展名>``，同名会话自动加 ``_2``/``_3``），
    多数格式另附一个 ``index.*`` 会话清单；
  * ``format_status()`` / ``format_outputs()`` 会说明每个格式到底产出哪几类文件，
    UI 可在导出前提示用户。
"""

from __future__ import annotations

import os
from typing import Sequence

from ..model import Chat, ExportResult
from .base import (
    APP_NAME,
    ExcelUnavailable,
    PdfUnavailable,
    WriterError,
    ensure_dir,
)

__all__ = [
    "FORMATS",
    "EXTENSIONS",
    "FORMAT_OUTPUTS",
    "available_formats",
    "format_status",
    "format_outputs",
    "format_output_desc",
    "is_available",
    "write_export",
    "WriterError",
    "PdfUnavailable",
    "ExcelUnavailable",
]

# key=格式标识, value=显示名
FORMATS: dict[str, str] = {
    "txt": "TXT",
    "html": "HTML",
    "json": "JSON",
    "pdf": "PDF",
    "markdown": "Markdown",
    "csv": "CSV",
    "excel": "Excel",
}

# key=格式标识, value=文件扩展名
EXTENSIONS: dict[str, str] = {
    "txt": ".txt",
    "html": ".html",
    "json": ".json",
    "pdf": ".pdf",
    "markdown": ".md",
    "csv": ".csv",
    "excel": ".xlsx",
}

# key=格式标识, value=该格式会产出的文件类别说明（供 UI 在导出前提示用户）
FORMAT_OUTPUTS: dict[str, list[str]] = {
    "txt": [
        "每会话 <类型>_<会话名>.txt",
        "index.txt（会话清单）",
    ],
    "html": [
        "每会话 <类型>_<会话名>.html（单文件自包含，无需外部资源）",
        "index.html（会话清单，可点击跳转）",
    ],
    "json": [
        "qq_export.json（全部会话合并为单文件，可被程序重新读入）",
    ],
    "pdf": [
        "qq_export.pdf（合订本：标题页 + 每会话分页）",
        "每会话 <类型>_<会话名>.pdf",
        "index.txt（会话清单）",
    ],
    "markdown": [
        "每会话 <类型>_<会话名>.md",
        "index.md（会话清单）",
    ],
    "csv": [
        "每会话 <类型>_<会话名>.csv（UTF-8 带 BOM，Excel 可直接打开）",
        "index.csv（会话清单）",
    ],
    "excel": [
        "qq_export.xlsx（合订本：一个会话一个 sheet）",
        "每会话 <类型>_<会话名>.xlsx（每个会话一个独立工作簿）",
        "index.xlsx（会话汇总表）",
    ],
}


# --------------------------------------------------------------------------
# 依赖探测
# --------------------------------------------------------------------------
def _pdf_ok() -> bool:
    """reportlab 是否可用。"""
    try:
        from . import pdf_writer

        return bool(getattr(pdf_writer, "_HAS_REPORTLAB", False))
    except Exception:  # noqa: BLE001
        return False


def _excel_ok() -> bool:
    """openpyxl 是否可用。"""
    try:
        from . import excel_writer

        return bool(getattr(excel_writer, "_HAS_OPENPYXL", False))
    except Exception:  # noqa: BLE001
        return False


def is_available(fmt: str) -> bool:
    """某个格式当前是否真的能导出（依赖齐备）。"""
    key = (fmt or "").strip().lower()
    if key not in FORMATS:
        return False
    if key == "pdf":
        return _pdf_ok()
    if key == "excel":
        return _excel_ok()
    return True


def available_formats() -> list[str]:
    """返回当前真正可用的格式列表（按 FORMATS 的顺序）。"""
    return [key for key in FORMATS if is_available(key)]


def format_outputs() -> dict[str, list[str]]:
    """每个格式会产出哪几类文件（UI 可在导出前提示用户）。"""
    return {key: list(items) for key, items in FORMAT_OUTPUTS.items()}


def format_output_desc(fmt: str) -> str:
    """单个格式的产物说明（人类可读一行）。"""
    key = (fmt or "").strip().lower()
    items = FORMAT_OUTPUTS.get(key) or []
    return "、".join(items) if items else ""


def format_status() -> dict[str, str]:
    """每个格式的可用状态 + 产物说明。

    可用时形如 ``可用｜产物：每会话 <类型>_<会话名>.txt、index.txt（会话清单）``；
    缺依赖时形如 ``不可用：缺少 reportlab（pip install reportlab）｜产物：…``。
    """
    status: dict[str, str] = {}
    for key in FORMATS:
        desc = format_output_desc(key)
        if is_available(key):
            status[key] = "可用｜产物：%s" % desc if desc else "可用"
        elif key == "pdf":
            status[key] = "不可用：缺少 reportlab（pip install reportlab）｜产物：%s" % desc
        elif key == "excel":
            status[key] = "不可用：缺少 openpyxl（pip install openpyxl）｜产物：%s" % desc
        else:  # pragma: no cover - 其余格式无外部依赖
            status[key] = "不可用：未知原因｜产物：%s" % desc
    return status


# --------------------------------------------------------------------------
# writer 装载
# --------------------------------------------------------------------------
def _writer_class(fmt: str):
    """按格式取 writer 类（延迟导入，避免缺依赖时整个包导入失败）。"""
    if fmt == "txt":
        from .txt_writer import TxtWriter

        return TxtWriter
    if fmt == "html":
        from .html_writer import HtmlWriter

        return HtmlWriter
    if fmt == "json":
        from .json_writer import JsonWriter

        return JsonWriter
    if fmt == "pdf":
        from .pdf_writer import PdfWriter

        return PdfWriter
    if fmt == "markdown":
        from .markdown_writer import MarkdownWriter

        return MarkdownWriter
    if fmt == "csv":
        from .csv_writer import CsvWriter

        return CsvWriter
    if fmt == "excel":
        from .excel_writer import ExcelWriter

        return ExcelWriter
    raise ValueError("未知导出格式：%r" % (fmt,))


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
class _CountingSource:
    """包一层，顺手统计「写了多少个会话 / 多少条消息」。

    透明地转发 :meth:`metas`（元数据）与 :meth:`__len__`（会话数），
    所以写器内部照旧可以用 ``prepare_names`` 与 ``len(chats)``。
    """

    def __init__(self, source) -> None:
        self._source = source
        self.chats = 0
        self.messages = 0

    def metas(self):
        getter = getattr(self._source, "metas", None)
        if callable(getter):
            return getter()
        return list(self._source or ())

    def __iter__(self):
        for chat in self._source:
            self.chats += 1
            self.messages += getattr(chat, "count", 0) or 0
            yield chat

    def __len__(self) -> int:
        try:
            return len(self._source)
        except TypeError:
            return self.chats


def write_export(
    fmt: str,
    chats,
    out_dir: str,
    *,
    progress=None,
) -> ExportResult:
    """把会话导出成指定格式。

    产物目录：``<out_dir>/<格式key>/``。该目录下 **两类文件并存**：

      1. **合订本**：把所有会话合并到一个文件里（方便整体保存/打印/归档）：
         - Excel -> ``qq_export.xlsx``（一个会话一个 sheet）
         - PDF   -> ``qq_export.pdf``（标题页 + 每会话分页）
         - JSON  -> ``qq_export.json``（全部会话的合并 JSON，只有这一类文件）
      2. **每会话文件**：每个会话一个独立文件，文件名形如
         ``<单聊|群聊>_<会话名><扩展名>``，同名会话自动追加 ``_2``、``_3`` 后缀，
         互不覆盖（Excel 也额外输出 ``<类型>_<会话名>.xlsx``）。
         另外多数格式还有一个 ``index.*`` 会话清单文件。

    各类格式的具体产物清单见 ``FORMAT_OUTPUTS`` / ``format_outputs()``。

    参数：
      fmt      格式标识，见 ``FORMATS``（大小写不敏感）。
      chats    ``list[qqexport.model.Chat]``，或
                :class:`qqexport.model.ChatStream`（**流式源**：元数据先行、
                消息逐个会话地补上，内存占用只与最大的那个会话有关）。
                全量导出（几百万条消息）请用后者。
      out_dir  输出根目录，产物落在 ``out_dir/<格式key>/`` 下。
      progress 可选回调 ``progress(done: int, total: int, msg: str)``。

    返回：
      ``qqexport.model.ExportResult``。``path`` 是本格式的输出**目录**；
      ``error`` 汇总所有「单个会话写失败」的信息（为空表示全部成功）。

    异常：
      ValueError          —— fmt 未知。
      PdfUnavailable      —— 缺少 reportlab。
      ExcelUnavailable    —— 缺少 openpyxl。
    """
    key = (fmt or "").strip().lower()
    if key not in FORMATS:
        raise ValueError(
            "未知导出格式：%r（可用格式：%s）" % (fmt, ", ".join(FORMATS.keys()))
        )

    root = os.path.abspath(out_dir)
    sub_dir = ensure_dir(os.path.join(root, key))

    # 依赖检查：缺依赖抛明确异常，UI 捕获后提示用户即可
    if key == "pdf" and not _pdf_ok():
        raise PdfUnavailable("未安装 reportlab，无法导出 PDF（pip install reportlab）")
    if key == "excel" and not _excel_ok():
        raise ExcelUnavailable("未安装 openpyxl，无法导出 Excel（pip install openpyxl）")

    # 普通列表原样传下去；流式源保持原样（写器内部用 metas() 预留文件名）
    source = chats if hasattr(chats, "metas") else list(chats or [])
    counting = _CountingSource(source)

    writer = _writer_class(key)(sub_dir)
    files = writer.write(counting, progress)

    errors = list(getattr(writer, "errors", []) or [])
    if errors:
        errors.append("（成功写出 %d 个文件）" % len(files))
    return ExportResult(
        path=sub_dir,
        fmt=key,
        chats=counting.chats,
        messages=counting.messages,
        ok=not errors,
        error="\n".join(errors),
    )
