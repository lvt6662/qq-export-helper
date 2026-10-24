/* ============================================================
   QQ导出助手 —— 前端逻辑（原生 JS，无框架、无 CDN）
   通过 fetch 与本地 HTTP 服务通信，所有错误都以 JSON 返回

   界面被原生窗口（WebView2）承载，不提供任何外部链接 / 新窗口。
   ============================================================ */
(function () {
  'use strict';

  /* ---------------------------------------------------------
     状态
     --------------------------------------------------------- */
  var state = {
    stage: 'idle',
    task: '',
    qq: {},
    db_path: '',
    plain_db: '',
    chats: 0,
    messages: 0,
    error: '',
    key_hint: '',
    key_ready: false,
    progress: [],
    out_dir: 'D:\\QQ导出',
    last_export: {},
    modules: {},
    accounts: [],        // v2：账号列表
    current_uin: '',     // v2：当前账号
    progress_pct: null,  // v2：0-100 或 null（null = 不定进度）
    progress_text: ''    // v2：一句话进度
  };

  var currentChats = [];      // 全部会话（summary）
  var filteredChats = [];     // 搜索过滤后
  var currentChat = null;     // 当前打开的会话（含消息）
  var pollTimer = null;
  var pollFails = 0;

  var accounts = [];          // 账号列表（来自 /api/accounts）
  var currentUin = '';        // 当前选中的账号 uin
  var loadingUin = '';        // 正在读取的账号 uin
  var needKeyUin = '';        // 需要密钥的账号 uin
  var needKeyAutoShown = false;  // 本次会话是否已自动弹过「识别密钥」引导框
  var serverV2 = false;       // 服务端是否支持 v2 接口（多账号 / 新抓取流程）
  var acctHintText = '';      // 找不到账号时服务端给的排查提示

  var $ = function (id) { return document.getElementById(id); };

  /* ---------------------------------------------------------
     通用工具
     --------------------------------------------------------- */
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function humanSize(num) {
    num = Number(num || 0);
    if (!isFinite(num) || num <= 0) { return ''; }
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (num >= 1024 && i < units.length - 1) { num /= 1024; i++; }
    return (i === 0 ? num.toFixed(0) : num.toFixed(1)) + ' ' + units[i];
  }

  function tsText(ts) {
    // UNIX 秒 -> 'YYYY-MM-DD HH:MM'
    var n = Number(ts || 0);
    if (!n) { return ''; }
    var d = new Date(n * 1000);
    if (isNaN(d.getTime()) || d.getFullYear() < 1971) { return ''; }
    function p(x) { return (x < 10 ? '0' : '') + x; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  }

  function shortTime(text) {
    if (!text) { return ''; }
    // 2024-05-01 12:30:00 -> 05-01 12:30
    var m = String(text).match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (!m) { return String(text); }
    var year = new Date().getFullYear();
    if (parseInt(m[1], 10) === year) { return m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5]; }
    return m[1] + '-' + m[2] + '-' + m[3];
  }

  function api(path, options) {
    options = options || {};
    return fetch(path, {
      method: options.method || 'GET',
      headers: options.body ? { 'Content-Type': 'application/json' } : {},
      body: options.body ? JSON.stringify(options.body) : undefined,
      cache: 'no-store'
    }).then(function (res) {
      return res.text().then(function (txt) {
        var data = null;
        var broken = false;
        try { data = txt ? JSON.parse(txt) : null; } catch (e) { broken = true; }
        if (!res.ok) {
          var msg = (data && data.error) ? data.error : ('请求失败（HTTP ' + res.status + '）');
          var err = new Error(msg);
          err.payload = data;
          err.httpStatus = res.status;
          throw err;
        }
        if (broken) {
          throw new Error('服务端返回的不是合法 JSON：' + path);
        }
        if (!data || typeof data !== 'object') {
          throw new Error('服务端没有返回数据：' + path);
        }
        return data;
      });
    }, function (netErr) {
      // 网络层失败（服务没起来、连接被拒）——也给出可读的提示
      var err = new Error('无法连接本地服务：' +
        (netErr && netErr.message ? netErr.message : String(netErr)));
      err.network = true;
      throw err;
    });
  }

  /* 服务端返回 ok:false（或 stage=error）时抛出，调用方统一处理 */
  function checkOk(data, fallback) {
    if (!data) { throw new Error(fallback || '操作失败'); }
    if (data.ok === false) {
      var err = new Error(data.error || fallback || '操作失败');
      err.payload = data;
      throw err;
    }
    if (data.stage === 'error' && data.error) {
      var err2 = new Error(data.error);
      err2.payload = data;
      throw err2;
    }
    return data;
  }

  /* 老服务端没有这个接口（404 / “未知接口”）时用于降级 */
  function isUnknownEndpoint(err) {
    if (!err) { return false; }
    if (err.httpStatus === 404) { return true; }
    return /未知接口|not found/i.test(err.message || '');
  }

  function setStatus(text, isError) {
    var el = $('statusText');
    if (!el) { return; }
    el.textContent = text;
    el.style.color = isError ? '#ffd7d7' : '';
  }

  /* 错误提示条（右上角堆叠） */
  function showToast(text, kind) {
    var wrap = $('toastWrap');
    if (!wrap) { return; }
    var el = document.createElement('div');
    el.className = 'toast' + (kind ? (' toast-' + kind) : '');
    el.textContent = text;
    el.addEventListener('click', function () {
      if (el.parentNode) { el.parentNode.removeChild(el); }
    });
    wrap.appendChild(el);
    while (wrap.childNodes.length > 5) { wrap.removeChild(wrap.firstChild); }
    setTimeout(function () {
      if (el.parentNode) { el.parentNode.removeChild(el); }
    }, 7000);
  }

  function reportError(err) {
    var msg = (err && err.message) ? err.message : String(err);
    setStatus(msg, true);
    showToast(msg, 'error');
  }

  /* ============================================================
     悬浮进度窗
     ============================================================ */
  var TASK_TITLES = {
    'scan': '正在扫描 QQ',
    'load': '正在读取聊天记录',
    'auto-key': '正在自动获取数据库口令',
    'key-auto': '正在自动获取数据库口令',
    'key': '正在获取数据库口令',
    'export': '正在导出聊天记录'
  };

  var taskStack = [];        // 本地发起的长任务（可嵌套）
  var prog = {
    serverTask: '',         // 服务端正在跑的任务
    serverStartedAt: 0,
    minimized: false
  };
  var progTimer = null;

  function startTask(title) {
    taskStack.push({ title: title, startedAt: Date.now() });
    renderProgress();
  }

  function endTask() {
    taskStack.pop();
    renderProgress();
  }

  function currentLocalTask() {
    return taskStack.length ? taskStack[taskStack.length - 1] : null;
  }

  function progressTitle() {
    var local = currentLocalTask();
    if (local) { return local.title; }
    return TASK_TITLES[prog.serverTask] || '正在处理…';
  }

  function progressStartedAt() {
    var t = taskStack.length ? taskStack[0].startedAt : 0;
    if (prog.serverTask && prog.serverStartedAt) {
      t = t ? Math.min(t, prog.serverStartedAt) : prog.serverStartedAt;
    }
    return t || Date.now();
  }

  /* 百分比：null = 不定进度 */
  function currentPct() {
    var p = state.progress_pct;
    if (p === null || p === undefined || p === '') { return null; }
    var n = Number(p);
    if (!isFinite(n)) { return null; }
    if (n < 0) { n = 0; }
    if (n > 100) { n = 100; }
    return n;
  }

  function currentStep() {
    if (state.progress_text) { return String(state.progress_text); }
    var logs = state.progress || [];
    if (logs.length) { return String(logs[logs.length - 1]); }
    return '';
  }

  function fmtElapsed(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    if (sec < 60) { return sec + ' 秒'; }
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    if (m < 60) { return m + ' 分 ' + (s < 10 ? '0' : '') + s + ' 秒'; }
    return Math.floor(m / 60) + ' 小时 ' + (m % 60) + ' 分';
  }

  function renderProgress() {
    var overlay = $('progressOverlay');
    var capsule = $('progressCapsule');
    var thin = $('thinProgress');
    if (!overlay || !capsule) { return; }

    var busy = !!(taskStack.length || prog.serverTask);
    if (!busy) {
      overlay.hidden = true;
      capsule.hidden = true;
      if (thin) { thin.hidden = true; }
      if (progTimer) { clearInterval(progTimer); progTimer = null; }
      return;
    }

    var pct = currentPct();
    var title = progressTitle();
    var step = currentStep();
    var pctText = (pct === null) ? '进行中' : (Math.round(pct * 10) / 10) + '%';

    $('progTitle').textContent = title;
    $('progStep').textContent = step || '处理中…';
    $('progPct').textContent = pctText;
    $('progElapsed').textContent = '已用时 ' + fmtElapsed((Date.now() - progressStartedAt()) / 1000);
    $('progFill').style.width = (pct === null) ? '38%' : pct + '%';
    overlay.classList.toggle('indeterminate', pct === null);

    // 底部细进度条（辅助）
    if (thin) {
      thin.hidden = false;
      thin.classList.toggle('indeterminate', pct === null);
      $('thinFill').style.width = (pct === null) ? '38%' : pct + '%';
    }

    // 最小化后的胶囊
    $('capPct').textContent = (pct === null) ? '…' : pctText;
    $('capTitle').textContent = title;

    if (prog.minimized) {
      overlay.hidden = true;
      capsule.hidden = false;
    } else {
      // 有模态框打开时让位（否则大卡片会压住弹窗的按钮）
      overlay.hidden = anyModalOpen();
      capsule.hidden = true;
    }

    if (!progTimer) {
      progTimer = setInterval(renderProgress, 500);
    }
  }

  /* 当前是否有模态框打开 */
  function anyModalOpen() {
    var ids = ['keyModal', 'needKeyModal', 'exportModal'];
    for (var i = 0; i < ids.length; i++) {
      var el = $(ids[i]);
      if (el && !el.hidden) { return true; }
    }
    return false;
  }

  function minimizeProgress() {
    prog.minimized = true;
    renderProgress();
  }

  function restoreProgress() {
    prog.minimized = false;
    renderProgress();
  }

  /* 胶囊拖动 */
  function bindCapsuleDrag() {
    var capsule = $('progressCapsule');
    if (!capsule) { return; }
    var dragging = false;
    var moved = false;
    var dx = 0;
    var dy = 0;

    capsule.addEventListener('mousedown', function (e) {
      dragging = true;
      moved = false;
      var rect = capsule.getBoundingClientRect();
      dx = e.clientX - rect.left;
      dy = e.clientY - rect.top;
      capsule.classList.add('dragging');
      e.preventDefault();
    });

    document.addEventListener('mousemove', function (e) {
      if (!dragging) { return; }
      moved = true;
      var x = e.clientX - dx;
      var y = e.clientY - dy;
      var maxX = Math.max(0, window.innerWidth - capsule.offsetWidth - 4);
      var maxY = Math.max(0, window.innerHeight - capsule.offsetHeight - 4);
      capsule.style.left = Math.max(0, Math.min(maxX, x)) + 'px';
      capsule.style.top = Math.max(0, Math.min(maxY, y)) + 'px';
      capsule.style.right = 'auto';
    });

    document.addEventListener('mouseup', function () {
      if (!dragging) { return; }
      dragging = false;
      capsule.classList.remove('dragging');
    });

    capsule.addEventListener('click', function () {
      if (moved) { moved = false; return; }   // 刚拖过就别弹回卡片
      restoreProgress();
    });
  }

  /* ============================================================
     账号选择器
     ============================================================ */
  var ACCOUNT_STATE_TEXT = {
    ready: '已就绪',
    no_key: '需要口令',
    stale: '口令失效',
    loading: '读取中',
    none: '未知'
  };

  function accountStateOf(acc) {
    if (!acc) { return 'none'; }
    if (loadingUin && String(acc.uin) === String(loadingUin)) { return 'loading'; }
    var s = acc.state;
    if (s === 'ready' || s === 'no_key' || s === 'stale') { return s; }
    // 降级：服务端没给 state 时按 has_key 判断
    if (acc.has_key === true) { return 'ready'; }
    if (acc.has_key === false) { return 'no_key'; }
    return 'none';
  }

  function findAccount(uin) {
    for (var i = 0; i < accounts.length; i++) {
      if (String(accounts[i].uin) === String(uin)) { return accounts[i]; }
    }
    return null;
  }

  function renderAccounts() {
    var sel = $('acctSelect');
    if (!sel) { return; }
    var prev = sel.value || currentUin;
    sel.innerHTML = '';

    if (!accounts.length) {
      var empty = document.createElement('option');
      empty.value = '';
      empty.textContent = '（没有找到 QQ 账号）';
      sel.appendChild(empty);
      sel.disabled = true;
    } else {
      sel.disabled = false;
      for (var i = 0; i < accounts.length; i++) {
        var acc = accounts[i];
        var st = accountStateOf(acc);
        var opt = document.createElement('option');
        opt.value = String(acc.uin);
        opt.setAttribute('data-state', st);
        opt.textContent = String(acc.uin) + ' · ' + (ACCOUNT_STATE_TEXT[st] || '未知');
        sel.appendChild(opt);
      }
    }

    var want = String(prev || '');
    if (want && !findAccount(want)) { want = ''; }
    if (!want && currentUin && findAccount(currentUin)) { want = String(currentUin); }
    if (!want && accounts.length) { want = String(accounts[0].uin); }
    sel.value = want;
    currentUin = want;
    updateAccountBadge();
  }

  function updateAccountBadge() {
    var badge = $('acctBadge');
    var meta = $('acctMeta');
    if (!badge || !meta) { return; }
    var acc = findAccount(currentUin);
    if (!acc) {
      badge.setAttribute('data-state', 'none');
      badge.textContent = accounts.length ? '未选择' : '未找到';
      meta.textContent = accounts.length ? '请选择一个账号'
        : (acctHintText || '没找到 QQ 数据目录（程序会自动全盘搜索一遍）');
      meta.title = meta.textContent;
      $('btnGetKey').disabled = true;
      toggleAcctMore(!accounts.length);
      return;
    }
    toggleAcctMore(false);
    var st = accountStateOf(acc);
    badge.setAttribute('data-state', st);
    badge.textContent = ACCOUNT_STATE_TEXT[st] || '未知';
    $('btnGetKey').disabled = false;

    var parts = [];
    if (acc.db_path) { parts.push(acc.db_path); }
    var size = humanSize(acc.db_size);
    if (size) { parts.push(size); }
    if (acc.db_mtime) { parts.push('更新于 ' + acc.db_mtime); }
    if (acc.key_time) { parts.push('口令 ' + acc.key_time); }
    meta.textContent = parts.join(' · ') || '（没有更多信息）';
    meta.title = parts.join('\n');
  }

  function applyAccounts(list, uin) {
    accounts = Array.isArray(list) ? list.slice() : [];
    if (uin && !currentUin) { currentUin = String(uin); }
    renderAccounts();
    renderKeyBanner();
    renderNextHint();
  }

  /* 老服务端没有 /api/accounts 时，从状态里猜一个账号出来 */
  function guessAccount() {
    var path = state.db_path || '';
    var uin = state.current_uin || '';
    var m = String(path).match(/Tencent Files[\\\/]+(\d+)/i);
    if (!uin && m) { uin = m[1]; }
    if (!uin) { return null; }
    return {
      uin: String(uin),
      db_path: path,
      db_size: 0,
      db_mtime: '',
      has_key: !!state.key_ready,
      key_time: '',
      state: state.key_ready ? 'ready' : 'no_key'
    };
  }

  function refreshAccounts() {
    return api('/api/accounts').then(function (data) {
      serverV2 = true;
      checkOk(data, '读取账号列表失败');
      acctHintText = data.hint || '';
      applyAccounts(data.accounts || [], data.current_uin || '');
    }).catch(function (err) {
      if (isUnknownEndpoint(err)) { serverV2 = false; }
      // 降级：/api/state 里的 accounts
      if (state.accounts && state.accounts.length) {
        applyAccounts(state.accounts, state.current_uin || '');
        return;
      }
      var guess = guessAccount();
      if (guess) {
        applyAccounts([guess], guess.uin);
        return;
      }
      applyAccounts([], state.current_uin || '');
      setStatus('读取账号列表失败：' + err.message, true);
    });
  }

  /* 老服务端不读请求体，发过去会让 keep-alive 连接错乱 → 只在新服务端上传 uin */
  function uinBody(target) {
    if (!target || !serverV2) { return null; }
    return { uin: String(target) };
  }

  /* ============================================================
     找不到 QQ 数据目录时的兜底：全盘扫描 / 手动指定 / 诊断
     ============================================================ */
  function setAcctHint(text) {
    var el = $('acctHint');
    if (el && text) { el.textContent = text; }
  }

  function syncAcctMoreToggle(open) {
    var btn = $('btnAcctMore');
    if (btn) { btn.setAttribute('aria-expanded', open ? 'true' : 'false'); }
  }

  function setAcctMoreAttention(on) {
    var btn = $('btnAcctMore');
    if (!btn) { return; }
    btn.classList.toggle('is-attention', !!on);
  }

  function toggleAcctMore(show) {
    var box = $('acctMore');
    if (!box) { return; }
    box.hidden = !show;
    syncAcctMoreToggle(!!show);
    if (!show) {
      var row = $('acctInputRow');
      if (row) { row.hidden = true; }
    }
  }

  function onAcctMoreToggle() {
    var box = $('acctMore');
    if (!box) { return; }
    toggleAcctMore(box.hidden);
  }

  function submitDataRoot() {
    var input = $('dataRootInput');
    var path = input ? String(input.value || '').trim() : '';
    if (!path) {
      showToast('请先粘贴 Tencent Files 目录路径', 'warn');
      return;
    }
    setStatus('正在读取指定目录…');
    api('/api/accounts/root', { method: 'POST', body: { path: path } })
      .then(function (data) {
        checkOk(data, '读取指定目录失败');
        var list = data.accounts || [];
        applyAccounts(list, data.current_uin || '');
        setAcctHint(data.hint || '');
        toggleAcctMore(!list.length);
        if (list.length) {
          setStatus('已找到 ' + list.length + ' 个账号');
          if (input) { input.value = ''; }
        } else {
          setStatus('这个目录里没有找到 QQ 消息库', true);
        }
      })
      .catch(function (err) {
        setStatus(err.message, true);
        showToast(err.message, 'error');
      });
  }

  function waitDeepScan(tries) {
    if (tries > 150) {
      setStatus('全盘扫描时间过长，已停止等待', true);
      return;
    }
    setTimeout(function () {
      refreshState().then(function () {
        if (state.task === 'deep-scan') {
          var last = (state.progress || []).slice(-1)[0] || '';
          if (last) { setStatus(last); }
          waitDeepScan(tries + 1);
          return;
        }
        refreshAccounts().then(function () {
          var n = accounts.length;
          setStatus(n ? ('全盘扫描完成，共找到 ' + n + ' 个账号')
                      : '全盘扫描完成，但没有找到 QQ 消息库', !n);
        });
      });
    }, 1200);
  }

  function startDeepScan() {
    setStatus('正在全盘扫描 QQ 数据目录（最多约 2 分钟）…');
    api('/api/accounts/deep', { method: 'POST', body: {} })
      .then(function (data) {
        if (data && data.error) {
          setStatus(data.error, true);
          return;
        }
        waitDeepScan(0);
      })
      .catch(function (err) {
        setStatus(err.message, true);
        showToast(err.message, 'error');
      });
  }

  function showDiagnose() {
    setStatus('正在生成诊断报告…');
    api('/api/diagnose').then(function (data) {
      if (!data || !data.ok) {
        setStatus((data && data.error) || '诊断失败', true);
        return;
      }
      var text = data.text || '';
      if (window.console && console.log) { console.log(text); }
      var done = function () {
        showToast('诊断报告已复制，可直接粘贴给开发者（也存到了运行目录的 diagnose.txt）', 'ok');
        setStatus('诊断报告已生成');
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {
          showToast('诊断报告已生成，见运行目录的 diagnose.txt', 'ok');
          setStatus('诊断报告已生成');
        });
      } else {
        showToast('诊断报告已生成，见运行目录的 diagnose.txt', 'ok');
        setStatus('诊断报告已生成');
      }
    }).catch(function (err) {
      setStatus(err.message, true);
    });
  }

  function onAccountChange() {
    var sel = $('acctSelect');
    var uin = sel ? (sel.value || '') : '';
    if (!uin) { return; }
    currentUin = uin;
    updateAccountBadge();
    clearChats();
    doLoad(uin);
  }

  function clearChats() {
    currentChats = [];
    filteredChats = [];
    currentChat = null;
    $('chatTitle').textContent = '未选择会话';
    $('chatSub').textContent = '从左侧列表中选择一个会话，即可查看聊天记录';
    $('headAvatar').textContent = '—';
    $('btnExportCurrent').disabled = true;
    $('msgScroll').innerHTML = '<div class="placeholder big"><div class="ph-icon">🗨</div><div>正在读取…</div></div>';
    renderChats();
  }

  /* ============================================================
     状态栏 / 阶段条
     ============================================================ */
  var STAGE_TEXT = {
    idle: '空闲',
    scanning: '处理中',
    need_key: '等待口令',
    loading: '加载中',
    ready: '就绪',
    error: '出错'
  };

  function applyState(st) {
    if (!st || typeof st !== 'object') { return; }
    // 合并（服务端可能只回一部分字段）
    for (var key in st) {
      if (!Object.prototype.hasOwnProperty.call(st, key)) { continue; }
      state[key] = st[key];
    }

    // 标题栏版本号
    var ver = $('appVer');
    if (ver) { ver.textContent = 'v' + (state.app_version || '1.0'); }

    // 标题栏 QQ 信息
    var qq = state.qq || {};
    var info = $('qqInfo');
    if (info) {
      if (qq.installed) {
        info.textContent = 'QQ ' + (qq.version || '?') + ' · ' + (qq.install_dir || '');
        info.title = qq.install_dir || '';
      } else if (state.db_path) {
        info.textContent = '未找到 QQ 安装（仍可直接加载消息库）';
        info.title = state.db_path;
      } else {
        info.textContent = '未扫描';
        info.title = '';
      }
    }

    // 阶段徽章
    var badge = $('stageBadge');
    if (badge) {
      badge.className = 'stage-badge';
      if (state.stage === 'ready') { badge.classList.add('ready'); }
      else if (state.stage === 'error') { badge.classList.add('error'); }
      else if (state.task) { badge.classList.add('busy'); }
      badge.textContent = STAGE_TEXT[state.stage] || state.stage || '空闲';
    }

    // 阶段条
    var dot = $('stageDot');
    if (dot) {
      dot.className = 'dot' + (state.task ? ' busy'
        : (state.stage === 'ready' ? ' ready'
          : (state.stage === 'error' ? ' error'
            : (state.stage === 'need_key' ? ' need_key' : ''))));
    }
    var text = $('stageText');
    if (text) {
      if (state.task === 'auto-key') { text.textContent = '正在自动抓取数据库口令…'; }
      else if (state.task === 'scan') { text.textContent = '正在扫描 QQ 安装与消息库…'; }
      else if (state.task === 'load') { text.textContent = '正在解密并读取聊天记录…'; }
      else if (state.stage === 'need_key') { text.textContent = state.key_hint || '需要数据库口令'; }
      else if (state.stage === 'ready') {
        text.textContent = '已加载 ' + (state.chats || 0) + ' 个会话 / ' + (state.messages || 0) + ' 条消息';
      }
      else if (state.stage === 'error') { text.textContent = state.error || '发生错误'; }
      else { text.textContent = '就绪'; }
    }

    var progText = $('stageProgress');
    if (progText) {
      var logs = state.progress || [];
      progText.textContent = logs.length ? logs[logs.length - 1] : '';
    }

    // 状态栏
    var dbEl = $('statusDb');
    if (dbEl) { dbEl.textContent = '数据库：' + (state.db_path || '—'); }
    var countEl = $('statusCount');
    if (countEl) {
      countEl.textContent = '会话 ' + (state.chats || 0) + ' · 消息 ' + (state.messages || 0);
    }

    // 按钮可用性
    $('btnLoad').disabled = !!state.task || (!state.db_path && !currentUin);
    $('btnAutoKey').disabled = !!state.task;
    $('btnExport').disabled = !state.chats;

    // 顶部步骤引导条
    renderSteps();
    // 「下一步」常驻提示条
    renderNextHint();

    if (state.error && state.stage === 'error') {
      setStatus(state.error, true);
    } else if (state.task) {
      setStatus('处理中…', false);
    } else if (state.stage === 'ready') {
      setStatus('聊天记录已加载', false);
    }

    // 服务端任务 → 悬浮窗
    var task = state.task || '';
    if (task && task !== prog.serverTask) {
      prog.serverTask = task;
      prog.serverStartedAt = Date.now();
    } else if (!task) {
      prog.serverTask = '';
      prog.serverStartedAt = 0;
    }
    renderProgress();

    // 账号列表（服务端在 /api/state 里也带了 accounts）
    if (!accounts.length && Array.isArray(state.accounts) && state.accounts.length) {
      applyAccounts(state.accounts, state.current_uin || '');
    }

    // 缺密钥时的醒目横幅（不再自动弹手动输入框）
    renderKeyBanner();
  }

  /* ---------------------------------------------------------
     缺密钥时的醒目横幅
     --------------------------------------------------------- */
  function qqLoggedIn() {
    // 后端给了 qq.running 就用它；老服务端没有这个字段时退回「有库路径就算登录过」
    var q = state && state.qq;
    if (q && typeof q.running === 'boolean') { return q.running; }
    return !!(state && state.db_path);
  }

  function needKeyNow() {
    if (!state) { return false; }
    if (state.key_ready) { return false; }
    if (state.stage === 'error') { return false; }
    if (state.stage === 'need_key') { return true; }
    // 只针对当前选中的账号：别的账号缺密钥不该一直亮着横幅
    var uin = currentUin || state.current_uin || '';
    var accts = accounts || [];
    if (!uin && accts.length) { uin = String(accts[0].uin || ''); }
    for (var i = 0; i < accts.length; i++) {
      var a = accts[i];
      if (!a) { continue; }
      if (uin && String(a.uin) !== String(uin)) { continue; }
      if (a.state && a.state !== 'ready') { return true; }
    }
    return false;
  }

  function renderKeyBanner() {
    var bar = $('keyBanner');
    if (!bar) { return; }
    var show = needKeyNow();
    bar.hidden = !show;
    var btn = $('btnAutoKey');
    if (btn) {
      btn.classList.toggle('is-attention', show);
    }
    if (!show) { return; }

    var logged = qqLoggedIn();
    var title = $('kbTitle');
    var desc = $('kbDesc');
    if (title) {
      title.textContent = logged
        ? '检测到 QQ 已登录，但还没有数据库密钥'
        : '还没有数据库密钥';
    }
    if (desc) {
      desc.textContent = logged
        ? '点右侧「识别密钥」：程序会先自动退出 QQ，再以调试方式重新启动并登录，在它打开数据库的瞬间截获密钥，成功后 QQ 会自动恢复。整个过程约 10 秒，期间请不要手动操作 QQ。'
        : '还没检测到 QQ 在运行。请先启动并登录 QQ，然后点「扫描 QQ」和右侧「识别密钥」。识别时程序会自动退出并重新登录 QQ 来截获密钥。';
    }
    var kb = $('btnKeyBanner');
    if (kb) {
      kb.disabled = !!(state && state.task);
    }

    // 第一次检测到「QQ 已登录但没有密钥」时，自动弹一次引导框说明流程
    if (!needKeyAutoShown && logged && !(state && state.task)
        && $('needKeyModal').hidden && $('helpModal') && $('helpModal').hidden) {
      needKeyAutoShown = true;
      var uin0 = currentUin || state.current_uin || '';
      var reason0 = 'no_key';
      var accts0 = accounts || [];
      for (var k = 0; k < accts0.length; k++) {
        if (accts0[k] && accts0[k].uin === uin0 && accts0[k].state === 'stale') {
          reason0 = 'stale_key';
          break;
        }
      }
      openNeedKeyModal(uin0, reason0, '');
    }
  }

  /* ---------------------------------------------------------
     顶部步骤引导条
     --------------------------------------------------------- */
  function computeSteps() {
    var qqOk = !!(state.qq && state.qq.installed) || !!state.db_path;
    var keyOk = !!state.key_ready;
    var loaded = (state.chats || 0) > 0;
    var exported = false;
    try { exported = Object.keys(state.last_export || {}).length > 0; } catch (e) { exported = false; }
    var task = state.task || '';
    return [
      { id: 1, done: qqOk,     busy: task === 'scan' },
      { id: 2, done: keyOk,    busy: task === 'auto-key' },
      { id: 3, done: loaded,   busy: task === 'load' },
      { id: 4, done: exported, busy: false }
    ];
  }

  function renderSteps() {
    var steps = computeSteps();
    var cur = 0;
    var i;
    for (i = 0; i < steps.length; i++) {
      if (!steps[i].done) { cur = steps[i].id; break; }
    }
    for (i = 0; i < steps.length; i++) {
      if (steps[i].busy) { cur = steps[i].id; }
    }
    for (i = 0; i < steps.length; i++) {
      var el = $('step' + steps[i].id);
      if (!el) { continue; }
      var cls = 'step';
      if (steps[i].done) { cls += ' is-done'; }
      if (steps[i].id === cur) {
        cls += ' is-current';
        if (state.stage === 'error') { cls += ' is-error'; }
      }
      el.className = cls;
      var no = el.querySelector('.step-no');
      if (no) { no.textContent = steps[i].done ? '\u2713' : String(steps[i].id); }
      el.title = steps[i].done ? '这一步已完成，点击可以再做一次' : '点击直接执行这一步';
    }
  }

  function onStepClick(n) {
    if (state.task) {
      setStatus('正在处理中，请稍候…', true);
      return;
    }
    if (n === 1) { doScan(); return; }
    if (n === 2) { doGetKey(); return; }
    if (n === 3) { doLoad(currentUin || state.current_uin || ''); return; }
    if (n === 4) {
      if (!state.chats) {
        setStatus('还没有加载聊天记录，请先完成第 3 步', true);
        return;
      }
      openExportModal('all');
    }
  }

  /* ---------------------------------------------------------
     「下一步」常驻提示条 + 目标按钮呼吸高亮 + 指向上方的箭头
     --------------------------------------------------------- */
  var NEXT_TARGETS = ['btnScan', 'btnAutoKey', 'btnLoad', 'btnExport',
                      'btnGetKey', 'acctSelect', 'btnAcctMore'];
  /* 用户手动关掉提示条时记住是哪一个步骤，同一步不再打扰 */
  var nextHintDismissedId = '';

  function clearNextHighlight() {
    var i;
    for (i = 0; i < NEXT_TARGETS.length; i++) {
      var el = $(NEXT_TARGETS[i]);
      if (el) { el.classList.remove('is-next'); }
    }
    var arrow = $('nextArrow');
    if (arrow) { arrow.hidden = true; }
  }

  function pickNextAccount() {
    var uin = currentUin || (state && state.current_uin) || '';
    var acc = uin ? findAccount(uin) : null;
    if (!acc && accounts && accounts.length) {
      uin = String(accounts[0].uin || '');
      acc = accounts[0];
    }
    return { uin: uin, acc: acc };
  }

  /* 算出「现在最该点哪儿」。返回 {id,text,go,busy,kind}
     kind:  step = 正常下一步；busy = 正在跑；warn = 需要用户处理 */
  function computeNextHint() {
    var task = (state && state.task) || '';
    var qqOk = !!(state && state.qq && state.qq.installed) || !!(state && state.db_path);

    if (task) {
      var nameMap = {
        scan: '正在查找 QQ 与消息库',
        'auto-key': '正在识别数据库密钥（QQ 会被自动重启一次）',
        load: '正在读取聊天记录',
        export: '正在导出文件',
        'deep-scan': '正在全盘搜索 QQ 数据目录（界面可以照常使用）'
      };
      var ptext = (state && state.progress_text) || '';
      return { id: 'busy:' + task, kind: 'busy',
               text: (nameMap[task] || '正在处理') + (ptext ? ' · ' + ptext : ''),
               go: null };
    }
    if (!qqOk) {
      return { id: 'noqq', kind: 'warn',
               text: '还没有找到本机 QQ。请先安装并登录 QQ，然后点「扫描 QQ」重试。',
               go: function () { doScan(); } };
    }

    var accts = accounts || [];
    if (!accts.length) {
      return { id: 'nodata', kind: 'warn',
               text: '没找到 QQ 数据目录。程序会自动全盘搜一遍；也可以点「扫描 QQ」重试，或展开左侧「找不到？点这里」手动指定。',
               go: function () { doScan(); } };
    }

    var picked = pickNextAccount();
    var acc = picked.acc;
    if (!picked.uin || !acc) {
      return { id: 'pickaccount', kind: 'step', target: 'acctSelect',
               text: '请在左侧「账号」里选一个要导出的 QQ 号。',
               go: null };
    }

    var st = (acc.state || (state.key_ready ? 'ready' : 'no_key'));
    if (st !== 'ready' && !(state.key_ready && String(picked.uin) === String(state.current_uin || ''))) {
      return { id: 'needkey:' + picked.uin, kind: 'warn', target: 'btnAutoKey',
               text: '账号 ' + picked.uin + ' 还没有数据库密钥。点「获取密钥」，程序会自动退出并重新登录 QQ 来截获。',
               go: function () { doGetKey(); } };
    }

    if (!(state.chats > 0)) {
      return { id: 'load:' + picked.uin, kind: 'step', target: 'btnLoad',
               text: '密钥已就绪。点「加载聊天记录」，开始读取 ' + picked.uin + ' 的会话与消息。',
               go: function () { doLoad(picked.uin); } };
    }

    var exported = false;
    try { exported = Object.keys(state.last_export || {}).length > 0; } catch (e) { exported = false; }
    if (!exported) {
      return { id: 'export:' + picked.uin, kind: 'step', target: 'btnExport',
               text: '已经读到 ' + state.chats + ' 个会话。勾选要导出的会话后点「导出」。',
               go: function () {
                 if (currentChat) { openExportModal('current'); }
                 else { openExportModal('all'); }
               } };
    }
    return { id: 'done', kind: 'step', target: null,
             text: '导出完成。可以继续挑别的会话导出，或者切换账号再导一份。',
             go: null };
  }

  function placeNextArrow(target) {
    var arrow = $('nextArrow');
    if (!arrow) { return; }
    var el = target ? $(target) : null;
    if (!el) { arrow.hidden = true; return; }
    var r = el.getBoundingClientRect();
    if (!r.width || !r.height) { arrow.hidden = true; return; }
    arrow.hidden = false;
    var w = arrow.offsetWidth || 22;
    var left = r.left + (r.width - w) / 2;
    left = Math.max(4, Math.min(left, (window.innerWidth || 1200) - w - 4));
    arrow.style.left = left + 'px';
    arrow.style.top = (r.bottom + 4) + 'px';
  }

  function anyModalOpen() {
    var ids = ['keyModal', 'needKeyModal', 'exportModal', 'helpModal'];
    for (var i = 0; i < ids.length; i++) {
      var el = $(ids[i]);
      if (el && !el.hidden) { return true; }
    }
    return false;
  }

  function renderNextHint() {
    var bar = $('nextHint');
    if (!bar || !state) { return; }
    var info = computeNextHint();
    clearNextHighlight();

    if (info.id === nextHintDismissedId || info.id === 'done') {
      bar.hidden = true;
      setAcctMoreAttention(false);
      return;
    }
    bar.hidden = false;
    bar.className = 'next-hint' + (info.kind === 'busy' ? ' is-busy' : '');

    var stepEl = $('nhStep');
    if (stepEl) {
      stepEl.textContent = info.kind === 'busy' ? '进行中'
        : (info.kind === 'warn' ? '需要处理' : '下一步');
    }
    var textEl = $('nhText');
    if (textEl) { textEl.textContent = info.text; }

    var goBtn = $('nhGo');
    if (goBtn) {
      goBtn.hidden = !info.go;
      goBtn.disabled = !!(state && state.task);
      goBtn.textContent = info.kind === 'warn' ? '立即处理' : '去操作';
    }
    var closeBtn = $('nhClose');
    if (closeBtn) { closeBtn.hidden = false; }

    if (info.target && !anyModalOpen()) {
      var tel = $(info.target);
      if (tel) { tel.classList.add('is-next'); }
      placeNextArrow(info.target);
    }
    /* 「找不到？点这里」在没账号时也闪一下，提示还有兜底入口 */
    setAcctMoreAttention(!(accounts || []).length && info.kind === 'warn');
  }

  function dismissNextHint() {
    var info = state ? computeNextHint() : null;
    nextHintDismissedId = info ? info.id : '';
    var bar = $('nextHint');
    if (bar) { bar.hidden = true; }
    clearNextHighlight();
  }


  function openHelp() {
    var m = $('helpModal');
    if (m) { m.hidden = false; }
  }

  function closeHelp() {
    var m = $('helpModal');
    if (m) { m.hidden = true; }
  }

  function refreshState() {
    return api('/api/state').then(function (st) {
      pollFails = 0;
      applyState(st);
      return st;
    }).catch(function (err) {
      setStatus('无法连接本地服务：' + err.message, true);
    });
  }

  function pollState() {
    api('/api/state').then(function (st) {
      pollFails = 0;
      applyState(st);
    }).catch(function (err) {
      pollFails += 1;
      if (pollFails === 1) {
        setStatus('无法连接本地服务：' + err.message, true);
        showToast('无法连接本地服务：' + err.message, 'error');
      }
    });
  }

  /* ---------------------------------------------------------
     会话列表
     --------------------------------------------------------- */
  function avatarFor(chat) {
    var name = (chat.name || chat.chat_id || '?').trim();
    var first = name.charAt(0) || '?';
    var cls = 'avatar' + (chat.is_group ? ' avatar-group' : '');
    return '<div class="' + cls + '">' + esc(first) + '</div>';
  }

  function renderChats() {
    var box = $('chatList');
    if (!box) { return; }
    if (!currentChats.length) {
      box.innerHTML = '<div class="placeholder">还没有数据<br><small>点击上方「扫描 QQ」→「加载聊天记录」</small></div>';
      $('sideFoot').textContent = '共 0 个会话';
      return;
    }
    var html = '';
    for (var i = 0; i < filteredChats.length; i++) {
      var c = filteredChats[i];
      var active = (currentChat && currentChat.chat_id === c.chat_id &&
                    !!currentChat.is_group === !!c.is_group) ? ' active' : '';
      html += '<div class="chat-item' + active + '" data-chat-id="' + esc(c.chat_id) +
        '" data-chat-group="' + (c.is_group ? '1' : '0') + '">' +
        avatarFor(c) +
        '<div class="ci-main">' +
          '<div class="ci-row1">' +
            '<span class="ci-name">' + esc(c.name || c.chat_id) + '</span>' +
            '<span class="ci-time">' + esc(shortTime(c.last_time_text)) + '</span>' +
          '</div>' +
          '<div class="ci-row2">' +
            '<span class="ci-preview">' + esc(c.preview || '（无消息）') + '</span>' +
            '<span class="ci-count">' + (c.count || 0) + '</span>' +
          '</div>' +
        '</div>' +
      '</div>';
    }
    if (!filteredChats.length) {
      box.innerHTML = '<div class="placeholder">没有匹配的会话</div>';
    } else {
      box.innerHTML = html;
    }
    $('sideFoot').textContent = '共 ' + filteredChats.length + ' / ' + currentChats.length + ' 个会话';
  }

  function applyFilter(keyword) {
    var kw = (keyword || '').trim().toLowerCase();
    if (!kw) {
      filteredChats = currentChats.slice();
    } else {
      filteredChats = currentChats.filter(function (c) {
        var hay = ((c.name || '') + ' ' + (c.chat_id || '') + ' ' + (c.peer || '')).toLowerCase();
        return hay.indexOf(kw) >= 0;
      });
    }
    renderChats();
  }

  /* ---------------------------------------------------------
     消息渲染
     --------------------------------------------------------- */
  var ATTACH_ICON = { image: '🖼', video: '🎬', audio: '🎤', file: '📎', sticker: '😊', unknown: '📎' };

  function isSystem(msg) {
    return msg.msg_type === 17 || msg.direction === 3;
  }

  function renderAttach(a) {
    var kind = a.kind || 'file';
    var icon = ATTACH_ICON[kind] || ATTACH_ICON.file;
    var size = humanSize(a.size);
    var meta = [];
    if (size) { meta.push(size); }
    if (a.local_path) { meta.push(a.local_path); }
    return '<div class="attach-card">' +
      '<div class="attach-icon ' + esc(kind) + '">' + icon + '</div>' +
      '<div class="attach-info">' +
        '<div class="attach-name" title="' + esc(a.name) + '">' + esc(a.name || '未命名文件') + '</div>' +
        '<div class="attach-meta" title="' + esc(a.local_path || '') + '">' + esc(meta.join(' · ')) + '</div>' +
      '</div>' +
    '</div>';
  }

  function renderMessage(msg) {
    if (isSystem(msg)) {
      var sysText = (msg.content || msg.text || msg.type_name || '系统消息');
      return '<div class="msg-row"><div class="msg-col" style="max-width:100%">' +
        '<div class="bubble sys">' + esc(sysText) + '</div></div></div>';
    }
    var mine = !!msg.is_mine;
    var body = esc(msg.content || msg.text || '');
    var atts = msg.attachments || [];
    var attachHtml = '';
    for (var i = 0; i < atts.length; i++) { attachHtml += renderAttach(atts[i]); }
    if (!body && !attachHtml) { body = '[' + esc(msg.type_name || '消息') + ']'; }
    return '<div class="msg-row' + (mine ? ' mine' : '') + '">' +
      '<div class="msg-col">' +
        '<div class="msg-meta">' +
          '<span class="msg-sender">' + esc(msg.sender || (mine ? '我' : '对方')) + '</span>' +
          '<span class="msg-time">' + esc(msg.time_text || '') + '</span>' +
        '</div>' +
        '<div class="bubble">' + body + attachHtml + '</div>' +
      '</div>' +
    '</div>';
  }

  function renderChat(chat) {
    currentChat = chat;
    var shown = chat.messages ? chat.messages.length : 0;
    var total = parseInt(chat.total_count, 10) || shown;
    // 大会话服务端只载入最近 3000 条，这里明确告诉用户「不是全部」
    var countText = (chat.truncated && total > shown)
      ? ('最近 ' + shown + ' / 共 ' + total + ' 条消息')
      : (shown + ' 条消息');
    var av = $('headAvatar');
    av.className = 'avatar avatar-lg' + (chat.is_group ? ' avatar-group' : '');
    av.textContent = (chat.name || chat.chat_id || '?').trim().charAt(0) || '?';
    $('chatTitle').textContent = chat.name || chat.chat_id;
    var span = '';
    if (chat.first_time && chat.last_time) {
      span = ' · ' + tsText(chat.first_time) + ' ~ ' + tsText(chat.last_time);
    }
    $('chatSub').textContent = (chat.is_group ? '群聊 ' : '单聊 ') + chat.chat_id +
      ' · ' + countText + span;
    $('btnExportCurrent').disabled = false;

    var msgs = chat.messages || [];
    if (!msgs.length) {
      $('msgScroll').innerHTML = '<div class="placeholder big"><div class="ph-icon">🗨</div><div>这个会话没有消息</div></div>';
    } else {
      var html = '';
      for (var i = 0; i < msgs.length; i++) { html += renderMessage(msgs[i]); }
      $('msgScroll').innerHTML = html;
      $('msgScroll').scrollTop = $('msgScroll').scrollHeight;
    }
    renderChats();
  }

  var chatReqSeq = 0;   // 打开会话的请求序号：慢响应不能覆盖用户后来选的会话
  function openChat(chatId, isGroup) {
    var seq = ++chatReqSeq;
    setStatus('正在读取会话…');
    var url = '/api/chats/' + encodeURIComponent(chatId);
    if (typeof isGroup === 'boolean') {
      url += '?group=' + (isGroup ? '1' : '0');
    }
    api(url).then(function (data) {
      if (seq !== chatReqSeq) { return; }   // 用户已经点了别的会话
      if (!data || !data.chat) { throw new Error('会话不存在'); }
      renderChat(data.chat);
      setStatus('已打开会话：' + (data.chat.name || data.chat.chat_id));
    }).catch(function (err) {
      if (seq !== chatReqSeq) { return; }
      reportError(err);
    });
  }

  /* ---------------------------------------------------------
     动作
     --------------------------------------------------------- */
  function doScan() {
    setStatus('正在扫描…');
    startTask('正在扫描 QQ');
    api('/api/scan', { method: 'POST' }).then(function (st) {
      applyState(st);
      setStatus(st && st.db_path ? '扫描完成：' + st.db_path : '扫描完成，但没找到消息库');
      refreshAccounts();
    }).catch(function (err) {
      reportError(err);
    }).then(function () {
      endTask();
      refreshState();
    });
  }

  /* 抓取口令：新接口 /api/key/auto，老服务端降级到 /api/auto-key */
  function apiAutoKey(body) {
    if (serverV2) {
      return api('/api/key/auto', { method: 'POST', body: body }).catch(function (err) {
        if (isUnknownEndpoint(err)) {
          return api('/api/auto-key', { method: 'POST' });
        }
        throw err;
      });
    }
    // 还没确认服务端版本：先试老接口（不带 body —— 老服务端读不掉请求体）
    return api('/api/auto-key', { method: 'POST' }).catch(function (err) {
      if (isUnknownEndpoint(err)) {
        serverV2 = true;
        return api('/api/key/auto', { method: 'POST', body: body });
      }
      throw err;
    });
  }

  /* 结束 QQ 进程（老服务端没有这个接口时忽略） */
  function closeQQ() {
    return api('/api/qq/close', { method: 'POST' }).catch(function () {
      return null;   // 关不掉不致命，后面的抓取会自己处理
    });
  }

  /* 等服务端把后台任务跑完（老接口是异步的，立即返回状态） */
  function waitForServerIdle(limitMs) {
    var waited = 0;
    var limit = limitMs || 180000;
    function step() {
      return api('/api/state').then(function (st) {
        applyState(st);
        var busy = !!(st && st.task) || (st && (st.stage === 'scanning' || st.stage === 'loading'));
        if (!busy || waited >= limit) { return st; }
        waited += 800;
        return new Promise(function (resolve) { setTimeout(resolve, 800); }).then(step);
      });
    }
    return step();
  }

  /* 自动抓取口令：先关 QQ，再抓口令，成功后刷新账号状态并重新读取 */
  function runAutoKey(uin) {
    var target = uin || currentUin || state.current_uin || '';
    var body = uinBody(target);
    var captured = null;          // 接口返回体（可能是老服务端返回的完整 state）
    setStatus('正在自动获取数据库口令…');
    startTask('正在自动获取数据库口令');
    return closeQQ()
      .then(function () { return apiAutoKey(body); })
      .then(function (data) {
        captured = data;
        checkOk(data, '自动获取口令失败');
        applyState(data);
        return waitForServerIdle();
      })
      .then(function (st) {
        // 只有服务端真的拿到了口令才算成功：抓取失败时 stage 可能是
        // need_key/error，不能继续提示「口令已获取」并强行去读库。
        if (!st || st.error) {
          throw new Error((st && st.error) || '自动获取口令失败');
        }
        if (!st.key_ready) {
          throw new Error('没有获取到数据库口令，请重试，或手动填写口令');
        }
        if (captured && captured.accounts) {
          applyAccounts(captured.accounts, captured.uin || target);
        }
        setStatus('口令已获取');
        showToast('口令已获取', 'ok');
        return refreshAccounts().then(function () {
          return doLoad(target);
        });
      })
      .catch(function (err) {
        reportError(err);
      })
      .then(function () {
        endTask();
      });
  }

  function doAutoKey() {
    openNeedKeyModal(currentUin || state.current_uin || '', '');
  }

  function doGetKey() {
    var uin = currentUin || state.current_uin || '';
    if (!uin) {
      showToast('请先选择一个账号', 'warn');
      return;
    }
    openNeedKeyModal(uin, '');
  }

  function doLoad(uin) {
    var target = uin || currentUin || state.current_uin || '';
    // 老服务端不读请求体，带 body 会把 keep-alive 连接弄乱 → uinBody 在老服务端返回 null
    var body = uinBody(target);
    loadingUin = target;
    setStatus('正在加载聊天记录…');
    startTask('正在读取聊天记录');
    return api('/api/load', { method: 'POST', body: body }).then(function (data) {
      if (data && data.need_key) {
        handleNeedKey(data);
        return null;
      }
      checkOk(data, '读取聊天记录失败');
      applyState(data);
      return api('/api/chats').then(function (cdata) {
        currentChats = (cdata && cdata.chats) ? cdata.chats : [];
        currentChat = null;
        chatReqSeq++;   // 作废切换账号前发出的「打开会话」请求
        applyFilter($('searchInput').value);
        setStatus('已加载 ' + currentChats.length + ' 个会话');
      });
    }).catch(function (err) {
      reportError(err);
    }).then(function () {
      loadingUin = '';
      endTask();
      renderAccounts();
      refreshState();
    });
  }

  function doExport(scope) {
    var fmt = $('exportFormat').value || 'html';
    var dir = $('exportDir').value || state.out_dir || 'D:\\QQ导出';
    var limit = parseInt(($('exportLimit') && $('exportLimit').value) || '0', 10);
    if (isNaN(limit) || limit < 0) { limit = 0; }
    var body = { format: fmt, out_dir: dir, chat_ids: [], limit_per_chat: limit };
    if (scope === 'current' && currentChat) {
      body.chat_ids = [{ id: currentChat.chat_id, group: !!currentChat.is_group }];
    }
    $('exportOk').disabled = true;
    startTask('正在导出聊天记录');
    api('/api/export', { method: 'POST', body: body }).then(function (res) {
      closeExportModal();
      var msg = (res && res.ok) ? ('导出完成：' + (res.path || ''))
        : ('导出失败：' + ((res && res.error) || '未知错误'));
      setStatus(msg, !(res && res.ok));
      showToast(msg, (res && res.ok) ? 'ok' : 'error');
      refreshState();
    }).catch(function (err) {
      var box = $('exportError');
      if (box) {
        box.hidden = false;
        box.textContent = err.message;
      }
      reportError(err);
    }).then(function () {
      $('exportOk').disabled = false;
      endTask();
    });
  }

  /* ---------------------------------------------------------
     口令失效 / 缺少口令 的引导
     --------------------------------------------------------- */
  function handleNeedKey(data) {
    openNeedKeyModal(
      data.uin || currentUin || '',
      data.reason || 'no_key',
      data.error || ''
    );
    setStatus('需要识别数据库密钥', true);
  }

  /* 打开「识别密钥」引导框：说明会自动退出并重新登录 QQ */
  function openNeedKeyModal(uin, reason, error) {
    var who = uin ? String(uin) : (currentUin || '');
    var title;
    var desc;
    if (reason === 'stale_key') {
      title = '数据库密钥已失效';
      desc = '账号 ' + (who || '（当前账号）') + ' 的数据库密钥已失效（通常是换了 QQ 号或改过密码）。'
        + '重新识别会退出正在运行的 QQ 并重新登录它。';
    } else {
      title = '需要识别数据库密钥';
      desc = '账号 ' + (who || '（当前账号）') + ' 还没有数据库密钥，识别一次之后就能读取聊天记录了。';
    }
    needKeyUin = who ? String(who) : '';
    $('needKeyTitle').textContent = title;
    $('needKeyDesc').textContent = desc;
    var errBox = $('needKeyError');
    if (errBox) {
      if (error) {
        errBox.hidden = false;
        errBox.textContent = error;
      } else {
        errBox.hidden = true;
        errBox.textContent = '';
      }
    }
    $('needKeyModal').hidden = false;
    // 引导框已经弹出 → 别再让轮询重复弹出
    needKeyAutoShown = true;
    $('keyModal').setAttribute('data-dismissed', '1');
    renderProgress();
  }

  function closeNeedKeyModal() {
    $('needKeyModal').hidden = true;
    renderProgress();
  }

  /* ---------------------------------------------------------
     模态框
     --------------------------------------------------------- */
  function openKeyModal() {
    $('keyModal').hidden = false;
    $('keyError').hidden = true;
    renderProgress();
    var desc = $('keyModalDesc');
    if (desc) {
      var hint = state && state.key_hint;
      var who = currentUin || state.current_uin || '';
      desc.textContent = (hint ? hint : '请输入消息库口令（16 或 20 个可打印字符）。若无法自动提取，可从 QQ 进程内存中手动获取后粘贴。')
        + (who ? ('（当前账号：' + who + '）') : '');
    }
    $('keyInput').focus();
  }
  function closeKeyModal() {
    $('keyModal').hidden = true;
    $('keyModal').setAttribute('data-dismissed', '1');
    renderProgress();
  }
  function submitKey() {
    var key = $('keyInput').value || '';
    var errBox = $('keyError');
    if (key.length !== 16 && key.length !== 20) {
      errBox.hidden = false;
      errBox.textContent = '口令长度应为 16 或 20 个字符，当前为 ' + key.length + ' 个';
      return;
    }
    var body = { key: key };
    var uin = currentUin || state.current_uin || '';
    if (uin) { body.uin = String(uin); }
    api('/api/key', { method: 'POST', body: body }).then(function (data) {
      checkOk(data, '口令保存失败');
      closeKeyModal();
      applyState(data);
      setStatus('口令已保存');
      showToast('口令已保存', 'ok');
      return refreshAccounts().then(function () {
        if (uin) { return doLoad(uin); }
        return null;
      });
    }).catch(function (err) {
      errBox.hidden = false;
      errBox.textContent = err.message;
    });
  }

  function openExportModal(scope) {
    $('exportError').hidden = true;
    if (!$('exportDir').value) { $('exportDir').value = state.out_dir || 'D:\\QQ导出'; }
    var radios = document.getElementsByName('exportScope');
    for (var i = 0; i < radios.length; i++) {
      radios[i].checked = (radios[i].value === (scope || 'all'));
    }
    $('exportModal').hidden = false;
    renderProgress();
    loadFormats();
  }
  function closeExportModal() {
    $('exportModal').hidden = true;
    renderProgress();
  }

  function loadFormats() {
    var sel = $('exportFormat');
    if (sel.options.length > 0) { return; }
    api('/api/formats').then(function (data) {
      var list = (data && data.formats) ? data.formats : [];
      if (!list.length) {
        // writers 模块还没就绪：先给一份常见格式占位
        list = ['html', 'csv', 'txt', 'json'].map(function (f) { return { id: f, name: f, ext: '' }; });
      }
      sel.innerHTML = '';
      list.forEach(function (f) {
        var opt = document.createElement('option');
        opt.value = f.id;
        opt.textContent = f.name + (f.ext ? '（' + f.ext + '）' : '');
        sel.appendChild(opt);
      });
    }).catch(function (err) {
      showToast('读取导出格式失败：' + err.message, 'error');
    });
  }

  /* ---------------------------------------------------------
     事件绑定
     --------------------------------------------------------- */
  function bind() {
    $('btnScan').addEventListener('click', doScan);
    $('btnAutoKey').addEventListener('click', doAutoKey);
    $('btnLoad').addEventListener('click', function () { doLoad(currentUin || state.current_uin || ''); });
    $('btnExport').addEventListener('click', function () { openExportModal('all'); });
    $('btnExportCurrent').addEventListener('click', function () { openExportModal('current'); });

    // 步骤引导条：点哪一步就执行哪一步
    for (var si = 1; si <= 4; si++) {
      (function (n) {
        var el = $('step' + n);
        if (el) { el.addEventListener('click', function () { onStepClick(n); }); }
      })(si);
    }

    // 使用帮助面板
    if ($('btnHelp')) { $('btnHelp').addEventListener('click', openHelp); }
    if ($('helpClose')) { $('helpClose').addEventListener('click', closeHelp); }
    if ($('helpOk')) { $('helpOk').addEventListener('click', closeHelp); }
    if ($('helpModal')) {
      $('helpModal').addEventListener('click', function (e) {
        if (e.target === $('helpModal')) { closeHelp(); }
      });
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && $('helpModal') && !$('helpModal').hidden) { closeHelp(); }
    });

    // 隐私说明：打开帮助面板并定位到「隐私与数据安全」
    if ($('btnPrivacy')) {
      $('btnPrivacy').addEventListener('click', function () {
        openHelp();
        var sec = $('privacySec');
        if (sec && sec.scrollIntoView) { sec.scrollIntoView({ block: 'start' }); }
      });
    }

    // 账号选择器
    $('acctSelect').addEventListener('change', onAccountChange);
    $('btnGetKey').addEventListener('click', doGetKey);
    if ($('btnAcctMore')) { $('btnAcctMore').addEventListener('click', onAcctMoreToggle); }

    // 「下一步」提示条：去操作 / 先不提示
    if ($('nhGo')) {
      $('nhGo').addEventListener('click', function () {
        var info = state ? computeNextHint() : null;
        if (info && info.go) { info.go(); }
      });
    }
    if ($('nhClose')) { $('nhClose').addEventListener('click', dismissNextHint); }
    window.addEventListener('resize', function () {
      var info = state ? computeNextHint() : null;
      if (info && info.target) { placeNextArrow(info.target); }
    });

    $('btnRefreshAccounts').addEventListener('click', function () {
      setStatus('正在刷新账号列表…');
      refreshAccounts().then(function () {
        setStatus('账号列表已刷新');
      });
    });
    $('btnPickFolder').addEventListener('click', function () {
      var row = $('acctInputRow');
      if (!row) { return; }
      row.hidden = !row.hidden;
      if (!row.hidden) { $('dataRootInput').focus(); }
    });
    $('btnDataRootOk').addEventListener('click', submitDataRoot);
    $('dataRootInput').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { submitDataRoot(); }
    });
    $('btnDeepScan').addEventListener('click', startDeepScan);
    $('btnDiagnose').addEventListener('click', showDiagnose);

    // 悬浮进度窗
    $('btnProgressMin').addEventListener('click', minimizeProgress);
    bindCapsuleDrag();

    // 左侧栏折叠
    $('btnSidebar').addEventListener('click', function () {
      var app = $('appRoot');
      if (!app) { return; }
      app.classList.toggle('sidebar-collapsed');
      var collapsed = app.classList.contains('sidebar-collapsed');
      $('btnSidebar').title = collapsed ? '展开左侧栏' : '折叠左侧栏';
    });

    $('btnExit').addEventListener('click', function () {
      if (window.confirm('确定要退出 QQ导出助手 吗？')) {
        api('/api/shutdown', { method: 'POST' }).catch(function () {});
        setTimeout(function () { window.close(); }, 300);
      }
    });

    $('searchInput').addEventListener('input', function (e) {
      applyFilter(e.target.value);
    });

    $('chatList').addEventListener('click', function (e) {
      var item = e.target.closest ? e.target.closest('.chat-item') : null;
      if (!item) { return; }
      var id = item.getAttribute('data-chat-id');
      var grp = item.getAttribute('data-chat-group');
      if (id) { openChat(id, grp === '1'); }
    });

    $('keyOk').addEventListener('click', submitKey);
    $('keyCancel').addEventListener('click', closeKeyModal);
    $('keyInput').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { submitKey(); }
    });

    // 密钥识别引导
    $('needKeyCancel').addEventListener('click', closeNeedKeyModal);
    $('needKeyRetry').addEventListener('click', function () {
      var uin = needKeyUin || currentUin || '';
      closeNeedKeyModal();
      needKeyAutoShown = true;
      runAutoKey(uin);
    });
    // 顶部大按钮已在上面绑定；页面中央横幅同样先弹引导框
    if ($('btnKeyBanner')) { $('btnKeyBanner').addEventListener('click', doAutoKey); }
    // 帮助面板里的「高级：手动填写密钥」
    if ($('helpManualKey')) {
      $('helpManualKey').addEventListener('click', function () {
        closeHelp();
        openKeyModal();
      });
    }

    $('exportOk').addEventListener('click', function () {
      var scope = 'all';
      var radios = document.getElementsByName('exportScope');
      for (var i = 0; i < radios.length; i++) {
        if (radios[i].checked) { scope = radios[i].value; }
      }
      doExport(scope);
    });
    $('exportCancel').addEventListener('click', closeExportModal);
  }

  /* ---------------------------------------------------------
     启动
     --------------------------------------------------------- */
  function start() {
    bind();
    // 回报一声：界面里的 JS 真的跑起来了（用来判断窗口是不是白屏）
    try {
      fetch('/api/ui-alive', { method: 'POST', cache: 'no-store' }).catch(function () {});
    } catch (e) { /* 忽略 */ }
    refreshState();
    refreshAccounts();
    pollTimer = setInterval(pollState, 1500);
    window.addEventListener('beforeunload', function () {
      if (pollTimer) { clearInterval(pollTimer); }
      if (progTimer) { clearInterval(progTimer); }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
