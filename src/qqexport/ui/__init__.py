# -*- coding: utf-8 -*-
"""qqexport.ui —— 界面层（本地 HTTP 服务 + 静态前端）。

本子包只依赖 Python 标准库：

* :mod:`qqexport.ui.server` —— ``ThreadingHTTPServer`` 承载的 JSON API，
  对外提供扫描 / 取密钥 / 加载 / 导出四类动作；
* ``static/`` —— 纯原生 HTML + CSS + JS 的前端（无框架、无 CDN、可离线）。

前端通过 ``fetch`` 与后端通信，所有错误都以 JSON 形式返回，
不会给用户看到 Python 堆栈。
"""

from .server import QQExportServer

__all__ = ["QQExportServer"]
