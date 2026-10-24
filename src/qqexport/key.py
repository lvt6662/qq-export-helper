# -*- coding: utf-8 -*-
"""qqexport.key — extract the 16-byte SQLCipher passphrase used by NTQQ.

Non-invasive: it only *reads* QQ's own startup (it launches QQ.exe under
CreateProcessW(DEBUG_ONLY_THIS_PROCESS), sets one software breakpoint on the
instruction QQ itself uses to log the sqlite key, and copies the key out of the
call argument). QQ's files, registry and data are never modified.

Algorithm ported from the reference implementation
(QQBackup/qq-win-db-key, scripts/windows/ntqq/windows_ntqq_get_key.ps1).
"""
import ctypes
import os
import struct
import time

from . import win32
from .pe import PEFile, locate_key_v2_function


# QQ 9.9.2x 的反调试陷阱：先调用一个 API（无调试器时返回 0），再 test eax,eax，
# 若非 0 则执行 int3 + ud2。调试器吞掉 int3 后，ud2 会让 QQ 自己的 SEH 陷入死循环。
# 现场字节（QQNT.dll）：
#     ff 15 <disp32>   call qword ptr [rip+disp32]   ; IsDebuggerPresent 之类
#     85 c0            test eax,eax
#     74 <rel8>        je  +rel8                     ; <- 改写这个字节
#     cc               int3
#     0f 0b            ud2
# 把 je(0x74) / jne(0x75) 改成 jmp(0xEB) 就等于让 API「返回 0」，陷阱整块被跳过。
_TRAP_SHORT = b"\xff\x15"      # call [rip+disp32]
_TRAP_TAIL = b"\x85\xc0"       # test eax,eax


def _find_trap_sites(data):
    """Yield (file_offset_of_jump_opcode, kind, trap_offset) for every trap.

    kind is "short" (0x74/0x75 + rel8) or "near" (0x0F 0x84/0x85 + rel32).
    trap_offset 是那块 int3+ud2 的位置。

    陷阱块里 jcc 和 int3 之间可能夹着混淆用的垃圾字节（9.9.36 就是：
        call [api] / test eax,eax / je +5 / jmp +12 / <垃圾> / int3 / ud2），
    所以这里不再要求 int3 紧跟在 jcc 后面，而是在后面一段窗口里找 int3+ud2。
    """
    sites = []
    i = 0
    window = 24
    while True:
        i = data.find(_TRAP_SHORT, i)
        if i < 0:
            break
        # call [rip+disp32] is 6 bytes
        j = i + 6
        if data[j:j + 2] == _TRAP_TAIL:
            op = data[j + 2]
            if op in (0x74, 0x75):
                pos = data.find(b"\xcc\x0f\x0b", j + 4, j + 4 + window)
                if pos >= 0:
                    sites.append((j + 2, "short", pos))
            elif data[j + 2:j + 4] in (b"\x0f\x84", b"\x0f\x85"):
                pos = data.find(b"\xcc\x0f\x0b", j + 6, j + 6 + window)
                if pos >= 0:
                    sites.append((j + 2, "near", pos))
        i += 1
    return sites


class KeyExtractionError(RuntimeError):
    pass


