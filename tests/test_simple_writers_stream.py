# -*- coding: utf-8 -*-
"""5 种「简单格式」导出器（txt/html/markdown/csv/json）在**流式源**下的行为测试。

不依赖 pytest：可直接 ``python tests\\test_simple_writers_stream.py`` 运行，
也可以用 pytest 收集（函数名以 ``test_`` 开头）。

背景：真实库里约 290 万条消息 / 454 个会话，最大的一个群 130 万条。
写器必须做到：
  * **只遍历一遍** —— 流式源只能走一次，绝不能 ``list(chats)`` 或遍历两次；
  * **边写边放** —— 写完一个会话立刻释放，不能把 Chat 留在内存里；
  * **index 后写** —— 会话清单在遍历完之后才落盘；
  * **JSON 边序列化边写** —— 不能再把每个会话的 JSON 片段攒进内存。

覆盖点：
  * 假流式源（``FakeStream``）记录 ``__iter__`` 被调用了几次 —— 断言只遍历一遍；
  * 5 种格式都能吃流式源，产物内容正确；
  * 写完之后写器不再持有任何 Chat 对象（weakref 断言）；
  * index 文件在遍历期间还不存在，遍历完之后才出现；
  * JSON 产物是合法 JSON、能被 ``json.load`` 读回，统计数字正确；
  * JSON 是**边写边落盘**的（导出途中磁盘上就已经有内容了）；
  * 单个会话序列化失败不中断整体导出，产物仍是合法 JSON；
  * 老用法（普通 ``list[Chat]``）继续可用。
"""

from __future__ import annotations

