# -*- coding: utf-8 -*-
"""Self-test the static PE parser against the real wrapper.node.

QQ 会自动升级，所以这里不写死版本号：在 ``D:\\QQ\\versions`` 下找**最新的**
``resources\\app\\wrapper.node``；找不到就退回到安装目录里找。
"""
import glob
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from qqexport.pe import locate_key_v2_function


def find_wrapper():
    """找到本机 QQ 的 wrapper.node（挑 mtime 最新的那个）。"""
    cands = []
    for pat in (
        r"D:\QQ\versions\*\resources\app\wrapper.node",
        r"D:\QQ\resources\app\wrapper.node",
        os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Tencent", "QQ", "*", "resources", "app", "wrapper.node"),
    ):
        for path in glob.glob(pat):
            try:
                cands.append((os.path.getmtime(path), path))
            except OSError:
                pass
    if not cands:
        return None
    cands.sort()
    return cands[-1][1]


wrapper = find_wrapper()
if not wrapper:
    print("SKIP: 本机没有找到 wrapper.node（QQ 未安装？）")
    sys.exit(0)
print("wrapper.node    = %s" % wrapper)

t0 = time.time()
image = open(wrapper, "rb").read()
print("read %d bytes in %.1fs" % (len(image), time.time() - t0))

t0 = time.time()
func_rva, lea_rva, string_rva = locate_key_v2_function(image)
print("string RVA      = 0x%X" % string_rva)
print("LEA instr RVA   = 0x%X" % lea_rva)
print("function RVA    = 0x%X" % func_rva)
print("scan time       = %.2fs" % (time.time() - t0))

base = 0  # we only care about RVA here
print("breakpoint VA   = 0x%X (RVA)" % func_rva)
