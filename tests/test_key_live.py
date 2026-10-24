# -*- coding: utf-8 -*-
"""Live key-extraction smoke test: launches QQ under the debugger and grabs the key.

Run:  python tests\test_key_live.py
The script prints progress; QQ must be logged in for nt_sqlite3_key_v2 to fire.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from qqexport.key import extract_key, find_qq_install

print("locating QQ ...")
info = find_qq_install()
print("QQ.exe      :", info[0])
print("InstallDir  :", info[1])
print("Version     :", info[2])
print("wrapper.node:", info[3])
print()

t0 = time.time()
key = extract_key(progress=lambda m: print("[%6.1fs] %s" % (time.time() - t0, m)))
print()
print("KEY =", repr(key))
print("len =", len(key))
