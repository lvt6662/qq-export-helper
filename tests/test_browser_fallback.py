# -*- coding: utf-8 -*-
"""浏览器兜底逻辑自测：应用窗口白屏时要能自动改用默认浏览器。

覆盖 ``qqexport.app._wait_page_loaded`` 的三种结局，以及服务端的
``index_hits`` 计数（浏览器真的把首页取走了才会 +1）。

不依赖 pytest：可直接 ``python tests/test_browser_fallback.py`` 运行。
"""

import os
import sys
import threading
import time
import urllib.request

os.environ["QQEXPORT_NO_NOTIFY"] = "1"   # 测试时不要往用户桌面弹消息框

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qqexport import app as app_module  # noqa: E402
from qqexport.ui import server as srv_mod  # noqa: E402


class _AliveProc:
    """假装浏览器进程还活着。"""

    def poll(self):
        return None

    def terminate(self):
        return None

    pid = 12345


class _DeadProc:
    """假装浏览器进程已经退出。"""

    def poll(self):
        return 0

    def terminate(self):
        return None

    pid = 12345


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------
def test_index_hits_counts_home_page_requests():
    """请求首页时 index_hits 必须 +1（只是诊断用，不能拿它判断白屏）。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    try:
        assert srv.index_hits == 0
        with urllib.request.urlopen(base + "/", timeout=30) as resp:
            assert resp.status == 200
        assert srv.index_hits == 1, srv.index_hits
        with urllib.request.urlopen(base + "/", timeout=30) as resp:
            assert resp.status == 200
        assert srv.index_hits == 2, srv.index_hits
        # 其它静态资源不算首页
        with urllib.request.urlopen(base + "/app.js", timeout=30) as resp:
            assert resp.status == 200
        assert srv.index_hits == 2, srv.index_hits
    finally:
        srv.stop()


def _beacon(base):
    """模拟界面里的 JS 跑起来后回报一声。"""
    req = urllib.request.Request(base + "/api/ui-alive", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def test_ui_alive_beacon_sets_flag():
    """界面里的 JS 回报后，服务端要认得出来。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    try:
        assert srv.ui_alive is False
        _beacon(base)
        assert srv.ui_alive is True
    finally:
        srv.stop()


def test_home_page_fetched_without_beacon_is_still_blank():
    """回归：首页被下载了但界面没跑起来（白屏）时，不能算 loaded。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    try:
        with urllib.request.urlopen(base + "/", timeout=30) as resp:
            resp.read()
        assert srv.index_hits == 1
        assert srv.ui_alive is False
        state = app_module._wait_page_loaded(srv, _AliveProc(), timeout=0.6)
        assert state == "timeout", state
    finally:
        srv.stop()


def test_wait_page_loaded_returns_loaded():
    """界面回报过了就应立刻返回 loaded。"""
    srv = srv_mod.QQExportServer()
    srv.ui_alive = True
    start = time.time()
    state = app_module._wait_page_loaded(srv, _AliveProc(), timeout=5.0)
    assert state == "loaded", state
    assert time.time() - start < 1.0


def test_wait_page_loaded_times_out_on_blank_window():
    """窗口开着但首页一直没被请求 → timeout（触发兜底改用默认浏览器）。"""
    srv = srv_mod.QQExportServer()
    state = app_module._wait_page_loaded(srv, _AliveProc(), timeout=0.6)
    assert state == "timeout", state


def test_wait_page_loaded_detects_closed_browser():
    """浏览器进程没了 → closed（用户主动关窗时不该再弹浏览器）。"""
    srv = srv_mod.QQExportServer()
    state = app_module._wait_page_loaded(srv, _DeadProc(), timeout=5.0)
    assert state == "closed", state


def test_wait_page_loaded_accepts_late_request():
    """浏览器刚退出但界面随后才回报 → 仍算 loaded。"""
    srv = srv_mod.QQExportServer()

    def hit_later():
        time.sleep(0.4)
        srv.ui_alive = True

    threading.Thread(target=hit_later, daemon=True).start()
    state = app_module._wait_page_loaded(srv, _DeadProc(), timeout=5.0)
    assert state == "loaded", state


def test_wait_page_loaded_without_process():
    """proc 为 None 时只按首页请求判断，超时返回 timeout。"""
    srv = srv_mod.QQExportServer()
    assert app_module._wait_page_loaded(srv, None, timeout=0.4) == "timeout"


# --------------------------------------------------------------------------
# _open_interface：白屏 → 改用默认浏览器
# --------------------------------------------------------------------------
class _FakeBrowser:
    """假的 webbrowser.open：调用时去真的把首页取一次。"""

    def __init__(self, srv, base, works=True):
        self.srv = srv
        self.base = base
        self.works = works
        self.called = 0

    def open(self, url, *a, **kw):
        self.called += 1
        if self.works:
            _beacon(self.base)
        return self.works


def test_open_interface_loaded():
    """界面正常跑起来 → 状态 loaded，浏览器进程原样交回。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    orig = app_module.open_window
    try:
        def fake_open(url, profile="", **kw):
            _beacon(base)
            return True, _AliveProc(), True
        app_module.open_window = fake_open
        proc, owns, state = app_module._open_interface(srv, base, timeout=5.0)
        assert state == "loaded", state
        assert owns is True
        assert proc is not None
    finally:
        app_module.open_window = orig
        srv.stop()