import csv
import gc
import json
import os
import shutil
import sys
import tracemalloc
import uuid
import weakref

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
for _p in (_SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from qqexport import writers  # noqa: E402
from qqexport.model import Chat, ChatStream  # noqa: E402
from qqexport.writers.csv_writer import CsvWriter  # noqa: E402
from qqexport.writers.csv_writer import write_chats as csv_write_chats  # noqa: E402
from qqexport.writers.html_writer import HtmlWriter  # noqa: E402
from qqexport.writers.html_writer import write_chats as html_write_chats  # noqa: E402
from qqexport.writers.json_writer import JsonWriter  # noqa: E402
from qqexport.writers.json_writer import write_chats as json_write_chats  # noqa: E402
from qqexport.writers.markdown_writer import MarkdownWriter  # noqa: E402
from qqexport.writers.markdown_writer import write_chats as md_write_chats  # noqa: E402
from qqexport.writers.txt_writer import TxtWriter  # noqa: E402
from qqexport.writers.txt_writer import write_chats as txt_write_chats  # noqa: E402
from make_fixture_chats import make_chats  # noqa: E402

# 沙箱不允许写 %TEMP%，临时目录统一放在工程内的 _scratch 下
SCRATCH = os.path.join(_HERE, "_scratch")

HELLO = "你好，世界"

# 4 种「每会话一个文件」的格式 + 1 种合订本格式
PER_CHAT_WRITERS = {
    "txt": (TxtWriter, txt_write_chats, ".txt", "index.txt"),
    "html": (HtmlWriter, html_write_chats, ".html", "index.html"),
    "markdown": (MarkdownWriter, md_write_chats, ".md", "index.md"),
    "csv": (CsvWriter, csv_write_chats, ".csv", "index.csv"),
}
ALL_FORMATS = ("txt", "html", "markdown", "csv", "json")


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _tmpdir() -> str:
    """在工程内创建一个临时目录。

    不用 ``tempfile.mkdtemp``：受限沙箱下它建出来的目录 ACL 会变得连
    ``rmtree`` 都删不掉（会留下一堆空目录）。普通 ``makedirs`` 没这个问题。
    """
    os.makedirs(SCRATCH, exist_ok=True)
    path = os.path.join(SCRATCH, "stream_" + uuid.uuid4().hex[:10])
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


def _read(path: str, encoding: str = "utf-8") -> str:
    with open(path, "r", encoding=encoding, newline="") as fh:
        return fh.read()


def _big_chats(n_chats: int = 3, n_msgs: int = 400) -> list[Chat]:
    """造几个「大」会话（每条消息几十个字符），用来验证边写边落盘。"""
    chats = []
    for i in range(n_chats):
        chat = Chat(
            chat_id="8000%d" % i,
            name="大会话%d" % i,
            peer="8000%d" % i,
            is_group=True,
        )
        for j in range(n_msgs):
            chat.messages.append(
                _make_msg(j, "第 %d 个会话的第 %d 条消息：%s" % (i, j, "内容" * 20))
            )
        chats.append(chat)
    return chats


def _make_msg(msg_id: int, text: str):
    from qqexport.model import DIRECTION_OUT, Message

    return Message(
        msg_id=msg_id,
        time=1792497600 + msg_id * 60,
        direction=DIRECTION_OUT,
        sender="我",
        text=text,
        content=text,
        is_mine=True,
    )


# --------------------------------------------------------------------------
# 假流式源
# --------------------------------------------------------------------------
class FakeStream:
    """假流式源：记录被遍历了几次，**只允许遍历一遍**。

    契约与 :class:`qqexport.model.ChatStream` 一致：
      * ``metas()`` —— 只有元数据、没有消息的会话列表（用来预留文件名）；
      * ``__len__`` —— 会话总数；
      * ``__iter__`` —— 逐个产出带消息的 Chat，**只能走一次**。
    """

    def __init__(self, chats) -> None:
        self._chats = list(chats)
        self.iter_calls = 0
        self.metas_calls = 0

    def metas(self) -> list[Chat]:
        self.metas_calls += 1
        out = []
        for c in self._chats:
            meta = Chat(
                chat_id=c.chat_id,
                name=c.name,
                peer=c.peer,
                is_group=c.is_group,
                source=c.source,
            )
            meta.total_count = c.count
            out.append(meta)
        return out

    def __len__(self) -> int:
        return len(self._chats)

    def __iter__(self):
        self.iter_calls += 1
        if self.iter_calls > 1:
            raise AssertionError("流式源被遍历了 %d 次，只允许遍历一遍" % self.iter_calls)
        for c in self._chats:
            yield c


class BoomChat:
    """序列化时必定抛异常的假会话。"""

    name = "坏会话"
    peer = ""
    chat_id = "boom"
    is_group = False
    messages: list = []
    count = 0
    total_count = 0

    def to_dict(self, with_messages: bool = True) -> dict:
        raise RuntimeError("boom")


# --------------------------------------------------------------------------
# 单遍遍历
# --------------------------------------------------------------------------
def test_stream_is_iterated_only_once():
    """5 种格式都只遍历流式源一遍。"""
    for fmt in ALL_FORMATS:
        chats = make_chats()
        stream = FakeStream(chats)
        out = _tmpdir()
        try:
            if fmt == "json":
                JsonWriter(out).write(stream)
            else:
                cls = PER_CHAT_WRITERS[fmt][0]
                cls(out).write(stream)
            assert stream.iter_calls == 1, (fmt, stream.iter_calls)
        finally:
            _cleanup(out)


def test_stream_metas_used_for_names():
    """预留文件名走的是 metas()，不是把流消费掉。"""
    chats = make_chats()
    stream = FakeStream(chats)
    out = _tmpdir()
    try:
        TxtWriter(out).write(stream)
        assert stream.metas_calls >= 1, stream.metas_calls
        assert stream.iter_calls == 1, stream.iter_calls
        # 会话文件按「类型_名字」命名（用的是 meta 里的名字）
        names = os.listdir(out)
        assert "单聊_张小明.txt" in names, names
        assert "群聊_产品研发群.txt" in names, names
    finally:
        _cleanup(out)


def test_real_chat_stream_works():
    """真的 ChatStream（lead 用的那个）也能喂给写器。"""
    base = make_chats()
    metas = []
    for c in base:
        meta = Chat(chat_id=c.chat_id, name=c.name, peer=c.peer, is_group=c.is_group)
        meta.total_count = c.count
        metas.append(meta)
    by_id = {c.chat_id: c for c in base}

    def loader(meta: Chat) -> Chat:
        full = by_id[meta.chat_id]
        return Chat(
            chat_id=full.chat_id,
            name=full.name,
            peer=full.peer,
            is_group=full.is_group,
            messages=list(full.messages),
        )

    stream = ChatStream(metas, loader)
    out = _tmpdir()
    try:
        TxtWriter(out).write(stream)
        text = _read(os.path.join(out, "单聊_张小明.txt"))
        assert HELLO in text, text[:200]
        index = _read(os.path.join(out, "index.txt"))
        assert "张小明" in index and "产品研发群" in index, index[:300]
    finally:
        _cleanup(out)


# --------------------------------------------------------------------------
# 内容正确
# --------------------------------------------------------------------------
def test_stream_content_is_correct():
    """流式导出出来的内容与普通列表一致。"""
    for fmt in ("txt", "html", "markdown", "csv"):
        cls, _fn, ext, index_name = PER_CHAT_WRITERS[fmt]
        chats = make_chats()
        stream = FakeStream(chats)
        out = _tmpdir()
        try:
            cls(out).write(stream)
            # index 清单
            index = os.path.join(out, index_name)
            assert os.path.exists(index), index
            index_text = _read(index, encoding="utf-8-sig")
            for chat in chats:
                assert chat.name in index_text, (fmt, chat.name)
            # 会话文件
            for chat in chats:
                prefix = "群聊" if chat.is_group else "单聊"
                path = os.path.join(out, "%s_%s%s" % (prefix, chat.name, ext))
                assert os.path.exists(path), (fmt, path)
                assert os.path.getsize(path) > 0, path
            if fmt == "csv":
                with open(os.path.join(out, "单聊_张小明.csv"), "r", encoding="utf-8-sig", newline="") as fh:
                    rows = list(csv.reader(fh))
                assert len(rows) == chats[0].count + 1, len(rows)
            else:
                blob = "".join(
                    _read(os.path.join(out, n), encoding="utf-8-sig")
                    for n in os.listdir(out)
                    if n.endswith(ext) and n != index_name
                )
                assert HELLO in blob, (fmt, blob[:200])
        finally:
            _cleanup(out)


def test_json_stream_output_is_valid():
    """流式导出的 JSON 是合法 JSON，且能被 json.load 读回。"""
    chats = make_chats()
    stream = FakeStream(chats)
    out = _tmpdir()
    try:
        JsonWriter(out).write(stream)
        path = os.path.join(out, "qq_export.json")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["app"] == "QQ导出助手", data.get("app")
        assert data["version"] == 1
        assert "exported_at" in data
        assert data["chat_count"] == len(chats), data["chat_count"]
        assert data["message_count"] == sum(c.count for c in chats), data["message_count"]
        assert len(data["chats"]) == len(chats), len(data["chats"])
        assert HELLO in json.dumps(data, ensure_ascii=False)
        # 会话字段与 Chat.to_dict() 一致
        assert data["chats"][0]["chat_id"] == chats[0].chat_id
        assert len(data["chats"][0]["messages"]) == chats[0].count
    finally:
        _cleanup(out)


# --------------------------------------------------------------------------
# 边写边放
# --------------------------------------------------------------------------
def test_writer_does_not_retain_chats():
    """写完之后写器不再持有 Chat 对象（大群的百万条消息能被回收）。"""
    for fmt in ALL_FORMATS:
        chats = make_chats()
        refs = [weakref.ref(c) for c in chats]
        stream = FakeStream(chats)
        out = _tmpdir()
        try:
            if fmt == "json":
                writer = JsonWriter(out)
            else:
                writer = PER_CHAT_WRITERS[fmt][0](out)
            writer.write(stream)
            del chats, stream
            gc.collect()
            for ref in refs:
                assert ref() is None, (fmt, "写器还留着会话对象没释放")
        finally:
            _cleanup(out)


# --------------------------------------------------------------------------
# index 后写
# --------------------------------------------------------------------------
def test_index_written_after_iteration():
    """index 清单在遍历完之后才写（遍历期间它还不存在）。"""
    for fmt in ("txt", "html", "markdown", "csv"):
        cls, _fn, _ext, index_name = PER_CHAT_WRITERS[fmt]
        chats = make_chats()
        stream = FakeStream(chats)
        out = _tmpdir()
        try:
            index_path = os.path.join(out, index_name)
            states = []

            def progress(done, total, msg):
                states.append((done, total, os.path.exists(index_path)))

            cls(out).write(stream, progress)
            assert states, fmt
            # 除最后一次（收尾）之外，遍历期间 index 都不该存在
            for done, _total, exists in states[:-1]:
                assert not exists, (fmt, done, "index 提前写出来了")
            assert os.path.exists(index_path), index_path
            # 进度回调的 done/total 合法
            for done, total, _msg in states:
                assert 0 < done <= total, (fmt, done, total)
            assert states[-1][0] == states[-1][1], states[-1]
        finally:
            _cleanup(out)


# --------------------------------------------------------------------------
# JSON 边序列化边写
# --------------------------------------------------------------------------
def test_json_is_written_incrementally():
    """JSON 是边序列化边落盘的：导出途中磁盘上就已经有内容了。"""
    chats = _big_chats(n_chats=3, n_msgs=400)
    stream = FakeStream(chats)
    out = _tmpdir()
    try:
        path = os.path.join(out, "qq_export.json")
        steps = []

        def progress(done, total, msg):
            size = os.path.getsize(path) if os.path.exists(path) else 0
            head = ""
            if size:
                with open(path, "r", encoding="utf-8") as fh:
                    head = fh.read()
            steps.append((done, total, size, head))

        JsonWriter(out).write(stream, progress)
        assert steps, "进度回调没被调用"
        # 第 1 个会话写完时，磁盘上的文件里已经有它的内容了
        done, _total, size, head = steps[0]
        assert size > 0, "第 1 个会话写完后文件还是空的（说明是攒完才落盘）"
        assert "大会话0" in head, head[:300]
        # 文件只增不减
        sizes = [s for _d, _t, s, _h in steps]
        assert sizes == sorted(sizes), sizes
        assert sizes[-1] > 0
        # 最终产物合法
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["chat_count"] == 3, data["chat_count"]
        assert data["message_count"] == 3 * 400, data["message_count"]
    finally:
        _cleanup(out)


def test_json_does_not_accumulate_in_memory():
    """JSON 导出不能把会话片段攒在内存里（旧实现会把峰值撑到产物大小的 2~3 倍）。"""
    chats = _big_chats(n_chats=3, n_msgs=2000)
    out = _tmpdir()
    try:
        tracemalloc.start()
        JsonWriter(out).write(chats)
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        size = os.path.getsize(os.path.join(out, "qq_export.json"))
        assert size > 1024, size
        # 峰值内存必须明显小于产物本身 —— 说明是「边序列化边写」而不是「攒完再写」
        assert peak < size, ("峰值内存 %.1f MB >= 产物 %.1f MB" % (peak / 1e6, size / 1e6))
    finally:
        _cleanup(out)


def test_json_single_chat_failure_keeps_valid_json():
    """单个会话序列化失败不中断整体导出，产物仍然是合法 JSON。"""
    good = make_chats()[0]
    out = _tmpdir()
    try:
        writer = JsonWriter(out)
        writer.write([good, BoomChat(), make_chats()[1]])
        path = os.path.join(out, "qq_export.json")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["chat_count"] == 2, data["chat_count"]
        assert len(data["chats"]) == 2, len(data["chats"])
        assert writer.errors, "失败应当记进 writer.errors"
        assert "boom" in " ".join(writer.errors), writer.errors
    finally:
        _cleanup(out)


def test_json_failure_in_middle_rolls_back():
    """会话写了一半失败时，已经写出去的部分会回滚，JSON 仍然合法。"""
    class HalfBoom:
        """会话字段能写，但消息写到一半炸了。"""

        name = "半路炸的会话"
        peer = ""
        chat_id = "half"
        is_group = False
        count = 2
        total_count = 2

        def __init__(self, messages):
            self.messages = messages

        def to_dict(self, with_messages: bool = True) -> dict:
            return {
                "chat_id": self.chat_id,
                "name": self.name,
                "peer": self.peer,
                "is_group": self.is_group,
                "count": self.count,
                "total_count": self.total_count,
                "first_time": 0,
                "last_time": 0,
                "last_time_text": "",
                "preview": "",
                "source": "nt_msg",
            }

    class BadMsg:
        def to_dict(self):
            raise RuntimeError("半路炸")

    out = _tmpdir()
    try:
        writer = JsonWriter(out)
        writer.write([make_chats()[0], HalfBoom([BadMsg()])])
        path = os.path.join(out, "qq_export.json")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["chat_count"] == 1, data["chat_count"]
        assert len(data["chats"]) == 1, len(data["chats"])
        assert writer.errors, writer.errors
    finally:
        _cleanup(out)


# --------------------------------------------------------------------------
# 老用法继续可用
# --------------------------------------------------------------------------
def test_plain_list_still_works():
    """普通 list[Chat]（老用法）继续可用。"""
    chats = make_chats()
    for fmt in ALL_FORMATS:
        if fmt == "json":
            fn = json_write_chats
        else:
            fn = PER_CHAT_WRITERS[fmt][1]
        out = _tmpdir()
        try:
            files = fn(chats, out)
            assert files, fmt
            for path in files:
                assert os.path.exists(path), path
                assert os.path.getsize(path) > 0, path
        finally:
            _cleanup(out)


def test_write_export_with_stream():
    """write_export() 喂流式源也能用（UI 走的就是这条路）。"""
    chats = make_chats()
    for fmt in ALL_FORMATS:
        stream = FakeStream(chats)
        out = _tmpdir()
        try:
            res = writers.write_export(fmt, stream, out)
            assert res.ok, (fmt, res.error)
            assert res.chats == len(chats), (fmt, res.chats)
            assert res.messages == sum(c.count for c in chats), (fmt, res.messages)
            assert stream.iter_calls == 1, (fmt, stream.iter_calls)
        finally:
            _cleanup(out)


def test_write_export_with_plain_list():
    """write_export() 喂普通列表也没问题。"""
    chats = make_chats()
    for fmt in ALL_FORMATS:
        out = _tmpdir()
        try:
            res = writers.write_export(fmt, chats, out)
            assert res.ok, (fmt, res.error)
            assert res.chats == len(chats), (fmt, res.chats)
            assert res.messages == sum(c.count for c in chats), (fmt, res.messages)
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
        print("STREAM WRITER TESTS FAILED")
        return 1
    print("all stream writer tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
