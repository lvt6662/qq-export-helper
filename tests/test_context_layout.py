# -*- coding: utf-8 -*-
"""验证 CONTEXT64 的内存布局与 Windows x64 真实布局一致。

历史教训：曾经把 Dr0..Dr7 放在 Esp/Eip/Segs 的位置，导致所有通用寄存器
整体偏移 40 字节 —— GetThreadContext 读出来的 R8/Rip 全是垃圾值，断点
就算命中也永远取不到口令。

本机的真实偏移是用「挂起线程」实测出来的：
  * 线程入口地址 (lpStartAddress) 落在 Rcx  (offset 0x80)
  * 线程参数           (lpParameter) 落在 Rdx  (offset 0x88)
  * 调试器首个断点的 exception_address + 1 (即 Rip) 落在 offset 0xF8

用法:
    python tests\\test_context_layout.py
"""
from __future__ import annotations

import ctypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from qqexport import win32 as W  # noqa: E402

MAGIC = 0x1122334455667788


def _thread_proc(param):
    while True:
        W.kernel32.Sleep(50)
        return 0


def main() -> int:
    print("sizeof(CONTEXT64) =", ctypes.sizeof(W.CONTEXT64))
    print("offsets: EFlags=0x%X Rax=0x%X Rcx=0x%X Rdx=0x%X Rsp=0x%X R8=0x%X R9=0x%X Rip=0x%X" % (
        W.CONTEXT64.EFlags.offset, W.CONTEXT64.Rax.offset, W.CONTEXT64.Rcx.offset,
        W.CONTEXT64.Rdx.offset, W.CONTEXT64.Rsp.offset,
        W.CONTEXT64.R8.offset, W.CONTEXT64.R9.offset, W.CONTEXT64.Rip.offset))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateThread.restype = ctypes.c_void_p
    kernel32.CreateThread.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
    ]
    proto = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)
    fn = proto(_thread_proc)
    start_addr = ctypes.cast(fn, ctypes.c_void_p).value
    tid = ctypes.c_uint32(0)
    # CREATE_SUSPENDED = 0x4 -> the thread has not run yet, its context is clean
    h = kernel32.CreateThread(None, 0, ctypes.cast(fn, ctypes.c_void_p),
                              ctypes.c_void_p(MAGIC), 0x4, ctypes.byref(tid))
    if not h:
        print("CreateThread 失败:", ctypes.get_last_error())
        return 1
    try:
        ctx = W.CONTEXT64()
        ctx.ContextFlags = W.CONTEXT_DEBUGGER_FULL
        if not kernel32.GetThreadContext(ctypes.c_void_p(h), ctypes.byref(ctx)):
            print("GetThreadContext 失败:", ctypes.get_last_error())
            return 1

        print("Rcx  = 0x%X  (期望 = 线程入口 0x%X)" % (ctx.Rcx, start_addr))
        print("Rdx  = 0x%X  (期望 = 线程参数 0x%X)" % (ctx.Rdx, MAGIC))
        print("Rsp  = 0x%X (mod 8 = %d)" % (ctx.Rsp, ctx.Rsp % 8))
        print("Rip  = 0x%X" % ctx.Rip)

        ok = True
        if ctx.Rcx != start_addr:
            print("!! Rcx 不对 —— CONTEXT64 布局有误")
            ok = False
        if ctx.Rdx != MAGIC:
            print("!! Rdx 不对 —— CONTEXT64 布局有误")
            ok = False
        if ctx.Rsp % 8 != 0:
            print("!! Rsp 未对齐 —— CONTEXT64 布局有误")
            ok = False
        if not (0x10000 < ctx.Rip < 0x7FFFFFFFFFFF):
            print("!! Rip 不像代码地址 —— CONTEXT64 布局有误")
            ok = False

        # 运行时自检：同一套测量逻辑再跑一遍
        try:
            measured = W.verify_context_layout()
            print("verify_context_layout ->", {k: hex(v) for k, v in measured.items()})
        except RuntimeError as exc:
            print("!! 运行时自检失败:", exc)
            ok = False

        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        kernel32.TerminateThread(ctypes.c_void_p(h), 0)
        kernel32.CloseHandle(ctypes.c_void_p(h))


if __name__ == "__main__":
    sys.exit(main())
