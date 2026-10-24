# -*- coding: utf-8 -*-
"""chase_key.py -- recover the SQLCipher key of a QQ nt_* database by chasing
pointers inside a live process image.

Why this works
--------------
SQLCipher keeps the cipher context in heap memory:

    cipher_ctx  { int derive_key; int pass_sz; u8 *key; u8 *hmac_key; u8 *pass; }
    codec_ctx  { ... u8 *kdf_salt; u8 *hmac_kdf_salt; ... cipher_ctx *read_ctx; ... }

  * codec_ctx->kdf_salt      = a *copy* of the 16 salt bytes read from the file
  * codec_ctx->hmac_kdf_salt = salt ^ 0x3a          <- unique in-memory anchor
  * cipher_ctx->pass         = a copy of the raw passphrase (memcpy in
                                sqlcipher_cipher_ctx_set_pass)

So: find `salt ^ 0x3a` in the process image -> the pointer that references it
lives inside the codec_ctx -> the codec_ctx references the read_ctx
(cipher_ctx) -> the cipher_ctx references the passphrase buffer.

The search is done on an *address aware* dump (see dump_proc_mem2.py), because
the pointer values are real virtual addresses.

Usage:
    python chase_key.py --pid 12345 [--db <nt_msg.db>] [--out hits.txt]
    python chase_key.py --bin dump.bin [--map dump.bin.map] [--db ...]
    python chase_key.py --list
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import mmap
import os
import struct
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple


def _numpy():
    """惰性导入 numpy。

    这个模块只用于整库指针扫描（当前没有任何调用者），而打包体积里
    并没有带 numpy（约 40MB）。缺依赖时给出可读的中文错误，而不是
    在函数中间抛一个让人摸不着头脑的 ImportError。
    """
    try:
        import numpy
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            "该功能需要 numpy，但当前运行环境没有安装；请改用「自动抓取」或手动填写口令。"
        ) from exc
    return numpy

from . import sqlcipher_py as ps
from . import _memdump

DEFAULT_DB = ""
DEFAULT_HIT_FILE = ""

# nt_msg.db
NT_MSG_SALT = bytes.fromhex("d2e12773e7fa677042798f7da97711e0")
NT_MSG_KEY_META = ("ea30d39abfb2c95b3d22319e7ec3cacd8c390d16c2c52e627531d293b016b5a"
                   "b151253b59e36de886cea48bba0a917afe2d6e3454d0c1c3f60c17f31e8f361ee")
WINDOW = 64          # bytes scanned around a pointer slot (the "struct")
MAX_DEPTH = 3


# ---------------------------------------------------------------------------
# address aware image
# ---------------------------------------------------------------------------
class Image:
    """A memory dump + the region map (base address / size / file offset)."""

    def __init__(self, bin_path: str, map_path: Optional[str] = None):
        self.bin_path = bin_path
        self.map_path = map_path or (bin_path + ".map")
        with open(self.map_path, "r", encoding="utf-8") as fh:
            regions = json.load(fh)
        # regions: [[base, size, file_offset], ...]
        regions.sort(key=lambda r: r[0])
        self.regions: List[Tuple[int, int, int]] = [
            (int(b), int(s), int(o)) for b, s, o in regions]
        self._bases = [r[0] for r in self.regions]
        self._ends = [r[0] + r[1] for r in self.regions]
        self.fh = open(bin_path, "rb")
        self.filesize = os.path.getsize(bin_path)
        self.mm = mmap.mmap(self.fh.fileno(), 0, access=mmap.ACCESS_READ)
        # regions sorted by *file offset* (for offset -> address)
        self._by_off = sorted(self.regions, key=lambda r: r[2])
        self._offs = [r[2] for r in self._by_off]
        self._ptr_idx = None
        self._ptr_val = None

    # -- address <-> file offset ------------------------------------------
    def addr_to_off(self, addr: int) -> Optional[int]:
        i = bisect.bisect_right(self._bases, addr) - 1
        if i < 0:
            return None
        base, size, foff = self.regions[i]
        if addr < base or addr >= base + size:
            return None
        return foff + (addr - base)

    def off_to_addr(self, off: int) -> Optional[int]:
        # binary search over the file offsets
        i = bisect.bisect_right(self._offs, off) - 1
        if i < 0:
            return None
        base, size, foff = self._by_off[i]
        if off >= foff + size:
            return None
        return base + (off - foff)

    def read_at(self, addr: int, n: int) -> bytes:
        off = self.addr_to_off(addr)
        if off is None:
            return b""
        return self.mm[off:off + n]

    def contains(self, addr: int) -> bool:
        i = bisect.bisect_right(self._bases, addr) - 1
        if i < 0:
            return False
        base, size, _ = self.regions[i]
        return base <= addr < base + size

    # -- scanning ---------------------------------------------------------
    def find_all(self, needle: bytes) -> List[int]:
        """Return every virtual address where `needle` occurs."""
        out: List[int] = []
        start = 0
        while True:
            i = self.mm.find(needle, start)
            if i < 0:
                break
            a = self.off_to_addr(i)
            if a is not None:
                out.append(a)
            start = i + 1
        return out

    def build_pointer_table(self, chunk: int = 8 << 20) -> int:
        """Scan the whole image once and keep every 8 byte value that looks
        like a pointer into the image.  Returns the number of hits."""
        np = _numpy()
        lo = min(r[0] for r in self.regions)
        hi = max(r[0] + r[1] for r in self.regions)
        n = self.filesize // 8
        idx_parts = []
        val_parts = []
        for start in range(0, n, chunk):
            end = min(n, start + chunk)
            arr = np.ndarray((end - start,), dtype="<u8", buffer=self.mm,
                             offset=start * 8)
            mask = (arr >= np.uint64(lo)) & (arr < np.uint64(hi))
            sel = np.flatnonzero(mask)
            if sel.size:
                idx_parts.append(sel.astype(np.int64) + start)
                val_parts.append(arr[sel].copy())
        if idx_parts:
            self._ptr_idx = np.concatenate(idx_parts)
            self._ptr_val = np.concatenate(val_parts)
        else:
            self._ptr_idx = np.zeros(0, dtype=np.int64)
            self._ptr_val = np.zeros(0, dtype=np.uint64)
        return int(self._ptr_idx.size)

    def slots_for(self, target: int) -> List[int]:
        """Addresses of the 8 byte slots that hold a pointer to `target`."""
        np = _numpy()
        if self._ptr_idx is None:
            self.build_pointer_table()
        m = np.flatnonzero(self._ptr_val == np.uint64(target))
        out = []
        for i in m.tolist():
            a = self.off_to_addr(int(self._ptr_idx[i]) * 8)
            if a is not None:
                out.append(a)
        return out


# ---------------------------------------------------------------------------
# candidate validation
# ---------------------------------------------------------------------------
class Validator:
    def __init__(self, db_path: str = DEFAULT_DB, header_size: int = 1024,
                 page_sizes: Sequence[int] = (4096, 1024)):
        self.db_path = db_path
        self.header_size = header_size
        self.page_sizes = list(page_sizes)
        self.files = {}
        for psz in self.page_sizes:
            try:
                self.files[psz] = ps.get_file(db_path, header_size, psz)
            except Exception:
                pass
        self.tried = 0
        self.hits: List[dict] = []
        # hits for the real target go to PURE_KEY_HIT.txt, hits for any other
        # database (e.g. the login.db sanity check) go to a side file so they
        # never pollute the nt_msg.db result
        base = os.path.basename(db_path).replace(".db", "").replace(".", "_")
        self.hit_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "HIT_%s.txt" % base)

    def check_passphrase(self, blob: bytes) -> Optional[int]:
        self.tried += 1
        for psz, f in self.files.items():
            try:
                if f.check(blob, deep=True):
                    return psz
            except Exception:
                pass
        return None

    def check_derived_key(self, key: bytes) -> Optional[int]:
        """Validate an already derived 32 byte key (no passphrase needed)."""
        self.tried += 1
        for psz, f in self.files.items():
            salt = f.salt
            try:
                hmac_key = hashlib.pbkdf2_hmac(
                    f.params.kdf_hash, key,
                    bytes(b ^ ps.HMAC_SALT_MASK for b in salt),
                    f.params.fast_kdf_iter, f.params.key_sz)
            except Exception:
                continue
            try:
                if f.check_keys(key, hmac_key, deep=True):
                    return psz
            except Exception:
                pass
        return None

    def record(self, kind: str, blob: bytes, page_size: int, where: str) -> None:
        safe = blob.decode("ascii", "replace").encode("ascii", "replace").decode("ascii")
        rec = {
            "kind": kind,
            "page_size": page_size,
            "where": where,
            "hex": blob.hex(),
            "ascii": safe,
            "sha256": hashlib.sha256(blob).hexdigest(),
        }
        self.hits.append(rec)
        line = ("HIT %s page_size=%d where=%s ascii=%r hex=%s sha256=%s\n"
                % (kind, page_size, where, safe, rec["hex"], rec["sha256"]))
        print(line, end="")
        try:
            with open(self.hit_file, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# the chase
# ---------------------------------------------------------------------------
PRINTABLE = set(range(0x20, 0x7F))
PASS_LENS = (16, 20, 24, 32, 40, 48, 64)


def is_printable(b: bytes) -> bool:
    return len(b) > 0 and all(c in PRINTABLE for c in b)


def printable_run(b: bytes) -> bytes:
    n = 0
    for c in b:
        if c not in PRINTABLE:
            break
        n += 1
    return b[:n]


RAW_BUDGET = [20000]          # how many raw (non printable) windows we may test
RAW_LENS = (16, 20, 24, 32, 40)


def try_candidates(img: Image, val: "Validator", ptr: int, where: str,
                   seen: set, raw: bool = False) -> None:
    """At `ptr` try every plausible key material.

    * printable runs starting at ANY offset (the pass buffer may sit at an
      arbitrary offset inside the inspected window)
    * raw byte windows of 16/20/24/32/40 bytes (the Windows client hex-decodes
      an 80 char hex string into 40 raw bytes before sqlite3_key_v2, so the
      passphrase is not necessarily printable ASCII)
    * the first 32 raw bytes as an already derived key
    """
    blob = img.read_at(ptr, 96)
    if not blob:
        return
    i = 0
    while i < len(blob):
        if blob[i] not in PRINTABLE:
            i += 1
            continue
        run = printable_run(blob[i:])
        for L in PASS_LENS:
            if L > len(run):
                break
            cand = run[:L]
            if cand in seen:
                continue
            seen.add(cand)
            psz = val.check_passphrase(cand)
            if psz:
                val.record("passphrase%d" % L, cand, psz, where)
                return
        i += max(1, len(run))
    if raw and RAW_BUDGET[0] > 0:
        for L in RAW_LENS:
            for off in range(0, len(blob) - L + 1):
                if RAW_BUDGET[0] <= 0:
                    return
                cand = blob[off:off + L]
                if cand in seen:
                    continue
                seen.add(cand)
                RAW_BUDGET[0] -= 1
                psz = val.check_passphrase(cand)
                if psz:
                    val.record("raw_passphrase%d" % L, cand, psz, where)
                    return
    if len(blob) >= 32:
        k32 = blob[:32]
        if k32 not in seen:
            seen.add(k32)
            psz = val.check_derived_key(k32)
            if psz:
                val.record("derived_key32", k32, psz, where)


def window_pointers(img: Image, addr: int) -> List[int]:
    """All pointer values stored in [addr-WINDOW, addr+WINDOW).

    `addr` may be the address of a heap object (the object's own fields) or
    the address of a pointer slot (the struct that contains the slot).
    """
    win = img.read_at(addr - WINDOW, 2 * WINDOW)
    if not win:
        return []
    out = []
    for off in range(0, len(win) - 7, 8):
        ptr = struct.unpack_from("<Q", win, off)[0]
        if img.contains(ptr):
            out.append(ptr)
    return out


def chase(img: Image, val: Validator, anchors: Dict[str, bytes],
          max_depth: int = MAX_DEPTH) -> None:
    # 1) locate the anchors
    anchor_addrs: List[Tuple[str, int]] = []
    for name, needle in anchors.items():
        for a in img.find_all(needle):
            anchor_addrs.append((name, a))
    print("[+] anchors found: %s" % ", ".join("%s@0x%X" % (n, a)
                                            for n, a in anchor_addrs[:40]))
    if not anchor_addrs:
        return
    # 2) one pass over the image: pointer table
    t0 = time.time()
    nptr = img.build_pointer_table()
    print("[+] pointer table: %d pointer-like slots (%.1f s)"
          % (nptr, time.time() - t0))

    visited = set()
    seen_cands = set()
    frontier = [(n, a) for n, a in anchor_addrs]
    for depth in range(max_depth + 1):
        nxt: List[Tuple[str, int]] = []
        for name, addr in frontier:
            # (a) the object itself (e.g. the cipher_ctx holds the pass pointer)
            sources = [addr]
            # (b) every struct that contains a pointer to `addr`
            sources.extend(img.slots_for(addr))
            for src in sources:
                for ptr in window_pointers(img, src):
                    if ptr in visited:
                        continue
                    visited.add(ptr)
                    nxt.append((name, ptr))
                    try_candidates(img, val, ptr,
                                   "depth%d %s->0x%X" % (depth, name, ptr),
                                   seen_cands, raw=(depth <= 1))
        frontier = nxt
        print("[+] depth %d: %d new pointers, %d candidates tried"
              % (depth, len(nxt), val.tried))
        if not nxt:
            break


def adjacent_candidates(img: Image, val: Validator, anchors: Dict[str, bytes],
                         span: int = 64) -> None:
    """The client stores `<key_meta><passphrase>` contiguously -> the bytes
    right after the 128 char key_meta are the passphrase."""
    seen: set = set()
    for name, needle in anchors.items():
        if name in ("salt", "hmac_salt"):
            continue
        for a in img.find_all(needle):
            for off in range(0, span):
                try_candidates(img, val, a + len(needle) + off,
                               "adjacent:%s@0x%X+%d" % (name, a, len(needle) + off),
                               seen)


# ---------------------------------------------------------------------------
def list_qq_pids() -> List[int]:
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x2
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    pids = []
    if k32.Process32FirstW(snap, ctypes.byref(pe)):
        while True:
            if pe.szExeFile.lower() == "qq.exe":
                pids.append(pe.th32ProcessID)
            if not k32.Process32NextW(snap, ctypes.byref(pe)):
                break
    k32.CloseHandle(snap)
    return pids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, action="append", default=[])
    ap.add_argument("--bin")
    ap.add_argument("--map")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--header-size", type=int, default=1024)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--depth", type=int, default=MAX_DEPTH)
    ap.add_argument("--salt", default=NT_MSG_SALT.hex(),
                    help="kdf salt of the target db (hex)")
    ap.add_argument("--key-meta", default=NT_MSG_KEY_META,
                    help="128 hex char key_meta of the target db")
    args = ap.parse_args()

    if args.list:
        print(list_qq_pids())
        return 0

    val = Validator(args.db, args.header_size)

    images: List[Tuple[str, Image]] = []
    if args.bin:
        images.append(("file", Image(args.bin, args.map)))
    for pid in args.pid:
        out = os.path.join(os.environ.get("TEMP", "."), "qq_mem_%d.bin" % pid)
        print("[+] dumping pid %d -> %s" % (pid, out))
        _memdump.dump(pid, out)
        images.append(("pid%d" % pid, Image(out)))

    if not images:
        ap.error("need --pid or --bin")

    anchors = {
        "hmac_salt": bytes(b ^ ps.HMAC_SALT_MASK for b in bytes.fromhex(args.salt)),
        "salt": bytes.fromhex(args.salt),
        "key_meta_ascii": args.key_meta.encode("ascii"),
    }
    try:
        anchors["key_meta_raw"] = bytes.fromhex(args.key_meta)
    except Exception:
        pass
    for tag, img in images:
        print("=== image %s (%s) ===" % (tag, img.bin_path))
        chase(img, val, anchors, args.depth)
        adjacent_candidates(img, val, anchors)
    print("[+] total candidates validated: %d, hits: %d" % (val.tried, len(val.hits)))
    return 0 if val.hits else 1


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# product facing API
# ---------------------------------------------------------------------------
def read_db_anchors(db_path: str, header_size: int = 1024):
    """Return (salt, key_meta) of a QQ nt_* database.

    The QQ proprietary header occupies the first `header_size` bytes; the
    SQLCipher salt lives right behind it and the 128 hex character key_meta
    sits inside the header at offset 0x2F.
    """
    with open(db_path, "rb") as fh:
        head = fh.read(header_size + 0x40)
    salt = head[header_size:header_size + 16]
    kmeta = head[0x2F:0x2F + 128]
    try:
        kmeta = kmeta.decode("ascii")
    except Exception:
        kmeta = ""
    return salt, kmeta


def capture_key(db_path: str, pids=None, header_size: int = 1024,
                progress=None, workdir: Optional[str] = None) -> Optional[bytes]:
    """Grab the SQLCipher passphrase of `db_path` from a running QQ.exe.

    The passphrase lives in the SQLCipher cipher context of the process that
    has the database open.  We look for the in-memory anchor ``salt ^ 0x3a``
    (the HMAC kdf salt, which only exists in RAM), follow the pointers to the
    cipher context and validate every candidate against the database itself.

    Returns the raw passphrase bytes, or None when nothing could be found.
    """
    salt, kmeta = read_db_anchors(db_path, header_size)
    if not salt:
        return None
    if pids is None:
        pids = list_qq_pids()
    if not pids:
        return None
    val = Validator(db_path, header_size)
    anchors = {
        "hmac_salt": bytes(b ^ ps.HMAC_SALT_MASK for b in salt),
        "salt": salt,
    }
    if kmeta:
        anchors["key_meta_ascii"] = kmeta.encode("ascii")
        try:
            anchors["key_meta_raw"] = bytes.fromhex(kmeta)
        except Exception:
            pass
    workdir = workdir or os.environ.get("TEMP", ".")
    for pid in pids:
        if progress:
            progress("正在读取 QQ 进程 %d 的内存 ..." % pid)
        out = os.path.join(workdir, "qq_mem_%d.bin" % pid)
        try:
            _memdump.dump(pid, out)
        except Exception as exc:
            if progress:
                progress("进程 %d 读取失败：%s" % (pid, exc))
            continue
        try:
            img = Image(out)
            if progress:
                progress("正在分析进程 %d 的内存 ..." % pid)
            chase(img, val, anchors, MAX_DEPTH)
            adjacent_candidates(img, val, anchors)
        except Exception as exc:
            if progress:
                progress("分析进程 %d 失败：%s" % (pid, exc))
        finally:
            for suffix in ("", ".map"):
                try:
                    os.remove(out + suffix)
                except OSError:
                    pass
        if val.hits:
            hit = val.hits[0]
            raw = bytes.fromhex(hit["hex"])
            if progress:
                progress("已捕获密钥：%s" % hit["ascii"])
            return raw
    return None
