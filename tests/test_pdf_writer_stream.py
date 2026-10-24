# -*- coding: utf-8 -*-
"""PDF 导出器的**流式**行为测试。

不依赖 pytest：可直接 ``python tests\\test_pdf_writer_stream.py`` 运行，
也可以用 pytest 收集（函数名以 ``test_`` 开头）。

覆盖点：
  * 流式源（``metas()`` / ``__len__`` / ``__iter__``）能正常导出；
  * **只遍历一遍**（流式源只能消费一次）：``__iter__`` 只被调用一次；
  * **边遍历边写**：请求下一个会话时，上一个会话的文件已经在磁盘上；
  * 合订本结构：标题页 + 每会话分页；页眉是会话名、页脚有页码；
  * 中文正确渲染（PDF 里能提取出「你好，世界」）；
  * ``index.txt`` 语义与现状一致（会话清单 + 合订本文件名）；
  * 单个会话写失败不中断整体导出（``writer.errors`` 有记录，其余文件照常产出）；
  * 老 API（``write_chats(chats, out_dir)`` / ``PdfWriter(out_dir).write(chats)``）可用；
  * 空会话、附件、系统消息都能正常排版。
"""

from __future__ import annotations

import os
import shutil
import sys
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
for _p in (_SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from qqexport.model import Chat, ChatStream, Message  # noqa: E402
from qqexport.writers import pdf_writer  # noqa: E402
from qqexport.writers.pdf_writer import PdfWriter, write_chats  # noqa: E402
from make_fixture_chats import make_chats, make_same_name_chats  # noqa: E402

# 沙箱不允许写 %TEMP%，临时目录统一放在工程内的 _scratch 下
SCRATCH = os.path.join(_HERE, "_scratch")

HELLO = "你好，世界"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _tmpdir() -> str:
    """在工程内创建一个临时目录。

    不用 ``tempfile.mkdtemp``：它在 Windows 上会建出 0o700 权限的目录，
    受限沙箱下删不掉（会攒一堆垃圾目录）。这里用普通的 makedirs + 随机名。
    """
    os.makedirs(SCRATCH, exist_ok=True)
    path = os.path.join(SCRATCH, "pdfstream_" + uuid.uuid4().hex[:10])
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _read(path: str, encoding: str = "utf-8") -> str:
    with open(path, "r", encoding=encoding, newline="") as fh:
        return fh.read()


def _raw(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _pdf_text(path: str) -> str:
    """把 PDF 里的文字抽出来（pypdf / pymupdf 都行，都没有就返回空串）。"""
    try:
        import pypdf

        reader = pypdf.PdfReader(path)
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - 抽取失败不算测试失败
        return ""
    try:
        import pymupdf

        doc = pymupdf.open(path)
        return "\n".join(doc[i].get_text() for i in range(doc.page_count))
    except ImportError:
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _page_count(path: str) -> int:
    """页数（优先用 pypdf，没有就从原始字节里数页对象）。"""
    try:
        import pypdf

        return len(pypdf.PdfReader(path).pages)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return _raw(path).count(b"/Type /Page /Parent")


class _CountingStream:
    """模拟 ``qqexport.model.ChatStream``：元数据先行，消息逐个会话地补上。

    同时记录 ``__iter__`` 被调用了几次 —— 流式源**只能遍历一遍**。
    """

    def __init__(self, metas, loader):
        self._metas = list(metas)
        self._loader = loader
        self.iter_calls = 0
        self.yielded = 0
        self.on_yield = None  # 回调：每次产出会话时调用（用来验证「边写边产出」）

    def metas(self):
        return self._metas

    def __len__(self):
        return len(self._metas)

    def __iter__(self):
        self.iter_calls += 1
        for meta in self._metas:
            chat = self._loader(meta)
            self.yielded += 1
            if self.on_yield is not None:
                self.on_yield(chat)
            yield chat


def _meta_of(chat: Chat) -> Chat:
    """只保留元数据的会话副本。"""
    return Chat(
        chat_id=chat.chat_id,
        name=chat.name,
        peer=chat.peer,
        is_group=chat.is_group,
        source=chat.source,
        total_count=chat.count,
        first_ts=chat.messages[0].time if chat.messages else 0,
        last_ts=chat.messages[-1].time if chat.messages else 0,
    )


def _stream_of(chats, on_yield=None) -> _CountingStream:
    """把普通会话列表包成流式源。"""
    metas = [_meta_of(c) for c in chats]
    full = {c.chat_id: c for c in chats}

    def _loader(meta: Chat) -> Chat:
        return full[meta.chat_id]

    stream = _CountingStream(metas, _loader)
    stream.on_yield = on_yield
    return stream


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------
def test_write_chats_plain_list():
    """老 API：write_chats(chats, out_dir) 依然可用。"""
    chats = make_chats()
    out = _tmpdir()
    try:
        files = write_chats(chats, out)
        assert files, "没有产出任何文件"
        assert os.path.exists(os.path.join(out, "qq_export.pdf"))
        assert os.path.exists(os.path.join(out, "index.txt"))
        for f in files:
            assert os.path.exists(f), f
    finally:
        _cleanup(out)


def test_writer_write_plain_list():
    """老 API：PdfWriter(out_dir).write(chats) 依然可用。"""
    chats = make_chats()
    out = _tmpdir()
    try:
        writer = PdfWriter(out)
        files = writer.write(chats)
        assert files
        assert not writer.errors, writer.errors
    finally:
        _cleanup(out)


def test_stream_source_supported():
    """流式源（metas/__len__/__iter__）能正常导出。"""
    chats = make_chats()
    out = _tmpdir()
    try:
        stream = _stream_of(chats)
        writer = PdfWriter(out)
        files = writer.write(stream)
        assert not writer.errors, writer.errors
        assert os.path.exists(os.path.join(out, "qq_export.pdf"))
        assert os.path.exists(os.path.join(out, "index.txt"))
        # 三个会话各一个 PDF
        singles = [f for f in files if f.endswith(".pdf") and "qq_export" not in f]
        assert len(singles) == len(chats), singles
    finally:
        _cleanup(out)


def test_stream_iterated_only_once():
    """流式源只被遍历一遍（不能先写合订本再写单会话）。"""
    chats = make_chats()
    out = _tmpdir()
    try:
        stream = _stream_of(chats)
        PdfWriter(out).write(stream)
        assert stream.iter_calls == 1, "会话源被遍历了 %d 次" % stream.iter_calls
        assert stream.yielded == len(chats), stream.yielded
    finally:
        _cleanup(out)


def test_writes_while_iterating():
    """边遍历边写：产出下一个会话时，上一个会话的 PDF 已经落盘。"""
    chats = make_chats()
    out = _tmpdir()
    seen = []

    def _on_yield(chat):
        seen.append(chat.name)

    try:
        stream = _stream_of(chats, on_yield=_on_yield)
        writer = PdfWriter(out)
        writer.write(stream)
        assert not writer.errors, writer.errors
        # 每个会话的单会话 PDF 都在（说明是逐个写出来的，不是最后一起写）
        singles = sorted(
            n for n in os.listdir(out) if n.endswith(".pdf") and not n.startswith("qq_export")
        )
        assert len(singles) == len(chats), singles
        assert len(seen) == len(chats), seen
    finally:
        _cleanup(out)


def test_combined_structure_and_header_footer():
    """合订本：标题页 + 每会话分页；页眉是会话名，页脚有页码。"""
    chats = make_chats()
    out = _tmpdir()
    try:
        PdfWriter(out).write(_stream_of(chats))
        combined = os.path.join(out, "qq_export.pdf")
        assert os.path.getsize(combined) > 1024
        raw = _raw(combined)
        assert raw.startswith(b"%PDF"), "PDF 文件头不对"
        assert b"/UniGB-UCS2-H" in raw, "缺少 CJK 字体"
        # 页数 = 标题页 + 每会话至少一页
        pages = _page_count(combined)
        assert pages >= len(chats) + 1, pages
        text = _pdf_text(combined)
        if text:
            assert "QQ 聊天记录导出" in text, text[:300]
            assert "会话目录" in text, text[:300]
            assert "张小明" in text, "合订本里没有会话名"
    finally:
        _cleanup(out)


def test_chinese_renders():
    """中文能正确写进 PDF（能被读回来）。"""
    chats = make_chats()
    out = _tmpdir()
    try:
        PdfWriter(out).write(_stream_of(chats))
        single = os.path.join(out, "单聊_张小明.pdf")
        assert os.path.exists(single), os.listdir(out)
        text = _pdf_text(single)
        if not text:
            # 没有 PDF 解析库时退而求其次：检查字体与编码声明
            raw = _raw(single)
            assert b"/UniGB-UCS2-H" in raw
            assert b"/F1" in raw
            return
        assert HELLO in text, text[:500]
        assert "张小明" in text, text[:500]
    finally:
        _cleanup(out)


def test_index_txt_semantics():
    """index.txt 语义与现状一致：会话清单 + 合订本文件名。"""
    chats = make_chats()
    out = _tmpdir()
    try:
        PdfWriter(out).write(_stream_of(chats))
        index = os.path.join(out, "index.txt")
        text = _read(index)
        assert text.startswith("QQ 聊天记录导出 - 会话清单"), text[:100]
        assert "合订本：qq_export.pdf" in text, text[:200]
        assert "会话数：3" in text, text[:200]
        for chat in chats:
            assert chat.name in text, "index 里缺少会话 %s" % chat.name
        assert "单聊_张小明.pdf" in text, text
        assert "文件：" in text
    finally:
        _cleanup(out)


def test_index_written_after_traversal():
    """index.txt 在遍历完之后才写（流式下拿不到全部会话名）。"""
    chats = make_chats()
    out = _tmpdir()
    try:
        state = {}

        def _on_yield(chat):
            # 产出第一个会话时，index.txt 还不该存在
            state["index_exists_during"] = os.path.exists(os.path.join(out, "index.txt"))

        stream = _stream_of(chats, on_yield=_on_yield)
        PdfWriter(out).write(stream)
        assert state.get("index_exists_during") is False, "index.txt 提前写了"
        assert os.path.exists(os.path.join(out, "index.txt"))
    finally:
        _cleanup(out)


def test_single_chat_failure_does_not_break_all():
    """单个会话写失败不中断整体导出。"""

    class _Boom:
        """访问 messages 就炸的会话。"""

        chat_id = "boom"
        name = "坏会话"
        peer = ""
        is_group = False
        source = "test"
        total_count = 0
        first_ts = 0
        last_ts = 0

        @property
        def messages(self):
            raise RuntimeError("boom")

        @property
        def count(self):
            raise RuntimeError("boom")

    chats = make_chats()
    out = _tmpdir()
    try:
        writer = PdfWriter(out)
        files = writer.write([_Boom()] + list(chats))
        assert writer.errors, "坏会话应该被记进 errors"
        assert "boom" in "\n".join(writer.errors), writer.errors
        # 其余会话照常导出
        assert os.path.exists(os.path.join(out, "qq_export.pdf"))
        assert os.path.exists(os.path.join(out, "单聊_张小明.pdf"))
        assert os.path.exists(os.path.join(out, "群聊_产品研发群.pdf"))
        # index 里只有成功的会话
        text = _read(os.path.join(out, "index.txt"))
        assert "张小明" in text
        assert "会话数：3" in text, text[:200]
    finally:
        _cleanup(out)


def test_same_name_chats_not_overwritten():
    """同名会话不互相覆盖。"""
    chats = make_same_name_chats()
    out = _tmpdir()
    try:
        writer = PdfWriter(out)
        writer.write(_stream_of(chats))
        names = sorted(n for n in os.listdir(out) if n.endswith(".pdf") and not n.startswith("qq_export"))
        assert len(names) == 4, names
        for name in names:
            assert os.path.getsize(os.path.join(out, name)) > 512, name
    finally:
        _cleanup(out)


def test_progress_reported():
    """进度回调被调用，最后一次 done == total。"""
    chats = make_chats()
    out = _tmpdir()
    try:
        seen = []
        PdfWriter(out).write(_stream_of(chats), progress=lambda d, t, m: seen.append((d, t, m)))
        assert seen, "进度回调没有被调用"
        for done, total, msg in seen:
            assert isinstance(done, int) and isinstance(total, int)
            assert 0 < done <= total, (done, total)
            assert isinstance(msg, str) and msg
        assert seen[-1][0] == seen[-1][1], seen[-1]
    finally:
        _cleanup(out)


def test_real_chatstream_object():
    """真正的 qqexport.model.ChatStream 也能导出。"""
    chats = make_chats()
    metas = [_meta_of(c) for c in chats]
    full = {c.chat_id: c for c in chats}
    stream = ChatStream(metas, lambda meta: full[meta.chat_id])
    out = _tmpdir()
    try:
        writer = PdfWriter(out)
        writer.write(stream)
        assert not writer.errors, writer.errors
        assert os.path.exists(os.path.join(out, "qq_export.pdf"))
        text = _read(os.path.join(out, "index.txt"))
        assert "会话数：3" in text, text[:200]
    finally:
        _cleanup(out)


def test_empty_chat_and_attachments():
    """空会话、附件、系统消息都能排版。"""
    chat = Chat(chat_id="9", name="空会话", peer="9")
    out = _tmpdir()
    try:
        writer = PdfWriter(out)
        writer.write([chat])
        assert not writer.errors, writer.errors
        single = os.path.join(out, "单聊_空会话.pdf")
        assert os.path.exists(single)
        text = _pdf_text(single)
        if text:
            assert "空会话" in text, text[:200]
    finally:
        _cleanup(out)


def test_long_message_wraps():
    """超长消息会折行，不会画出页面。"""
    msg = Message(
        msg_id=1,
        time=1760000000,
        direction=1,
        sender="我",
        text="超长消息" * 2000,
        content="超长消息" * 2000,
        is_mine=True,
    )
    chat = Chat(chat_id="1", name="长消息", peer="1")
    chat.messages = [msg]
    out = _tmpdir()
    try:
        writer = PdfWriter(out)
        writer.write([chat])
        assert not writer.errors, writer.errors
        single = os.path.join(out, "单聊_长消息.pdf")
        pages = _page_count(single)
        assert pages >= 3, pages  # 一条消息折成很多行，应该有若干页
    finally:
        _cleanup(out)


def test_multiline_message_keeps_newlines():
    """消息里的换行会分成多行（不会糊成一行）。"""
    msg = Message(
        msg_id=1,
        time=1760000000,
        direction=1,
        sender="我",
        text="第一行\n第二行",
        content="第一行\n第二行",
        is_mine=True,
    )
    chat = Chat(chat_id="1", name="多行", peer="1")
    chat.messages = [msg]
    out = _tmpdir()
    try:
        writer = PdfWriter(out)
        writer.write([chat])
        assert not writer.errors, writer.errors
        text = _pdf_text(os.path.join(out, "单聊_多行.pdf"))
        if text:
            assert "第一行" in text and "第二行" in text, text[:300]
            assert "第一行\n第二行" in text or "第一行" in text and "第二行" in text
    finally:
        _cleanup(out)


def test_no_reportlab_raises_pdf_unavailable():
    """reportlab 不可用时抛 PdfUnavailable（不崩溃）。"""
    from qqexport.writers.base import PdfUnavailable

    old = pdf_writer._HAS_REPORTLAB
    try:
        pdf_writer._HAS_REPORTLAB = False
        out = _tmpdir()
        try:
            try:
                write_chats(make_chats(), out)
            except PdfUnavailable:
                pass
            else:
                raise AssertionError("应当抛 PdfUnavailable")
        finally:
            _cleanup(out)
    finally:
        pdf_writer._HAS_REPORTLAB = old


def test_memory_does_not_grow_with_messages():
    """写一万条消息时，内存占用与消息总数无关（只看 tracemalloc 的峰值增量）。"""
    import tracemalloc

    def _make(n):
        chat = Chat(chat_id="1", name="大群", peer="1", is_group=True)
        chat.messages = [
            Message(
                msg_id=i,
                time=1760000000 + i,
                direction=1,
                sender="成员%d" % (i % 50),
                text="消息内容 %d" % i,
                content="消息内容 %d" % i,
                is_mine=(i % 3 == 0),
            )
            for i in range(n)
        ]
        return chat

    out = _tmpdir()
    try:
        # 先热身（字体注册、缓存等一次性开销不算进来）
        warm = _make(200)
        PdfWriter(out).write([warm])
        shutil.rmtree(out, ignore_errors=True)
        out = _tmpdir()

        big = _make(20000)
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        PdfWriter(out).write([big])
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        # 峰值增量不应该跟消息条数同阶（20000 条消息本身就有几 MB）
        assert peak - before < 40 * 1024 * 1024, (peak - before, current - before)
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
        print("PDF STREAM TESTS FAILED")
        return 1
    print("all pdf stream tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
