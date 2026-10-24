# -*- coding: utf-8 -*-
"""端到端 API 冒烟：合成加密库 -> 起 app 服务 -> 灌口令 -> 读会话 -> 导出 7 种格式。

用法：
    python tests\\e2e_api_smoke.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from test_e2e_synthetic import _build_encrypted_msg_db  # noqa: E402
from test_decrypt import _prepend_qq_header  # noqa: E402

KEY = "SYNTHETIC_KEY_16"
WORK = os.path.join(ROOT, "build", "e2e")
DB = os.path.join(WORK, "nt_msg.db")
OUT = os.path.join(WORK, "out")
PORT = 8811
BASE = "http://127.0.0.1:%d" % PORT


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=900) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    enc = _build_encrypted_msg_db(os.path.join(WORK, "enc.db"), KEY)
    _prepend_qq_header(enc, DB)
    print("[1] synthetic encrypted db:", DB, os.path.getsize(DB))

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    args = [
        os.path.join(ROOT, "build", "payload", "python.exe"), "-m", "qqexport.app",
        "--no-browser", "--port", str(PORT), "--db", DB, "--out", OUT,
    ]
    import subprocess
    proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    try:
        state = None
        for _ in range(60):
            try:
                state = get("/api/state")
                break
            except Exception:
                import time
                time.sleep(1)
        if state is None:
            print("[!] 服务未启动")
            return 1
        print("[2] state:", state.get("stage"), "| db:", state.get("db_path"))

        r = post("/api/key", {"key": KEY})
        print("[3] /api/key ->", {k: r.get(k) for k in ("stage", "key_ready", "error")})

        r = post("/api/load", {})
        print("[4] /api/load ->", {k: r.get(k) for k in ("stage", "chats", "messages", "error")})

        chats = get("/api/chats")
        items = chats.get("chats") or []
        print("[5] /api/chats ->", len(items), "个会话")
        for c in items:
            print("      -", c.get("name"), c.get("count"), "条", c.get("last_time_text"))
        if not items:
            print("[!] 没有会话，导出测试无法进行")
            return 1

        fmts = get("/api/formats")
        raw = fmts.get("formats") or []
        names = [f.get("id") for f in raw if isinstance(f, dict)] if raw and isinstance(raw[0], dict) else raw
        print("[6] /api/formats ->", names)

        ok_all = True
        for fmt in names:
            if not isinstance(fmt, str):
                continue
            r = post("/api/export", {"format": fmt})
            print("[7] export %-9s -> ok=%s path=%s" % (fmt, r.get("ok"), r.get("path")))
            if not r.get("ok"):
                ok_all = False
        files = []
        for dirpath, _dirs, fs in os.walk(OUT):
            for f in fs:
                files.append(os.path.join(dirpath, f))
        print("[8] 输出文件 %d 个:" % len(files))
        for f in sorted(files):
            print("      ", os.path.relpath(f, OUT), os.path.getsize(f))
        print("RESULT:", "PASS" if ok_all and files else "FAIL")
        return 0 if (ok_all and files) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
