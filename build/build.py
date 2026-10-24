#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QQ导出助手 —— 一键打包脚本

产物：dist\\QQ导出助手.exe
  = C# 自解压启动器（csc.exe 编译，内嵌 payload.zip）
  payload.zip = Python 3.12 嵌入运行时 + qqexport 包 + 第三方依赖

用法：
    python build\\build.py            # 完整打包
    python build\\build.py --no-zip   # 只编译启动器（复用已有 payload.zip）
"""

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
PAYLOAD = os.path.join(BUILD, "payload")
PAYLOAD_ZIP = os.path.join(BUILD, "payload.zip")
DIST = os.path.join(ROOT, "dist")
VENDOR_DIR = os.path.join(ROOT, "vendor")

PYTHON_EMBED_VERSION = "3.12.10"
PYTHON_EMBED_URL = (
    "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip"
    % (PYTHON_EMBED_VERSION, PYTHON_EMBED_VERSION)
)
PYTHON_ZIP = os.path.join(BUILD, "python-%s-embed-amd64.zip" % PYTHON_EMBED_VERSION)
LAUNCHER_CS = os.path.join(BUILD, "launcher", "Program.cs")
LAUNCHER_LIB = os.path.join(BUILD, "launcher", "lib")

# 原生窗口（WebView2）需要的程序集：编译期引用 + 随包发布
WEBVIEW2_DLLS = ["Microsoft.Web.WebView2.Core.dll",
                 "Microsoft.Web.WebView2.WinForms.dll",
                 "WebView2Loader.dll"]

CSC = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
FW = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319"

def _detect_site_packages():
    """当前解释器的 site-packages；换机/CI 打包时不再依赖作者本机路径。"""
    try:
        import sysconfig

        purelib = sysconfig.get_paths().get("purelib")
        if purelib and os.path.isdir(purelib):
            return purelib
    except Exception:  # noqa: BLE001
        pass
    fallback = os.path.join(
        os.path.expanduser("~"),
        "AppData", "Local", "Programs", "Python", "Python312", "Lib", "site-packages",
    )
    return fallback if os.path.isdir(fallback) else ""


SITE_PACKAGES = _detect_site_packages()

# 需要打包进发布版的第三方库
VENDOR = ["openpyxl", "et_xmlfile", "reportlab", "PIL", "jinja2", "markupsafe",
          "cryptography", "cffi", "pycparser"]
VENDOR_FILES = ["_cffi_backend.cp312-win_amd64.pyd"]

#: 可选依赖：sqlcipher3 只用于「纯 Python 解密失败」时的回退路径。
#: 源码仓库不携带这个二进制包；可放在 vendor/sqlcipher3_pkg/ 下，
#: 或直接用 pip 装进 site-packages（见 README「从源码构建」）。
VENDOR_EXTRA = [(os.path.join(VENDOR_DIR, "sqlcipher3_pkg", "sqlcipher3"), "sqlcipher3")]


def log(msg):
    print("[build] " + msg, flush=True)


def rm_tree(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def _download(url, dest):
    """下载到 dest（先写 .part，成功后原子改名）。"""
    log("下载 %s" % url)
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    log("  %.1f / %.1f MB" % (done / 1048576.0, total / 1048576.0))
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def extract_python_runtime():
    if not os.path.isfile(PYTHON_ZIP):
        if os.environ.get("QQEXPORT_NO_DOWNLOAD"):
            raise SystemExit(
                "缺少 Python 嵌入运行时压缩包: %s（已设置 QQEXPORT_NO_DOWNLOAD，"
                "请手动下载 %s 放到该位置）" % (PYTHON_ZIP, PYTHON_EMBED_URL)
            )
        log("本地没有 Python 嵌入运行时，尝试自动下载 ...")
        _download(PYTHON_EMBED_URL, PYTHON_ZIP)
    rm_tree(PAYLOAD)
    os.makedirs(PAYLOAD, exist_ok=True)
    with zipfile.ZipFile(PYTHON_ZIP) as z:
        z.extractall(PAYLOAD)
    log("Python 运行时已释放: %s" % PAYLOAD)

    # 允许 site-packages 生效，并挂上我们自己的包目录
    pth = os.path.join(PAYLOAD, "python312._pth")
    with open(pth, "w", encoding="utf-8", newline="\n") as f:
        f.write("python312.zip\n.\nLib\\site-packages\nimport site\n")
    log("写入 python312._pth")


def copy_tree(src, dst):
    if not os.path.isdir(src):
        return False
    os.makedirs(dst, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    return True


def vendor_dependencies():
    sp = os.path.join(PAYLOAD, "Lib", "site-packages")
    os.makedirs(sp, exist_ok=True)

    missing = []
    for name in VENDOR:
        src = os.path.join(SITE_PACKAGES, name)
        if copy_tree(src, os.path.join(sp, name)):
            log("vendor: %s" % name)
        else:
            missing.append(name)
            log("!! 缺少依赖: %s" % name)

    for src, name in VENDOR_EXTRA:
        if copy_tree(src, os.path.join(sp, name)):
            log("vendor: %s" % name)
        elif copy_tree(os.path.join(SITE_PACKAGES, name), os.path.join(sp, name)):
            log("vendor: %s（来自 site-packages）" % name)
        else:
            # sqlcipher3 是可选回退路径，缺了不影响主流程（纯 Python 解密优先）
            log("!! 未找到可选依赖 %s（纯 Python 解密路径仍可用，继续打包）" % name)

    for name in VENDOR_FILES:
        src = os.path.join(SITE_PACKAGES, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(sp, name))
            log("vendor: %s" % name)
        else:
            missing.append(name)
            log("!! 缺少依赖: %s" % name)

    # 缺依赖时不能「跳过但构建成功」——否则产物运行到导出才 ImportError
    if missing:
        raise SystemExit(
            "缺少打包必需的第三方库：%s（请先 pip install）" % ", ".join(missing)
        )

    # 本工程自身的包
    pkg = os.path.join(ROOT, "src", "qqexport")
    if not copy_tree(pkg, os.path.join(sp, "qqexport")):
        raise SystemExit("找不到源码包: " + pkg)
    log("vendor: qqexport")


def vendor_webview2():
    """把原生窗口用的 WebView2 组件拷进 payload（运行目录下的 WebView2\\）"""
    src_dir = LAUNCHER_LIB
    dst_dir = os.path.join(PAYLOAD, "WebView2")
    if not os.path.isdir(src_dir):
        log("!! 缺少 WebView2 组件目录（跳过）：%s" % src_dir)
        return
    os.makedirs(dst_dir, exist_ok=True)
    for name in WEBVIEW2_DLLS:
        src = os.path.join(src_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dst_dir, name))
            log("vendor: WebView2/%s" % name)
        else:
            log("!! 缺少 WebView2 组件（跳过）：%s" % src)


def make_zip():
    if os.path.exists(PAYLOAD_ZIP):
        os.remove(PAYLOAD_ZIP)
    count = 0
    with zipfile.ZipFile(PAYLOAD_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, dirs, files in os.walk(PAYLOAD):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(base, name)
                rel = os.path.relpath(full, PAYLOAD).replace(os.sep, "/")
                z.write(full, rel)
                count += 1
    log("payload.zip: %d 个文件, %.1f MB" % (count, os.path.getsize(PAYLOAD_ZIP) / 1048576.0))


def ensure_bom(path):
    """返回带 UTF-8 BOM 的临时副本路径（不就地改写源码树）。"""
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"\xef\xbb\xbf"):
        return path
    tmp = os.path.join(BUILD, "_launcher_build.cs")
    with open(tmp, "wb") as f:
        f.write(b"\xef\xbb\xbf" + data)
    log("已生成带 UTF-8 BOM 的临时源文件: " + tmp)
    return tmp


def compile_launcher():
    if not os.path.isfile(CSC):
        raise SystemExit("找不到 csc.exe: " + CSC)
    launcher_src = ensure_bom(LAUNCHER_CS)
    os.makedirs(DIST, exist_ok=True)
    exe = os.path.join(DIST, "QQ导出助手.exe")
    cmd = [
        CSC, "/nologo", "/target:winexe", "/optimize+", "/platform:x64",
        "/out:" + exe,
        "/win32manifest:" + os.path.join(BUILD, "launcher", "app.manifest"),
        "/reference:System.Windows.Forms.dll",
        "/reference:" + os.path.join(FW, "System.Drawing.dll"),
        "/reference:" + os.path.join(FW, "System.IO.Compression.dll"),
        "/reference:" + os.path.join(FW, "System.IO.Compression.FileSystem.dll"),
        "/resource:" + PAYLOAD_ZIP + ",QQExportHelper.payload.zip",
        launcher_src,
    ]
    # 原生窗口要用 WebView2（编译期引用）
    for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
        ref = os.path.join(LAUNCHER_LIB, name)
        if os.path.isfile(ref):
            cmd.append("/reference:" + ref)
        else:
            log("!! 缺少 WebView2 引用（原生窗口将不可用）：%s" % ref)
    log("csc 编译中 ...")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        if launcher_src != LAUNCHER_CS and os.path.isfile(launcher_src):
            os.remove(launcher_src)
    except OSError:
        pass
    if proc.stdout.strip():
        log(proc.stdout.strip())
    if proc.stderr.strip():
        log(proc.stderr.strip())
    if proc.returncode != 0:
        raise SystemExit("编译失败，退出码 %d" % proc.returncode)
    log("产物: %s (%.2f MB)" % (exe, os.path.getsize(exe) / 1048576.0))

    # 安装包形态：二进制完全相同，只是文件名带「安装程序」，
    # 双击时启动器会识别出来、直接进安装向导（见 Program.LooksLikeInstaller）。
    installer = os.path.join(DIST, "QQ导出助手-安装程序.exe")
    shutil.copy2(exe, installer)
    log("安装包: %s (%.2f MB)" % (installer, os.path.getsize(installer) / 1048576.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-zip", action="store_true", help="只编译启动器")
    args = ap.parse_args()

    if not args.no_zip:
        extract_python_runtime()
        vendor_dependencies()
        vendor_webview2()
        make_zip()
    compile_launcher()
    log("完成。")


if __name__ == "__main__":
    main()
