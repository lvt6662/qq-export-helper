# -*- coding: utf-8 -*-
"""UI 层自测：本地 HTTP 服务、静态资源、前后端接口契约。

不依赖 pytest：可直接 ``python tests/test_ui.py`` 运行；
也可以用 pytest 收集（函数名以 test_ 开头）。
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qqexport import app as app_module  # noqa: E402
from qqexport.model import Attachment, Chat, Message  # noqa: E402
from qqexport.ui import server as srv_mod  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(HERE, "_scratch")


class _Skipped(Exception):
    """测试被跳过（缺少可选依赖等）。"""


def _skip(msg):
    raise _Skipped(msg)


def _scratch_dir():
    """临时目录必须放在工作区内（沙箱禁止写 %TEMP%）。"""
    os.makedirs(SCRATCH, exist_ok=True)
    return SCRATCH


# --------------------------------------------------------------------------
# HTTP 小工具
# --------------------------------------------------------------------------
def _request(method, url, payload=None, timeout=30):
    """发一个请求，返回 ``(状态码, 响应文本, Content-Type)``；错误状态不抛异常。"""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, body, resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body, exc.headers.get("Content-Type", "")


def _json_request(method, url, payload=None):
    status, body, ctype = _request(method, url, payload)
    try:
        data = json.loads(body) if body else None
    except ValueError:
        data = None
    return status, data, ctype


def _sample_chat():
    """构造一个带两条消息的会话。"""
    chat = Chat(chat_id="10001", name="测试好友", peer="10001", is_group=False)
    chat.messages.append(Message(
        msg_id=1, time=1737113429, direction=2, sender="对方",
        msg_type=2, text="在吗", content="在吗", is_mine=False,
    ))
    chat.messages.append(Message(
        msg_id=2, time=1737113500, direction=1, sender="我",
        msg_type=3, text="文件", content="[文件] 报告.pdf", is_mine=True,
        attachments=[Attachment(kind="file", name="报告.pdf", size=2048,
                                local_path=r"C:\Users\<用户名>\Desktop\报告.pdf")],
    ))
    return chat


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------
def test_static_assets_exist():
    """三个静态文件必须齐全（前端零 CDN 依赖）。"""
    for name in ("index.html", "app.js", "style.css"):
        path = os.path.join(srv_mod.STATIC_DIR, name)
        assert os.path.isfile(path), path
        assert os.path.getsize(path) > 0, path


def test_static_assets_have_no_external_urls():
    """静态资源里不能出现外链（离线可用、也避免第三方脚本）。"""
    for name in ("index.html", "app.js", "style.css"):
        path = os.path.join(srv_mod.STATIC_DIR, name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for token in ("http://", "https://", "//cdn.", "cdnjs", "unpkg", "jsdelivr"):
            assert token not in text, "%s 里出现了外链 %s" % (name, token)


def test_index_html_has_core_panels():
    """界面骨架：会话列表 / 聊天区 / 工具栏 / 状态栏 / 口令与导出弹窗。"""
    path = os.path.join(srv_mod.STATIC_DIR, "index.html")
    with open(path, "r", encoding="utf-8") as fh:
        html = fh.read()
    for element_id in ("chatList", "msgScroll", "btnScan", "btnAutoKey", "btnLoad",
                       "btnExport", "statusText", "keyModal", "exportModal",
                       "searchInput", "btnExit"):
        assert 'id="%s"' % element_id in html, "缺少元素 #%s" % element_id


def test_printable_ascii():
    """口令校验辅助函数。"""
    assert srv_mod._is_printable_ascii("0123456789abcdef")
    assert srv_mod._is_printable_ascii("a" * 20)
    assert not srv_mod._is_printable_ascii("")
    assert not srv_mod._is_printable_ascii("中文口令")
    assert not srv_mod._is_printable_ascii("abc\ndef")


def test_set_key_validation():
    """口令必须是 16 或 20 个可打印 ASCII 字符。"""
    srv = srv_mod.QQExportServer()
    srv.set_key("0123456789abcdef")
    assert srv.key == "0123456789abcdef"
    srv.set_key("a" * 20)
    for bad in ("", "short", "a" * 15, "a" * 17, "中文" * 8):
        try:
            srv.set_key(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("口令 %r 应当被拒绝" % bad)


def test_state_snapshot_fields():
    """状态快照必须包含前端用到的全部字段。"""
    srv = srv_mod.QQExportServer()
    state = srv.state()
    for key in ("stage", "task", "qq", "db_path", "chats", "messages",
                "error", "key_hint", "key_ready", "progress", "out_dir",
                "last_export", "modules", "app_version"):
        assert key in state, "state() 缺少字段 %s" % key
    assert isinstance(state["modules"], dict)
    assert state["stage"] == srv_mod.STAGE_IDLE
    assert state["chats"] == 0 and state["messages"] == 0


def test_load_failure_reports_error():
    """读取失败时不能抛异常，应转入 error 阶段并给出中文错误提示。"""
    srv = srv_mod.QQExportServer()
    srv.db_path = os.path.join(_scratch_dir(), "不存在的消息库.db")
    state = srv.load()
    assert state["stage"] == srv_mod.STAGE_ERROR, state
    assert state["error"], "失败时应给出错误说明"
    assert state["chats"] == 0
    # 再次读取仍然要保持 error 状态而不是崩溃
    state2 = srv.load()
    assert state2["stage"] == srv_mod.STAGE_ERROR


def test_chat_list_and_detail():
    """会话列表与单个会话（含消息）的数据结构。"""
    srv = srv_mod.QQExportServer()
    chat = _sample_chat()
    srv.chats = [chat]
    data = chat.to_dict(with_messages=False)
    assert data["count"] == 2
    assert data["preview"]
    assert "messages" not in data
    full = chat.to_dict(with_messages=True)
    assert len(full["messages"]) == 2
    assert full["messages"][0]["is_mine"] is False
    assert full["messages"][1]["is_mine"] is True
    assert full["messages"][1]["attachments"][0]["name"] == "报告.pdf"


def test_export_requires_loaded_chats():
    """没加载数据就导出必须报错（而不是生成空文件）。"""
    srv = srv_mod.QQExportServer()
    try:
        srv.export("txt", srv.out_dir)
    except RuntimeError:
        pass
    else:
        raise AssertionError("没有会话时导出应当抛出 RuntimeError")


def test_export_unknown_format():
    """未知格式要报 ValueError。"""
    srv = srv_mod.QQExportServer()
    srv.chats = [_sample_chat()]
    try:
        srv.export("不存在的格式", _scratch_dir())
    except ValueError:
        pass
    else:
        raise AssertionError("未知格式应当抛出 ValueError")


def test_formats_list_shape():
    """格式列表是 [{id,name,ext}] 结构。"""
    srv = srv_mod.QQExportServer()
    formats = srv.formats()
    assert isinstance(formats, list)
    for item in formats:
        assert set(("id", "name", "ext")) <= set(item.keys()), item


def test_http_endpoints():
    """HTTP 接口：状态、格式、会话列表、静态资源、404 均为 JSON。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    try:
        srv.chats = [_sample_chat()]

        status, data, ctype = _json_request("GET", base + "/api/state")
        assert status == 200, status
        assert "json" in ctype, ctype
        assert data["chats"] == 1
        assert data["messages"] == 2

        status, data, _ = _json_request("GET", base + "/api/formats")
        assert status == 200
        assert isinstance(data["formats"], list)

        status, data, _ = _json_request("GET", base + "/api/chats")
        assert status == 200
        assert len(data["chats"]) == 1
        assert data["chats"][0]["chat_id"] == "10001"

        status, data, _ = _json_request("GET", base + "/api/chats/10001")
        assert status == 200
        assert len(data["chat"]["messages"]) == 2

        # 不存在的会话 -> 404 + JSON
        status, data, ctype = _json_request("GET", base + "/api/chats/nope")
        assert status == 404, status
        assert data and "error" in data, data
        assert "json" in ctype, ctype

        # 未知接口 -> 404 + JSON
        status, data, _ = _json_request("GET", base + "/api/whatever")
        assert status == 404
        assert data and "error" in data

        # 静态资源
        status, body, ctype = _request("GET", base + "/")
        assert status == 200
        assert "text/html" in ctype, ctype
        assert "QQ导出助手" in body
        status, body, ctype = _request("GET", base + "/app.js")
        assert status == 200 and "javascript" in ctype, ctype
        status, body, ctype = _request("GET", base + "/style.css")
        assert status == 200 and "text/css" in ctype, ctype
    finally:
        srv.stop()


