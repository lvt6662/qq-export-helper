# -*- coding: utf-8 -*-
"""qqexport.ui.server —— 「QQ导出助手」的本地 HTTP 服务。

只用 Python 标准库（``http.server`` + ``ThreadingHTTPServer``）实现，
零第三方依赖，前端是纯原生 JS。

设计要点
--------
* **只监听回环地址**（默认 ``127.0.0.1``），端口随机分配，避免与其它程序冲突。
* **所有异常都转成 JSON 错误响应**（HTTP 500 + ``{"error": "..."}``），
  前端永远拿不到 Python 堆栈，不会白屏。
* **延迟导入 + 优雅降级**：``db`` / ``decrypt`` / ``writers`` 由别的队友并行编写，
  这里用 ``try: ... except ImportError`` 引入；模块还没就绪时对应接口返回
  「读取模块尚未就绪」之类的中文提示，而不是崩溃。
* **长耗时动作异步化**：``/api/auto-key``（可能需要 10 分钟并要求用户在 QQ 窗口
  登录）放到后台线程跑，前端轮询 ``/api/state`` 看进度条。

对外 JSON API
-------------
=================================  ==========================================
``GET  /api/state``               全局状态（阶段 / QQ 信息 / 计数 / 进度）
``POST /api/scan``                扫描本机 QQ 安装与数据目录
``POST /api/key``                 手动填写 16(20) 字节数据库口令
``POST /api/auto-key``            触发自动抓取口令（后台线程 + 进度）
``POST /api/load``                解密并读取会话 / 消息
``GET  /api/chats``               会话列表（不含消息）
``GET  /api/chats/<chat_id>``     单个会话（**含**消息），chat_id 会 URL 解码
``POST /api/export``              按格式导出到指定目录
``GET  /api/formats``             可用导出格式列表
``POST /api/shutdown``            关闭服务（供界面上的「退出」按钮调用）
=================================  ==========================================
"""

from __future__ import annotations

import json
import os
import posixpath
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import paths, __version__
from ..model import Chat, Message, sort_chats

try:  # pragma: no cover - 多账号模块（较新，缺失时降级为单账号）
    from .. import accounts as _accounts
except Exception:  # noqa: BLE001
    _accounts = None

# --------------------------------------------------------------------------
# 延迟导入 + 优雅降级：队友的模块还没落地时，UI 要能正常起来并给出中文提示
# --------------------------------------------------------------------------
try:  # pragma: no cover - 取决于队友模块是否已就绪
    from .. import db as _db
except Exception:  # noqa: BLE001
    _db = None

try:  # pragma: no cover
    from .. import decrypt as _decrypt
except Exception:  # noqa: BLE001
    _decrypt = None

try:  # pragma: no cover
    from ..writers import write_export, FORMATS, EXTENSIONS
except Exception:  # noqa: BLE001
    write_export = None
    FORMATS = []
    EXTENSIONS = {}

try:  # pragma: no cover
    from .. import key as _keymod
except Exception:  # noqa: BLE001
    _keymod = None

#: 静态资源目录（index.html / app.js / style.css）
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

#: 阶段常量（前端状态条用）
STAGE_IDLE = "idle"
STAGE_SCANNING = "scanning"
STAGE_NEED_KEY = "need_key"
STAGE_LOADING = "loading"
STAGE_READY = "ready"
STAGE_ERROR = "error"

#: 默认输出目录
DEFAULT_OUT_DIR = r"D:\QQ导出"

#: 程序版本号（前端标题栏显示），与包版本保持一致
APP_VERSION = __version__

#: 读取器候选函数名（按优先级；兼容队友不同命名）
_READER_NAMES = (
    "read_chats",
    "load_chats",
    "list_chats",
    "read_all_chats",
    "get_chats",
    "open_chats",
    "read_messages",
)

#: 解密器候选函数名（按优先级）
_DECRYPT_NAMES = (
    "decrypt_database",
    "decrypt_db",
    "decrypt_file",
    "decrypt_to_file",
    "decrypt",
)

#: 口令长度限制：NTQQ 的 SQLCipher 口令是 16 字节 ASCII；宽松接受 20 字节
_KEY_LENGTHS = (16, 20)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _is_printable_ascii(text: str) -> bool:
    """判断字符串是否全部由可打印 ASCII 组成（0x20 ~ 0x7E）。"""
    if not text:
        return False
    return all(0x20 <= ord(ch) <= 0x7E for ch in text)