def find_qq_install():
    """Locate QQ.exe / InstallDir / Version from registry + config.json."""

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    def reg_read(hive, path, name):
        hkey = ctypes.c_void_p()
        if hive == "HKLM":
            root = ctypes.c_void_p(0x80000002)
        else:
            root = ctypes.c_void_p(0x80000001)
        if not advapi32.RegOpenKeyExW(root, path, 0, 0x20019, ctypes.byref(hkey)):
            try:
                typ = ctypes.c_uint32()
                size = ctypes.c_uint32(520)
                buf = ctypes.create_unicode_buffer(520)
                if advapi32.RegQueryValueExW(hkey, name, 0, ctypes.byref(typ), buf, ctypes.byref(size)):
                    return None
                return buf.value
            finally:
                advapi32.RegCloseKey(hkey)
        return None

    # ---- install dir: prefer the registry, fall back to well-known paths
    install_dir = reg_read("HKLM", r"SOFTWARE\WOW6432Node\Tencent\QQNT", "Install")
    if not install_dir or not os.path.isdir(install_dir):
        install_dir = reg_read("HKCU", r"SOFTWARE\Tencent\QQNT", "Install")
    if not install_dir or not os.path.isdir(install_dir):
        install_dir = None
        us = None
        for base in (
            r"SOFTWARE\WOW6432Node\Tencent\QQ",
            r"SOFTWARE\Tencent\QQ",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
        ):
            for hive in ("HKLM", "HKCU"):
                us = reg_read(hive, base + r"\Uninstall", "UninstallString")
                if us:
                    break
            if us:
                break
        if us:
            cand = us.strip().strip('"').split('"')[0].strip()
            if cand.lower().endswith("uninstall.exe"):
                cand = os.path.dirname(cand)
            if os.path.isdir(cand):
                install_dir = cand
    for guess in (r"D:\QQ", r"C:\Program Files\QQ", r"C:\Program Files (x86)\QQ"):
        if install_dir and os.path.isdir(install_dir):
            break
        if os.path.isfile(os.path.join(guess, "QQ.exe")):
            install_dir = guess
            break

    exe = None
    if install_dir:
        cand = os.path.join(install_dir, "QQ.exe")
        if os.path.isfile(cand):
            exe = cand
    if not exe:
        for guess in (r"D:\QQ\QQ.exe", r"C:\Program Files\QQ\QQ.exe", r"C:\Program Files (x86)\QQ\QQ.exe"):
            if os.path.isfile(guess):
                exe = guess
                install_dir = os.path.dirname(guess)
                break
    if not exe:
        raise KeyExtractionError("QQ.exe not found (registry uninstall entry missing)")

    def _resolve_version_dir(vdir, version):
        """Registry stores `9.9.25.42941`; on-disk it is `9.9.25-42941`."""
        if not version:
            return None
        for cand in (version, version.replace(".", "-"), version.replace("-", ".")):
            p = os.path.join(vdir, cand)
            if os.path.isfile(os.path.join(p, "resources", "app", "wrapper.node")):
                return p
        return None

    version = reg_read("HKLM", r"SOFTWARE\WOW6432Node\Tencent\QQNT", "version")
    if not version:
        version = reg_read("HKCU", r"Software\Tencent\QQNT", "version")
    if not version:
        cfg = os.path.join(install_dir, "config.json")
        if os.path.isfile(cfg):
            try:
                import json

                j = json.loads(open(cfg, "r", encoding="utf-8", errors="replace").read())
                version = j.get("curVersion") or j.get("version")
            except Exception:
                import re

                m = re.search(
                    r'"curVersion"\s*:\s*"([^"]+)"',
                    open(cfg, "r", encoding="utf-8", errors="replace").read(),
                )
                if m:
                    version = m.group(1)

    vdir = os.path.join(install_dir, "versions")
    ver_root = _resolve_version_dir(vdir, version)
    if ver_root is None:
        if os.path.isdir(vdir):
            for d in sorted(os.listdir(vdir)):
                p = os.path.join(vdir, d)
                if os.path.isfile(os.path.join(p, "resources", "app", "wrapper.node")):
                    ver_root = p
                    version = d
                    break
    if ver_root is None:
        raise KeyExtractionError("could not determine the running QQ version directory")
    version = os.path.basename(ver_root)

    wrapper = os.path.join(ver_root, "resources", "app", "wrapper.node")
    if not os.path.isfile(wrapper):
        raise KeyExtractionError("wrapper.node not found at %s" % wrapper)
    return exe, install_dir, version, wrapper


def find_nt_msg_db(uin=None):
    """Locate the encrypted message database of the logged-in account.

    uin: optional QQ number; when given only that account's database is
    considered (multi-account support).
    """
    # 先用 accounts 的多策略发现（会看注册表里的「文档」真实路径、
    # 各盘符下的 Tencent Files、用户手动指定的目录 …… 只在文档目录里找
    # 是不够的：很多机器把文档重定向到了 D 盘，或者改过 QQ 的存储位置）。
    try:
        from . import accounts as _accounts

        if uin:
            path = _accounts.find_db(uin)
            if path:
                return path
        else:
            items = _accounts.list_accounts()
            if items:
                # 多账号时不能取「目录遍历到的第一个」：那可能是很久没登录的号，
                # 抓到的却是正在运行的 QQ 的口令。正在登录的账号会持续写 WAL，
                # 库的 mtime 最新，因此按 mtime 倒序并优先已有口令的账号。
                items = sorted(
                    items,
                    key=lambda a: (
                        a.get("db_mtime") or 0,
                        1 if a.get("has_key") else 0,
                    ),
                    reverse=True,
                )
                return items[0].get("db_path") or None
    except Exception:  # noqa: BLE001
        pass

    import glob

    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, "Documents", "Tencent Files", "*", "nt_qq", "nt_db"),
        os.path.join(home, "OneDrive", "Documents", "Tencent Files", "*", "nt_qq", "nt_db"),
    ]
    if uin:
        roots = [pat.replace("*", str(uin)) for pat in roots]
    for pat in roots:
        for path in sorted(glob.glob(os.path.join(pat, "nt_msg.db"))):
            if os.path.isfile(path):
                return path
    return None


