# -*- coding: utf-8 -*-
"""qqexport.app —— 「QQ导出助手」桌面程序入口。

职责
----
1. 启动本地 HTTP 服务（:mod:`qqexport.ui.server`）；
2. 用 **Microsoft Edge 的 app 模式**（``msedge --app=URL``）打开一个无地址栏的
   独立窗口，尽量还原 Electron 桌面应用的观感；找不到 Edge 时依次退化到
   Chrome 的 app 模式、最后退化到系统默认浏览器；
3. 启动时自动扫描一次本机 QQ 安装与消息库；
4. 阻塞等待，直到用户在界面上点「退出」或按下 ``Ctrl+C``。

命令行参数
----------
``--out DIR``        导出目录（默认 ``D:\\QQ导出``）
``--port N``         本地服务端口（默认 0 = 随机空闲端口）
``--host HOST``      监听地址（默认 ``127.0.0.1``，仅本机可访问）
``--no-browser``     不自动打开窗口（调试用）
``--key KEY``        预先填好数据库口令（16/20 字节可打印 ASCII）
``--db PATH``        指定消息库路径（默认自动扫描）

示例::

    python -m qqexport.app
    python -m qqexport.app --out "D:\\我的导出" --no-browser
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import threading
import time
import webbrowser

from . import __version__, paths
from .ui.server import DEFAULT_OUT_DIR, QQExportServer


def _ensure_streams():
    """保证 ``sys.stdout`` / ``sys.stderr`` 可用。

    打包后的程序用 ``pythonw.exe`` 启动（没有控制台窗口），此时
    ``sys.stdout`` 与 ``sys.stderr`` 都是 ``None``：``print()`` 会静默丢弃
    输出，``stream.reconfigure()`` 也会抛异常。这里把它们换成日志文件
    （``%LOCALAPPDATA%\\QQExportHelper\\app.log``，写不了就退化为空设备），
    这样出问题时还能从日志里看到程序走了哪一步。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        handle = None
        try:
            log_dir = paths.home_dir()
            os.makedirs(log_dir, exist_ok=True)
            handle = open(os.path.join(log_dir, "app.log"), "a",
                          encoding="utf-8", buffering=1)
        except Exception:  # noqa: BLE001
            handle = None
        if handle is None:
            try:
                handle = open(os.devnull, "w", encoding="utf-8")
            except Exception:  # noqa: BLE001
                handle = io.StringIO()
        setattr(sys, name, handle)
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:  # noqa: BLE001
                setattr(sys, name, io.StringIO())
        else:
            # 启动器把输出重定向到 app.out.log / app.err.log；默认是块缓冲，
            # 出问题时看不到中间状态。这里改成行缓冲，日志随写随落盘。
            try:
                getattr(sys, name).reconfigure(line_buffering=True)
            except Exception:  # noqa: BLE001
                pass

