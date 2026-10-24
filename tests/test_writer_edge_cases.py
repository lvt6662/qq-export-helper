# -*- coding: utf-8 -*-
"""导出格式的边界情况回归测试（不依赖 pytest）。

覆盖：
  * ``excel_sheet_title``：截断后以单引号结尾、全单引号名、保留名 History、
    emoji（按 UTF-16 码元计超过 31）、空名回退；
  * ``quote_filename``：空格 / # / % / 括号 / 中文全部正确编码；
  * HTML index：文件名含 ``#``、``%``、括号时链接仍能指向真实文件；
  * Markdown index：会话名含 ``|`` / 换行时表格不错行；
  * Excel 导出：会话名恰好让第 31 个字符是单引号时，sheet 名仍然合法。
"""

from __future__ import annotations

import html
import os
import shutil
import sys
import uuid
from urllib.parse import unquote

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
for _p in (_SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from qqexport.model import Chat, Message  # noqa: E402
from qqexport.writers import base as wbase  # noqa: E402
from qqexport.writers.html_writer import write_chats as write_html  # noqa: E402
from qqexport.writers.markdown_writer import write_chats as write_markdown  # noqa: E402

SCRATCH = os.path.join(_HERE, "_scratch")


def _tmpdir() -> str:
    """在工程内创建临时目录。

    不用 ``tempfile.mkdtemp``：受限沙箱下它建出的 0o700 目录 ACL 不允许
    后续写入（PermissionError），普通 ``makedirs`` 没这个问题。
    """
    os.makedirs(SCRATCH, exist_ok=True)
    path = os.path.join(SCRATCH, "edge_" + uuid.uuid4().hex[:10])
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup(path: str) -> None:
    """删掉临时目录（Windows 上偶尔会因为只读/ACL 失败，忽略即可）。"""
    def _onerror(func, p, exc):  # noqa: ANN001 - shutil 的回调签名
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, ignore_errors=True, onerror=_onerror)


def _chat(name: str, n: int = 1) -> Chat:
    msgs = [
        Message(msg_id=i + 1, time=1760000000 + i, direction=2, sender="对方",
                msg_type=2, content="第 %d 条消息" % (i + 1), text="第 %d 条消息" % (i + 1))
        for i in range(n)
    ]
    return Chat(chat_id="c1", name=name, peer="", is_group=False, messages=msgs)


def _valid_sheet_title(title: str) -> None:
    """断言一个 sheet 名满足 Excel 的全部硬性限制。"""
    assert title, "sheet 名不能为空"
    assert len(title.encode("utf-16-le")) // 2 <= 31, "超过 31 个 UTF-16 码元: %r" % title
    assert not set(title) & set("[]:*?/\\"), "含非法字符: %r" % title
    assert not title.startswith("'") and not title.endswith("'"), "首尾单引号: %r" % title
    assert title.lower() != "history", "Excel 保留名: %r" % title


# --------------------------------------------------------------------------
# excel_sheet_title
# --------------------------------------------------------------------------
def test_excel_title_trailing_apostrophe_after_truncation():
    """第 31 个字符恰好是单引号：截断后必须去掉，不能留下非法结尾。"""
    name = "A" * 30 + "'" + "后面的内容"
    title = wbase.excel_sheet_title(name)
    _valid_sheet_title(title)
    assert title == "A" * 30, repr(title)


def test_excel_title_all_apostrophes_falls_back():
    title = wbase.excel_sheet_title("'''")
    _valid_sheet_title(title)
    assert title == "未命名会话", repr(title)


def test_excel_title_reserved_history():
    for name in ("History", "history", "HISTORY"):
        title = wbase.excel_sheet_title(name)
        _valid_sheet_title(title)
        assert title.lower() != "history", repr(title)


def test_excel_title_emoji_counts_as_utf16_units():
    """16 个 emoji = 32 个 UTF-16 码元，必须截到 15 个以内。"""
    title = wbase.excel_sheet_title("😀" * 20)
    _valid_sheet_title(title)
    assert len(title) <= 15, repr(title)


def test_excel_title_plain_and_empty():
    assert wbase.excel_sheet_title("正常群名") == "正常群名"
    assert wbase.excel_sheet_title("") == "未命名会话"
    assert wbase.excel_sheet_title(None) == "未命名会话"
    # 去掉 []:*?/\ 之后为空，也要回退
    assert wbase.excel_sheet_title("[]:*?/\\") == "未命名会话"


