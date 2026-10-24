# -*- coding: utf-8 -*-
"""accounts.py —— 多账号支持。

QQ 的消息库是**按账号**存放的::

    %USERPROFILE%\\Documents\\Tencent Files\\<QQ号>\\nt_qq\\nt_db\\nt_msg.db

数据库口令（SQLCipher 的 16 字节 key）也是**跟着账号走的** —— 换了 QQ 号，
旧口令就对不上新库了。所以这里做两件事：

1. 找出本机**所有** QQ 账号的消息库（:func:`list_accounts`）；
2. 按「数据库完整路径」缓存每个账号的口令（:func:`get_key` / :func:`set_key`），
   缓存文件是运行目录下的 ``keys.json``。

口令状态有三种：

``no_key``
    还没抓到过这个账号的口令；
``ready``
    抓到了，而且数据库自那之后没再被改过；
``stale``
    抓到了，但数据库在那之后又变过（换号 / 重新登录过），
    口令很可能已经失效，需要重新抓一次。
"""

from __future__ import annotations

import json
import os
import threading
import time

from . import paths

#: 口令缓存 / roots.json 的进程内写锁（多个 HTTP 线程可能同时写）
_write_lock = threading.RLock()

#: 口令缓存文件名（放在运行目录下）
KEYS_FILE = "keys.json"

#: 判断「数据库比口令新」时容忍的时间误差（秒）
STALE_TOLERANCE = 60


# ---------------------------------------------------------------- 账号发现
#: 用户手动指定的额外数据目录（持久化，支持多台机器各自不同）
ROOTS_FILE = "roots.json"

#: 一个账号目录里 nt_msg.db 可能出现的位置（相对账号目录）
_DB_PATTERNS = (
    os.path.join("nt_qq", "nt_db", "nt_msg.db"),
    os.path.join("nt_db", "nt_msg.db"),
    "nt_msg.db",
)

#: 深扫时直接跳过的目录名（小写）
_SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "$windows.~bt",
    "$windows.~ws", "perflogs", "msocache", "recovery", "intel", "amd",
    "nvidia", "drivers", "node_modules", ".git", "__pycache__", "appdata",
    "temp", "tmp", "logs", "tencent files (cache)", ".vscode", ".cache",
    "packages", "installer", "installed apps", "microsoft",
}

#: 账号根目录缓存（秒）—— 扫描过程中会被反复调用，避免每次重扫磁盘
_ROOTS_TTL = 8.0
_roots_cache: dict = {"time": 0.0, "value": None, "key": None}


def _roots_cache_key():
    """缓存键 —— 环境变量一变（例如测试里改了 USERPROFILE）就重新扫。"""
    return (os.environ.get("USERPROFILE", ""), os.environ.get("PUBLIC", ""))


def _fixed_drives() -> list[str]:
    """本机固定磁盘 / 可移动磁盘的根，例如 ``['C:\\', 'D:\\']``。"""
    out: list[str] = []
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = letter + ":\\"
            try:
                kind = int(kernel32.GetDriveTypeW(root))
            except Exception:  # noqa: BLE001
                kind = 0
            # 2=可移动 3=固定盘 5=光驱 6=内存盘
            if kind in (2, 3, 6):
                out.append(root)
    except Exception:  # noqa: BLE001
        for letter in "CDEFGH":
            root = letter + ":\\"
            if os.path.isdir(root):
                out.append(root)
    return out


def _reg_read_str(hive: str, path: str, name: str):
    """读注册表字符串值（失败返回 None，任何异常都吞掉）。"""
    try:
        import ctypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        hkey = ctypes.c_void_p()
        root = ctypes.c_void_p(0x80000001 if hive == "HKCU" else 0x80000002)
        if advapi32.RegOpenKeyExW(root, path, 0, 0x20019, ctypes.byref(hkey)):
            return None
        try:
            size = ctypes.c_uint32(4196)
            buf = ctypes.create_unicode_buffer(2100)
            if advapi32.RegQueryValueExW(hkey, name, 0, None, buf,
                                         ctypes.byref(size)):
                return None
            return buf.value
        finally:
            advapi32.RegCloseKey(hkey)
    except Exception:  # noqa: BLE001
        return None


