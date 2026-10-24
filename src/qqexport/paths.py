# -*- coding: utf-8 -*-
"""paths.py —— 解析应用的「可写根目录」。

打包后的 EXE 会把运行目录（内嵌 Python 运行时）释放到
``%LOCALAPPDATA%\\QQExportHelper\\app-<hash>\\``，但有些机器不允许写那里：
装了安全软件/沙箱、或 %LOCALAPPDATA% 被策略锁写时，建目录会抛
``UnauthorizedAccessException``。启动器（``build/launcher/Program.cs``）会依次尝试

    ``%LOCALAPPDATA%\\QQExportHelper`` → ``%TEMP%\\QQExportHelper`` → ``<exe 同目录>\\runtime``

并把选中的那个通过环境变量 ``QQEXPORT_HOME`` 传给子进程。

本模块在 Python 侧复现同样的顺序，让**日志、明文库缓存、浏览器配置目录、
口令文件**都落在同一个可写根目录下（否则程序会在"能启动但一加载就报错"）。
"""

from __future__ import annotations

import os
import sys
import threading
import uuid

#: 应用目录名（%LOCALAPPDATA% 下的子目录名）
APP_DIR_NAME = "QQExportHelper"

#: 启动器传给子进程的环境变量：已经确认可写的根目录
HOME_ENV = "QQEXPORT_HOME"


def _exe_dir() -> str:
    """exe（或源码包）所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def candidate_home_dirs() -> list[str]:
    """按优先级列出候选根目录（第一个通常就是启动器选定的位置）。"""
    out: list[str] = []
    env = os.environ.get(HOME_ENV)
    if env:
        out.append(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(os.path.join(local, APP_DIR_NAME))
    temp = os.environ.get("TEMP")
    if temp:
        out.append(os.path.join(temp, APP_DIR_NAME))
    out.append(os.path.join(_exe_dir(), "runtime"))
    # 去重（保持顺序）
    seen: set[str] = set()
    uniq: list[str] = []
    for path in out:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(path)
    return uniq


def ensure_dir(path: str) -> bool:
    """确保目录存在且可写（写探针）；成功返回 True。"""
    try:
        os.makedirs(path, exist_ok=True)
        # 探针文件名必须唯一：多个线程/进程同时探测同一个目录时，固定文件名
        # 会互相删除对方刚创建的文件（Windows 上表现为 PermissionError）。
        probe = os.path.join(path, ".writable.%d.%s" % (os.getpid(), uuid.uuid4().hex))
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("1")
        try:
            os.remove(probe)
        except OSError:
            pass
        return True
    except OSError:
        return False


def _dir_writable(path: str) -> bool:
    """目录是否可写（写一个临时文件试试）。"""
    if not path or not os.path.isdir(path):
        return False
    probe = os.path.join(path, ".qqexport_probe_%d" % os.getpid())
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("1")
        os.remove(probe)
        return True
    except OSError:
        return False


def ensure_temp_dir() -> str:
    """保证系统临时目录可写，返回实际使用的临时目录。

    SQLite 排序 / 临时表需要往临时目录写文件：%TEMP% 不可写时会报
    ``unable to open database file``（实测，整表排序直接失败）。
    这里探测一次，不可写就改用应用根目录下的 ``tmp\\``，并同时改
    ``TEMP`` / ``TMP`` / ``TMPDIR`` 与 ``sqlite3.temp_directory``。
    """
    import tempfile

    tmp = tempfile.gettempdir()
    if _dir_writable(tmp):
        return tmp
    alt = os.path.join(home_dir(), "tmp")
    if not ensure_dir(alt):
        return tmp
    os.environ["TEMP"] = alt
    os.environ["TMP"] = alt
    os.environ["TMPDIR"] = alt
    tempfile.tempdir = alt
    try:
        import sqlite3

        sqlite3.temp_directory = alt
    except Exception:  # noqa: BLE001 - 老版本没有这个属性
        pass
    return alt


#: 缓存已探测过的根目录：``home_dir()`` 被频繁调用（日志/缓存/浏览器配置），
#: 每次都建目录+写探针既慢又会在沙箱里放大竞态。缓存键带上 HOME 环境变量，
#: 测试里切换 QQEXPORT_HOME 时能自动失效。
_home_cache: tuple[str, str] | None = None
_home_lock = threading.Lock()


def home_dir() -> str:
    """返回可写的根目录：第一个探测成功的候选；都不行时返回最后一个。"""
    global _home_cache
    env_key = os.environ.get(HOME_ENV) or ""
    cached = _home_cache
    if cached is not None and cached[0] == env_key:
        return cached[1]
    with _home_lock:
        cached = _home_cache
        if cached is not None and cached[0] == env_key:
            return cached[1]
        cands = candidate_home_dirs()
        chosen = None
        for cand in cands:
            if ensure_dir(cand):
                chosen = cand
                break
        if chosen is None:
            chosen = (cands[-1] if cands
                      else os.path.join(os.path.expanduser("~"), APP_DIR_NAME))
        _home_cache = (env_key, chosen)
        return chosen


def sub_dir(name: str) -> str:
    """根目录下的子目录（不存在就建）。"""
    path = os.path.join(home_dir(), name)
    ensure_dir(path)
    return path


def key_file() -> str:
    """口令文件路径。

    优先用**已经存在**的那个（抓取脚本写在标准位置
    ``%LOCALAPPDATA%\\QQExportHelper\\db_key.txt``），否则用当前根目录。
    """
    local = os.environ.get("LOCALAPPDATA")
    temp = os.environ.get("TEMP")
    ordered: list[str] = []
    if local:
        ordered.append(os.path.join(local, APP_DIR_NAME))
    if temp:
        ordered.append(os.path.join(temp, APP_DIR_NAME))
    ordered.extend(candidate_home_dirs())
    for base in ordered:
        path = os.path.join(base, "db_key.txt")
        if os.path.isfile(path):
            return path
    return os.path.join(home_dir(), "db_key.txt")