# --------------------------------------------------------------------------
# quote_filename
# --------------------------------------------------------------------------
def test_quote_filename_encodes_specials():
    assert wbase.quote_filename("a b#c%(d).html") == "a%20b%23c%25%28d%29.html"
    assert wbase.quote_filename("单聊_张三.html") == (
        "%E5%8D%95%E8%81%8A_%E5%BC%A0%E4%B8%89.html"
    )
    assert wbase.quote_filename("") == ""


# --------------------------------------------------------------------------
# HTML / Markdown index 链接
# --------------------------------------------------------------------------
def test_html_index_links_survive_special_names():
    names = ["C# 学习群", "100%达成", "群(2026)"]
    chats = [_chat(n) for n in names]
    out = _tmpdir()
    try:
        files = write_html(chats, out)
        index = os.path.join(out, "index.html")
        assert os.path.isfile(index)
        text = open(index, encoding="utf-8").read()
        hrefs = []
        pos = 0
        while True:
            pos = text.find('href="', pos)
            if pos < 0:
                break
            end = text.find('"', pos + 6)
            hrefs.append(html.unescape(text[pos + 6:end]))
            pos = end + 1
        assert len(hrefs) == len(chats), hrefs
        for href in hrefs:
            assert "#" not in href, href
            target = os.path.join(out, unquote(href))
            assert os.path.isfile(target), "链接指向不存在的文件: %r -> %r" % (href, target)
        # 三个会话文件都在
        assert len([f for f in files if f.endswith(".html")]) == len(chats) + 1
    finally:
        _cleanup(out)


def test_markdown_index_table_escapes_pipe_and_newline():
    chats = [_chat("a|b\nc"), _chat("正常群")]
    out = _tmpdir()
    try:
        write_markdown(chats, out)
        index = os.path.join(out, "index.md")
        text = open(index, encoding="utf-8").read()
        rows = [ln for ln in text.splitlines() if ln.startswith("|") and "---" not in ln]
        # 表头 + 2 个会话 = 3 行，且没有被换行拆成多行
        assert len(rows) == 3, rows
        assert "a\\|b ⏎ c" in text, text
    finally:
        _cleanup(out)


def test_markdown_index_links_survive_special_names():
    chats = [_chat("群(2026)#1 100%")]
    out = _tmpdir()
    try:
        write_markdown(chats, out)
        text = open(os.path.join(out, "index.md"), encoding="utf-8").read()
        assert "%23" in text and "%28" in text and "%25" in text, text
        # 从链接里还原出真实文件名并确认存在
        start = text.find("](")
        end = text.find(")", start)
        href = text[start + 2:end]
        assert os.path.isfile(os.path.join(out, unquote(href))), href
    finally:
        _cleanup(out)


# --------------------------------------------------------------------------
# Excel 端到端
# --------------------------------------------------------------------------
def test_excel_export_sheet_names_valid():
    if not __import__("qqexport.writers", fromlist=["is_available"]).is_available("excel"):
        print("  (跳过：openpyxl 不可用)")
        return
    import openpyxl

    from qqexport.writers.excel_writer import write_chats as write_excel

    chats = [
        _chat("A" * 30 + "'" + "尾巴"),   # 截断后第 31 个字符是单引号
        _chat("History"),                 # Excel 保留名
        _chat("😀" * 20),                 # emoji 超过 31 个 UTF-16 码元
    ]
    out = _tmpdir()
    try:
        write_excel(chats, out)
        main = os.path.join(out, "qq_export.xlsx")
        assert os.path.isfile(main), os.listdir(out)
        # 汇总工作簿里应当有 3 个会话 sheet
        wb = openpyxl.load_workbook(main, read_only=True)
        try:
            names = list(wb.sheetnames)
        finally:
            wb.close()
        assert len(names) == len(chats), names
        for name in names:
            _valid_sheet_title(name)
    finally:
        _cleanup(out)


# --------------------------------------------------------------------------
# 运行入口
# --------------------------------------------------------------------------
def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %s: %s: %s" % (name, type(exc).__name__, exc))
            import traceback

            traceback.print_exc()
        else:
            print("ok", name)
    print("-" * 50)
    print("共 %d 个用例，失败 %d 个" % (len(tests), failed))
    if failed:
        print("WRITER EDGE TESTS FAILED")
        return 1
    print("all writer edge tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
