"""
sqlcipher_py.py - pure-Python SQLCipher v4 page validator (no sqlcipher3 C module).

Implements just enough of SQLCipher 4.x to *verify* a passphrase against a
SQLCipher-encrypted SQLite file (no decryption of the whole DB is required).

Layout implemented (derived from the SQLCipher 4.x sources, see PURE_FINDINGS.md
for the exact source lines):

  * The file is a raw SQLite page store.  Page size P (4096 for QQ).
  * reserve_sz = round_up(iv_sz + hmac_sz, block_sz)
                 = round_up(16 + 20, 16) = 48
    (crypto_impl.c: sqlcipher_codec_ctx_reserve_setup)
  * For every page the *last* 48 bytes are the reserve area:
        [P-48 : P-32]  IV      (16 bytes, AES-CBC, random per page)
        [P-32 : P-12]  HMAC    (20 bytes, HMAC-SHA1 for QQ)
        [P-12 : P]     random padding (12 bytes)
  * The ciphertext is the first (P - reserve_sz) bytes of the page.
  * Page 1 is special: its first FILE_HEADER_SZ (=16) bytes are the *KDF salt*
    in clear text and are NOT part of the ciphered region nor of the HMAC.
    (crypto.c: sqlite3Codec(), "adjust starting pointers in data page for
     header offset on first page")
  * Key derivation (crypto_impl.c: sqlcipher_cipher_ctx_key_derive):
        key      = PBKDF2-HMAC-<KDFHASH>(pass, salt, kdf_iter, key_sz)
        hmac_salt= salt XOR 0x3a  (HMAC_SALT_MASK)   for every byte
        hmac_key = PBKDF2-HMAC-<KDFHASH>(key, hmac_salt, fast_kdf_iter, key_sz)
    NOTE: the hmac key is derived from the *encryption key*, not from the
    passphrase - the "PBKDF2(pass, salt, 4000, 64)[32:64]" shortcut is wrong.
  * Page HMAC (crypto_impl.c: sqlcipher_page_hmac + crypto_openssl.c
    sqlcipher_openssl_hmac):
        HMAC-<HMACHASH>(hmac_key, page[offset : offset+size+iv_sz] || pgno_le32)
    where offset = 16 for page 1 and 0 otherwise, size = (P-offset)-reserve_sz,
    and pgno_le32 is the little endian page number (DEFAULT_CIPHER_FLAGS
    contains CIPHER_FLAG_LE_PGNO).
  * Cipher: AES-256-CBC, no padding, key/iv as above.

Only stdlib (hashlib, hmac, struct) + `cryptography` (AES-CBC) are used.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# constants taken from SQLCipher 4.x
# --------------------------------------------------------------------------
FILE_HEADER_SZ = 16          # crypto.h: #define FILE_HEADER_SZ 16
HMAC_SALT_MASK = 0x3A        # crypto.h: #define HMAC_SALT_MASK 0x3a
FAST_PBKDF2_ITER = 2         # crypto.h: #define FAST_PBKDF2_ITER 2

try:  # AES-256-CBC
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _HAVE_CRYPTOGRAPHY = True
except Exception:  # pragma: no cover
    _HAVE_CRYPTOGRAPHY = False


# --------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------
class SqlCipherParams:
    """SQLCipher cipher parameters (defaults = the values QQ uses)."""

    __slots__ = (
        "kdf_iter", "fast_kdf_iter", "key_sz", "salt_sz", "iv_sz", "hmac_sz",
        "block_sz", "kdf_hash", "hmac_hash", "use_hmac", "plaintext_header_sz",
    )

    def __init__(
        self,
        kdf_iter: int = 4000,
        fast_kdf_iter: int = FAST_PBKDF2_ITER,
        key_sz: int = 32,
        salt_sz: int = 16,
        iv_sz: int = 16,
        hmac_sz: int = 20,
        block_sz: int = 16,
        kdf_hash: str = "sha512",
        hmac_hash: str = "sha1",
        use_hmac: bool = True,
        plaintext_header_sz: int = 0,
    ):
        self.kdf_iter = int(kdf_iter)
        self.fast_kdf_iter = int(fast_kdf_iter)
        self.key_sz = int(key_sz)
        self.salt_sz = int(salt_sz)
        self.iv_sz = int(iv_sz)
        self.hmac_sz = int(hmac_sz)
        self.block_sz = int(block_sz)
        self.kdf_hash = kdf_hash
        self.hmac_hash = hmac_hash
        self.use_hmac = bool(use_hmac)
        self.plaintext_header_sz = int(plaintext_header_sz)

    # crypto_impl.c: sqlcipher_codec_ctx_reserve_setup
    @property
    def reserve_sz(self) -> int:
        reserve = self.iv_sz
        if self.use_hmac:
            reserve += self.hmac_sz
        if self.block_sz > 0 and (reserve % self.block_sz) != 0:
            reserve = ((reserve // self.block_sz) + 1) * self.block_sz
        return reserve

    def replace(self, **kw) -> "SqlCipherParams":
        d = {s: getattr(self, s) for s in self.__slots__}
        d.update(kw)
        return SqlCipherParams(**d)

    def __repr__(self) -> str:  # pragma: no cover
        return ("SqlCipherParams(kdf_iter=%d, fast_kdf_iter=%d, kdf_hash=%s, "
                "hmac_hash=%s, use_hmac=%s, reserve_sz=%d)" %
                (self.kdf_iter, self.fast_kdf_iter, self.kdf_hash,
                 self.hmac_hash, self.use_hmac, self.reserve_sz))


DEFAULT_PARAMS = SqlCipherParams()

# QQ's NT databases use a 4096 byte SQLCipher page size (the encrypted SQLite
# header inside page 1 carries the page-size field 0x1000).
DEFAULT_PAGE_SIZE = 4096
PAGE_SIZES = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)


# --------------------------------------------------------------------------
# crypto primitives
# --------------------------------------------------------------------------
def derive_keys(passphrase: bytes, salt: bytes,
                params: Optional[SqlCipherParams] = None) -> Tuple[bytes, bytes]:
    """Derive (key, hmac_key) exactly like sqlcipher_cipher_ctx_key_derive()."""
    p = params or DEFAULT_PARAMS
    if isinstance(passphrase, str):
        passphrase = passphrase.encode("utf-8")
    key = hashlib.pbkdf2_hmac(p.kdf_hash, passphrase, salt, p.kdf_iter, p.key_sz)
    if not p.use_hmac:
        return key, b""
    hmac_salt = bytes(b ^ HMAC_SALT_MASK for b in salt)
    hmac_key = hashlib.pbkdf2_hmac(p.kdf_hash, key, hmac_salt, p.fast_kdf_iter, p.key_sz)
    return key, hmac_key


def _aes_cbc_decrypt(key: bytes, iv: bytes, ct: bytes) -> bytes:
    if not _HAVE_CRYPTOGRAPHY:
        raise RuntimeError("the 'cryptography' package is required for AES-CBC")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(ct) + dec.finalize()


def page_layout(pgno: int, page_size: int,
                params: Optional[SqlCipherParams] = None) -> Tuple[int, int, int, int]:
    """Return (offset, size, iv_off, hmac_off) *relative to the start of the page*.

    offset   - first byte that belongs to the ciphered region
    size     - length of the ciphered region
    iv_off   - offset of the IV inside the page
    hmac_off - offset of the stored HMAC inside the page
    """
    p = params or DEFAULT_PARAMS
    offset = p.plaintext_header_sz if p.plaintext_header_sz else (FILE_HEADER_SZ if pgno == 1 else 0)
    size = (page_size - offset) - p.reserve_sz
    iv_off = offset + size
    hmac_off = iv_off + p.iv_sz
    return offset, size, iv_off, hmac_off


def page_hmac(hmac_key: bytes, page: bytes, pgno: int, page_size: int,
              params: Optional[SqlCipherParams] = None) -> bytes:
    """HMAC over (ciphertext || iv || pgno_le32) - crypto_impl.c:sqlcipher_page_hmac."""
    p = params or DEFAULT_PARAMS
    offset, size, iv_off, hmac_off = page_layout(pgno, page_size, p)
    data = page[offset:hmac_off]              # ciphertext || iv
    pgno_raw = struct.pack("<I", pgno & 0xFFFFFFFF)   # CIPHER_FLAG_LE_PGNO
    return hmac.new(hmac_key, data + pgno_raw, p.hmac_hash).digest()


# --------------------------------------------------------------------------
# plaintext sanity helpers
# --------------------------------------------------------------------------
# valid b-tree page types
_BTREE_TYPES = (0x02, 0x05, 0x0A, 0x0D)


def _sqlite_header_ok(pt: bytes, page_size: int) -> bool:
    """`pt` is the decrypted page-1 payload *excluding* the 16 salt bytes.

    `pt[0]` is therefore SQLite header byte 16.  Checked fields:
    page size (16), file format version (18/19), schema format (44) and
    text encoding (56).
    """
    if len(pt) < 44:
        return False
    # 16..17: database page size (must match the cipher page size)
    if page_size < 65536:
        if pt[0:2] != struct.pack(">H", page_size):
            return False
    elif pt[0:2] != b"\x00\x01":
        return False
    # 18/19: file format write/read version (1 = legacy, 2 = WAL)
    if pt[2] not in (1, 2) or pt[3] not in (1, 2):
        return False
    # 44..47: schema format number (1..4)
    if not (1 <= struct.unpack(">I", pt[28:32])[0] <= 4):
        return False
    # 56..59: text encoding (1 = UTF-8, 2 = UTF-16le, 3 = UTF-16be)
    if not (1 <= struct.unpack(">I", pt[40:44])[0] <= 3):
        return False
    return True


def _btree_page_ok(pt: bytes) -> bool:
    """Very loose sanity check of a decrypted interior page."""
    if not pt:
        return False
    if pt[0] not in _BTREE_TYPES:
        return False
    ncell = struct.unpack(">H", pt[3:5])[0]
    if ncell > 4096:
        return False
    if len(pt) >= 8:
        cell_start = struct.unpack(">H", pt[5:7])[0]
        if cell_start == 0:
            cell_start = 65536
        if cell_start > len(pt) and cell_start != 65536:
            return False
    return True


# --------------------------------------------------------------------------
# the file wrapper
# --------------------------------------------------------------------------
class SqlCipherFile:
    """A SQLCipher-4 encrypted SQLite file (with a possible proprietary header)."""

    def __init__(self, db_path: str, header_size: int = 1024,
                 page_size: int = DEFAULT_PAGE_SIZE,
                 params: Optional[SqlCipherParams] = None,
                 max_interior_pages: int = 8):
        self.path = os.path.abspath(db_path)
        self.header_size = int(header_size)
        self.page_size = int(page_size)
        self.params = params or DEFAULT_PARAMS
        self.max_interior_pages = int(max_interior_pages)
        self.fh = open(self.path, "rb")
        self.file_size = os.path.getsize(self.path)
        self.data_size = self.file_size - self.header_size
        self.page_count = self.data_size // self.page_size
        # salt: SQLCipher reads the kdf salt from file offset 0 of the *db*
        self.fh.seek(self.header_size)
        self.salt = self.fh.read(self.params.salt_sz)
        self.page1 = self._read_page(1)
        self.interior_pages: List[Tuple[int, bytes]] = self._pick_interior_pages()

    # -- io helpers --------------------------------------------------------
    def _read_page(self, pgno: int) -> bytes:
        if pgno < 1 or pgno > self.page_count:
            return b""
        self.fh.seek(self.header_size + (pgno - 1) * self.page_size)
        return self.fh.read(self.page_size)

    def _pick_interior_pages(self) -> List[Tuple[int, bytes]]:
        """Pick a few non-empty interior pages spread over the file."""
        if self.max_interior_pages <= 0 or self.page_count < 2:
            return []
        out: List[Tuple[int, bytes]] = []
        n = self.page_count
        # spread the probes over the file, skipping page 1
        for i in range(self.max_interior_pages * 4):
            if len(out) >= self.max_interior_pages:
                break
            pgno = 2 + (int((i + 1) * (n - 2) / (self.max_interior_pages * 4 + 1)) % (n - 2))
            if pgno < 2 or pgno > n:
                continue
            page = self._read_page(pgno)
            if len(page) < self.page_size:
                continue
            if not any(page):          # short read / free page
                continue
            out.append((pgno, page))
        return out

    # -- verification ------------------------------------------------------
    def verify_page(self, key: bytes, hmac_key: bytes, page: bytes, pgno: int,
                    strict_plaintext: bool = True) -> bool:
        p = self.params
        if len(page) < self.page_size:
            return False
        offset, size, iv_off, hmac_off = page_layout(pgno, self.page_size, p)
        if p.use_hmac:
            stored = page[hmac_off:hmac_off + p.hmac_sz]
            if len(stored) != p.hmac_sz:
                return False
            if not hmac.compare_digest(stored, page_hmac(hmac_key, page, pgno,
                                                         self.page_size, p)):
                return False
        if not strict_plaintext:
            return True
        ct = page[offset:offset + size]
        iv = page[iv_off:iv_off + p.iv_sz]
        try:
            pt = _aes_cbc_decrypt(key, iv, ct)
        except Exception:
            return False
        if pgno == 1:
            return _sqlite_header_ok(pt, self.page_size)
        return _btree_page_ok(pt)

    def check_keys(self, key: bytes, hmac_key: bytes, deep: bool = True) -> bool:
        """True iff the *already derived* key/hmac_key decrypt this database."""
        # page 1 is the schema page - always present, always decisive
        if not self.verify_page(key, hmac_key, self.page1, 1):
            return False
        if not deep:
            return True
        if not self.interior_pages:
            return True
        ok = 0
        for pgno, page in self.interior_pages:
            if self.verify_page(key, hmac_key, page, pgno):
                ok += 1
        return ok > 0

    def check(self, passphrase: bytes, deep: bool = True) -> bool:
        """True iff `passphrase` really decrypts this database."""
        try:
            key, hmac_key = derive_keys(passphrase, self.salt, self.params)
        except Exception:
            return False
        return self.check_keys(key, hmac_key, deep)

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass

    def __enter__(self) -> "SqlCipherFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):  # pragma: no cover
        self.close()


# --------------------------------------------------------------------------
# module level convenience API
# --------------------------------------------------------------------------
_FILE_CACHE: Dict[Tuple[str, int, int, int], SqlCipherFile] = {}


def get_file(db_path: str, header_size: int = 1024, page_size: int = DEFAULT_PAGE_SIZE,
             params: Optional[SqlCipherParams] = None,
             max_interior_pages: int = 8) -> SqlCipherFile:
    """Return a (cached) SqlCipherFile for `db_path`."""
    p = params or DEFAULT_PARAMS
    key = (os.path.abspath(db_path), int(header_size), int(page_size),
           p.kdf_iter, p.fast_kdf_iter, p.kdf_hash, p.hmac_hash, int(p.use_hmac))
    f = _FILE_CACHE.get(key)
    if f is None:
        f = SqlCipherFile(db_path, header_size, page_size, p, max_interior_pages)
        _FILE_CACHE[key] = f
    return f


def check_passphrase(passphrase: bytes, db_path: str, header_size: int = 1024,
                     page_size: int = DEFAULT_PAGE_SIZE,
                     params: Optional[SqlCipherParams] = None) -> bool:
    """Return True iff `passphrase` actually decrypts `db_path`.

    Never raises: any problem is reported as "not a match".
    """
    try:
        if isinstance(passphrase, str):
            passphrase = passphrase.encode("utf-8")
        f = get_file(db_path, header_size, page_size, params)
        return bool(f.check(passphrase))
    except Exception:
        return False


def detect_page_size(passphrase: bytes, db_path: str, header_size: int = 1024,
                     params: Optional[SqlCipherParams] = None,
                     sizes: Sequence[int] = PAGE_SIZES) -> Optional[int]:
    """Return the cipher page size that makes `passphrase` work (or None)."""
    for sz in sizes:
        try:
            if check_passphrase(passphrase, db_path, header_size, sz, params):
                return sz
        except Exception:
            continue
    return None


def _parse_pass(arg: str) -> bytes:
    """CLI passphrase parser: plain ASCII, or 'hex:...' / 'b64:...' prefixed."""
    import base64
    if arg.startswith("hex:"):
        return bytes.fromhex(arg[4:])
    if arg.startswith("b64:"):
        return base64.b64decode(arg[4:])
    return arg.encode("latin-1")


if __name__ == "__main__":  # tiny self test
    import sys
    if len(sys.argv) < 3:
        print("usage: sqlcipher_py.py <db> <passphrase|hex:<hex>|b64:<b64>] "
              "[header_size] [page_size]")
        print("  exit code 0 = the passphrase decrypts the database, 1 = no match")
        raise SystemExit(2)
    hsz = int(sys.argv[3]) if len(sys.argv) > 3 else 1024
    psz = int(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_PAGE_SIZE
    pw = _parse_pass(sys.argv[2])
    if check_passphrase(pw, sys.argv[1], hsz, psz):
        print("OK  (passphrase_len=%d, header_size=%d, page_size=%d)" % (len(pw), hsz, psz))
        raise SystemExit(0)
    print("FAIL (passphrase_len=%d, header_size=%d, page_size=%d)" % (len(pw), hsz, psz))
    alt = detect_page_size(pw, sys.argv[1], hsz)
    if alt:
        print("  ... but it works with page_size=%d" % alt)
    raise SystemExit(1)


# --------------------------------------------------------------------------
# full-file decryption (pure python)
# --------------------------------------------------------------------------
def _decrypt_page(page: bytes, key: bytes, pgno: int, page_size: int,
                  params: SqlCipherParams) -> bytes:
    """Decrypt one raw SQLCipher page and return the plain 4096 byte page."""
    offset, size, iv_off, hmac_off = page_layout(pgno, page_size, params)
    iv = page[iv_off:iv_off + params.iv_sz]
    ct = page[offset:offset + size]
    pt = _aes_cbc_decrypt(key, iv, ct)
    if pgno == 1:
        # The first 16 bytes of the sqlite header are not encrypted.
        # byte 20 of the sqlite header is the "reserved bytes per page" field.
        # QQ sets it to 80 - that is the value the *data* pages are laid out
        # with (usable = 4096 - 80 = 4016).  SQLCipher's own reserve area is
        # only 48 bytes and lives at the very end of the page, so byte 20 must
        # NOT be overwritten: forcing it to 48 makes sqlite believe the page
        # is 4048 bytes usable, which shifts every overflow cell and makes it
        # report "database disk image is malformed" on the group message table.
        return b"SQLite format 3\x00" + pt + b"\x00" * params.reserve_sz
    # the reserve area (iv/hmac/padding) is not part of the plain page
    return pt + b"\x00" * params.reserve_sz


def decrypt_file(src_path: str, dst_path: str, passphrase,
                 header_size: int = 1024,
                 page_size: int = DEFAULT_PAGE_SIZE,
                 params: Optional[SqlCipherParams] = None,
                 progress=None,
                 max_pages: int = 0,
                 verify: bool = False) -> str:
    """Decrypt a whole SQLCipher database into a plain SQLite file.

    *src_path* may carry a proprietary header (QQ uses 1024 bytes); the salt
    is read from offset *header_size* of that file, and the encrypted stream
    starts there as well (the salt is the first 16 bytes of page 1).
    Returns the path of the plain database.
    """
    p = params or DEFAULT_PARAMS
    if isinstance(passphrase, str):
        passphrase = passphrase.encode("utf-8")
    chunk_pages = 256
    written = 0
    header_pages = 0
    with open(src_path, "rb") as fh, open(dst_path, "wb") as out:
        fh.seek(header_size)
        salt = fh.read(p.salt_sz)
        fh.seek(header_size)          # the salt is the first 16 bytes of page 1
        key, hmac_key = derive_keys(passphrase, salt, p)
        while True:
            raw = fh.read(chunk_pages * page_size)
            if not raw:
                break
            n = len(raw) // page_size
            buf = bytearray()
            for i in range(n):
                pgno = written + i + 1
                page = raw[i * page_size:(i + 1) * page_size]
                if not any(page):
                    # never written page (allocated but unused) -> keep zeros
                    buf += b"\x00" * page_size
                    continue
                if verify:
                    _o, _s, _iv, _hm = page_layout(pgno, page_size, p)
                    stored = page[_hm:_hm + p.hmac_sz]
                    if not hmac.compare_digest(
                            stored, page_hmac(hmac_key, page, pgno, page_size, p)):
                        raise RuntimeError("hmac verification failed on page %d" % pgno)
                plain = _decrypt_page(page, key, pgno, page_size, p)
                if pgno == 1 and len(plain) >= 32:
                    header_pages = struct.unpack(">I", plain[28:32])[0]
                buf += plain
            out.write(buf)
            written += n
            if max_pages and written >= max_pages:
                break
            if progress is not None:
                progress(written, header_pages)
        # a trailing partial page (torn write) is not usable -> pad with zeros
        if len(raw) % page_size:
            out.write(b"\x00" * (page_size - len(raw) % page_size))
    if header_pages and written < header_pages:
        with open(dst_path, "ab") as out:
            out.write(b"\x00" * ((header_pages - written) * page_size))
    return dst_path
