# -*- coding: utf-8 -*-
"""test_accounts.py —— 多账号 / 口令缓存 / 账号感知接口的测试。

沿用项目里"无 pytest"的风格：直接 ``python tests/test_accounts.py`` 跑，
每个 ``test_*`` 函数返回 True（通过）或抛异常（失败）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qqexport import accounts  # noqa: E402
from qqexport.ui import server as srv_mod  # noqa: E402


# ---------------------------------------------------------------- 工具
def _make_tree(root: str, uin: str, size: int = 1024, mtime: float = 1000.0) -> str:
    """造一个假的 "Tencent Files\\<uin>\\nt_qq\\nt_db\\nt_msg.db"。"""
    d = os.path.join(root, uin, "nt_qq", "nt_db")
    os.makedirs(d, exist_ok=True)
    db = os.path.join(d, "nt_msg.db")
    with open(db, "wb") as fh:
        fh.write(b"x" * size)
    os.utime(db, (mtime, mtime))
    return db


def _with_home(fn):
    """把运行目录指到临时目录（口令缓存别写到真实目录里）。

    不用 ``tempfile.mkdtemp``：它在 Windows 上会建出 0o700 权限的目录，
    受限沙箱下连子目录都建不了（``WinError 5``）。这里用普通的 makedirs
    + 随机名，跟 test_pdf_writer_stream.py 的做法一致。
    """
    old = os.environ.get("QQEXPORT_HOME")
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_tests")
    os.makedirs(base, exist_ok=True)
    tmp = os.path.join(base, "qqexport_acct_" + uuid.uuid4().hex[:10])
    os.makedirs(tmp, exist_ok=True)
    os.environ["QQEXPORT_HOME"] = tmp
    try:
        return fn(tmp)
    finally:
        if old is None:
            os.environ.pop("QQEXPORT_HOME", None)
        else:
            os.environ["QQEXPORT_HOME"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def _tencent_root(tmp: str) -> str:
    return os.path.join(tmp, "Documents", "Tencent Files")


# ---------------------------------------------------------------- 用例
def test_list_accounts_finds_every_uin():
    def body(tmp):
        root = _tencent_root(tmp)
        db1 = _make_tree(root, "10001")
        db2 = _make_tree(root, "10002")
        old = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = tmp
        try:
            items = accounts.list_accounts()
        finally:
            if old is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = old
        uins = sorted(str(i["uin"]) for i in items)
        assert uins == ["10001", "10002"], uins
        paths = sorted(i["db_path"] for i in items)
        assert db1 in paths and db2 in paths
        assert all(i["state"] == "no_key" for i in items), items
        assert all(i["has_key"] is False for i in items)
    _with_home(body)


def test_set_and_get_key_roundtrip():
    def body(tmp):
        db = _make_tree(_tencent_root(tmp), "20002")
        assert accounts.set_key(db, "0123456789abcdef") is True
        assert accounts.get_key(db) == "0123456789abcdef"
        cache = accounts.load_key_cache()
        assert cache, cache
        with open(accounts.key_cache_path(), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        assert any(v.get("key") == "0123456789abcdef" for v in raw.values()), raw
    _with_home(body)


def test_key_state_ready_and_stale():
    def body(tmp):
        import time as _time

        db = _make_tree(_tencent_root(tmp), "30003", mtime=_time.time())
        accounts.set_key(db, "0123456789abcdef")
        old = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = tmp
        try:
            items = {str(i["uin"]): i for i in accounts.list_accounts()}
        finally:
            if old is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = old
        item = items.get("30003")
        if item is not None:
            assert item["state"] in ("ready", "stale"), item
            assert item["has_key"] is True
    _with_home(body)


def test_forget_key():
    def body(tmp):
        db = _make_tree(_tencent_root(tmp), "40004")
        accounts.set_key(db, "0123456789abcdef")
        assert accounts.get_key(db) == "0123456789abcdef"
        accounts.forget_key(db)
        assert accounts.get_key(db) != "0123456789abcdef"
    _with_home(body)


def test_parse_pct():
    assert srv_mod._parse_pct("正在处理 42%") == 42.0
    assert srv_mod._parse_pct("完成 100%") == 100.0
    assert srv_mod._parse_pct("读取完成：454 个会话") is None
    assert srv_mod._parse_pct("") is None
    assert srv_mod._parse_pct("120%") == 100.0


def test_looks_like_key_error():
    assert srv_mod._looks_like_key_error(RuntimeError("file is not a database"))
    assert srv_mod._looks_like_key_error(RuntimeError("无法用该口令打开加密库"))
    assert not srv_mod._looks_like_key_error(RuntimeError("消息库不存在"))


def test_state_reports_qq_running():
    """界面要靠 qq.running 判断「QQ 已登录但没密钥」，所以它必须一直在 state 里。"""

    def body(tmp):
        srv = srv_mod.QQExportServer()
        base, _port = srv.start()
        try:
            import urllib.request

            with urllib.request.urlopen(base + "/api/state", timeout=10) as resp:
                st = json.loads(resp.read().decode("utf-8"))
            qq = st.get("qq") or {}
            assert "running" in qq, qq
            assert isinstance(qq.get("running"), bool), qq
            assert isinstance(qq.get("running_count"), int), qq
        finally:
            srv.stop()
    _with_home(body)


def test_qq_running_never_raises():
    """受限令牌下 tasklist 会被拒，qq_running 必须自己扛住（不能抛异常给界面）。"""
    from qqexport import key as key_mod

    n = key_mod.qq_running()
    assert isinstance(n, int) and n >= 0, n
    assert isinstance(key_mod._qq_pids(), list)


def test_close_qq_noop_when_not_running(monkeypatch=None):
    """QQ 没在跑时 close_qq() 直接返回 0，且不会误删任何东西。"""
    from qqexport import key as key_mod

    if key_mod.qq_running() == 0:
        assert key_mod.close_qq() == 0


def test_state_running_true_when_qq_running():
    """把 qq_running() 打桩成 3，state.qq.running 必须变成 True ——
    界面就是靠这个字段决定「弹识别密钥引导」的。"""
    from qqexport import key as key_mod

    original = key_mod.qq_running
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return 3

    def body(tmp):
        key_mod.qq_running = fake
        try:
            import urllib.request

            srv = srv_mod.QQExportServer()
            base, _port = srv.start()
            try:
                with urllib.request.urlopen(base + "/api/state", timeout=10) as resp:
                    st = json.loads(resp.read().decode("utf-8"))
                qq = st.get("qq") or {}
                assert qq.get("running") is True, qq
                assert qq.get("running_count") == 3, qq
                # 3 秒缓存：连续取 state 不该反复做进程快照
                first = calls["n"]
                with urllib.request.urlopen(base + "/api/state", timeout=10) as resp:
                    json.loads(resp.read().decode("utf-8"))
                assert calls["n"] == first, (first, calls["n"])
            finally:
                srv.stop()
        finally:
            key_mod.qq_running = original
    _with_home(body)


def test_server_accounts_endpoint():
    def body(tmp):
        srv = srv_mod.QQExportServer()
        base, _port = srv.start()
        try:
            import urllib.request

            with urllib.request.urlopen(base + "/api/accounts", timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            assert data.get("ok") is True, data
            assert isinstance(data.get("accounts"), list)
            assert "current_uin" in data

            with urllib.request.urlopen(base + "/api/state", timeout=10) as resp:
                st = json.loads(resp.read().decode("utf-8"))
            for field in ("accounts", "current_uin", "progress_pct", "progress_text"):
                assert field in st, (field, sorted(st.keys()))
        finally:
            srv.stop()
    _with_home(body)


def test_server_set_key_with_uin():
    def body(tmp):
        srv = srv_mod.QQExportServer()
        base, _ = srv.start()
        try:
            import urllib.request

            payload = json.dumps({"key": "0123456789abcdef", "uin": ""}).encode("utf-8")
            req = urllib.request.Request(
                base + "/api/key", data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            assert data.get("key_ready") is True, data
        finally:
            srv.stop()
    _with_home(body)


def test_server_qq_close_endpoint():
    def body(tmp):
        srv = srv_mod.QQExportServer()
        base, _ = srv.start()
        try:
            import urllib.request

            req = urllib.request.Request(base + "/api/qq/close", data=b"{}", method="POST")
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            assert "ok" in data, data
            assert "closed" in data, data
        finally:
            srv.stop()
    _with_home(body)


def test_load_without_db_reports_error():
    def body(tmp):
        srv = srv_mod.QQExportServer()
        try:
            srv.db_path = ""
            result = srv.load()
            assert result.get("stage") in ("error", "idle"), result
        finally:
            srv.stop()
    _with_home(body)


# ---------------------------------------------------------------- 目录探测
def test_extra_root_makes_accounts_visible():
    """用户手动指定的目录（不在文档下）也能被认出来 —— 这是换机器最常见的情况。"""
    def body(tmp):
        custom = os.path.join(tmp, "我的QQ数据", "Tencent Files")
        db = _make_tree(custom, "30003")
        old = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = os.path.join(tmp, "nohome")
        try:
            assert accounts.list_accounts() == []
            assert accounts.add_extra_root(custom) is True
            items = accounts.list_accounts()
            uins = [str(i["uin"]) for i in items]
            assert "30003" in uins, uins
            assert any(i["db_path"] == db for i in items)
            # 再写一次不应该重复
            assert accounts.add_extra_root(custom) is True
            roots = accounts.extra_roots()
            assert len([r for r in roots if os.path.normcase(r) == os.path.normcase(custom)]) == 1
        finally:
            if old is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = old
    _with_home(body)


def test_add_extra_root_rejects_missing_dir():
    def body(tmp):
        assert accounts.add_extra_root(os.path.join(tmp, "not-here")) is False
        assert accounts.add_extra_root("") is False
    _with_home(body)


def test_deep_scan_finds_tencent_files():
    """全盘扫描（这里只扫一个假的"磁盘"）能发现深层目录并自动记住。"""
    def body(tmp):
        fake_drive = os.path.join(tmp, "fakedrive")
        deep = os.path.join(fake_drive, "Users", "someone", "Documents", "Tencent Files")
        db = _make_tree(deep, "40004")
        old = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = os.path.join(tmp, "nohome")
        try:
            logs = []
            result = accounts.deep_scan(progress=logs.append,
                                        drives=[fake_drive], max_depth=5,
                                        time_budget=20.0)
            assert result["ok"] is True
            assert any(os.path.normcase(r) == os.path.normcase(deep)
                       for r in result["found_roots"]), result["found_roots"]
            assert result["added"] >= 1, result
            items = accounts.list_accounts()
            assert any(i["db_path"] == db for i in items), items
            assert any("发现消息库" in line for line in logs), logs
        finally:
            if old is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = old
    _with_home(body)


def test_diagnose_mentions_roots_and_accounts():
    def body(tmp):
        root = _tencent_root(tmp)
        _make_tree(root, "50005")
        old = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = tmp
        try:
            text = accounts.diagnose()
        finally:
            if old is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = old
        assert "QQ导出助手 诊断报告" in text
        assert "50005" in text
        assert "已扫描的候选根目录" in text
    _with_home(body)


def test_server_set_data_root_endpoint():
    def body(tmp):
        custom = os.path.join(tmp, "custom", "Tencent Files")
        _make_tree(custom, "60006")
        old = os.environ.get("USERPROFILE")
        os.environ["USERPROFILE"] = os.path.join(tmp, "nohome")
        srv = srv_mod.QQExportServer()
        try:
            srv.db_path = ""
            payload = srv.set_data_root(custom)
            uins = [str(i["uin"]) for i in payload.get("accounts") or []]
            assert "60006" in uins, payload
            assert payload.get("hint") == "", payload.get("hint")
        finally:
            srv.stop()
            if old is None:
                os.environ.pop("USERPROFILE", None)
            else:
                os.environ["USERPROFILE"] = old
    _with_home(body)


def test_server_set_data_root_rejects_bad_path():
    def body(tmp):
        srv = srv_mod.QQExportServer()
        try:
            try:
                srv.set_data_root(os.path.join(tmp, "nope"))
            except ValueError as exc:
                assert "Tencent Files" in str(exc), str(exc)
            else:
                raise AssertionError("坏路径应该报错")
        finally:
            srv.stop()
    _with_home(body)


def test_server_diagnose_text_written():
    def body(tmp):
        srv = srv_mod.QQExportServer()
        try:
            text = srv.diagnose_text()
            assert "诊断报告" in text
            saved = os.path.join(tmp, "diagnose.txt")
            assert os.path.isfile(saved), saved
        finally:
            srv.stop()
    _with_home(body)


# ---------------------------------------------------------------- 启动时自动兜底扫描
def _patch_accounts(list_result, deep_calls):
    """把 accounts 的枚举/扫描换成假的，返回还原用的原始函数。"""
    old = (accounts.list_accounts, accounts.deep_scan)
    accounts.list_accounts = lambda: list(list_result)

    def fake_deep(progress=None, **kwargs):  # noqa: ANN001, ANN003
        deep_calls.append(kwargs)
        if progress is not None:
            progress("全盘扫描：已检查 100 个目录")
            progress("全盘扫描：已检查 200 个目录")
        return {"ok": True, "visited": 200, "found_roots": [], "found_dbs": [],
                "added": 0, "accounts": 0, "elapsed": 0.1}

    accounts.deep_scan = fake_deep
    return old


def _restore_accounts(old):
    accounts.list_accounts, accounts.deep_scan = old


def _wait_for(check, timeout=5.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(step)
    return check()


def test_auto_deep_scan_runs_when_no_accounts():
    """常规位置一个账号都没找到时，应自动排一次全盘扫描（不需要用户点按钮）。"""
    def body(tmp):
        deep_calls = []
        old = _patch_accounts([], deep_calls)
        os.environ["QQEXPORT_AUTOSCAN_DELAY"] = "0.02"
        srv = srv_mod.QQExportServer()
        try:
            srv.accounts = []
            srv.task = ""
            srv._maybe_auto_deep_scan()
            assert _wait_for(lambda: bool(deep_calls)), "没有自动触发全盘扫描"
            assert srv._auto_scan_started is True
        finally:
            srv.stop()
            _restore_accounts(old)
            os.environ.pop("QQEXPORT_AUTOSCAN_DELAY", None)

    _with_home(body)


def test_auto_deep_scan_skipped_when_accounts_found():
    """已经找到账号就别再扫盘了。"""
    def body(tmp):
        deep_calls = []
        old = _patch_accounts([{"uin": "12345", "db_path": "x", "state": "ready"}], deep_calls)
        srv = srv_mod.QQExportServer()
        try:
            srv.accounts = [{"uin": "12345"}]
            srv.task = ""
            srv._maybe_auto_deep_scan()
            time.sleep(0.3)
            assert not deep_calls, deep_calls
            assert srv._auto_scan_started is False
        finally:
            srv.stop()
            _restore_accounts(old)

    _with_home(body)


def test_auto_deep_scan_disabled_by_env():
    """QQEXPORT_NO_AUTOSCAN=1 时完全不自动扫描（测试/自动化用）。"""
    def body(tmp):
        deep_calls = []
        old = _patch_accounts([], deep_calls)
        os.environ["QQEXPORT_NO_AUTOSCAN"] = "1"
        srv = srv_mod.QQExportServer()
        try:
            srv.accounts = []
            srv.task = ""
            srv._maybe_auto_deep_scan()
            time.sleep(0.3)
            assert not deep_calls, deep_calls
        finally:
            srv.stop()
            _restore_accounts(old)
            os.environ.pop("QQEXPORT_NO_AUTOSCAN", None)

    _with_home(body)


def test_scan_calls_auto_deep_scan():
    """scan() 结束时必须去排自动兜底扫描，否则启动时不会自动找。"""
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "src", "qqexport", "ui", "server.py")
    with open(src, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "self._maybe_auto_deep_scan()" in text
    # 必须在 scan() 里调用：取 scan 到 _load_saved_key 之间那段
    start = text.index("    def scan(self) -> dict:")
    end = text.index("    def _load_saved_key(self)")
    assert "_maybe_auto_deep_scan()" in text[start:end]


# ---------------------------------------------------------------- 入口
def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = failed = 0
    for name, func in tests:
        try:
            func()
            print("PASS %s" % name)
            passed += 1
        except Exception as exc:  # noqa: BLE001
            import traceback

            print("FAIL %s: %s" % (name, exc))
            traceback.print_exc()
            failed += 1
    print("---- %d passed, %d failed ----" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
