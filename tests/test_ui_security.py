# -*- coding: utf-8 -*-
"""本机 HTTP 服务的安全护栏：Host / Origin 校验（防 DNS rebinding 与跨站请求）。

不依赖 pytest：可直接 ``python tests/test_ui_security.py`` 运行。
"""

import http.client
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from qqexport.ui import server as srv_mod  # noqa: E402


def _raw(port, method, path, headers):
    """发一个可自定义 Host / Origin 的原始请求，返回 (状态码, 文本)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        body = b""
        if method == "POST":
            body = b"{}"
            conn.putheader("Content-Length", str(len(body)))
            conn.putheader("Content-Type", "application/json")
        conn.endheaders(body if body else None)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _with_server(fn):
    srv = srv_mod.QQExportServer()
    base, port = srv.start("127.0.0.1", 0)
    try:
        return fn(srv, base, port)
    finally:
        srv.stop()


def test_hostname_of_parsing():
    """Host / Origin 解析：端口、scheme、IPv6 方括号都要处理。"""
    assert srv_mod._hostname_of("127.0.0.1:57011") == "127.0.0.1"
    assert srv_mod._hostname_of("localhost:8080") == "localhost"
    assert srv_mod._hostname_of("http://127.0.0.1:57011") == "127.0.0.1"
    assert srv_mod._hostname_of("https://evil.example.com/x") == "evil.example.com"
    assert srv_mod._hostname_of("[::1]:57011") == "::1"
    assert srv_mod._hostname_of("") == ""


def test_local_host_allowed():
    """本机 Host（127.0.0.1 / localhost）正常放行。"""

    def run(srv, base, port):
        status, body = _raw(port, "GET", "/api/state", {"Host": "127.0.0.1:%d" % port})
        assert status == 200, (status, body[:200])
        status, body = _raw(port, "GET", "/api/state", {"Host": "localhost:%d" % port})
        assert status == 200, (status, body[:200])

    _with_server(run)


def test_foreign_host_rejected():
    """DNS rebinding：Host 是外部域名时必须 403。"""

    def run(srv, base, port):
        status, body = _raw(port, "GET", "/api/state", {"Host": "evil.example.com"})
        assert status == 403, (status, body[:200])
        assert "error" in body
        # 静态资源同样拦
        status, body = _raw(port, "GET", "/", {"Host": "evil.example.com"})
        assert status == 403, (status, body[:200])

    _with_server(run)


def test_foreign_origin_rejected():
    """跨站请求：Origin 不是本机时 POST 必须 403。"""

    def run(srv, base, port):
        host = "127.0.0.1:%d" % port
        status, body = _raw(port, "POST", "/api/scan",
                            {"Host": host, "Origin": "https://evil.example.com"})
        assert status == 403, (status, body[:200])
        # 同源 Origin 正常放行
        status, body = _raw(port, "POST", "/api/scan",
                            {"Host": host, "Origin": "http://127.0.0.1:%d" % port})
        assert status == 200, (status, body[:200])
        # 没有 Origin（curl / 原生窗口的部分请求）也放行
        status, body = _raw(port, "POST", "/api/scan", {"Host": host})
        assert status == 200, (status, body[:200])

    _with_server(run)


def test_allow_remote_env_bypass():
    """QQEXPORT_ALLOW_REMOTE=1 时关闭校验（内网穿透等场景）。"""

    def run(srv, base, port):
        status, body = _raw(port, "GET", "/api/state", {"Host": "evil.example.com"})
        assert status == 403, (status, body[:200])
        os.environ["QQEXPORT_ALLOW_REMOTE"] = "1"
        try:
            status, body = _raw(port, "GET", "/api/state", {"Host": "evil.example.com"})
            assert status == 200, (status, body[:200])
        finally:
            os.environ.pop("QQEXPORT_ALLOW_REMOTE", None)

    _with_server(run)


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %s: %s" % (fn.__name__, exc))
        else:
            print("ok   %s" % fn.__name__)
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