def test_http_post_endpoints():
    """POST：scan / key / load / export 都要返回 JSON，出错也不能是 HTML。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    try:
        status, data, _ = _json_request("POST", base + "/api/key", {"key": "0123456789abcdef"})
        assert status == 200, status
        assert data["key_ready"] is True

        status, data, _ = _json_request("POST", base + "/api/key", {"key": "太短"})
        assert status == 400, status
        assert "error" in data

        status, data, ctype = _json_request("POST", base + "/api/scan")
        assert status == 200, status
        assert "json" in ctype

        # 没有会话时导出 -> 400 + JSON 错误
        status, data, ctype = _json_request(
            "POST", base + "/api/export", {"format": "txt", "out_dir": _scratch_dir()})
        assert status == 400, status
        assert "json" in ctype, ctype
        assert data and "error" in data
    finally:
        srv.stop()


def test_http_static_path_traversal_blocked():
    """目录穿越必须被挡住。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    try:
        status, data, _ = _json_request("GET", base + "/../server.py")
        assert status == 404, status
        assert data and "error" in data
    finally:
        srv.stop()


def test_export_txt_end_to_end():
    """端到端：注入会话 -> 调 writers -> 产出 txt 文件。"""
    try:
        from qqexport import writers
    except Exception as exc:  # noqa: BLE001
        _skip("writers 模块不可用: %r" % exc)
    if "txt" not in getattr(writers, "FORMATS", {}):
        _skip("writers 还没有 txt 格式")

    out_root = os.path.join(_scratch_dir(), "ui_export")
    srv = srv_mod.QQExportServer()
    srv.chats = [_sample_chat()]
    result = srv.export("txt", out_root)
    assert result["ok"], result
    assert result["chats"] == 1
    assert result["messages"] == 2
    assert os.path.isdir(result["path"]), result["path"]
    produced = [f for f in os.listdir(result["path"]) if f.endswith(".txt")]
    assert produced, "没有生成 txt 文件: %s" % result["path"]
    # 清理
    for root, dirs, files in os.walk(out_root, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    if os.path.isdir(out_root):
        os.rmdir(out_root)


def test_app_cli_parser():
    """命令行参数解析。"""
    args = app_module.build_parser().parse_args([])
    assert args.out_dir == srv_mod.DEFAULT_OUT_DIR
    assert args.port == 0
    assert args.no_browser is False

    args = app_module.build_parser().parse_args(
        ["--out", r"D:\导出", "--port", "8765", "--no-browser",
         "--key", "0123456789abcdef", "--db", r"C:\x\nt_msg.db"])
    assert args.out_dir == r"D:\导出"
    assert args.port == 8765
    assert args.no_browser is True
    assert args.key == "0123456789abcdef"
    assert args.db_path == r"C:\x\nt_msg.db"


def test_find_browser():
    """浏览器探测：找不到也要返回 (None, None) 而不是抛异常。"""
    exe, kind = app_module.find_browser()
    assert (exe is None) == (kind is None)
    if exe:
        assert os.path.isfile(exe)
        assert kind in ("edge", "chrome")


def test_export_rejects_concurrent_run():
    """并发导出：同一时间只允许一个导出任务，第二个请求要拿到友好错误。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    try:
        srv.chats = [_sample_chat()]
        # 模拟「已经有一个导出在跑」：先占住锁，再调一次
        assert srv._export_lock.acquire(blocking=False)
        try:
            try:
                srv.export("txt", _scratch_dir())
            except RuntimeError as exc:
                assert "导出任务" in str(exc), exc
            else:
                raise AssertionError("并发导出没有被拒绝")
        finally:
            srv._export_lock.release()
        # 锁释放后应能正常导出
        result = srv.export("txt", _scratch_dir())
        assert result["ok"] is True, result
    finally:
        srv.stop()


def test_shutdown_endpoint_stops_service():
    """「退出」按钮：调用 /api/shutdown 后服务应停止。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    try:
        status, data, _ = _json_request("POST", base + "/api/shutdown")
        assert status == 200
        assert data["ok"] is True
        for _ in range(50):
            if srv.stopped:
                break
            import time
            time.sleep(0.1)
        assert srv.stopped, "服务没有进入停止状态"
    finally:
        srv.stop()


def main():
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except _Skipped as exc:
            print("SKIP %s (%s)" % (name, exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %s: %r" % (name, exc))
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