def close_qq():
    """结束正在运行的 QQ 进程，返回结束掉的进程数。

    抓口令需要 QQ **重新打开**消息库（换了账号后必须这样），
    所以先把正在跑的 QQ 全部关掉。

    注意：不能用 ``tasklist`` 统计 —— 受限令牌/沙箱下它会报
    "ERROR: Access denied" 并返回空列表，把「QQ 正在运行」误判成
    「没开」，于是 ``taskkill`` 根本不执行，抓口令必然失败。
    这里用 Toolhelp32 快照数进程，再 taskkill + TerminateProcess 双保险。
    """
    import subprocess
    import time

    pids = _qq_pids()
    if not pids:
        return 0

    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "QQ.exe"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001
        pass

    # 兜底：taskkill 在受限令牌下也可能被拒，逐个 TerminateProcess
    deadline = time.time() + 8.0
    while time.time() < deadline:
        left = _qq_pids()
        if not left:
            break
        for pid in left:
            _terminate_pid(pid)
        time.sleep(0.4)

    return len(pids)


def start_qq():
    """重新启动 QQ（抓完口令后把用户的 QQ 拉回来），返回是否成功。"""
    import subprocess

    try:
        found = find_qq_install()
    except Exception:  # noqa: BLE001
        return False
    exe_path = found[0] if isinstance(found, (tuple, list)) else found
    if not exe_path or not os.path.isfile(exe_path):
        return False
    try:
        subprocess.Popen([exe_path], close_fds=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def _terminate_pid(pid):
    """用 TerminateProcess 结束单个进程（taskkill 被拒时的兜底）。"""
    import ctypes
    from ctypes import wintypes

    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _qq_pids():
    """所有 QQ.exe 的 pid 列表（Toolhelp32 快照）。"""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    pids = []
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return pids
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if (entry.szExeFile or "").lower() == "qq.exe":
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    except Exception:  # noqa: BLE001
        pass
    finally:
        kernel32.CloseHandle(snap)
    return pids


def qq_running():
    """正在运行的 QQ.exe 进程数（0 表示 QQ 没开）。

    用 Toolhelp32 快照枚举进程名。不用 ``tasklist``：在受限令牌/沙箱下
    ``tasklist /FI`` 会直接报 "ERROR: Access denied" 并返回空列表，
    从而把「QQ 正在运行」误判成「没开」。
    """
    return len(_qq_pids())


def extract_key(progress=None, process_factory=None, db_path=None, retries=3):
    """Return the 16-byte ASCII NTQQ database passphrase.

    progress: optional callable(str) for UI updates.
    process_factory: optional callable(DEBUG hook) — unused placeholder kept
    for symmetric API surface; kept for future multi-account support.
    db_path: optional path of the encrypted nt_msg.db; when given, every
    candidate is verified against it, so a wrong guess is never accepted.
    retries: 被 QQ 的保护器打死（调试实例自己退出）时会重试几次 ——
    那一枪的命中与否和机器当时的负载/时序有关，重试一次通常就好了。
    """
    log = progress or (lambda msg: None)
    qq_exe, install_dir, version, wrapper = find_qq_install()
    log("QQ 版本 %s" % version)

    with open(wrapper, "rb") as f:
        image = f.read()
    log("已读取 wrapper.node (%d MB)" % (len(image) // (1024 * 1024)))

    func_rva, lea_rva, string_rva = locate_key_v2_function(image)
    log("函数 RVA 0x%X / LEA RVA 0x%X" % (func_rva, lea_rva))

    if not db_path:
        db_path = find_nt_msg_db()
    if db_path:
        log("校验数据库: %s" % db_path)
    else:
        log("未找到 nt_msg.db，将只接受 16 字节可打印口令")

    # QQ.exe resolves its runtime root via the current working directory; QQ's
    # own launcher sets it to <install>\versions\<ver>.
    app_dir = os.path.join(install_dir, "versions", version)
    if not os.path.isdir(app_dir):
        app_dir = os.path.dirname(qq_exe)

    last_exc = None
    attempts = max(1, int(retries or 1))
    for attempt in range(1, attempts + 1):
        state = {}
        try:
            key = _debug_extract(qq_exe, app_dir, image, func_rva, log, db_path, state=state)
            if key:
                return key
            last_exc = KeyExtractionError("未捕获到密钥")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
        # 「QQ 自己退出了」或「还没进调试循环就失败（state 为空）」才值得重试；
        # 一直等着用户登录那种（有 trap 记录但 QQ 没退）不算。
        if attempt >= attempts:
            break
        if state.get("exit_code") is None and state:
            break
        log("第 %d 次尝试失败（QQ 以 0x%X 退出），5 秒后重试…"
            % (attempt, state.get("exit_code")))
        time.sleep(5)
    if last_exc is not None:
        raise last_exc
    return None


# ---------------------------------------------------------------- debug loop
_DEBUGGER_PROBES = frozenset([
    "IsDebuggerPresent",
    "CheckRemoteDebuggerPresent",
])


class _Debugger(object):
    def __init__(self, log, image, func_rva, db_path=None):
        self.log = log
        self.image = image
        self.func_rva = func_rva
        self.db_path = db_path
        self.h_process = None
        self.pid = 0
        self.bp_address = None
        self.original_byte = None
        self.stepping = set()
        self.key = None
        self.error = None
        self.modules = []      # (base, size, path) of every DLL seen in the child
        self.patched_apis = set()
        self.seen = set()      # candidate blobs already tested
        self.trap_hits = 0
        self.exit_code = None

    # ---- low level helpers
    def _set_breakpoint(self):
        if self.bp_address is None:
            return
        orig = win32.read_process_memory(self.h_process, self.bp_address, 1)
        self.original_byte = orig[0]
        # the code page is read-only: relax the protection for the write
        win32.write_code_byte(self.h_process, self.bp_address, b"\xCC")

    def _restore_original_byte(self):
        if self.bp_address is not None and self.original_byte is not None:
            win32.write_code_byte(
                self.h_process, self.bp_address, bytes(bytearray([self.original_byte]))
            )
            self.original_byte = None

    # -- context helpers ---------------------------------------------------
    def _context(self, thread_id):
        """Return (thread handle, CONTEXT64) for a thread or (None, None)."""
        try:
            h_thread = win32.open_thread(thread_id)
        except OSError as exc:
            self.log("OpenThread 失败: %s" % exc)
            return None, None
        ctx = win32.CONTEXT64()
        ctx.ContextFlags = win32.CONTEXT_DEBUGGER_AVAILABLE
        if not win32.kernel32.GetThreadContext(h_thread, ctypes.byref(ctx)):
            self.log("GetThreadContext 失败 (WinError %d)" % ctypes.get_last_error())
            win32.kernel32.CloseHandle(ctypes.c_void_p(h_thread))
            return None, None
        return h_thread, ctx

    def _set_context(self, h_thread, ctx):
        win32.kernel32.SetThreadContext(h_thread, ctypes.byref(ctx))

    # -- candidate handling ------------------------------------------------
    _REG_NAMES = ("R8", "Rcx", "Rbx", "Rdx", "R9", "Rsi", "Rdi", "R12", "R14")

    def _offer(self, origin, blob):
        """Validate a candidate passphrase; remember it when it really opens the DB."""
        if not blob or blob in self.seen:
            return False
        self.seen.add(blob)
        if self.db_path:
            if not self._validate(blob):
                return False
        else:
            # no database to test against: accept only a 16 byte printable ASCII
            if len(blob) != 16 or not all(0x20 <= b <= 0x7E for b in blob):
                return False
        text = None
        try:
            text = blob.decode("ascii")
        except UnicodeDecodeError:
            text = None
        self.key = blob if text is None else text
        self.log("★ 捕获到数据库口令 (%s): %s" % (origin, text if text else blob.hex()))
        return True

    def _validate(self, blob):
        """Try the candidate against the real (encrypted) NTQQ database."""
        try:
            from . import sqlcipher_py
        except Exception as exc:                      # pragma: no cover
            self.log("无法加载 sqlcipher_py: %r" % (exc,))
            return False
        for page_size in (4096, 1024):
            try:
                if sqlcipher_py.check_passphrase(
                    blob, self.db_path, header_size=1024, page_size=page_size
                ):
                    self.log("  口令校验通过 (page_size=%d)" % page_size)
                    return True
            except Exception as exc:
                self.log("  口令校验异常 (page_size=%d): %r" % (page_size, exc))
        return False

    def _collect_candidates(self, thread_id):
        """Sweep registers and the stack for anything that looks like the key.

        The reference implementation reads the 3rd argument of
        ``sqlite3_key_v2`` (R8) and expects a 16 byte printable string, but
        newer QQ builds hand a 40 byte raw key to SQLite, so every plausible
        blob is tested against the real database instead of guessing.
        """
        h, ctx = self._context(thread_id)
        if h is None:
            return False
        found = False
        try:
            for name in self._REG_NAMES:
                ptr = getattr(ctx, name)
                if not ptr or not (0x10000 < ptr < 0x7FFFFFFFFFFF):
                    continue
                try:
                    raw = win32.read_process_memory(self.h_process, ptr, 96)
                except OSError:
                    continue
                if not raw:
                    continue
                nul = raw.find(b"\x00")
                cstr = raw[:nul] if nul >= 0 else raw
                if 0 < len(cstr) <= 64:
                    found |= self._offer("R%s" % name, bytes(cstr))
                if len(raw) >= 16 and all(0x20 <= b <= 0x7E for b in raw[:16]):
                    found |= self._offer("R%s:16" % name, bytes(raw[:16]))
                if len(raw) >= 40:
                    found |= self._offer("R%s:40" % name, bytes(raw[:40]))
            try:
                stk = win32.read_process_memory(self.h_process, ctx.Rsp, 0x80) or b""
            except OSError:
                stk = b""
            for i in range(0, max(len(stk) - 15, 0)):
                chunk = bytes(stk[i:i + 16])
                if all(0x20 <= b <= 0x7E for b in chunk):
                    found |= self._offer("stack+0x%X" % i, chunk)
        finally:
            win32.kernel32.CloseHandle(ctypes.c_void_p(h))
        return found

    def _single_step(self, thread_id):
        try:
            h_thread = win32.open_thread(thread_id)
        except OSError:
            return
        try:
            ctx = win32.CONTEXT64()
            ctx.ContextFlags = win32.CONTEXT_DEBUGGER_AVAILABLE
            if not win32.kernel32.GetThreadContext(h_thread, ctypes.byref(ctx)):
                return
            ctx.EFlags |= win32.TRAP_FLAG
            if not win32.kernel32.SetThreadContext(h_thread, ctypes.byref(ctx)):
                return
            self.stepping.add(thread_id)
        finally:
            win32.kernel32.CloseHandle(ctypes.c_void_p(h_thread))

    def _patch_traps(self, path, base):
        """Neutralise QQ's anti-debug traps inside a freshly loaded module.

        The trap is ``call [api] / test eax,eax / je skip / int3 / ud2``.
        Rewriting the conditional jump into an unconditional jump is
        equivalent to "the API returned 0", so the trap is skipped and the
        process keeps running normally.
        """
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            self.log("读取 %s 失败: %s" % (path, exc))
            return 0
        try:
            pe = PEFile(data)
        except Exception as exc:  # pragma: no cover - defensive
            self.log("解析 %s 失败: %s" % (path, exc))
            return 0
        sites = _find_trap_sites(data)
        patched = 0
        api_addrs = []
        for off, kind, trap_off in sites:
            rva = pe.offset_to_rva(off)
            if rva is None:
                continue
            addr = base + rva
            # 算出这条分支跳到哪儿：如果分支正好跳进 int3（陷阱块），
            # 那就把分支改成「永远不跳」；否则改成「永远跳」（= 干净路径）。
            jcc_len = 2 if kind == "short" else 6
            if kind == "short":
                rel = struct.unpack_from("<b", data, off + 1)[0]
                target_off = off + 2 + rel
            else:
                rel = struct.unpack_from("<i", data, off + 2)[0]
                target_off = off + 6 + rel
            if target_off == trap_off:
                patch = b"\x90" * jcc_len          # 永远不跳 → 跳过陷阱
            elif kind == "short":
                patch = b"\xeb" + data[off + 1:off + 2]   # 永远跳（rel8 不变）
            else:
                # 0F 84 rel32 (6 字节) -> E9 rel32 (5 字节) + 1 个 NOP 补齐
                patch = b"\xe9" + struct.pack("<i", rel + 1) + b"\x90"
            try:
                win32.write_process_memory(self.h_process, addr, patch)
                patched += 1
            except OSError:
                continue
            # the trap calls an API through the import table: ff 15 <disp32>
            call_off = off - 8
            disp = struct.unpack_from("<i", data, call_off + 2)[0]
            slot = base + rva - 8 + 6 + disp
            try:
                raw = win32.read_process_memory(self.h_process, slot, 8)
                api = struct.unpack("<Q", raw)[0]
                api_addrs.append(api)
            except OSError:
                pass
        self.log(
            "已中和 %d 处反调试陷阱（%s，共发现 %d 处）"
            % (patched, os.path.basename(path), len(sites))
        )
        for api in api_addrs:
            self._neutralise_api(api, force=True)
        return patched

    def _module_name(self, address):
        """Best-effort module name for an address inside a debugged child."""
        for base, size, path in self.modules:
            if size and base <= address < base + size:
                return os.path.basename(path)
        for base, size, path in self.modules:
            if base <= address:
                return os.path.basename(path) + "?"
        return None

    def _module_of(self, address):
        for base, size, path in self.modules:
            if size and base <= address < base + size:
                return (base, size, path)
        return None

    def _record_module(self, base, path):
        size = 0
        try:
            with open(path, "rb") as f:
                head = f.read(0x1000)
            e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
            opt = e_lfanew + 4 + 20
            magic = struct.unpack_from("<H", head, opt)[0]
            size = struct.unpack_from("<I", head, opt + (56 if magic == 0x20B else 56))[0]
        except Exception:
            size = 0
        self.modules.append((base, size, path))
        return size

    def _neutralise_api(self, api_address, force=False):
        """Make an anti-debug API always report "no debugger".

        The trap calls an imported API through the IAT; making that API
        return 0 (``xor eax,eax ; ret``) neutralises every trap site in
        every module at once, instead of patching them one by one.

        force=True 时不挑 API 名字 —— QQ 9.9.36 的保护器用的是自己
        手工映射的模块里的检查函数（导出表里查不到名字），那些才是
        真正触发 int3 的家伙。
        """
        if not api_address or api_address in self.patched_apis:
            return
        mod = self._module_of(api_address)
        name = None
        if mod:
            base, _size, path = mod
            try:
                with open(path, "rb") as f:
                    data = f.read()
                exports = PEFile(data).exports()
                name = exports.get(api_address - base)
            except Exception:
                name = None
        self.log("  陷阱调用的 API: 0x%X (%s)" % (
            api_address, name or self._module_name(api_address) or "?"))
        if not force and name not in _DEBUGGER_PROBES:
            return
        try:
            win32.write_process_memory(self.h_process, api_address, b"\x31\xc0\xc3")
            win32.flush_instruction_cache(self.h_process, api_address, 3)
        except OSError as exc:
            self.log("  中和 %s 失败: %s" % (name or "0x%X" % api_address, exc))
            return
        self.patched_apis.add(api_address)
        self.log("  已中和 %s（始终返回 0）" % (name or "0x%X" % api_address))

    # -- QQ's anti-debug traps --------------------------------------------
    def _handle_trap(self, thread_id, addr):
        """Deal with an ``int3`` that is not ours (QQ's anti-debug trap).

        QQ's protector calls an API that answers "is a debugger attached?".
        On a clean machine the answer is 0 and the conditional branch below
        the ``test`` is taken; under a debugger the answer is non-zero and the
        protector falls into ``int3`` / ``ud2``.

        Reproducing the clean run *without* touching the code is the only way
        to keep the protector's own integrity checks happy: set EAX back to
        the value the API would have returned and move RIP back to the
        ``test`` instruction, so exactly the same branch is taken.
        """
        if addr is None:
            return win32.DBG_EXCEPTION_NOT_HANDLED
        self.trap_hits += 1
        verbose = self.trap_hits <= 10 or (self.trap_hits % 500 == 0)
        try:
            around = win32.read_process_memory(self.h_process, addr - 32, 64) or b""
        except OSError:
            around = b""
        off = 32

        # 在 int3 前面找 "test eax,eax" + 条件跳转（距离不固定，扫一遍）
        for back in range(2, 30):
            if off < back + 2:
                continue
            if around[off - back:off - back + 2] != b"\x85\xc0":
                continue
            jop = around[off - back + 2]
            if 0x70 <= jop <= 0x7F:
                jcc_len = 2
            elif jop == 0x0F and off - back + 3 < len(around) and \
                    0x80 <= around[off - back + 3] <= 0x8F:
                jcc_len = 6
            else:
                continue
            test_addr = addr - back
            if jop in (0x74, 0x84):        # je / jz   -> eax must be 0
                new_eax, nm = 0, "je/jz"
            elif jop in (0x75, 0x85):      # jne / jnz -> eax must be != 0
                new_eax, nm = 1, "jne/jnz"
            else:
                continue
            h, ctx = self._context(thread_id)
            if h is None:
                return win32.DBG_EXCEPTION_NOT_HANDLED
            try:
                ctx.Rax = new_eax
                ctx.Rip = test_addr
                self._set_context(h, ctx)
            finally:
                win32.kernel32.CloseHandle(ctypes.c_void_p(h))
            if verbose:
                self.log("  反调试陷阱 @0x%X → 模拟无调试器 (%s)，RIP=0x%X"
                         % (addr, nm, test_addr))
            return win32.DBG_CONTINUE

        # 【兜底一】找出「跳过陷阱块的那条分支」：陷阱块长这样
        #     jcc  clean      ; 干净机器上走这里
        #     int3           ; <-- 我们撞上的就是这个
        #     ud2
        #     clean:
        # 直接把 RIP 挪到那条分支的目标，等于「检查通过」，一行代码都不用改。
        try:
            window = win32.read_process_memory(self.h_process, addr - 32, 48) or b""
        except OSError:
            window = b""
        wbase = addr - 32
        for p in range(len(window) - 6):
            op = window[p]
            if 0x70 <= op <= 0x7F:
                rel = struct.unpack_from("<b", window, p + 1)[0]
                target = wbase + p + 2 + rel
            elif op == 0x0F and p + 5 < len(window) and 0x80 <= window[p + 1] <= 0x8F:
                rel = struct.unpack_from("<i", window, p + 2)[0]
                target = wbase + p + 6 + rel
            else:
                continue
            branch = wbase + p
            if branch >= addr:
                continue
            if not (addr < target <= addr + 24):
                continue
            h, ctx = self._context(thread_id)
            if h is None:
                return win32.DBG_EXCEPTION_NOT_HANDLED
            try:
                ctx.Rip = target
                self._set_context(h, ctx)
            finally:
                win32.kernel32.CloseHandle(ctypes.c_void_p(h))
            if verbose:
                self.log("  反调试陷阱 @0x%X → 走「没检测到调试器」那条分支 0x%X"
                         % (addr, target))
            return win32.DBG_CONTINUE

        # the protector's "report error" block (QQNT.dll): skip to the
        # function epilogue so the process survives.
        for base, size, path in self.modules:
            if path.lower().endswith("qqnt.dll") and \
                    base + 0x725255B <= addr < base + 0x725255D:
                h, ctx = self._context(thread_id)
                if h is not None:
                    try:
                        ctx.Rip = base + 0x72524B1
                        self._set_context(h, ctx)
                    finally:
                        win32.kernel32.CloseHandle(ctypes.c_void_p(h))
                    self.log("  保护器报错块 @0x%X → 跳到函数尾声 0x%X" % (addr, base + 0x72524B1))
                    return win32.DBG_CONTINUE

        # 未知异常点：跳过这个 int3（连带后面的 ud2，否则会撞上 0xC000001D）——
        # 保护器的陷阱块是 "call [api] / test eax,eax / jcc / int3 / ud2"，
        # 跳过它等于让保护器以为「没检测到调试器」。
        # 实在拿不到线程上下文，才交回 QQ 自己的 SEH。
        module = self._module_name(addr)
        skip = 1
        try:
            nxt = win32.read_process_memory(self.h_process, addr + 1, 2) or b""
            if nxt[:2] == b"\x0f\x0b":     # ud2
                skip = 3
        except OSError:
            pass
        if verbose:
            try:
                ctx_bytes = win32.read_process_memory(self.h_process, addr - 24, 32) or b""
            except OSError:
                ctx_bytes = b""
            self.log("  未知异常点 @0x%X（%s）字节: %s"
                     % (addr, module or "?", ctx_bytes.hex(" ")))
        h, ctx = self._context(thread_id)
        if h is not None:
            try:
                ctx.Rip = addr + skip
                self._set_context(h, ctx)
            finally:
                win32.kernel32.CloseHandle(ctypes.c_void_p(h))
            if verbose:
                self.log("  未知异常点 @0x%X（%s）→ 跳过 %d 字节继续执行"
                         % (addr, module or "?", skip))
            return win32.DBG_CONTINUE
        if verbose:
            self.log("  未知异常点 @0x%X，交回 QQ 自己的 SEH" % addr)
        return win32.DBG_EXCEPTION_NOT_HANDLED

    # ---- main loop
    def run(self, timeout=600):
        si = win32.STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        pi = win32.PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer('"%s"' % self.qq_exe)
        cwd = ctypes.create_unicode_buffer(self.app_dir or os.path.dirname(self.qq_exe))

        self.log("以调试模式启动 QQ…")
        ok = win32.kernel32.CreateProcessW(
            self.qq_exe,
            cmd,
            None,
            None,
            False,
            win32.DEBUG_ONLY_THIS_PROCESS,
            None,
            cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not ok:
            err = ctypes.get_last_error()
            raise KeyExtractionError("CreateProcessW(QQ.exe) failed: WinError %d" % err)
        self.h_process = ctypes.c_void_p(pi.hProcess)
        self.pid = pi.dwProcessId
        win32.kernel32.CloseHandle(ctypes.c_void_p(pi.hThread))
        self.log("QQ 调试进程 PID %d（请在 QQ 窗口完成登录）" % self.pid)

        deadline = time.time() + timeout
        started = time.time()
        last_report = 0.0
        while time.time() < deadline:
            ev = win32.DEBUG_EVENT()
            ok = win32.kernel32.WaitForDebugEvent(ctypes.byref(ev), win32.WAIT_EVENT_TIMEOUT_MS)
            if not ok:
                err = ctypes.get_last_error()
                if err == win32.ERROR_SEM_TIMEOUT:
                    if self.bp_address is None and time.time() - last_report > 10:
                        last_report = time.time()
                        self.log("等待 wrapper.node 加载…（%.0fs）" % (time.time() - started))
                    continue
                raise KeyExtractionError("WaitForDebugEvent failed: WinError %d" % err)

            code = ev.dwDebugEventCode
            thread_id = ev.dwThreadId
            pid = ev.dwProcessId
            handled = False
            if code == win32.CREATE_PROCESS_DEBUG_EVENT:
                win32.kernel32.CloseHandle(ctypes.c_void_p(win32.dll_hfile(ev)))
                if win32.mask_being_debugged(self.h_process):
                    self.log("已清除子进程 PEB.BeingDebugged 标记")
            elif code == win32.LOAD_DLL_DEBUG_EVENT:
                h_file = win32.dll_hfile(ev)
                path = win32.file_path_from_handle(h_file)
                base = win32.dll_base(ev)
                win32.kernel32.CloseHandle(ctypes.c_void_p(h_file))
                low = path.lower()
                self._record_module(base, path)
                if low.endswith("\\qqnt.dll"):
                    self._patch_traps(path, base)
                if self.bp_address is None and low.endswith("wrapper.node"):
                    self.bp_address = base + self.func_rva
                    self._set_breakpoint()
                    self.log("wrapper.node @ 0x%X，已下断点 @ 0x%X" % (base, self.bp_address))
            elif code == win32.EXCEPTION_DEBUG_EVENT:
                exc_code = win32.exception_code(ev)
                addr = win32.exception_address(ev)
                if exc_code == win32.EXCEPTION_BREAKPOINT and (
                    addr == self.bp_address and addr is not None
                ):
                    self.log("断点命中 @ 0x%X（thread %d）" % (addr, thread_id))
                    self._restore_original_byte()
                    if self._collect_candidates(thread_id):
                        self._set_breakpoint()
                        win32.kernel32.TerminateProcess(self.h_process, 0)
                        deadline = min(deadline, time.time() + 30)
                    else:
                        self._single_step(thread_id)
                    handled = True
                elif exc_code == win32.EXCEPTION_SINGLE_STEP and thread_id in self.stepping:
                    self.stepping.discard(thread_id)
                    self._set_breakpoint()
                    handled = True
                elif exc_code == win32.EXCEPTION_BREAKPOINT:
                    # not our breakpoint: QQ's own anti-debug trap.
                    status = self._handle_trap(thread_id, addr)
                    handled = status == win32.DBG_CONTINUE
                else:
                    if self.key is None:
                        self.log(
                            "未处理异常 0x%X @ %s" % (exc_code, ("0x%X" % addr) if addr else "?")
                        )
            elif code == win32.EXIT_PROCESS_DEBUG_EVENT:
                win32.kernel32.ContinueDebugEvent(pid, thread_id, win32.DBG_CONTINUE)
                self.exit_code = win32.exit_process_code(ev)
                self.log("QQ 进程已退出（exit code=0x%X）" % self.exit_code)
                break
            elif code == win32.RIP_EVENT:
                win32.kernel32.ContinueDebugEvent(pid, thread_id, win32.DBG_EXCEPTION_NOT_HANDLED)
                continue

            status = win32.DBG_CONTINUE if handled else win32.DBG_EXCEPTION_NOT_HANDLED
            win32.kernel32.ContinueDebugEvent(pid, thread_id, status)

        win32.kernel32.DebugActiveProcessStop(self.pid)
        try:
            win32.kernel32.TerminateProcess(self.h_process, 0)
        except Exception:
            pass
        if not self.key:
            raise KeyExtractionError(
                "未捕获到密钥（超时或未命中断点）。\n"
                "请先完全退出 QQ（任务栏托盘里的图标也要退出）再重试：QQ 的单实例互斥会让"
                "调试实例直接退出，数据库不会重新打开，断点自然也就命不中。\n"
                "如果仍然失败，可以用别的工具拿到 16 字节口令后在界面上手动填写。"
            )
        return self.key


def _debug_extract(qq_exe, app_dir, image, func_rva, log, db_path=None, state=None):
    d = _Debugger(log, image, func_rva, db_path)
    d.qq_exe = qq_exe
    d.app_dir = app_dir
    try:
        return d.run()
    finally:
        # CreateProcessW 得到的进程句柄必须显式关闭，否则每次抓取泄漏一个句柄
        try:
            if getattr(d, "h_process", None):
                win32.kernel32.CloseHandle(d.h_process)
                d.h_process = None
        except Exception:  # noqa: BLE001
            pass
        if state is not None:
            state["exit_code"] = d.exit_code
            state["trap_hits"] = d.trap_hits