def _coerce_key(value):
    """把缓存里的 ``hex:<hex>`` 还原成 bytes；其余原样返回。

    自动抓取可能拿到 40 字节原始 key（非 ASCII），缓存时存成 hex 文本，
    读取时必须还原成 bytes，否则解密必然失败。
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str) and value.lower().startswith("hex:"):
        try:
            return bytes.fromhex(value[4:].strip())
        except ValueError:
            return value
    return value


def _as_group(value) -> bool:
    """把 ``True/False``、``1/0``、``'1'/'0'``、``'true'/'false'`` 统一成布尔值。

    JS 端传的是真布尔值，但手写 API 调用可能传字符串 —— ``bool('0')`` 是 True，
    不归一化就会把同号的单聊当成群聊（或反过来）导出。
    """
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "group", "群", "群聊")
    return bool(value)


def _looks_like_key_error(exc: Exception) -> bool:
    """判断异常是不是「口令不对 / 口令过期」造成的。"""
    text = ("%s %s" % (type(exc).__name__, exc)).lower()
    marks = (
        "not a database",
        "file is encrypted",
        "unable to open database file",
        "口令",
        "密钥",
        "cipher",
        "decrypt",
    )
    return any(mark in text for mark in marks)


def _parse_pct(text: str):
    """从进度文本里解析百分比（没有就返回 None = 不定进度）。"""
    import re

    if not text:
        return None
    match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", str(text))
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if value < 0:
        return 0.0
    if value > 100:
        return 100.0
    return value


def _human_size(num) -> str:
    """字节数转成易读文本。"""
    try:
        n = float(num or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return ""


def _call_flexible(func, variants):
    """按多种签名依次尝试调用 ``func``（用于兼容队友的接口差异）。

    ``variants`` 是 ``[(args, kwargs), ...]`` 列表；只有 ``TypeError``
    才会继续尝试下一个签名，其它异常直接抛出。
    """
    last_error = None
    for args, kwargs in variants:
        try:
            return func(*args, **kwargs)
        except TypeError as exc:  # 签名不匹配 -> 试下一个
            last_error = exc
            continue
    raise TypeError("无法匹配的调用签名: %s" % (last_error if last_error else "?"))


def _chat_from_dict(data: dict) -> Chat:
    """把 dict 形式的会话转成 :class:`qqexport.model.Chat`。"""
    chat = Chat(
        chat_id=str(data.get("chat_id", "") or ""),
        name=str(data.get("name", "") or ""),
        peer=str(data.get("peer", "") or ""),
        is_group=bool(data.get("is_group", False)),
        source=str(data.get("source", "nt_msg") or "nt_msg"),
    )
    for raw in data.get("messages") or []:
        if isinstance(raw, Message):
            chat.messages.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        chat.messages.append(
            Message(
                msg_id=int(raw.get("msg_id", 0) or 0),
                time=int(raw.get("time", 0) or 0),
                direction=int(raw.get("direction", 0) or 0),
                sender=str(raw.get("sender", "") or ""),
                sender_uid=str(raw.get("sender_uid", "") or ""),
                msg_type=int(raw.get("msg_type", 0) or 0),
                content_type=int(raw.get("content_type", 0) or 0),
                text=str(raw.get("text", "") or ""),
                content=str(raw.get("content", "") or ""),
                is_mine=bool(raw.get("is_mine", False)),
            )
        )
    return chat


def _normalize_chats(result) -> list:
    """把读取器的返回值统一成 ``list[Chat]``。"""
    if result is None:
        return []
    if isinstance(result, Chat):
        return [result]
    if isinstance(result, dict):
        for key in ("chats", "items", "list", "data"):
            if key in result:
                return _normalize_chats(result[key])
        return []
    if isinstance(result, tuple) and result and isinstance(result[0], (list, tuple)):
        return _normalize_chats(result[0])
    try:
        iterator = list(result)
    except TypeError:
        return []
    chats = []
    for item in iterator:
        if isinstance(item, Chat):
            chats.append(item)
        elif isinstance(item, dict):
            chats.append(_chat_from_dict(item))
    return chats


def _truncate_chat(chat, limit: int):
    """返回只保留最后 ``limit`` 条消息的会话副本（不改动原对象）。"""
    try:
        import dataclasses

        msgs = list(getattr(chat, "messages", []) or [])
        if len(msgs) <= limit:
            return chat
        return dataclasses.replace(chat, messages=msgs[-limit:])
    except Exception:  # noqa: BLE001 - 非 dataclass 就退回浅拷贝
        try:
            import copy

            new = copy.copy(chat)
            new.messages = list(getattr(chat, "messages", []) or [])[-limit:]
            return new
        except Exception:  # noqa: BLE001
            return chat


# --------------------------------------------------------------------------
# 服务端
# --------------------------------------------------------------------------
class QQExportServer:
    """「QQ导出助手」的本地服务。

    典型用法::

        srv = QQExportServer()
        base_url, port = srv.start()      # 随机空闲端口
        ...
        srv.stop()
    """

    def __init__(
        self,
        out_dir: str = DEFAULT_OUT_DIR,
        db_path: str = "",
        key: str = "",
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self.out_dir = out_dir or DEFAULT_OUT_DIR
        self.host = host
        self._requested_port = port

        self.stage = STAGE_IDLE
        self.error = ""
        self.task = ""            # 当前正在跑的动作：scan / auto-key / load / export
        self.qq = {"installed": False, "install_dir": "", "version": ""}
        # QQ 进程数（带缓存）：前端据此判断「QQ 已登录但没有密钥」
        self._qq_running_count = 0
        self._qq_running_ts = 0.0
        self.db_path = db_path or ""
        self.chats: list = []
        self.progress: list = []  # 最近若干条进度文本
        self.key_hint = ""
        self.last_export: dict = {}
        # 多账号：本机所有 QQ 账号的消息库 + 当前选中的账号
        self.accounts: list = []
        self.current_uin: str = ""
        # 进度百分比（None = 不定进度）与一句话进度
        self.progress_pct = None
        self.progress_text = ""
        # 首页被浏览器取走的次数：用来判断「界面窗口到底有没有加载出页面」
        # （有些机器上 Edge 的应用窗口会打开却一片空白）
        self.index_hits = 0
        #: 界面里的 JS 跑起来后会回报一次（用来判断窗口是不是真的白屏）
        self.ui_alive = False
        #: 启动时"常规位置一个账号都没找到 → 自动全盘兜底扫一次"是否已经排过
        self._auto_scan_started = False

        self._key = key or ""
        self._lock = threading.RLock()
        #: 导出互斥锁：同一时间只允许一个导出任务，避免两个页面/两个请求
        #: 同时往同一个目录写同名文件，把结果写花。
        self._export_lock = threading.Lock()
        #: 加载互斥锁：并发 /api/load（用户快速切账号）会交叉覆盖
        #: ``_reader`` / ``chats``，导致 A 的会话被当成 B 的导出。串行化。
        self._load_lock = threading.Lock()
        self._httpd = None
        self._thread = None
        self._stop_event = threading.Event()
        self._worker = None        # 后台长任务线程
        self._loader = None        # 可注入的自定义读取回调 (path, key) -> list[Chat]
        self._reader = None        # db.ChatReader：只读元数据，消息按需加载

    # ---------------------------------------------------------------- 基础
    def count_index_hit(self) -> None:
        """首页被取走一次（加锁，避免多线程下计数丢失）。"""
        with self._lock:
            self.index_hits += 1

    @property
    def port(self) -> int:
        """实际监听的端口（未启动时返回请求的端口）。"""
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return int(self._requested_port)

    @property
    def base_url(self) -> str:
        return "http://%s:%d" % (self.host, self.port)

    @property
    def stopped(self) -> bool:
        """``True`` 表示服务已被请求关闭。"""
        return self._stop_event.is_set()

    @property
    def key(self) -> str:
        with self._lock:
            return self._key

    @property
    def message_count(self) -> int:
        """库里的消息总条数。

        消息没载入内存时（``Chat.messages`` 是空的）用元数据里的
        ``total_count`` 累加 —— 大库上列表页要显示条数，靠的就是它。
        """
        total = 0
        for c in self.chats:
            count = len(getattr(c, "messages", []) or [])
            total += count or int(getattr(c, "total_count", 0) or 0)
        return total

    def set_loader(self, func):
        """注入自定义读取回调 ``func(db_path, key) -> list[Chat]``。

        队友的 ``db`` 模块如果最终采用别的接口，Lead 可以用这个钩子接上，
        不用改 UI 层。
        """
        self._loader = func

    def request_stop(self):
        """请求关闭服务（供 ``/api/shutdown`` 与主程序等待）。"""
        self._stop_event.set()

    def start(self, host: str = "127.0.0.1", port: int = 0):
        """启动服务，返回 ``(base_url, port)``。

        ``port=0`` 表示由系统分配一个空闲端口。
        """
        with self._lock:
            if self._httpd is not None:
                return self.base_url, self.port
            self.host = host
            self._requested_port = port
            handler = _make_handler(self)
            httpd = ThreadingHTTPServer((host, port), handler)
            httpd.daemon_threads = True
            self._httpd = httpd
            self._thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.2},
                name="qqexport-http",
                daemon=True,
            )
            self._thread.start()
        return self.base_url, self.port

    def stop(self):
        """停止服务并释放端口。"""
        with self._lock:
            httpd = self._httpd
            self._httpd = None
            thread = self._thread
            self._thread = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self.request_stop()

    # ---------------------------------------------------------------- 状态
    def state(self) -> dict:
        """全局状态快照（前端 ``/api/state`` 返回的就是它）。"""
        with self._lock:
            return {
                "stage": self.stage,
                "task": self.task,
                "app_version": APP_VERSION,
                "qq": {
                    "installed": bool(self.qq.get("installed")),
                    "install_dir": self.qq.get("install_dir", ""),
                    "version": self.qq.get("version", ""),
                    "exe": self.qq.get("exe", ""),
                    "running": self._qq_running(),
                    "running_count": self._qq_running_count,
                },
                "db_path": self.db_path,
                "plain_db": self._plain_db_path(),
                "chats": len(self.chats),
                "messages": self.message_count,
                "error": self.error,
                "key_hint": self.key_hint,
                "key_ready": bool(self._key),
                "progress": list(self.progress[-40:]),
                "progress_pct": self.progress_pct,
                "progress_text": self.progress_text,
                "accounts": list(self.accounts),
                "current_uin": self.current_uin,
                "out_dir": self.out_dir,
                "last_export": dict(self.last_export),
                "modules": self.module_status(),
            }

    def module_status(self) -> dict:
        """各并行模块的就绪情况（前端可提示「读取模块尚未就绪」）。"""
        return {
            "db": _db is not None,
            "decrypt": _decrypt is not None,
            "writers": write_export is not None,
            "key": _keymod is not None,
        }

    def _qq_running(self) -> bool:
        """QQ 是否正在运行（带 3 秒缓存，避免每次 /api/state 都做一次进程快照）。"""
        import time as _time

        now = _time.time()
        if now - self._qq_running_ts >= 3.0:
            count = 0
            if _keymod is not None and hasattr(_keymod, "qq_running"):
                try:
                    count = int(_keymod.qq_running() or 0)
                except Exception:  # noqa: BLE001
                    count = 0
            self._qq_running_count = count
            self._qq_running_ts = now
        return self._qq_running_count > 0

    def _plain_db_path(self) -> str:
        """队友 db 模块解密出来的明文库位置（没有就返回空串）。"""
        if _db is None:
            return ""
        getter = getattr(_db, "last_plain_db", None)
        if callable(getter):
            try:
                value = getter()
                if value:
                    return str(value)
            except Exception:  # noqa: BLE001
                pass
        cache = getattr(_db, "PLAIN_CACHE_DIR", "")
        return str(cache) if cache else ""

    def _log(self, text: str):
        with self._lock:
            self.progress.append(text)
            if len(self.progress) > 200:
                del self.progress[:-200]
            self.progress_text = str(text)
            self.progress_pct = _parse_pct(text)

    # ---------------------------------------------------------------- 账号
    def refresh_accounts(self) -> list:
        """刷新本机 QQ 账号列表（消息库路径 + 口令状态）。"""
        if _accounts is None:
            with self._lock:
                self.accounts = []
            return self.accounts
        try:
            items = _accounts.list_accounts()
        except Exception as exc:  # noqa: BLE001
            self._log("枚举账号失败: %s" % exc)
            items = []
        with self._lock:
            self.accounts = items
            if not self.current_uin and items:
                self.current_uin = str(items[0].get("uin", ""))
        return items

    def accounts_payload(self) -> dict:
        """``/api/accounts`` 的返回体（带上扫描到过哪些目录，方便排查）。"""
        items = self.refresh_accounts()
        roots: list = []
        hint = ""
        if _accounts is not None:
            try:
                roots = _accounts.account_roots()
            except Exception:  # noqa: BLE001
                roots = []
        if not items:
            if roots:
                hint = ("没有在这些目录里找到 QQ 消息库：%s。"
                        "如果 QQ 装在别处或改过存储位置，请点「手动指定目录」"
                        "选到 Tencent Files 文件夹，或点「全盘扫描」。"
                        % "、".join(roots))
            else:
                hint = ("本机没有找到 Tencent Files 目录。请先确认 QQ 已经登录过；"
                        "仍然找不到的话，点「全盘扫描」自动搜一遍，"
                        "或点「指定目录…」手动选到 Tencent Files 文件夹。")
        return {
            "ok": True,
            "current_uin": self.current_uin,
            "accounts": items,
            "roots": roots,
            "hint": hint,
        }

    def set_data_root(self, path: str) -> dict:
        """记住用户手动指定的 QQ 数据目录，并立刻重新扫描。"""
        if _accounts is None:
            raise ValueError("账号模块不可用")
        if not _accounts.add_extra_root(path):
            raise ValueError(
                "这个目录用不了：%s\n请选到「Tencent Files」文件夹本身"
                "（里面能看到以 QQ 号命名的子文件夹）。" % path
            )
        self._log("已添加数据目录：%s" % path)
        # 换了数据目录：旧库的会话列表/口令/读取器都不再适用，先全部清掉，
        # 否则界面会拿着旧账号的会话去打开、甚至导出成新账号的内容。
        with self._lock:
            old_reader = self._reader
            self._reader = None
            self.chats = []
            self.db_path = ""
            self.current_uin = ""
            self._key = ""
        if old_reader is not None:
            try:
                old_reader.close()
            except Exception:  # noqa: BLE001
                pass
        items = self.refresh_accounts()
        if items:
            with self._lock:
                self.db_path = items[0].get("db_path", "")
                self.current_uin = str(items[0].get("uin", ""))
                if self.db_path:
                    self.stage = STAGE_IDLE if self._key else STAGE_NEED_KEY
        return self.accounts_payload()

    def deep_scan_accounts(self) -> dict:
        """后台全盘扫描 QQ 数据目录（会写进 roots.json，下次启动直接用）。"""
        if _accounts is None:
            raise ValueError("账号模块不可用")
        with self._lock:
            if self.task:
                return {"ok": False, "error": "已有任务在进行中"}
            self.task = "deep-scan"
            self.error = ""
        threading.Thread(target=self._deep_scan_worker, daemon=True).start()
        return {"ok": True, "started": True}

    def _deep_scan_worker(self):
        try:
            with self._lock:
                self.progress_pct = None      # 目录数事先不知道 → 走不定进度动画
            self._log("开始全盘扫描 QQ 数据目录（最多约 2 分钟）…")
            result = _accounts.deep_scan(progress=self._log)
            items = self.refresh_accounts()
            with self._lock:
                if not self.db_path and items:
                    self.db_path = items[0].get("db_path", "")
                if not self.current_uin and items:
                    self.current_uin = str(items[0].get("uin", ""))
                if self.db_path and self.stage in (STAGE_SCANNING, STAGE_ERROR):
                    self.stage = STAGE_IDLE if self._key else STAGE_NEED_KEY
            self._log("全盘扫描完成：新增 %d 个目录，现在共 %d 个账号"
                      % (int(result.get("added") or 0), len(items)))
            if not items:
                self._log("全盘扫描也没找到 QQ 数据目录 —— 可以在账号栏展开「找不到？点这里」"
                          "手动指定 Tencent Files 路径，或点「诊断」生成报告")
        except Exception as exc:  # noqa: BLE001
            self._log("全盘扫描失败: %s" % exc)
        finally:
            with self._lock:
                self.task = ""
                self.progress_pct = None

    # ------------------------------------------------------ 启动时的自动兜底
    def _maybe_auto_deep_scan(self):
        """常规位置一个账号都没找到时，自动排一次全盘扫描（后台跑，界面照常可用）。

        只自动触发一次；用户手动点「重新扫描」仍会再扫。
        设 ``QQEXPORT_NO_AUTOSCAN=1`` 可关闭（测试用）。
        """
        if _accounts is None:
            return
        if os.environ.get("QQEXPORT_NO_AUTOSCAN"):
            return
        with self._lock:
            if self._auto_scan_started or self.accounts:
                return
            self._auto_scan_started = True
        threading.Thread(target=self._auto_deep_scan_worker, daemon=True).start()

    def _auto_deep_scan_worker(self):
        try:
            # 等启动时的常规扫描 / 界面加载先跑完，别把它挤掉
            deadline = time.time() + 30.0
            while time.time() < deadline:
                with self._lock:
                    if self.accounts:
                        return
                    ready = not self.task
                if ready:
                    break
                time.sleep(0.5)
            else:
                return
            time.sleep(float(os.environ.get("QQEXPORT_AUTOSCAN_DELAY", "1.5")))
            with self._lock:
                if self.accounts or self.task:
                    return
            self._log("常规位置没有找到 QQ 数据目录，自动开始全盘扫描"
                      "（后台进行，界面可以照常操作）…")
            result = self.deep_scan_accounts()
            if not result.get("ok"):
                self._log("自动全盘扫描没能启动：%s" % result.get("error", ""))
        except Exception as exc:  # noqa: BLE001
            self._log("自动全盘扫描失败: %s" % exc)

    def diagnose_text(self) -> str:
        """生成诊断报告（同时写一份到运行目录，方便用户发过来）。"""
        if _accounts is None:
            raise ValueError("账号模块不可用")
        text = _accounts.diagnose()
        try:
            target = os.path.join(paths.home_dir(), "diagnose.txt")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(text)
            text += "\n已保存到：%s\n" % target
        except OSError:
            pass
        return text

    def _db_for_uin(self, uin: str) -> str:
        """按账号号找消息库路径（没给 uin 就用当前选中的账号 / 第一个账号）。"""
        uin = str(uin or "").strip()
        if not uin:
            uin = self.current_uin
        if not uin and _accounts is not None:
            items = _accounts.list_accounts()
            if items:
                uin = str(items[0].get("uin", ""))
        if not uin:
            return self.db_path
        if _accounts is None:
            return self.db_path
        path = _accounts.find_db(uin)
        if path:
            return path
        raise ValueError("找不到账号 %s 的消息库" % uin)

    def _switch_account(self, uin: str):
        """切换到某个账号：更新 db_path / current_uin，并从缓存取它的口令。"""
        db_path = self._db_for_uin(uin)
        if not db_path:
            raise ValueError("找不到账号 %s 的消息库" % (uin or ""))
        # 先读缓存再换状态：原实现先清空 _key、出锁后才读缓存，
        # 这段窗口里并发调用 state()/load() 会看到「已切账号但没口令」。
        cached = _accounts.get_key(db_path) if _accounts is not None else None
        with self._lock:
            self.db_path = db_path
            self.current_uin = str(uin or "")
            self.chats = []
            self._reader = None
            # 换账号必须换口令：新账号没缓存就清空，否则会拿旧账号的口令去开新库
            self._key = _coerce_key(cached) if cached else ""
        return db_path

    # ---------------------------------------------------------------- 扫描
    def scan(self) -> dict:
        """扫描本机 QQ 安装信息与消息库路径。"""
        with self._lock:
            self.stage = STAGE_SCANNING
            self.task = "scan"
            self.error = ""
            self.progress = []
        try:
            self._scan_qq_install()
            if not self.db_path:
                self.db_path = self._find_db()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.stage = STAGE_ERROR
                self.error = "扫描失败: %s" % exc
        # 多账号：列出本机所有账号，并记住当前是哪一个
        items = self.refresh_accounts()
        if items:
            if not self.db_path:
                self.db_path = items[0].get("db_path", "")
            if not self.current_uin:
                self.current_uin = str(items[0].get("uin", ""))
            else:
                for item in items:
                    if os.path.normcase(item.get("db_path", "")) == os.path.normcase(self.db_path):
                        self.current_uin = str(item.get("uin", ""))
                        break
            self._log("发现 %d 个 QQ 账号：%s"
                      % (len(items), "、".join(str(i.get("uin", "")) for i in items)))
        if self.db_path and not self._key:
            # 优先按「数据库路径」从 keys.json 取（多账号，换号后各用各的口令），
            # 取不到再退回旧版的单文件 db_key.txt。
            saved = ""
            if _accounts is not None:
                try:
                    saved = _accounts.get_key(self.db_path)
                except Exception:  # noqa: BLE001
                    saved = ""
            if not saved:
                saved = self._load_saved_key()
            if saved:
                self._key = _coerce_key(saved)
                self._log("已读取保存的数据库口令（%d 字节）" % len(self._key))
        with self._lock:
            self.task = ""
            if self.stage != STAGE_ERROR:
                if self.db_path and not self._key:
                    self.stage = STAGE_NEED_KEY
                    self.key_hint = (
                        "未获取到数据库口令：请先点「自动获取密钥」，"
                        "失败时可在下方手动填写 16 字节口令"
                    )
                elif self.db_path:
                    self.stage = STAGE_IDLE
                    self.key_hint = "已持有口令，可直接加载聊天记录"
                else:
                    self.stage = STAGE_IDLE
                    self.key_hint = "未找到消息库（nt_msg.db），可用 --db 指定路径"
        self._maybe_auto_deep_scan()
        return self.state()

    def _load_saved_key(self) -> str:
        """读取已保存的口令（独立抓取脚本 / 上一次运行的产物）。"""
        path = paths.key_file()
        if not os.path.isfile(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read().strip()
        except OSError:
            return ""
        if not data:
            return ""
        # 抓取脚本在口令不是可打印 ASCII 时会写成 "hex:<hex>"
        if data.lower().startswith("hex:"):
            try:
                return bytes.fromhex(data[4:].strip())
            except ValueError:
                return ""
        return data

    def _scan_qq_install(self):
        """找 QQ 安装目录 / 版本（用 key.find_qq_install，失败不致命）。"""
        if _keymod is None:
            with self._lock:
                self.qq = {"installed": False, "install_dir": "", "version": ""}
            return
        try:
            exe, install_dir, version, wrapper = _keymod.find_qq_install()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.qq = {"installed": False, "install_dir": "", "version": ""}
                self._log("未找到 QQ 安装: %s" % exc)
            return
        with self._lock:
            self.qq = {
                "installed": True,
                "install_dir": install_dir,
                "version": version,
                "exe": exe,
                "wrapper": wrapper,
            }
            self._log("找到 QQ：%s（版本 %s）" % (install_dir, version))

    def _find_db(self) -> str:
        """搜索 ``nt_msg.db``（走 accounts 的多策略发现）。"""
        if _accounts is not None:
            try:
                items = _accounts.list_accounts()
                if items:
                    return items[0].get("db_path", "")
            except Exception as exc:  # noqa: BLE001
                self._log("枚举消息库失败: %s" % exc)
        roots = []
        documents = os.path.expandvars(r"%USERPROFILE%\Documents\Tencent Files")
        if os.path.isdir(documents):
            roots.append(documents)
        home = os.path.join(os.path.expanduser("~"), "Documents", "Tencent Files")
        if os.path.isdir(home) and home not in roots:
            roots.append(home)
        for root in roots:
            try:
                accounts = sorted(os.listdir(root))
            except OSError:
                continue
            for acc in accounts:
                cand = os.path.join(root, acc, "nt_qq", "nt_db", "nt_msg.db")
                if os.path.isfile(cand):
                    return cand
        return ""

    # ---------------------------------------------------------------- 口令
    def set_key(self, key: str, uin: str = "") -> dict:
        """手动设置数据库口令（自动抓取失败时的兜底）。"""
        key = (key or "").strip()
        if len(key) not in _KEY_LENGTHS:
            raise ValueError(
                "口令长度应为 %s 字节 ASCII，当前为 %d 字节"
                % (" 或 ".join(str(n) for n in _KEY_LENGTHS), len(key))
            )
        if not _is_printable_ascii(key):
            raise ValueError("口令必须是可打印 ASCII 字符（0x20~0x7E）")
        if uin:
            # 显式指定了账号就必须切过去；切不过去直接报错，绝不能把新口令
            # 写到上一个账号名下（那会让目标账号永远没有口令）。
            self._switch_account(uin)
        with self._lock:
            self._key = key
            self.error = ""
            self._log("已手动设置口令（%d 字节）" % len(key))
            if self.stage in (STAGE_NEED_KEY, STAGE_ERROR):
                self.stage = STAGE_IDLE
                self.key_hint = "口令已就绪，可点击「加载聊天记录」"
        if self.db_path and _accounts is not None:
            _accounts.set_key(self.db_path, key)
            self.refresh_accounts()
        return self.state()

    def close_qq(self) -> dict:
        """结束正在运行的 QQ 进程（换号 / 重抓口令前需要）。"""
        if _keymod is None or not hasattr(_keymod, "close_qq"):
            return {"ok": False, "closed": 0, "error": "密钥模块不可用"}
        try:
            count = _keymod.close_qq()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "closed": 0, "error": str(exc)}
        self._log("已结束 %d 个 QQ 进程" % count)
        return {"ok": True, "closed": int(count or 0)}

    def auto_key(self, uin: str = "") -> dict:
        """触发自动抓取口令（后台线程；进度通过 ``/api/state`` 查看）。"""
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                self._log("自动抓取已在进行中")
                return self.state()
            if _keymod is None:
                self.stage = STAGE_ERROR
                self.error = "密钥模块不可用，无法自动抓取"
                return self.state()
            self.stage = STAGE_SCANNING
            self.task = "auto-key"
            self.error = ""
            self.progress = []
            self._auto_key_uin = str(uin or "")
            self._worker = threading.Thread(
                target=self._auto_key_worker, name="qqexport-autokey", daemon=True
            )
            self._worker.start()
        return self.state()

    def _auto_key_worker(self):
        def progress(msg):
            self._log(str(msg))

        uin = getattr(self, "_auto_key_uin", "") or ""
        try:
            self._log("开始自动抓取口令（需要 QQ 已安装）…")
            db_path = ""
            if uin:
                db_path = self._db_for_uin(uin)
                self._log("目标账号：%s（%s）" % (uin, db_path))
            # 抓口令要求 QQ「重新打开」消息库，所以先把正在跑的 QQ 关掉
            try:
                closed = _keymod.close_qq()
                if closed:
                    self._log("已结束正在运行的 QQ（%d 个进程），稍后会自动重新启动" % closed)
                    time.sleep(1.5)
                # 关不干净就别往下走：调试器启动的 QQ 会撞上单实例互斥体直接退出，
                # 报出来的错很难懂，不如在这里明确告诉用户要手动退出 QQ。
                if hasattr(_keymod, "qq_running") and _keymod.qq_running():
                    raise RuntimeError(
                        "无法自动结束正在运行的 QQ（可能被安全软件拦住了）。"
                        "请先手动完全退出 QQ（任务栏托盘里的图标也要退出），再点一次「识别密钥」。")
            except RuntimeError:
                raise
            except Exception:  # noqa: BLE001
                pass
            key = _keymod.extract_key(progress=progress, db_path=db_path or None)
            if not key:
                raise RuntimeError("未抓到口令")
            raw_key = bytes(key) if isinstance(key, (bytes, bytearray)) else str(key)
            with self._lock:
                # 保持 bytes 原样：新版 QQ 的 40 字节原始 key 不是 ASCII，
                # str() 会得到 "b'...'" 这样的垃圾串，解密必然失败。
                self._key = raw_key
                self.stage = STAGE_IDLE
                self.key_hint = "自动抓取成功，可加载聊天记录"
                self._log("自动抓取成功（%d 字节）" % len(self._key))
            if db_path and _accounts is not None:
                _accounts.set_key(db_path, raw_key)
                self.refresh_accounts()
            try:
                if _keymod.start_qq():
                    self._log("已重新启动 QQ")
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            hint = ""
            # 被调试的 QQ 以 0x80000003 / 0xC0000005 / 0xC000001D 退出，几乎都是
            # 「本程序所在目录受限」（典型是 DSH 这类沙箱会限制工作区内子进程，
            # 它的 VirtualProtect 过滤器会让 V8 用 int3 中止）。用户自己的机器上
            # 把程序装到普通目录就不会出现，所以这里直接给一条可操作的提示。
            low = text.lower()
            if any(mark in low for mark in ("0x80000003", "0xc0000005", "0xc000001d",
                                            "0x80000004")):
                hint = ("\n提示：被调试的 QQ 被当前目录的沙箱/安全策略打断了。"
                        "请把本程序安装或复制到普通目录（例如 "
                        "C:\\Users\\<用户名>\\AppData\\Local\\Programs\\QQ导出助手）再试。")
            with self._lock:
                self.stage = STAGE_NEED_KEY
                self.error = "自动抓取失败: %s%s" % (exc, hint)
                self.key_hint = "请点「识别密钥」重试"
                self._log("自动抓取失败: %s" % exc)
                if hint:
                    self._log(hint.strip())
        finally:
            with self._lock:
                self.task = ""

    # ---------------------------------------------------------------- 加载
    def load(self, uin: str = "") -> dict:
        """解密并读取会话 / 消息（串行执行，避免并发加载互相覆盖）。"""
        if not self._load_lock.acquire(blocking=False):
            raise RuntimeError("正在读取另一个账号的聊天记录，请等它完成后再切换")
        try:
            return self._load_locked(uin)
        finally:
            self._load_lock.release()

    def _load_locked(self, uin: str = "") -> dict:
        """真正的加载逻辑；调用方需持有 ``_load_lock``。"""
        need_key = False
        with self._lock:
            self.stage = STAGE_LOADING
            self.task = "load"
            self.error = ""
            self.progress = []
        try:
            if uin:
                self._switch_account(uin)
            if not self.db_path:
                raise RuntimeError("尚未找到消息库，请先点「扫描 QQ」")
            if not os.path.isfile(self.db_path):
                raise RuntimeError("消息库不存在: %s" % self.db_path)
            if not self.key and _accounts is not None:
                cached = _accounts.get_key(self.db_path)
                if cached:
                    with self._lock:
                        self._key = _coerce_key(cached)
                    self._log("已从缓存读取该账号的口令")
            if _db is None and self._loader is None:
                raise RuntimeError("读取模块尚未就绪（db.py 还在编写中）")
            self._log("开始读取: %s" % self.db_path)
            reader = self._open_reader()
            if reader is not None:
                # 大库上只读元数据（几秒），消息等用户点开某个会话时再按需加载
                chats = reader.list_chats()
            else:
                chats = self._read_chats()
            if not chats:
                raise RuntimeError("没有读到任何会话（可能口令不对或库为空）")
            with self._lock:
                self._reader = reader
                self.chats = sort_chats(chats)
                self.stage = STAGE_READY
                self._log(
                    "读取完成：%d 个会话 / %d 条消息"
                    % (len(self.chats), self.message_count)
                )
        except Exception as exc:  # noqa: BLE001
            need_key = _looks_like_key_error(exc)
            with self._lock:
                old_reader = self._reader
                self._reader = None
                # 加载失败后旧会话列表已经不可信（可能属于另一个库/口令），
                # 必须清空，否则前端会拿着上一次成功的列表去打开会话。
                self.chats = []
                self.stage = STAGE_ERROR
                self.error = str(exc)
                self._log("读取失败: %s\n%s" % (exc, traceback.format_exc()))
            if old_reader is not None:
                try:
                    old_reader.close()
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            self.task = ""
        result = self.state()
        if need_key:
            # 口令不对 / 过期：让前端弹出「需要口令」的引导
            result["ok"] = False
            result["need_key"] = True
            result["reason"] = "stale_key" if self.key else "no_key"
            result["uin"] = self.current_uin
        return result

    def _open_reader(self):
        """尝试用 ``db.ChatReader`` 打开消息库。

        有它就能**只读元数据**（会话名/条数/时间范围），消息等用户点开某个会话
        时再按需加载 —— 真实库有 290 万条消息，一次全读进内存要 4GB 以上。
        不可用时返回 ``None``，由 :meth:`_read_chats` 走老路（整库读入）。
        """
        if _db is None:
            return None
        cls = getattr(_db, "ChatReader", None)
        if cls is None:
            return None
        try:
            reader = cls(self.db_path, self.key, progress=self._on_progress)
        except Exception as exc:  # noqa: BLE001 - 退回老路
            self._log("按需读取不可用，退回整库读取：%s" % exc)
            return None
        self._log("已建立按需读取（消息将按会话加载）")
        return reader

    def _read_chats(self) -> list:
        """调用队友的 db 模块读取会话（失败时尝试先解密再读）。"""
        key = self.key
        first_error = None
        try:
            return self._invoke_reader(self.db_path, key)
        except Exception as exc:  # noqa: BLE001
            first_error = exc
            self._log("直接读取失败: %s" % exc)

        plain = self._decrypt_db()
        if plain:
            return self._invoke_reader(plain, None)
        raise first_error if first_error else RuntimeError("读取失败")

    def _invoke_reader(self, path: str, key) -> list:
        if self._loader is not None:
            return _normalize_chats(self._loader(path, key))
        reader = None
        for name in _READER_NAMES:
            func = getattr(_db, name, None)
            if callable(func):
                reader = func
                break
        if reader is None:
            raise RuntimeError("读取模块尚未就绪（db.py 还在编写中）")
        self._log("调用 db.%s" % reader.__name__)
        # 优先带上 progress 回调（解密 / 群表重建的进度要显示给用户）
        variants = [
            ((path, key), {"progress": self._on_progress}),
            ((path,), {"key": key, "progress": self._on_progress}),
            ((path, key), {}),
            ((path,), {"key": key}),
            ((path,), {}),
        ]
        return _normalize_chats(_call_flexible(reader, variants))

    def _on_progress(self, text: str) -> None:
        """读取过程的进度回调（解密 / 群消息表重建）。"""
        line = str(text)
        with self._lock:
            self.progress.append(line)
            if len(self.progress) > 200:
                del self.progress[:-200]
            self.progress_text = line

    def _decrypt_db(self) -> str:
        """解密消息库到工作目录，返回明文库路径（失败返回空串）。"""
        if not self.key or _decrypt is None:
            return ""
        func = None
        for name in _DECRYPT_NAMES:
            candidate = getattr(_decrypt, name, None)
            if callable(candidate):
                func = candidate
                break
        if func is None:
            return ""
        work_dir = os.path.join(self.out_dir or ".", "_work")
        try:
            os.makedirs(work_dir, exist_ok=True)
        except OSError:
            work_dir = os.path.dirname(self.db_path) or "."
        plain = os.path.join(work_dir, "nt_msg.decrypted.db")
        self._log("调用 decrypt.%s" % func.__name__)
        # 队友约定签名: decrypt_database(src_db, key, out_db=None) -> 明文库路径
        variants = [
            ((self.db_path, self.key, plain), {}),
            ((self.db_path, self.key), {}),
            ((self.db_path,), {"key": self.key, "out_db": plain}),
            ((self.db_path, plain), {"key": self.key}),  # 兜底：out/key 顺序不同
        ]
        try:
            result = _call_flexible(func, variants)
        except Exception as exc:  # noqa: BLE001
            self._log("解密失败: %s" % exc)
            return ""
        if isinstance(result, str) and result and os.path.isfile(result):
            return result
        if isinstance(result, bytes) and result:
            try:
                with open(plain, "wb") as fh:
                    fh.write(result)
            except OSError as exc:
                self._log("写明文库失败: %s" % exc)
                return ""
            return plain
        return plain if os.path.isfile(plain) else ""

    # ---------------------------------------------------------------- 导出
    def formats(self) -> list:
        """可用导出格式列表 ``[{"id":..,"name":..,"ext":..}]``。

        ``writers.FORMATS`` 既可能是 ``{key: 显示名}`` 字典，也可能是 ``[key, ...]``
        列表，这里两种都兼容。
        """
        items = []
        if isinstance(FORMATS, dict):
            items = [(str(k), str(v)) for k, v in FORMATS.items()]
        else:
            for fmt in FORMATS or []:
                key = fmt if isinstance(fmt, str) else str(fmt)
                items.append((key, key))
        out = []
        for fid, name in items:
            ext = ""
            if isinstance(EXTENSIONS, dict):
                ext = EXTENSIONS.get(fid, "") or ""
            out.append({"id": fid, "name": name or fid, "ext": ext})
        return out

    def export(self, fmt: str, out_dir: str, chat_ids=None, progress=None,
               limit_per_chat: int = 0) -> dict:
        """执行导出（同一时间只允许一个导出任务）。

        并发导出会往同一个目录写同名文件、互相覆盖，所以这里用非阻塞锁
        直接拒绝第二个请求，让用户等前一个跑完。
        """
        if not self._export_lock.acquire(blocking=False):
            raise RuntimeError("已有导出任务正在进行，请等它完成后再试")
        try:
            return self._export_impl(fmt, out_dir, chat_ids, progress, limit_per_chat)
        finally:
            self._export_lock.release()

    def _export_impl(self, fmt: str, out_dir: str, chat_ids=None, progress=None,
                     limit_per_chat: int = 0) -> dict:
        """执行导出，返回 :class:`qqexport.model.ExportResult` 的字典形式。"""
        if write_export is None:
            raise RuntimeError("导出模块尚未就绪（writers 包还在编写中）")
        fmt = (fmt or "html").strip().lower()
        if FORMATS and fmt not in [f if isinstance(f, str) else str(f) for f in FORMATS]:
            raise ValueError(
                "不支持的格式: %s（可选 %s）" % (fmt, "、".join(str(f) for f in FORMATS))
            )
        out_dir = (out_dir or self.out_dir or DEFAULT_OUT_DIR).strip()
        with self._lock:
            chats = list(self.chats)
            reader = self._reader
        if not chats:
            raise RuntimeError("还没有加载聊天记录，无法导出")
        if chat_ids:
            wanted_ids = set()
            wanted_pairs = set()
            for item in chat_ids:
                if isinstance(item, dict):
                    cid = str(item.get("id") or item.get("chat_id") or "")
                    grp = item.get("is_group", item.get("group"))
                    wanted_pairs.add((_as_group(grp), cid))
                elif isinstance(item, (tuple, list)) and len(item) == 2:
                    wanted_pairs.add((_as_group(item[1]), str(item[0])))
                else:
                    wanted_ids.add(str(item))

            def _picked(chat) -> bool:
                if (bool(chat.is_group), str(chat.chat_id)) in wanted_pairs:
                    return True
                return str(chat.chat_id) in wanted_ids

            picked = [c for c in chats if _picked(c)]
            if not picked:
                raise RuntimeError("选中的会话不存在")
        else:
            picked = list(chats)
        # 用 (is_group, chat_id) 作为导出键：群号和好友 QQ 号可能撞号，
        # 只传 id 会把同号的单聊和群聊一起导出（隐私泄露）。
        selected_ids = [(c.chat_id, c.is_group) for c in picked]
        # 每个会话最多导出 N 条消息（0 = 不限）。全库有 290 多万条消息，
        # 不加限制时 PDF / Excel 体积和耗时都非常夸张，所以界面上给一个上限。
        try:
            limit = int(limit_per_chat or 0)
        except (TypeError, ValueError):
            limit = 0

        if reader is not None:
            # **流式导出**：逐个会话读出→写出，写完一个放一个。
            # 内存占用只与「最大的那个会话」有关，而不是整库。
            # 注意：limit 作为参数传入，不要改写共享 Reader 的 limit_per_chat，
            # 否则一次限量导出会让之后打开的会话永久只剩最近 N 条。
            selected = reader.stream(selected_ids, limit=limit)
        else:
            keep = {(bool(c.is_group), str(c.chat_id)) for c in picked}
            selected = [
                c for c in chats
                if (bool(c.is_group), str(c.chat_id)) in keep
            ]
            if limit > 0:
                limited = []
                for chat in selected:
                    msgs = getattr(chat, "messages", None)
                    if msgs is not None and len(msgs) > limit:
                        limited.append(_truncate_chat(chat, limit))
                    else:
                        limited.append(chat)
                selected = limited
        os.makedirs(out_dir, exist_ok=True)

        def _progress(*args):
            # writers 的进度回调签名是 progress(done, total, msg)，
            # 这里兼容 1 个或多个参数，统一转成一行文本记进进度日志。
            if not args:
                text = ""
            elif len(args) == 1:
                text = str(args[0])
            else:
                text = " ".join(str(a) for a in args)
            self._log(text)
            if progress:
                try:
                    progress(text)
                except Exception:  # noqa: BLE001
                    pass

        result = write_export(fmt, selected, out_dir, progress=_progress)
        data = {
            "path": getattr(result, "path", ""),
            "fmt": getattr(result, "fmt", fmt),
            # 注意：不要用 getattr 的默认值去遍历 selected ——
            # 默认值是**急切求值**的，会把流式源消费掉，导致导出拿不到数据。
            "chats": getattr(result, "chats", 0),
            "messages": getattr(result, "messages", 0),
            "ok": bool(getattr(result, "ok", True)),
            "error": getattr(result, "error", ""),
        }
        with self._lock:
            self.last_export = data
        return data

    # ---------------------------------------------------------------- 会话
    def chat_list(self) -> list:
        with self._lock:
            return [c.to_dict(with_messages=False) for c in self.chats]

    def find_chat(self, chat_id: str, is_group: bool | None = None,
                  limit: int | None = None):
        """按 id 找会话；``is_group`` 用于区分撞号的群聊/单聊。

        注意：锁内只做查找，真正的消息加载放到锁外 —— 大会话全量加载要
        几十秒甚至几分钟，持锁会让 ``/api/state`` 等所有接口一起假死。
        ``limit`` 限制本次载入的消息条数（只取最近 N 条），避免超大群聊把
        内存吃光；返回的会话里 ``messages`` 可能少于 ``total_count``。
        """
        target = None
        reader = None
        with self._lock:
            for c in self.chats:
                if str(c.chat_id) != str(chat_id):
                    continue
                if is_group is not None and bool(c.is_group) != bool(is_group):
                    continue
                target = c
                break
            reader = self._reader
        if target is None:
            return None
        if not getattr(target, "messages", None) and reader is not None:
            # 元数据会话 —— 这时才真的去读它的消息（锁外执行）
            try:
                reader.load_into(target, limit=limit)
            except Exception as exc:  # noqa: BLE001
                self._log("会话 %s 读取失败：%s" % (chat_id, exc))
                # 不能把「读取失败」当成「空会话」返回 200，前端会显示空白
                raise RuntimeError("会话 %s 读取失败：%s" % (chat_id, exc)) from exc
        return target


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


#: 本机回环主机名白名单（防 DNS rebinding / 跨站请求）
_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})

#: HTTP 请求体上限（8 MiB）：本机接口的 JSON 都很小，没必要接受超大请求
_MAX_BODY_BYTES = 8 * 1024 * 1024


def _hostname_of(value: str) -> str:
    """从 Host / Origin 头里取出主机名（去掉 scheme、端口与 IPv6 方括号）。"""
    text = (value or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = urllib.parse.urlsplit(text).netloc or ""
    if text.startswith("["):          # [::1]:57011
        end = text.find("]")
        return text[1:end].lower() if end > 0 else text.lower()
    if ":" in text:                   # 127.0.0.1:57011
        return text.rsplit(":", 1)[0].lower()
    return text.lower()


def _is_local_request(headers) -> bool:
    """Host 与 Origin（如果有）都必须是本机回环地址。

    浏览器访问本机页面时 Host 是 ``127.0.0.1:端口``；而恶意站点用 DNS
    rebinding 把自己的域名解析到 127.0.0.1 时，Host 会是攻击者的域名。
    这里只放行本机主机名，避免网页借浏览器把聊天记录读走。
    内网穿透等特殊场景可设 ``QQEXPORT_ALLOW_REMOTE=1`` 关掉该检查。
    """
    if os.environ.get("QQEXPORT_ALLOW_REMOTE"):
        return True
    host = _hostname_of(headers.get("Host") or "")
    if host and host not in _LOCAL_HOSTNAMES:
        return False
    origin = headers.get("Origin")
    if origin:
        name = _hostname_of(origin)
        if name and name not in _LOCAL_HOSTNAMES:
            return False
    return True


def _make_handler(app: QQExportServer):
    """为每个服务实例生成一个绑定它的请求处理类。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "QQExport/" + __version__
        protocol_version = "HTTP/1.1"

        # -- 基础工具 --------------------------------------------------
        def log_message(self, fmt, *args):  # 静默，别刷屏
            pass

        def _send(self, status, body: bytes, content_type="application/json; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(status, body)

        def _error(self, message, status=500):
            self._json({"error": str(message), "ok": False}, status)

        def _guard(self) -> bool:
            """只接受本机页面发来的请求（防 DNS rebinding / 跨站请求）。"""
            if _is_local_request(self.headers):
                return True
            self._error("仅接受本机页面发起的请求", 403)
            return False

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                raise ValueError("Content-Length 不是合法数字")
            if length <= 0:
                return {}
            # 本机接口也不能让一个超大 Content-Length 把内存打爆。
            if length > _MAX_BODY_BYTES:
                raise ValueError("请求体过大（%d 字节，上限 %d）" % (length, _MAX_BODY_BYTES))
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("请求体不是合法 JSON: %s" % exc)
            return data if isinstance(data, dict) else {"value": data}

        # -- 静态资源 --------------------------------------------------
        def _serve_static(self, path: str):
            if path in ("", "/"):
                rel = "index.html"
            else:
                rel = posixpath.normpath(path.lstrip("/")).replace("\\", "/")
            if rel.startswith("..") or os.path.isabs(rel):
                self._error("非法路径", 404)
                return
            if rel == "index.html":
                app.count_index_hit()
            # 先 realpath 再做包含校验：只靠 normpath 挡不住 Windows 的
            # "..\\..\\Windows\\win.ini"（反斜杠在 posixpath 里不是分隔符，
            # 换成正斜杠后 ".." 段会留在中间，绕过上面的 startswith 检查）。
            base = os.path.realpath(STATIC_DIR)
            full = os.path.realpath(os.path.join(base, rel.replace("/", os.sep)))
            try:
                inside = os.path.commonpath([base, full]) == base
            except ValueError:
                inside = False
            if not inside:
                self._error("非法路径", 404)
                return
            if not os.path.isfile(full):
                self._error("资源不存在: %s" % rel, 404)
                return
            ext = os.path.splitext(full)[1].lower()
            ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
            with open(full, "rb") as fh:
                data = fh.read()
            self._send(200, data, ctype)

        # -- 路由 ------------------------------------------------------
        def do_GET(self):  # noqa: N802
            if not self._guard():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            try:
                if path == "/api/state":
                    self._json(app.state())
                    return
                if path == "/api/formats":
                    self._json({"formats": app.formats()})
                    return
                if path == "/api/chats":
                    self._json({"chats": app.chat_list()})
                    return
                if path == "/api/accounts":
                    self._json(app.accounts_payload())
                    return
                if path == "/api/diagnose":
                    try:
                        self._json({"ok": True, "text": app.diagnose_text()})
                    except Exception as exc:  # noqa: BLE001
                        self._json({"ok": False, "error": str(exc)})
                    return
                if path.startswith("/api/chats/"):
                    chat_id = path[len("/api/chats/"):]
                    # ?group=1/0 用于区分撞号的群聊/单聊；不传则按 id 取第一个
                    qs = urllib.parse.parse_qs(parsed.query)
                    grp_raw = (qs.get("group") or qs.get("is_group") or [""])[0]
                    is_group = None
                    if grp_raw != "":
                        is_group = _as_group(grp_raw)
                    # 默认只载入最近 3000 条：百万条的大群全量载入 + 序列化
                    # 会直接卡死甚至 OOM；?limit=0 表示不限（老前端兼容）。
                    try:
                        limit = int((qs.get("limit") or ["3000"])[0])
                    except (TypeError, ValueError):
                        limit = 3000
                    chat = app.find_chat(chat_id, is_group=is_group, limit=limit)
                    if chat is None:
                        self._error("会话不存在: %s" % chat_id, 404)
                        return
                    payload = chat.to_dict(with_messages=True)
                    total = int(getattr(chat, "total_count", 0) or 0)
                    payload["truncated"] = bool(limit and total > len(chat.messages))
                    self._json({"chat": payload})
                    return
                if path.startswith("/api/"):
                    self._error("未知接口: %s" % path, 404)
                    return
                self._serve_static(path)
            except Exception as exc:  # noqa: BLE001
                self._error("%s" % exc, 500)

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_POST(self):  # noqa: N802
            if not self._guard():
                return
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            try:
                if path == "/api/ui-alive":
                    # 界面里的 JS 跑起来了：说明窗口真的把页面显示出来了
                    app.ui_alive = True
                    self._json({"ok": True})
                    return
                if path == "/api/accounts":
                    self._json(app.accounts_payload())
                    return
                if path == "/api/accounts/root":
                    data = self._body()
                    try:
                        self._json(app.set_data_root(str(data.get("path", "") or "")))
                    except ValueError as exc:
                        self._error(str(exc), 400)
                    return
                if path == "/api/accounts/deep":
                    try:
                        self._json(app.deep_scan_accounts())
                    except ValueError as exc:
                        self._error(str(exc), 400)
                    return
                if path == "/api/scan":
                    self._json(app.scan())
                    return
                if path == "/api/key":
                    data = self._body()
                    try:
                        self._json(app.set_key(str(data.get("key", "")),
                                              str(data.get("uin", "") or "")))
                    except ValueError as exc:
                        self._error(str(exc), 400)
                    return
                if path == "/api/auto-key":
                    self._json(app.auto_key())
                    return
                if path == "/api/key/auto":
                    data = self._body()
                    self._json(app.auto_key(str(data.get("uin", "") or "")))
                    return
                if path == "/api/qq/close":
                    self._json(app.close_qq())
                    return
                if path == "/api/load":
                    data = self._body()
                    self._json(app.load(str(data.get("uin", "") or "")))
                    return
                if path == "/api/export":
                    data = self._body()
                    try:
                        result = app.export(
                            fmt=str(data.get("format", "html") or "html"),
                            out_dir=str(data.get("out_dir", "") or ""),
                            chat_ids=data.get("chat_ids") or None,
                            limit_per_chat=int(data.get("limit_per_chat", 0) or 0),
                        )
                    except Exception as exc:  # noqa: BLE001
                        # 缺依赖 / 格式不支持 / 还没加载数据 —— 都是用户可处理的错误
                        self._error(str(exc), 400)
                        return
                    self._json(result)
                    return
                if path == "/api/shutdown":
                    self._json({"ok": True, "message": "正在退出…"})
                    app.request_stop()
                    threading.Thread(target=app.stop, daemon=True).start()
                    return
                if path.startswith("/api/"):
                    self._error("未知接口: %s" % path, 404)
                    return
                self._error("不支持的请求: %s" % path, 404)
            except ValueError as exc:
                # 请求体过大 / Content-Length 非法 / 参数不合法：都是客户端问题
                self._error(str(exc), 400)
            except Exception as exc:  # noqa: BLE001
                self._error(
                    "%s\n%s" % (exc, traceback.format_exc(limit=3)), 500
                )

    return Handler


__all__ = [
    "QQExportServer",
    "STATIC_DIR",
    "DEFAULT_OUT_DIR",
    "STAGE_IDLE",
    "STAGE_SCANNING",
    "STAGE_NEED_KEY",
    "STAGE_LOADING",
    "STAGE_READY",
    "STAGE_ERROR",
]