def _shell_documents() -> list[str]:
    """注册表里「文档」的真实路径 —— 用户常把文档重定向到 D 盘。"""
    out: list[str] = []
    key = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
           r"\User Shell Folders")
    for name in ("Personal", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"):
        value = _reg_read_str("HKCU", key, name)
        if not value:
            continue
        try:
            value = os.path.normpath(os.path.expandvars(value))
        except Exception:  # noqa: BLE001
            continue
        if os.path.isdir(value) and value not in out:
            out.append(value)
    return out


def extra_roots() -> list[str]:
    """用户手动指定的额外数据目录（存在运行目录下的 roots.json 里）。"""
    path = os.path.join(paths.home_dir(), ROOTS_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, str) and item.strip():
            out.append(os.path.normpath(item.strip()))
    return out


def add_extra_root(path: str) -> bool:
    """记住一个用户手动指定的数据目录（去重后写回 roots.json）。"""
    path = (path or "").strip().strip('"')
    if not path:
        return False
    path = os.path.normpath(os.path.expandvars(os.path.expanduser(path)))
    if not os.path.isdir(path):
        return False
    items = extra_roots()
    if not any(os.path.normcase(i) == os.path.normcase(path) for i in items):
        items.append(path)
    target = os.path.join(paths.home_dir(), ROOTS_FILE)
    tmp = "%s.%d.%d.tmp" % (target, os.getpid(), threading.get_ident())
    try:
        with _write_lock:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # 同样先写临时文件再原子替换：崩溃/并发时不会留下半截 JSON，
            # 否则 extra_roots() 解析失败会把用户手动添加的目录全部丢掉。
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(items, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False
    _roots_cache["time"] = 0.0
    _roots_cache["value"] = None
    return True


def account_roots(force: bool = False) -> list[str]:
    """返回所有可能的 ``Tencent Files`` 根目录（多策略）。"""
    now = time.time()
    cache_key = _roots_cache_key()
    if not force and _roots_cache["value"] is not None:
        if (_roots_cache["key"] == cache_key
                and now - _roots_cache["time"] < _ROOTS_TTL):
            return list(_roots_cache["value"])

    roots: list[str] = []

    def add(path):
        if not path:
            return
        try:
            path = os.path.normpath(os.path.expandvars(os.path.expanduser(path)))
        except Exception:  # noqa: BLE001
            return
        if os.path.isdir(path) and path not in roots:
            roots.append(path)

    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    public = os.environ.get("PUBLIC") or r"C:\Users\Public"

    # 1) 文档目录（含被重定向到其它盘的情况）+ OneDrive + 公用文档
    documents: list[str] = list(_shell_documents())
    for base in (home, public):
        documents.append(os.path.join(base, "Documents"))
        documents.append(os.path.join(base, "OneDrive", "Documents"))
        documents.append(os.path.join(base, "OneDrive", "文档"))
    for doc in documents:
        add(os.path.join(doc, "Tencent Files"))
        add(os.path.join(doc, "TencentFiles"))

    # 2) 同一台机器上的其它用户配置文件
    users_root = os.path.dirname(home)
    if users_root and os.path.isdir(users_root):
        try:
            for name in os.listdir(users_root):
                add(os.path.join(users_root, name, "Documents", "Tencent Files"))
        except OSError:
            pass

    # 3) 各磁盘根与常见自定义位置
    for drive in _fixed_drives():
        for rel in ("Tencent Files", "TencentFiles",
                    os.path.join("Documents", "Tencent Files"),
                    os.path.join("Documents", "TencentFiles"),
                    os.path.join("QQ", "Tencent Files"),
                    "QQ文件"):
            add(os.path.join(drive, rel))

    # 4) 用户手动指定的目录
    for extra in extra_roots():
        add(extra)

    _roots_cache["value"] = list(roots)
    _roots_cache["time"] = now
    _roots_cache["key"] = cache_key
    return roots


def _accounts_in_root(root: str):
    """从一个根目录里产出 ``(账号号, nt_msg.db 路径)``。"""
    # 用户手动指定的可能直接就是某个账号目录（里面有 nt_qq）
    base = os.path.basename(root.rstrip("\\/"))
    for pattern in _DB_PATTERNS:
        probe = os.path.join(root, pattern)
        if os.path.isfile(probe):
            uin = base if base.isdigit() else ""
            yield uin, probe
            return
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return
    for name in names:
        acc_dir = os.path.join(root, name)
        if not os.path.isdir(acc_dir):
            continue
        for pattern in _DB_PATTERNS:
            db = os.path.join(acc_dir, pattern)
            if os.path.isfile(db):
                yield (name if name.isdigit() else name), db
                break


def _iter_db_paths():
    """逐个产出 ``(账号号, nt_msg.db 路径)``（去重）。"""
    seen: set = set()
    for root in account_roots():
        for uin, db in _accounts_in_root(root):
            marker = os.path.normcase(db)
            if marker in seen:
                continue
            seen.add(marker)
            yield uin, db


def deep_scan(progress=None, max_depth: int = 5, time_budget: float = 150.0,
              drives=None) -> dict:
    """全盘搜 ``Tencent Files`` / ``nt_msg.db``（用户点「全盘扫描」时才跑）。

    找到的根目录会写进 ``roots.json``，下次启动直接可用。
    ``drives`` 只用于测试（默认取本机所有固定盘）。
    """
    started = time.time()
    found_roots: list[str] = []
    found_dbs: list[str] = []
    visited = 0

    def log(msg):
        if progress:
            try:
                progress(msg)
            except Exception:  # noqa: BLE001
                pass

    known = {os.path.normcase(r) for r in account_roots(force=True)}
    queue: list = []
    for drive in (drives if drives is not None else _fixed_drives()):
        queue.append((drive, 0))
        log("准备扫描 %s" % drive)

    while queue:
        if time.time() - started > time_budget:
            log("时间到（%.0f 秒），先停下" % time_budget)
            break
        path, depth = queue.pop(0)
        try:
            entries = list(os.scandir(path))
        except (OSError, PermissionError):
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = entry.name
            lower = name.lower()
            if lower in _SKIP_DIRS:
                continue
            visited += 1
            if "tencent" in lower and "file" in lower:
                found_roots.append(entry.path)
                log("发现目录：%s" % entry.path)
                # 顺便把里面的库列出来，用户能直接看到找到了哪些账号
                try:
                    for sub in list(os.scandir(entry.path)):
                        try:
                            if not sub.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        for pat in _DB_PATTERNS:
                            db = os.path.join(sub.path, pat)
                            try:
                                if os.path.isfile(db):
                                    if db not in found_dbs:
                                        found_dbs.append(db)
                                        log("发现消息库：%s" % db)
                                    break
                            except OSError:
                                pass
                except OSError:
                    pass
                continue
            # 直接命中库文件
            try:
                db = os.path.join(entry.path, "nt_qq", "nt_db", "nt_msg.db")
                if os.path.isfile(db):
                    found_dbs.append(db)
                    log("发现消息库：%s" % db)
                    # 往上退 4 层：nt_msg.db -> nt_db -> nt_qq -> <账号> -> 根目录
                    root = db
                    for _ in range(4):
                        root = os.path.dirname(root)
                    found_roots.append(root)
                    continue
            except OSError:
                pass
            if depth + 1 <= max_depth:
                queue.append((entry.path, depth + 1))
        if visited % 2000 == 0 and visited:
            log("已检查 %d 个目录…" % visited)

    added = 0
    for root in found_roots:
        if os.path.normcase(root) in known:
            continue
        if add_extra_root(root):
            added += 1
            known.add(os.path.normcase(root))

    items = list_accounts()
    log("全盘扫描结束：检查 %d 个目录，发现 %d 个候选，新增 %d 个，共 %d 个账号"
        % (visited, len(found_roots), added, len(items)))
    return {
        "ok": True,
        "visited": visited,
        "found_roots": found_roots,
        "found_dbs": found_dbs,
        "added": added,
        "accounts": items,
        "elapsed": round(time.time() - started, 1),
    }


def diagnose() -> str:
    """生成一份纯文本诊断报告（用户可以直接发给开发者）。"""
    lines: list[str] = []
    lines.append("QQ导出助手 诊断报告")
    lines.append("时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("用户：%s" % (os.environ.get("USERPROFILE") or "?"))
    lines.append("运行目录：%s" % paths.home_dir())
    lines.append("")

    lines.append("== 注册表「文档」真实路径 ==")
    docs = _shell_documents()
    lines.append("  " + ("、".join(docs) if docs else "（读不到）"))
    lines.append("")

    lines.append("== 磁盘 ==")
    for drive in _fixed_drives():
        try:
            free = os.statvfs(drive) if hasattr(os, "statvfs") else None
        except Exception:  # noqa: BLE001
            free = None
        lines.append("  %s%s" % (drive, "" if free is None else ""))
    lines.append("")

    lines.append("== 已扫描的候选根目录 ==")
    roots = account_roots(force=True)
    if not roots:
        lines.append("  （一个都没有）")
    for root in roots:
        try:
            names = sorted(os.listdir(root))[:20]
        except OSError as exc:
            lines.append("  %s  <无法列出: %s>" % (root, exc))
            continue
        lines.append("  %s" % root)
        for name in names:
            acc = os.path.join(root, name)
            if not os.path.isdir(acc):
                continue
            hit = ""
            for pattern in _DB_PATTERNS:
                if os.path.isfile(os.path.join(acc, pattern)):
                    hit = pattern
                    break
            lines.append("      - %s%s" % (name, ("  [有库: %s]" % hit) if hit else ""))
    lines.append("")

    lines.append("== 识别到的账号 ==")
    items = list_accounts()
    if not items:
        lines.append("  （没有找到任何消息库）")
    for item in items:
        lines.append("  %s  %s  %.1f MB  状态=%s"
                     % (item.get("uin"), item.get("db_path"),
                        (item.get("db_size") or 0) / 1048576.0,
                        item.get("state")))
    lines.append("")

    lines.append("== 运行中的 QQ 进程 ==")
    try:
        from . import key as _key

        pids = _key._qq_pids()
        lines.append("  " + (("%d 个：%s" % (len(pids), pids)) if pids else "（没在运行）"))
    except Exception as exc:  # noqa: BLE001
        lines.append("  查询失败：%s" % exc)
    lines.append("")

    return "\n".join(lines)


def list_accounts() -> list[dict]:
    """列出本机所有 QQ 账号及其消息库信息。

    返回的每一项形如::

        {"uin": "100001", "db_path": "...", "db_size": 3145728000,
         "db_mtime": "2026-10-21 22:00:00", "has_key": True,
         "key_time": "2026-10-21 21:00:00", "state": "ready"}
    """
    cache = load_key_cache()
    out: list[dict] = []
    for uin, db in _iter_db_paths():
        try:
            st = os.stat(db)
        except OSError:
            continue
        entry = _cache_entry(cache, db)
        key = entry.get("key") or ""
        key_time = entry.get("time") or ""
        state = "no_key"
        if key:
            state = "ready"
            if _db_newer_than_key(st.st_mtime, key_time):
                state = "stale"
        out.append(
            {
                "uin": uin,
                "db_path": db,
                "db_size": int(st.st_size),
                "db_mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "has_key": bool(key),
                "key_time": key_time,
                "state": state,
            }
        )
    return out


def find_db(uin) -> str:
    """按账号号找消息库路径（找不到返回空串）。"""
    want = str(uin or "").strip()
    if not want:
        return ""
    for item in list_accounts():
        if str(item["uin"]) == want:
            return item["db_path"]
    return ""


def find_uin(db_path) -> str:
    """按消息库路径反查账号号（找不到返回空串）。"""
    want = _norm(db_path)
    for uin, db in _iter_db_paths():
        if _norm(db) == want:
            return uin
    return ""


# ---------------------------------------------------------------- 口令缓存
def _norm(path: str) -> str:
    """路径归一化（Windows 下大小写不敏感）。"""
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(path))


def key_cache_path() -> str:
    """口令缓存文件路径（运行目录下的 ``keys.json``）。"""
    return os.path.join(paths.home_dir(), KEYS_FILE)


def load_key_cache() -> dict:
    """读取口令缓存（读不出来就当空缓存）。"""
    path = key_cache_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_key_cache(cache: dict) -> bool:
    """写入口令缓存（先写临时文件再替换，避免写坏）。"""
    path = key_cache_path()
    # 临时文件名带上 pid/线程号：两个线程同时写时不会互相覆盖临时文件，
    # 否则 os.replace 可能抛 FileNotFoundError 被下面的 except 吞掉。
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        with _write_lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        return True
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _cache_entry(cache: dict, db_path: str) -> dict:
    """从缓存里取某个库的口令条目（同时兼容未归一化的老键）。"""
    if not db_path:
        return {}
    entry = cache.get(_norm(db_path))
    if isinstance(entry, dict):
        return entry
    entry = cache.get(db_path)
    if isinstance(entry, dict):
        return entry
    return {}


def _db_newer_than_key(db_mtime: float, key_time: str) -> bool:
    """数据库是不是比口令新得多（换号 / 重新登录会让口令失效）。

    注意：QQ 只要收到一条消息就会写库，``mtime`` 几乎永远比抓口令的时间新，
    所以这里**不能**用「新一点点」就判定失效 —— 否则账号徽章永远显示「口令失效」。
    真正可靠的失效信号是「用这个口令打不开库」，由服务端在 ``load`` 失败时
    返回 ``reason="stale_key"``。这里只在库比口令新了 **7 天以上** 时才提示，
    属于「很久没动过、可能被重建过」的弱信号。
    """
    if not key_time:
        return False
    try:
        ts = time.mktime(time.strptime(key_time, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return False
    return db_mtime > ts + max(STALE_TOLERANCE, 7 * 24 * 3600)


def get_key(db_path: str) -> str | bytes:
    """取某个库的口令（优先缓存，其次兼容旧的 ``db_key.txt``）。

    缓存里 ``hex:`` 开头的是二进制口令，解码成 bytes 返回。
    """
    if not db_path:
        return ""
    entry = _cache_entry(load_key_cache(), db_path)
    key = entry.get("key") or ""
    if key:
        if isinstance(key, str) and key.lower().startswith("hex:"):
            try:
                return bytes.fromhex(key[4:].strip())
            except ValueError:
                return key
        return key
    return _legacy_key()


def _legacy_key() -> str:
    """读旧版单文件口令（``db_key.txt``），没有就返回空串。"""
    path = paths.key_file()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read().strip()
    except OSError:
        return ""
    if not data:
        return ""
    if data.lower().startswith("hex:"):
        try:
            return bytes.fromhex(data[4:].strip()).decode("latin-1")
        except ValueError:
            return ""
    return data


def set_key(db_path: str, key: str | bytes) -> bool:
    """把某个库的口令写进缓存（成功返回 True）。

    ``key`` 可以是 16 字节 ASCII 字符串，也可以是自动抓取得到的原始
    bytes —— 后者按 ``hex:<hex>`` 存，读取时再还原成 bytes，避免
    ``str(b"...")`` 把二进制口令变成不可用的垃圾串。
    """
    if not db_path or not key:
        return False
    if isinstance(key, (bytes, bytearray)):
        key = "hex:" + bytes(key).hex()
    # 读-改-写全程持锁，避免两个并发 set_key 互相覆盖对方刚写入的条目。
    with _write_lock:
        cache = load_key_cache()
        cache[_norm(db_path)] = {
            "key": key,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return save_key_cache(cache)


def forget_key(db_path: str) -> bool:
    """删掉某个库的口令缓存。"""
    with _write_lock:
        cache = load_key_cache()
        changed = False
        for key in [_norm(db_path), db_path]:
            if key and key in cache:
                del cache[key]
                changed = True
        if changed:
            save_key_cache(cache)
        return changed
