# -*- coding: utf-8 -*-
"""静态界面自测：悬浮进度窗、账号选择器、口令失效引导、v2 接口调用。

不依赖 pytest：可直接 ``python tests/test_ui_static.py`` 运行；
也可以用 pytest 收集（函数名以 test_ 开头）。
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.normpath(os.path.join(HERE, "..", "src", "qqexport", "ui", "static"))

# 改造前就存在的元素 id —— 不能被破坏（服务端 / 旧测试可能会用到）
LEGACY_IDS = (
    "appRoot", "sidebar", "appVer", "qqInfo", "btnExit",
    "btnScan", "btnAutoKey", "btnLoad", "btnExport",
    "stageBadge", "stageDot", "stageText", "stageProgress",
    "chatList", "sideFoot", "searchInput",
    "headAvatar", "chatTitle", "chatSub", "msgScroll", "btnExportCurrent",
    "statusText", "statusDb", "statusCount",
    "keyModal", "keyModalDesc", "keyInput", "keyError", "keyOk", "keyCancel",
    "exportModal", "exportFormat", "exportDir", "exportLimit",
    "exportOk", "exportCancel", "exportError",
)

# 悬浮进度窗（含最小化后的胶囊 + 底部辅助细条）
PROGRESS_IDS = (
    "progressOverlay", "progressCard", "progSpinner", "progTitle", "btnProgressMin",
    "progTrack", "progFill", "progPct", "progElapsed", "progStep",
    "progressCapsule", "capSpinner", "capPct", "capTitle",
    "thinProgress", "thinFill",
)

# 账号选择器
ACCOUNT_IDS = (
    "acctPanel", "acctSelect", "acctBadge", "acctMeta",
    "btnGetKey", "btnRefreshAccounts", "btnSidebar",
)

# 口令失效 / 缺少口令 的引导对话框
NEED_KEY_IDS = (
    "needKeyModal", "needKeyTitle", "needKeyDesc", "needKeyError",
    "needKeyCancel", "needKeyRetry",
)

FORBIDDEN_URLS = ("http://", "https://", "//cdn.", "cdnjs", "unpkg", "jsdelivr")
BROWSER_WORDS = ("浏览器", "window.open", "在浏览器中打开")


def _read(name):
    path = os.path.join(STATIC_DIR, name)
    assert os.path.isfile(path), "缺少静态文件 %s" % path
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _html():
    return _read("index.html")


def _js():
    return _read("app.js")


def _css():
    return _read("style.css")


def _css_rule(text, selector):
    """取出一条 CSS 规则的内容（找不到返回空串）。"""
    pattern = re.compile(re.escape(selector) + r"\s*\{([^}]*)\}", re.IGNORECASE)
    match = pattern.search(text)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------
def test_static_files_exist():
    """三个静态文件必须齐全（前端零 CDN 依赖）。"""
    for name in ("index.html", "app.js", "style.css"):
        path = os.path.join(STATIC_DIR, name)
        assert os.path.isfile(path), path
        assert os.path.getsize(path) > 0, path


def test_static_assets_have_no_external_urls():
    """静态资源里不能出现外链（原生窗口里完全离线运行）。"""
    for name in ("index.html", "app.js", "style.css"):
        text = _read(name)
        for token in FORBIDDEN_URLS:
            assert token not in text, "%s 里出现了外链 %s" % (name, token)


def test_index_html_keeps_legacy_ids():
    """改造前就有的元素 id 一个都不能少。"""
    html = _html()
    for element_id in LEGACY_IDS:
        assert 'id="%s"' % element_id in html, "缺少原有元素 #%s" % element_id


def test_index_html_has_floating_progress_window():
    """悬浮进度窗：卡片 + 最小化胶囊 + 底部辅助细条。"""
    html = _html()
    for element_id in PROGRESS_IDS:
        assert 'id="%s"' % element_id in html, "缺少进度窗元素 #%s" % element_id
    # 卡片里要有：标题 / 大进度条 / 百分比 / 已用时 / 当前步骤 / 最小化按钮
    assert "最小化" in html, "进度窗缺少「最小化」按钮"
    assert "正在处理" in html
    assert "已用时" in html
    assert 'class="prog-track"' in html
    assert 'class="prog-fill"' in html
    # 胶囊可拖动、点击可还原
    assert 'class="progress-capsule"' in html


def test_index_html_has_account_selector():
    """左侧栏顶部的账号选择器。"""
    html = _html()
    for element_id in ACCOUNT_IDS:
        assert 'id="%s"' % element_id in html, "缺少账号选择器元素 #%s" % element_id
    assert 'id="acctSelect"' in html
    assert "账号" in html


def test_index_html_has_need_key_modal():
    """密钥失效 / 缺少密钥 的识别引导对话框。"""
    html = _html()
    for element_id in NEED_KEY_IDS:
        assert 'id="%s"' % element_id in html, "缺少引导框元素 #%s" % element_id
    assert "识别密钥" in html, "引导框缺少「识别密钥」按钮"
    assert "先不处理" in html
    assert "自动退出" in html, "引导框应说明会自动退出 QQ"
    assert "重新启动" in html, "引导框应说明会重新启动 QQ"
    assert "不要手动操作 QQ" in html, "引导框应提示抓取期间不要手动操作 QQ"


def test_index_html_has_no_browser_wording():
    """原生窗口（WebView2）里不该再有「在浏览器中打开」这类文案/行为。"""
    html = _html()
    for word in BROWSER_WORDS:
        assert word not in html, "index.html 里仍有 %r" % word
    assert "<title>QQ导出助手</title>" in html


def test_index_html_ids_are_unique():
    """同一个 id 不能出现两次（否则 getElementById 取到错的元素）。"""
    html = _html()
    ids = re.findall(r'\bid="([^"]+)"', html)
    dupes = set(i for i in ids if ids.count(i) > 1)
    assert not dupes, "重复的 id: %s" % sorted(dupes)


def test_app_js_uses_v2_endpoints():
    """前端必须用上 v2 的新接口。"""
    js = _js()
    for endpoint in ("/api/accounts", "/api/key/auto", "/api/qq/close",
                     "/api/load", "/api/key", "/api/state", "/api/chats"):
        assert endpoint in js, "app.js 里没有调用 %s" % endpoint
    # 老服务端没有的接口要走降级分支
    assert "/api/auto-key" in js, "缺少老服务端 /api/auto-key 的降级分支"


def test_app_js_sends_uin():
    """切换账号 / 抓口令时要带上 uin。"""
    js = _js()
    assert "uin" in js
    assert re.search(r"uin\s*[:=]", js), "app.js 里没有给 uin 赋值"
    assert "uinBody" in js, "缺少 uinBody（按服务端版本决定是否带请求体）"


def test_app_js_handles_need_key():
    """load 返回 need_key 时要弹引导框，并区分 no_key / stale_key。"""
    js = _js()
    for token in ("need_key", "no_key", "stale_key", "needKeyModal",
                  "需要识别数据库密钥", "数据库密钥已失效", "openNeedKeyModal"):
        assert token in js, "app.js 里缺少 %r" % token


def test_index_html_has_key_banner_and_prominent_button():
    """缺密钥横幅 + 顶部醒目「获取密钥」按钮。"""
    html = _html()
    for element_id in ("keyBanner", "kbTitle", "kbDesc", "btnKeyBanner"):
        assert 'id="%s"' % element_id in html, "缺少横幅元素 #%s" % element_id
    assert "tb-btn tb-key" in html, "顶部「获取密钥」按钮应带 tb-key 醒目样式"
    assert "获取密钥" in html
    assert "识别密钥" in html


def test_style_has_key_button_and_banner_rules():
    """CSS 里要有醒目按钮与横幅的样式规则。"""
    css = _read("style.css")
    for token in (".tb-btn.tb-key", ".key-banner", ".kb-btn", ".key-steps",
                  "keyPulse"):
        assert token in css, "style.css 里缺少 %r" % token


def test_index_html_manual_key_is_hidden_in_help():
    """手动填写密钥只保留在帮助面板的「高级」里，主界面不再直接暴露。"""
    html = _html()
    assert 'id="helpManualKey"' in html, "帮助面板缺少「手动填写密钥」入口"
    assert "高级" in html
    # 主流程的引导框里不该再有「改为手动填写」这类直接入口
    assert "改为手动填写" not in html, "引导框里不应再有手动填写入口"


def test_app_js_auto_pops_need_key_modal_once():
    """检测到 QQ 已登录但没有密钥时，只自动弹一次引导框。"""
    js = _js()
    assert "needKeyAutoShown" in js
    assert "qqLoggedIn" in js
    assert "renderKeyBanner" in js


def test_index_html_has_next_hint_bar():
    """界面上要有「下一步」常驻提示条 + 指向箭头。"""
    html = _html()
    for element_id in ("nextHint", "nhArrow", "nhStep", "nhText", "nhGo",
                       "nhClose", "nextArrow"):
        assert 'id="%s"' % element_id in html, "缺少提示条元素 #%s" % element_id


def test_index_html_acct_more_is_collapsible():
    """「找不到？点这里」是可折叠的托底区，里面的扫描按钮叫「重新扫描」。"""
    html = _html()
    assert 'id="btnAcctMore"' in html, "缺少「找不到？点这里」折叠按钮"
    assert "找不到？点这里" in html
    assert "重新扫描" in html, "托底区里的扫描按钮应叫「重新扫描」"
    # 折叠按钮要带 aria-expanded，JS 才好同步状态
    assert 'aria-expanded="false"' in html


def test_style_has_next_hint_rules():
    """提示条 / 呼吸高亮 / 指向箭头的样式规则。"""
    css = _read("style.css")
    for token in (".next-hint", ".nh-arrow", ".nh-btn", ".next-arrow",
                  ".is-next", "nextGlow", "naBob", ".acct-more-toggle"):
        assert token in css, "style.css 里缺少 %r" % token


def test_app_js_computes_next_hint():
    """JS 要能算出「下一步该点哪儿」，并高亮目标控件。"""
    js = _js()
    for token in ("computeNextHint", "renderNextHint", "placeNextArrow",
                  "dismissNextHint", "clearNextHighlight", "is-next",
                  "nextHintDismissedId"):
        assert token in js, "app.js 里缺少 %r" % token
    # 每一个「下一步」都要落到具体的按钮上
    assert "btnScan" in js and "btnAutoKey" in js and "btnLoad" in js


def test_app_js_mentions_auto_scan():
    """自动全盘扫描要在界面里说清楚（不需要用户手动点）。"""
    js = _js()
    assert "全盘搜索" in js or "自动全盘" in js, "提示条里应说明会自动全盘搜索"


def test_app_js_uses_new_state_fields():
    """要用上 v2 的状态字段：progress_pct / progress_text / current_uin / accounts。"""
    js = _js()
    for token in ("progress_pct", "progress_text", "current_uin", "accounts",
                  "has_key", "db_mtime", "key_time"):
        assert token in js, "app.js 里没有使用字段 %s" % token


def test_app_js_reports_errors_visibly():
    """所有请求都要有错误处理，而且错误必须显示出来（不能静默）。"""
    js = _js()
    assert js.count("catch") >= 5, "错误处理点太少"
    for token in ("reportError", "showToast", "无法连接本地服务", "不是合法 JSON"):
        assert token in js, "app.js 里缺少 %r" % token


def test_app_js_is_vanilla():
    """保持原来的风格：原生 JS，不引框架。"""
    js = _js()
    for token in ("react", "vue.", "jquery", "angular"):
        assert token not in js.lower(), "app.js 里出现了框架痕迹 %r" % token
    assert "<script" not in js.lower(), "app.js 里不允许再内联脚本标签"


def test_app_js_only_uses_existing_element_ids():
    """app.js 里 $('xxx') 用到的 id 必须在 index.html 里真实存在。"""
    html = _html()
    js = _js()
    used = set(re.findall(r"\$\('([^']+)'\)", js))
    used |= set(re.findall(r'getElementById\("([^"]+)"\)', js))
    html_ids = set(re.findall(r'\bid="([^"]+)"', html))
    missing = sorted(used - html_ids)
    assert not missing, "app.js 引用了不存在的元素 id: %s" % missing


def test_style_progress_window_is_fixed_and_on_top():
    """悬浮进度窗：position: fixed + 高 z-index + 有边框/阴影。"""
    css = _css()
    overlay = _css_rule(css, ".progress-overlay")
    assert "position: fixed" in overlay, "进度窗必须是 position: fixed"
    zindex = re.search(r"z-index\s*:\s*(\d+)", overlay)
    assert zindex, "进度窗没有 z-index"
    assert int(zindex.group(1)) >= 900, "进度窗的 z-index 太小：%s" % zindex.group(1)

    card = _css_rule(css, ".prog-card")
    assert "border" in card, "进度卡片缺少边框"
    assert "box-shadow" in card, "进度卡片缺少阴影"

    capsule = _css_rule(css, ".progress-capsule")
    assert "position: fixed" in capsule, "胶囊必须是 position: fixed"


def test_style_progress_indeterminate_animation():
    """progress_pct 为 null 时要有不定进度动画。"""
    css = _css()
    assert "indeterminate" in css
    assert "@keyframes prog-indet" in css
    assert "prog-fill" in css


def test_style_account_badges_have_distinct_states():
    """账号状态徽章：ready / no_key / stale / loading 颜色各不相同。"""
    css = _css()
    colors = {}
    for state in ("ready", "no_key", "stale", "loading"):
        rule = _css_rule(css, '.acct-badge[data-state="%s"]' % state)
        assert rule, "缺少 %s 状态的徽章样式" % state
        match = re.search(r"background\s*:\s*([^;]+)", rule)
        assert match, "%s 状态徽章没有背景色" % state
        colors[state] = match.group(1).strip()
    assert len(set(colors.values())) == len(colors), "各状态徽章颜色应不同：%s" % colors


def test_style_sidebar_collapsible_and_responsive():
    """窄窗口时左侧栏可折叠，并覆盖 900×600 ~ 1920×1080。"""
    css = _css()
    assert ".sidebar-collapsed" in css, "缺少左侧栏折叠样式"
    assert "max-width: 1200px" in css
    assert "max-width: 1000px" in css
    assert "max-height: 720px" in css


def test_style_no_external_fonts():
    """不能引用外部字体（原生窗口要离线可用）。"""
    css = _css()
    assert "@import" not in css, "style.css 里不允许 @import"
    for token in ("fonts.googleapis", "fonts.gstatic", "cdn."):
        assert token not in css, "style.css 里出现了外部资源 %r" % token


def test_index_html_has_local_privacy_notice():
    """页面必须明确写出：仅在本机本地运行、聊天记录不上传云端。"""
    html = _html()
    for element_id in ("privacyBar", "btnPrivacy", "privacySec"):
        assert 'id="%s"' % element_id in html, "缺少隐私提示元素 #%s" % element_id
    for word in ("仅在本机本地运行", "不访问互联网", "不会上传", "云端服务器", "隐私与数据安全"):
        assert word in html, "隐私提示缺少关键表述 %r" % word
    css = _css()
    assert ".privacy-bar" in css, "style.css 缺少 .privacy-bar 样式"
    js = _js()
    assert "btnPrivacy" in js, "app.js 未绑定隐私说明按钮"
    assert "privacySec" in js, "app.js 未定位到隐私说明小节"


def main():
    fns = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print("PASS %s" % name)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("FAIL %s: %r" % (name, exc))
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