def test_open_interface_closed_window_is_not_reopened():
    """用户自己把窗口关掉了 → 状态 closed，不再多开浏览器。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    orig = app_module.open_window
    orig_wb = app_module.webbrowser.open
    calls = []
    try:
        app_module.open_window = lambda url, profile="", **kw: (True, _DeadProc(), True)
        app_module.webbrowser.open = lambda *a, **kw: calls.append(a) or True
        _proc, _owns, state = app_module._open_interface(srv, base, timeout=5.0)
        assert state == "closed", state
        assert not calls, "关闭窗口后不该再打开浏览器"
    finally:
        app_module.open_window = orig
        app_module.webbrowser.open = orig_wb
        srv.stop()


def test_open_interface_falls_back_to_default_browser():
    """应用窗口一直白屏 → 关掉它并改用默认浏览器。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    orig = app_module.open_window
    orig_wb = app_module.webbrowser.open
    closed = []
    try:
        app_module.open_window = lambda url, profile="", **kw: (True, _AliveProc(), True)
        fake = _FakeBrowser(srv, base, works=True)
        app_module.webbrowser.open = fake.open
        orig_close = app_module._close_browser
        app_module._close_browser = lambda p: closed.append(p)
        _proc, owns, state = app_module._open_interface(srv, base, timeout=0.8)
        assert state == "fallback", state
        assert fake.called == 1, fake.called
        assert len(closed) == 2, "白屏的窗口（普通/无沙箱两次）都应该被关掉"
        assert owns is False
    finally:
        app_module.open_window = orig
        app_module.webbrowser.open = orig_wb
        app_module._close_browser = orig_close
        srv.stop()


def test_open_interface_retries_without_browser_sandbox():
    """白屏时先用「关闭浏览器沙箱」重开一次，成功就不再动默认浏览器。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    orig = app_module.open_window
    orig_wb = app_module.webbrowser.open
    calls = []
    try:
        def fake_open(url, profile="", **kw):
            calls.append(kw)
            if kw.get("no_sandbox"):
                # 第二次（无沙箱）这次把页面真的取回来
                with urllib.request.urlopen(base + "/", timeout=30) as resp:
                    resp.read()
                with urllib.request.urlopen(urllib.request.Request(
                        base + "/api/ui-alive", method="POST"), timeout=30) as resp:
                    resp.read()
            return True, _AliveProc(), True
        app_module.open_window = fake_open
        app_module.webbrowser.open = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("不该走到默认浏览器"))
        _proc, _owns, state = app_module._open_interface(srv, base, timeout=0.8)
        assert state == "loaded", state
        assert len(calls) == 2, calls
        assert calls[0].get("no_sandbox") in (None, False)
        assert calls[1].get("no_sandbox") is True
    finally:
        app_module.open_window = orig
        app_module.webbrowser.open = orig_wb
        srv.stop()


def test_open_interface_failed_when_default_browser_also_fails():
    """默认浏览器也打不开 → 状态 failed（消息框里会给出地址）。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    orig = app_module.open_window
    orig_wb = app_module.webbrowser.open
    try:
        app_module.open_window = lambda url, profile="", **kw: (True, _AliveProc(), True)
        app_module.webbrowser.open = lambda *a, **kw: False
        _proc, _owns, state = app_module._open_interface(srv, base, timeout=0.5)
        assert state == "failed", state
    finally:
        app_module.open_window = orig
        app_module.webbrowser.open = orig_wb
        srv.stop()


def test_open_interface_reports_failure_to_start_browser():
    """浏览器根本起不来 → 状态 failed。"""
    srv = srv_mod.QQExportServer()
    base, _port = srv.start("127.0.0.1", 0)
    orig = app_module.open_window
    try:
        app_module.open_window = lambda url, profile="", **kw: (False, None, False)
        _proc, _owns, state = app_module._open_interface(srv, base, timeout=1.0)
        assert state == "failed", state
    finally:
        app_module.open_window = orig
        srv.stop()


def main():
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %s: %r" % (name, exc))
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