# --------------------------------------------------------------------------
# 浏览器
# --------------------------------------------------------------------------
#: 常见的 Edge / Chrome 安装位置（依次尝试）
_BROWSER_CANDIDATES = (
    # Microsoft Edge
    (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", "edge"),
    (r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe", "edge"),
    (r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe", "edge"),
    # Google Chrome（退化方案，同样支持 --app）
    (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe", "chrome"),
    (r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe", "chrome"),
    (r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe", "chrome"),
)


def find_browser():
    """返回 ``(可执行文件路径, 类型)``；找不到时返回 ``(None, None)``。"""
    for raw, kind in _BROWSER_CANDIDATES:
        path = os.path.expandvars(raw)
        if os.path.isfile(path):
            return path, kind
    return None, None


def default_edge_profile() -> str:
    """默认的浏览器独立配置目录。

    用独立配置目录启动 Edge，浏览器进程就归我们独占：用户把窗口关掉时
    进程会退出，我们据此把整个程序一起收掉（否则只能靠界面上的 ✕）。
    """
    return os.path.join(paths.home_dir(), "edge-profile")


def open_window(url: str, width: int = 1400, height: int = 900, profile: str = "",
                app_mode: bool = False, no_sandbox: bool = False):
    """打开界面窗口。

    默认用**普通浏览器窗口**（独立配置目录，不带标签页干扰）。少数机器上
    Edge 的「应用窗口」（``--app=``）会打开却一片空白，所以它变成了可选项
    （命令行加 ``--app-window``）。

    ``no_sandbox=True`` 时给浏览器加 ``--no-sandbox``：有些机器（受限令牌、
    沙箱软件、企业策略）上 Chromium 建不出低权限令牌，渲染进程会直接崩掉，
    表现就是"窗口打开了但一片空白"。我们只用自己的独立配置目录打开本地页面，
    关掉浏览器沙箱的影响有限。

    顺序：Edge → Chrome → 系统默认浏览器。
    """
    exe, kind = find_browser()
    if exe:
        cmd = [exe]
        if app_mode:
            cmd.append("--app=%s" % url)
            cmd.append("--window-size=%d,%d" % (width, height))
        else:
            cmd += ["--new-window", url, "--window-size=%d,%d" % (width, height)]
        cmd += ["--no-first-run", "--no-default-browser-check"]
        if no_sandbox:
            cmd.append("--no-sandbox")
        own = False
        if profile:
            try:
                os.makedirs(profile, exist_ok=True)
                cmd.append("--user-data-dir=%s" % profile)
                own = True
            except OSError as exc:
                print("[提示] 无法创建浏览器配置目录，改用默认配置：%s" % exc)
        try:
            # 不接管 stdout/stderr：浏览器是独立 GUI 程序
            proc = subprocess.Popen(cmd, close_fds=True)
            return True, proc, own
        except Exception as exc:  # noqa: BLE001
            print("[警告] 启动 %s 失败：%s" % (kind, exc))
    try:
        return bool(webbrowser.open(url)), None, False
    except Exception as exc:  # noqa: BLE001
        print("[警告] 打开默认浏览器失败：%s" % exc)
        return False, None, False


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（单独抽出来方便测试）。"""
    parser = argparse.ArgumentParser(
        prog="qqexport",
        description="QQ 聊天记录导出助手 —— 本地界面 + 本地导出，全程离线。",
    )
    parser.add_argument("--out", dest="out_dir", default=DEFAULT_OUT_DIR,
                        help="导出目录（默认：%s）" % DEFAULT_OUT_DIR)
    parser.add_argument("--port", dest="port", type=int, default=0,
                        help="本地服务端口，0 表示随机（默认：0）")
    parser.add_argument("--host", dest="host", default="127.0.0.1",
                        help="监听地址（默认：127.0.0.1）。改成 0.0.0.0 供其他机器访问时，"
                             "还需设 QQEXPORT_ALLOW_REMOTE=1 才会放行非本机请求")
    parser.add_argument("--no-browser", dest="no_browser", action="store_true",
                        help="不自动打开窗口（调试用）")
    parser.add_argument("--key", dest="key", default="",
                        help="数据库口令（16 或 20 字节可打印 ASCII）")
    parser.add_argument("--db", dest="db_path", default="",
                        help="指定消息库路径（默认自动扫描）")
    parser.add_argument("--edge-profile", dest="edge_profile",
                        default="", help="浏览器独立配置目录（默认自动选择）")
    parser.add_argument("--app-window", dest="app_window", action="store_true",
                        help="用浏览器的「应用窗口」打开界面（默认是普通窗口）")
    parser.add_argument("--ui", dest="ui", default="",
                        choices=["", "window", "browser"],
                        help="界面形态：window=由原生窗口承载（只把地址写进 ui-url.txt），"
                             "browser=用浏览器打开；留空则看环境变量 QQEXPORT_UI")
    return parser


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _auto_scan(srv: QQExportServer) -> None:
    """启动后自动扫描一次（失败也不影响界面使用）。"""
    try:
        srv.scan()
    except Exception as exc:  # noqa: BLE001
        print("[提示] 自动扫描失败：%s" % exc)


# --------------------------------------------------------------------------
# 浏览器兜底：应用窗口白屏时改用默认浏览器
# --------------------------------------------------------------------------
def _wait_page_loaded(srv: QQExportServer, proc=None, timeout: float = 15.0) -> str:
    """等浏览器真的把界面显示出来。

    判据是界面里的 JS 主动回报（``POST /api/ui-alive``）——只看到首页被下载
    是不够的：有的机器上浏览器窗口会打开、也会把首页取走，却一片空白。

    返回值：

    * ``"loaded"`` —— 界面已经跑起来了；
    * ``"closed"`` —— 浏览器进程自己没了（用户关掉了窗口，或浏览器没起来）；
    * ``"timeout"`` —— 窗口还开着，可界面一直没跑起来（白屏）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if srv.ui_alive:
            return "loaded"
        if proc is not None and proc.poll() is not None:
            time.sleep(1.0)
            return "loaded" if srv.ui_alive else "closed"
        time.sleep(0.25)
    return "timeout"


def _close_browser(proc) -> None:
    """关掉我们自己启动的浏览器进程（连同它的子进程）。"""
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        pass


def _notify(text: str) -> None:
    """弹一个提示框（pythonw 没有控制台，只能用消息框）。"""
    if os.environ.get("QQEXPORT_NO_NOTIFY"):
        # 自动化测试用：不让弹窗跑到用户桌面上
        return

    def show() -> None:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, "QQ导出助手", 0x40)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=show, daemon=True).start()


def _open_interface(srv: QQExportServer, base_url: str, profile: str = "",
                    timeout: float = 15.0, app_mode: bool = False):
    """打开界面窗口；窗口空白时自动换一种方式重开。

    返回 ``(proc, owns_proc, state)``：

    * ``"loaded"`` —— 界面正常加载出来了；
    * ``"closed"`` —— 窗口被关掉了（用户主动关的）；
    * ``"fallback"`` —— 前面的办法都不行，已改用系统默认浏览器；
    * ``"failed"`` —— 没能打开任何浏览器（消息框里会给出地址）。

    兜底顺序：浏览器正常打开 → 同一浏览器加 ``--no-sandbox`` 重开
    （渲染进程建不出低权限令牌时会崩，表现为白屏）→ 系统默认浏览器。
    """
    ok, proc, owns_proc = open_window(base_url, profile=profile, app_mode=app_mode)
    if not ok:
        print("[提示] 没能自动打开浏览器，请手动访问：%s" % base_url)
        return None, False, "failed"

    state = _wait_page_loaded(srv, proc, timeout)
    if state == "loaded":
        print("界面已加载。")
        return proc, owns_proc, "loaded"
    if state == "closed":
        code = None
        try:
            code = proc.poll() if proc is not None else None
        except Exception:  # noqa: BLE001
            code = None
        if code not in (0, None):
            # 浏览器进程异常退出（崩溃/被安全软件拦截）：不是用户关窗，
            # 继续走下面的兜底流程，而不是直接退出程序。
            print("浏览器进程异常退出（代码 %s），换一种方式重开…" % code)
        else:
            print("浏览器窗口已关闭。")
            return proc, owns_proc, "closed"

    # 兜底一：渲染进程可能因为建不出低权限令牌而崩掉（白屏），关掉重来一次，
    # 这次不启用浏览器沙箱
    print("[提示] 窗口没有加载出界面，换一种方式重开…")
    _close_browser(proc)
    ok, proc, owns_proc = open_window(base_url, profile=profile,
                                       app_mode=app_mode, no_sandbox=True)
    if ok:
        state = _wait_page_loaded(srv, proc, timeout)
        if state == "loaded":
            print("界面已加载（浏览器沙箱已关闭）。")
            return proc, owns_proc, "loaded"
        _close_browser(proc)
    proc, owns_proc = None, False

    # 兜底二：改用系统默认浏览器
    print("[提示] 仍没有加载出界面，改用默认浏览器打开…")
    opened = False
    try:
        opened = bool(webbrowser.open(base_url))
    except Exception as exc:  # noqa: BLE001
        print("[警告] 打开默认浏览器失败：%s" % exc)
    if opened and _wait_page_loaded(srv, None, 10.0) == "loaded":
        print("已改用默认浏览器打开界面。")
        _notify("已改用默认浏览器打开界面。\r\n\r\n"
                "要退出本程序，请点界面右上角的「✕」。")
        return None, False, "fallback"
    print("[警告] 浏览器仍没有加载出界面，请手动访问：%s" % base_url)
    _notify("界面没有自动打开，请手动在浏览器访问：\r\n%s" % base_url)
    return None, False, "failed"


def _wait_for_exit(srv: QQExportServer, proc=None, owns_proc: bool = False) -> None:
    """阻塞等待，直到服务收到关闭请求、用户关闭了应用窗口，或按下 Ctrl+C。

    ``proc`` 是我们启动的浏览器进程；``owns_proc`` 为真时它退出即代表
    用户把窗口关掉了（此时用的是独立配置目录，进程归我们独占）。
    """
    try:
        while not srv.stopped:
            if owns_proc and proc is not None and proc.poll() is not None:
                print("检测到窗口被关闭，正在退出…")
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出…")


def main(argv=None) -> int:
    """程序主入口，返回进程退出码。"""
    args = build_parser().parse_args(argv)

    # pythonw.exe（无控制台）下 stdout/stderr 为 None，先兜底再打印
    _ensure_streams()

    # %TEMP% 不可写时 SQLite 会报 "unable to open database file"，先纠正
    paths.ensure_temp_dir()

    # Windows 控制台默认 GBK，输出中文/符号时避免编码报错
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    srv = QQExportServer(out_dir=args.out_dir, db_path=args.db_path,
                         key=args.key, host=args.host, port=args.port)
    try:
        base_url, port = srv.start(args.host, args.port)
    except OSError as exc:
        print("[错误] 本地服务启动失败：%s" % exc)
        return 2

    print("=" * 56)
    print(" QQ导出助手 v%s" % __version__)
    print(" 界面地址：%s" % base_url)
    print(" 导出目录：%s" % srv.out_dir)
    print(" 关闭方式：在界面右上角点「✕」，或在此窗口按 Ctrl+C")
    print("=" * 56)

    # 预先设置口令（命令行给了才用）
    if args.key:
        try:
            srv.set_key(args.key)
            print("已使用命令行提供的数据库口令。")
        except ValueError as exc:
            print("[警告] 口令无效：%s" % exc)

    # 启动即扫描
    threading.Thread(target=_auto_scan, args=(srv,), daemon=True).start()

    proc = None
    owns_proc = False
    home = paths.home_dir()
    try:
        paths.ensure_dir(home)
    except Exception:  # noqa: BLE001
        pass

    # 界面形态：window = 由启动器的原生窗口承载（Windows 上的 WebView2 窗口），
    # 我们只负责把地址写出去；browser = 仍旧用浏览器打开（带白屏兜底）。
    ui_mode = (args.ui or os.environ.get("QQEXPORT_UI", "") or "browser").strip().lower()
    if ui_mode not in ("window", "browser"):
        ui_mode = "browser"

    if not args.no_browser:
        if ui_mode == "window":
            # 原生窗口模式：把界面地址写给启动器，由它开自己的窗口
            try:
                with open(os.path.join(home, "ui-url.txt"), "w", encoding="utf-8") as fh:
                    fh.write(base_url)
                print("界面地址已交给原生窗口：" + base_url)
            except Exception as exc:  # noqa: BLE001
                print("[警告] 写 ui-url.txt 失败：%s" % exc)
        else:
            profile = args.edge_profile or default_edge_profile()
            proc, owns_proc, _state = _open_interface(srv, base_url, profile,
                                                      app_mode=args.app_window)

    # 告诉启动器「界面已就绪」，启动器据此关掉准备中的提示窗口
    try:
        with open(os.path.join(home, "ui-ready.txt"), "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:  # noqa: BLE001
        pass

    try:
        _wait_for_exit(srv, proc, owns_proc)
    finally:
        # 不管怎么退出，都要把我们自己拉起来的浏览器进程关掉，
        # 否则 Edge 会带着独立配置目录残留在后台。
        _close_browser(proc)
    print("正在关闭…")
    srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
