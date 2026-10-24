// QQ导出助手 —— 自解压启动器 + 原生窗口（WebView2）+ 安装程序
// （C# / .NET Framework 4.x，csc.exe 编译）
//
// 流程：
//   1) 把内嵌 payload.zip（Python 嵌入运行时 + qqexport 包 + 依赖库 + WebView2 组件）
//      释放到 %LOCALAPPDATA%\QQExportHelper\app-<hash12>\；
//   2) 以 pythonw.exe -m qqexport.app 启动本地服务；
//   3) 服务把界面地址写进 <运行目录>\ui-url.txt，启动器用 WebView2 开一个
//      真正的应用窗口（不是浏览器）；WebView2 不可用时退回系统默认浏览器。
//   4) --install 走安装向导（复制本体、快捷方式、注册表卸载项）；--uninstall 卸载。

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("QQ导出助手")]
[assembly: AssemblyDescription("QQ 聊天记录本地导出工具")]
[assembly: AssemblyProduct("QQ导出助手")]
[assembly: AssemblyCompany("QQ导出助手")]
[assembly: AssemblyVersion("1.0.3.0")]
[assembly: AssemblyFileVersion("1.0.3.0")]
[assembly: AssemblyInformationalVersion("1.0.3")]

namespace QQExportHelper
{
    internal static class Program
    {
        private const string PayloadResource = "QQExportHelper.payload.zip";
        private const string MutexName = @"Local\QQExportHelper.SingleInstance";
        private const string AppFolder = "QQExportHelper";
        public const string AppTitle = "QQ导出助手";
        private const string AppVersion = "1.0.3";
        private const string UninstallKey = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\QQ导出助手";

        /// <summary>已选定的可写根目录（日志与子进程都用它）</summary>
        private static string _homeDir = "";

        /// <summary>准备阶段提示窗口上的文字（后台线程读）</summary>
        private static volatile string _prepStatus = "";

        /// <summary>准备阶段结束（提示窗口自行关闭）</summary>
        private static volatile bool _prepDone = false;

        /// <summary>单实例互斥体（必须在进程存活期间一直持有，不能 Dispose）</summary>
        private static Mutex _instanceMutex;
        /// <summary>准备阶段互斥体：串行化「解包 60MB 运行目录」，
        /// 避免两个实例同时解包到同一目录互相覆盖。</summary>
        private static Mutex _prepMutex;

        [STAThread]
        private static int Main(string[] args)
        {
            bool headless = HasFlag(args, "--console");
            try
            {
                CleanupStaleHelpers();
                // 卸载收尾：由 RunUninstall 复制出来的临时副本执行，此时原进程已退出，
                // 文件不再被占用，可以安全删除。
                if (HasFlag(args, "--uninstall-cleanup")) return RunUninstallCleanup(args);
                if (HasFlag(args, "--uninstall")) return RunUninstall(headless);
                // 安装包形态：把本程序改名成「…安装程序.exe / setup.exe / install.exe」后双击，
                // 直接进安装向导（二进制与绿色单文件完全一致，只是文件名触发不同入口）。
                if (HasFlag(args, "--install") || HasFlag(args, "--setup") || LooksLikeInstaller())
                    return RunInstall(args, headless);
                return RunApp(args, headless);
            }
            catch (Exception ex)
            {
                ClosePreparing();
                Log("启动失败：" + ex.Message + Environment.NewLine + ex.StackTrace);
                Report("启动失败：\r\n" + ex.Message + "\r\n\r\n" + ex.StackTrace, headless, true);
                return 1;
            }
        }

        // ------------------------------------------------------------------
        // 正常启动
        // ------------------------------------------------------------------
        private static int RunApp(string[] args, bool headless)
        {
            // 并发启动保护：单实例互斥体要到运行目录确定后才建，而解包
            // （60MB）发生在那之前。两个实例同时双击会同时往同一个候选
            // 目录解包、互相覆盖文件。这里先用独立的准备锁串行化。
            bool prepHeld = false;
            try
            {
                _prepMutex = new Mutex(false, MutexName + ".prep");
                try { prepHeld = _prepMutex.WaitOne(TimeSpan.FromSeconds(180)); }
                catch (AbandonedMutexException) { prepHeld = true; }
            }
            catch (Exception ex) { Log("准备锁不可用（忽略）：" + ex.Message); }
            byte[] payload = ReadPayload();
            string hash = Sha256Hex(payload);
            string appDir = null;
            string lastError = "";

            // 准备阶段提示（首次运行要解压 60MB，可能要等一会儿）
            if (!headless) ShowPreparing();
            _prepStatus = "正在释放运行文件（约 60 MB），首次运行需要稍等…";

            // 依次尝试几个可写位置：%LOCALAPPDATA% → %TEMP% → exe 同目录。
            // 有些机器（装了安全软件/沙箱、或 %LOCALAPPDATA% 被策略锁写）会拒绝写入。
            foreach (string baseDir in CandidateBaseDirs())
            {
                string candidate = Path.Combine(baseDir, "app-" + hash.Substring(0, 12));
                string marker = Path.Combine(candidate, ".payload.ok");
                string pointerFile = Path.Combine(baseDir, "current.txt");
                try
                {
                    // 上次实际用的目录（主目录不可写时用的是带时间戳的备用目录）——
                    // 记在 current.txt 里，免得每次启动都重新解压 60MB。
                    string pointed = ReadPointer(pointerFile, hash);
                    if (pointed != null && File.Exists(Path.Combine(pointed, ".payload.ok")))
                    {
                        Log("复用已解压的运行目录：" + pointed);
                        appDir = pointed;
                        break;
                    }
                    if (File.Exists(marker))
                    {
                        Log("复用已解压的运行目录：" + candidate);
                        appDir = candidate;
                        break;
                    }
                    Log("正在释放运行目录：" + candidate);
                    _prepStatus = "正在释放运行文件（约 60 MB），首次运行需要稍等…";
                    string extracted = Extract(payload, candidate, out lastError);
                    if (extracted == null)
                    {
                        Log("该位置不可用：" + lastError);
                        continue;
                    }
                    File.WriteAllText(Path.Combine(extracted, ".payload.ok"), DateTime.Now.ToString("s"));
                    CleanupOldVersions(baseDir, extracted);
                    try
                    {
                        File.WriteAllText(pointerFile, Path.GetFileName(extracted));
                    }
                    catch (Exception ex)
                    {
                        Log("无法记录当前运行目录：" + ex.Message);
                    }
                    appDir = extracted;
                    break;
                }
                catch (Exception ex)
                {
                    lastError = ex.GetType().Name + ": " + ex.Message;
                    Log("该位置不可用：" + lastError);
                }
            }

            if (appDir == null)
            {
                ClosePreparing();
                throw new IOException(
                    "无法释放运行目录。已尝试：" + string.Join("、", CandidateBaseDirs()) +
                    "。最后错误：" + lastError +
                    "。如果本机装有安全软件/沙箱或 %LOCALAPPDATA% 被策略限制写入，" +
                    "请允许本程序写入上述目录，或把本程序整个复制到可写目录（例如 D:\\）再运行。");
            }
            Log("运行时目录：" + appDir);
            _homeDir = Path.GetDirectoryName(appDir) ?? "";
            // 运行目录已就绪，准备阶段结束，放开准备锁
            if (prepHeld)
            {
                try { _prepMutex.ReleaseMutex(); } catch { }
                prepHeld = false;
            }
            _prepStatus = "正在启动界面…";

            bool createdNew;
            _instanceMutex = new Mutex(true, MutexName, out createdNew);
            string lockFile = Path.Combine(_homeDir, "instance.lock");
            if (!createdNew)
            {
                // 单实例锁已存在：先确认持有者还活着。上次异常退出（崩溃 / 被结束）
                // 可能留下没人释放的锁，这时不能死等，否则用户再也打不开程序。
                int holder = 0;
                try { int.TryParse(File.ReadAllText(lockFile).Trim(), out holder); } catch { }
                bool alive = false;
                if (holder > 0)
                {
                    try
                    {
                        Process other = Process.GetProcessById(holder);
                        alive = !other.HasExited;
                    }
                    catch { alive = false; }
                }
                if (alive)
                {
                    ClosePreparing();
                    Report(AppTitle + "已经在运行了，请切换到已打开的窗口。\n（若看不到窗口，可在任务栏中查找。）",
                           headless, false);
                    return 0;
                }
                Log("发现残留的单实例锁（进程 " + holder + " 已不存在），继续启动。");
            }
            try { File.WriteAllText(lockFile, Process.GetCurrentProcess().Id.ToString()); } catch { }
            try
            {
                return StartApp(appDir, args, _homeDir, headless);
            }
            finally
            {
                try { File.Delete(lockFile); } catch { }
            }
        }

        // ------------------------------------------------------------------
        // 启动本地服务 + 开界面
        // ------------------------------------------------------------------
        private static int StartApp(string appDir, string[] args, string homeDir, bool headless)
        {
            // 界面形态：非命令行模式下用原生窗口（WebView2）承载；命令行模式仍旧
            // 走浏览器，方便调试与自动化。没装 WebView2 运行时的机器直接走浏览器，
            // 免得开出一个空窗口再报错。
            bool native = !headless && !HasFlag(args, "--no-native-ui");
            if (native && !HasWebView2Runtime())
            {
                Log("未检测到 WebView2 运行时，改用系统默认浏览器打开界面。");
                native = false;
            }
            string uiMode = native ? "window" : "browser";

            Child child = StartChild(appDir, args, homeDir, uiMode);
            if (child == null) return 1;

            if (!native)
            {
                // 浏览器模式：Python 侧自己开窗口（带白屏兜底）
                return WaitChild(child);
            }

            // 原生窗口模式：等 Python 把界面地址写出来
            string url = WaitForUrl(homeDir, child.Proc, 120);
            if (url == null)
            {
                Log("没有拿到界面地址，改用系统默认浏览器。");
                ShowNotice("没有拿到本地服务的地址，请稍后重试。", 15);
                return WaitChild(child);
            }
            Log("界面地址：" + url);
            ClosePreparing();

            int exitCode = 0;
            string error = "";
            bool ok = TryRunNativeWindow(url, homeDir, appDir, child.Proc, out exitCode, out error);
            if (!ok)
            {
                Log("原生窗口打开失败：" + error);
                OpenDefaultBrowser(url);
                ShowNotice("界面窗口打开失败：\r\n" + error +
                           "\r\n\r\n已改用系统默认浏览器打开。\r\n" +
                           "若仍没看到界面，请手动在浏览器访问：\r\n" + url, 25);
                return WaitChild(child);
            }
            return WaitChild(child);
        }

        /// <summary>子进程句柄（含日志流）</summary>
        private sealed class Child
        {
            public Process Proc;
            public StreamWriter Out;
            public StreamWriter Err;
            public string OutLog = "";
            public string ErrLog = "";
        }

        /// <summary>启动内嵌 Python 主程序</summary>
        private static Child StartChild(string appDir, string[] args, string homeDir, string uiMode)
        {
            bool console = false;
            foreach (string a in args)
            {
                if (string.Equals(a, "--console", StringComparison.OrdinalIgnoreCase))
                {
                    console = true;
                }
            }

            string python = Path.Combine(appDir, console ? "python.exe" : "pythonw.exe");
            if (!File.Exists(python)) python = Path.Combine(appDir, "python.exe");
            if (!File.Exists(python))
            {
                throw new FileNotFoundException("未找到内嵌 Python 运行时：" + python);
            }

            StringBuilder sb = new StringBuilder();
            sb.Append("-m qqexport.app");
            foreach (string a in args)
            {
                if (string.Equals(a, "--console", StringComparison.OrdinalIgnoreCase)) continue;
                sb.Append(' ');
                if (a.Length > 0 && (a.IndexOf(' ') >= 0 || a.IndexOf('"') >= 0 ||
                                     a.EndsWith("\\", StringComparison.Ordinal)))
                {
                    // 按 CommandLineToArgvW 的规则转义：反斜杠只在「引号前」和
                    // 「参数结尾」时需要翻倍，结尾反斜杠必须 2n 个，否则
                    // --export-dir "D:\a b\" 会被解析成 D:\a b"（路径坏掉）。
                    StringBuilder q = new StringBuilder();
                    q.Append('"');
                    int bs = 0;
                    foreach (char ch in a)
                    {
                        if (ch == '\\') { bs++; continue; }
                        if (ch == '"')
                        {
                            q.Append('\\', bs * 2 + 1);
                            q.Append('"');
                            bs = 0;
                            continue;
                        }
                        if (bs > 0) { q.Append('\\', bs); bs = 0; }
                        q.Append(ch);
                    }
                    if (bs > 0) q.Append('\\', bs * 2);
                    q.Append('"');
                    sb.Append(q.ToString());
                }
                else
                {
                    sb.Append(a);
                }
            }

            ProcessStartInfo psi = new ProcessStartInfo(python, sb.ToString());
            psi.WorkingDirectory = appDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            // Python 侧固定按 UTF-8 输出；不指定的话 .NET 会用控制台代码页（中文系统是 GBK）
            // 解码，写进日志就成了乱码。
            try
            {
                psi.StandardOutputEncoding = Encoding.UTF8;
                psi.StandardErrorEncoding = Encoding.UTF8;
            }
            catch { }
            if (!string.IsNullOrEmpty(uiMode))
            {
                try { psi.EnvironmentVariables["QQEXPORT_UI"] = uiMode; } catch { }
            }
            // 告诉子程序：可写根目录就是这里（日志 / 明文库缓存 / 浏览器配置目录）
            if (!string.IsNullOrEmpty(homeDir))
            {
                try { psi.EnvironmentVariables["QQEXPORT_HOME"] = homeDir; } catch { }
                // %TEMP% 不可写时 SQLite 的排序/临时表会报
                // "unable to open database file"，把子进程的临时目录指到应用目录
                try
                {
                    string sysTmp = Path.GetTempPath();
                    if (!IsWritableDir(sysTmp))
                    {
                        string myTmp = Path.Combine(homeDir, "tmp");
                        Directory.CreateDirectory(myTmp);
                        psi.EnvironmentVariables["TEMP"] = myTmp;
                        psi.EnvironmentVariables["TMP"] = myTmp;
                        psi.EnvironmentVariables["TMPDIR"] = myTmp;
                        Log("系统临时目录不可写（" + sysTmp + "），已改用 " + myTmp);
                    }
                }
                catch (Exception ex) { Log("检查临时目录失败：" + ex.Message); }
            }

            // 先清掉上一次的"界面已就绪"标记（必须在启动子进程之前删，否则会误判）
            string readyFile = string.IsNullOrEmpty(homeDir) ? "" : Path.Combine(homeDir, "ui-ready.txt");
            if (readyFile != "")
            {
                try { if (File.Exists(readyFile)) File.Delete(readyFile); } catch { }
            }
            string urlFile = string.IsNullOrEmpty(homeDir) ? "" : Path.Combine(homeDir, "ui-url.txt");
            if (urlFile != "")
            {
                try { if (File.Exists(urlFile)) File.Delete(urlFile); } catch { }
            }

            Process proc = Process.Start(psi);
            if (proc == null) throw new InvalidOperationException("无法启动内嵌程序。");

            Child child = new Child();
            child.Proc = proc;
            string outLog = Path.Combine(appDir, "app.out.log");
            string errLog = Path.Combine(appDir, "app.err.log");
            StreamWriter so = new StreamWriter(outLog, true);
            StreamWriter se = new StreamWriter(errLog, true);
            child.Out = so;
            child.Err = se;
            child.OutLog = outLog;
            child.ErrLog = errLog;
            proc.OutputDataReceived += delegate (object s, DataReceivedEventArgs e)
            { if (e.Data != null) { try { so.WriteLine(e.Data); so.Flush(); } catch { } } };
            proc.ErrorDataReceived += delegate (object s, DataReceivedEventArgs e)
            { if (e.Data != null) { try { se.WriteLine(e.Data); se.Flush(); } catch { } } };
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();

            // 界面就绪握手：Python 侧写出 ui-ready.txt 后关掉"正在准备"提示窗口
            StartReadyWatchdog(readyFile, proc);
            return child;
        }

        /// <summary>等待子进程结束并收尾日志</summary>
        private static int WaitChild(Child child)
        {
            try { child.Proc.WaitForExit(); } catch { }
            int code = 0;
            try { code = child.Proc.ExitCode; } catch { }
            Log("子进程退出码：" + code);
            try { if (child.Out != null) { child.Out.Flush(); child.Out.Dispose(); } } catch { }
            try { if (child.Err != null) { child.Err.Flush(); child.Err.Dispose(); } } catch { }
            if (code != 0)
            {
                Log("子进程错误输出：" + ReadTail(child.ErrLog, 2000));
            }
            return code;
        }

        /// <summary>等待 Python 写出界面地址（ui-url.txt）</summary>
        private static string WaitForUrl(string homeDir, Process proc, int timeoutSeconds)
        {
            if (string.IsNullOrEmpty(homeDir)) return null;
            string file = Path.Combine(homeDir, "ui-url.txt");
            DateTime deadline = DateTime.Now.AddSeconds(timeoutSeconds);
            while (DateTime.Now < deadline)
            {
                try
                {
                    if (File.Exists(file))
                    {
                        string txt = ReadTail(file, 400);
                        txt = (txt ?? "").Trim();
                        if (txt.Length > 0) return txt;
                    }
                }
                catch { }
                try { if (proc.HasExited) return null; } catch { }
                Thread.Sleep(200);
            }
            return null;
        }

        /// <summary>
        /// 用反射调用 NativeUi（单独一个类，WebView2 组件缺失时不会影响主流程）。
        /// </summary>
        private static bool TryRunNativeWindow(string url, string homeDir, string appDir,
                                               Process proc, out int exitCode, out string error)
        {
            exitCode = 0;
            error = "";
            try
            {
                Assembly asm = Assembly.GetExecutingAssembly();
                Type t = asm.GetType("QQExportHelper.NativeUi");
                if (t == null) { error = "找不到 NativeUi"; return false; }
                MethodInfo mi = t.GetMethod("Run", BindingFlags.Public | BindingFlags.Static);
                if (mi == null) { error = "找不到 NativeUi.Run"; return false; }
                object[] argv = new object[] { url, homeDir, appDir, proc };
                exitCode = (int)mi.Invoke(null, argv);
                if (exitCode == 1)
                {
                    // NativeUi 用 1 表示「WebView2 没起来」——服务还在跑，交给浏览器兜底
                    error = "这台机器上的 WebView2 运行时没能启动（可能未安装或被安全软件拦住）";
                    return false;
                }
                return true;
            }
            catch (TargetInvocationException tie)
            {
                error = tie.InnerException != null ? tie.InnerException.Message : tie.Message;
                return false;
            }
            catch (Exception ex)
            {
                error = ex.GetType().Name + ": " + ex.Message;
                return false;
            }
        }

        /// <summary>本机是否装了 WebView2 运行时（Evergreen）。没装就直接走浏览器模式。</summary>
        private static bool HasWebView2Runtime()
        {
            const string clientId = @"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";
            string[] machineKeys = new string[]
            {
                @"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\" + clientId,
                @"SOFTWARE\Microsoft\EdgeUpdate\Clients\" + clientId,
            };
            foreach (string sub in machineKeys)
            {
                try
                {
                    using (RegistryKey k = Registry.LocalMachine.OpenSubKey(sub))
                    {
                        if (k != null)
                        {
                            object pv = k.GetValue("pv");
                            if (pv != null && pv.ToString() != "0.0.0.0") return true;
                        }
                    }
                }
                catch { }
            }
            try
            {
                using (RegistryKey k = Registry.CurrentUser.OpenSubKey(
                    @"SOFTWARE\Microsoft\EdgeUpdate\Clients\" + clientId))
                {
                    if (k != null)
                    {
                        object pv = k.GetValue("pv");
                        if (pv != null && pv.ToString() != "0.0.0.0") return true;
                    }
                }
            }
            catch { }

            // 兜底：直接看运行时目录里有没有 msedgewebview2.exe
            try
            {
                string[] roots = new string[]
                {
                    Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
                                 "Microsoft", "EdgeWebView", "Application"),
                    Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                                 "Microsoft", "EdgeWebView", "Application"),
                };
                foreach (string root in roots)
                {
                    if (!Directory.Exists(root)) continue;
                    string[] found = Directory.GetFiles(root, "msedgewebview2.exe", SearchOption.AllDirectories);
                    if (found != null && found.Length > 0) return true;
                }
            }
            catch { }
            return false;
        }

        /// <summary>用系统默认浏览器打开地址</summary>
        private static void OpenDefaultBrowser(string url)
        {
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo(url);
                psi.UseShellExecute = true;
                Process.Start(psi);
                Log("已改用系统默认浏览器打开：" + url);
            }
            catch (Exception ex)
            {
                Log("打开默认浏览器失败：" + ex.Message);
            }
        }

        /// <summary>请本地服务退出（界面右上角的 ✕ 走的是同一条路）</summary>
        private static void RequestShutdown(string baseUrl)
        {
            try
            {
                string url = (baseUrl ?? "").TrimEnd('/') + "/api/shutdown";
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
                req.Method = "POST";
                req.Timeout = 5000;
                req.ReadWriteTimeout = 5000;
                using (Stream s = req.GetResponse().GetResponseStream()) { }
                Log("已通知本地服务退出。");
            }
            catch (Exception ex)
            {
                Log("通知服务退出失败：" + ex.Message);
            }
        }

        // ------------------------------------------------------------------
        // 安装 / 卸载
        // ------------------------------------------------------------------
        private static int RunInstall(string[] args, bool headless)
        {
            byte[] payload = ReadPayload();
            string hash = Sha256Hex(payload);
            string defaultDir = Path.Combine(LocalAppData(), "Programs", AppTitle);

            // --install-dir <路径>：直接装到指定目录（用于静默安装 / 自动化测试）
            string dirArg = GetOption(args, "--install-dir");
            bool noLaunch = HasFlag(args, "--no-launch");

            // 静默安装（命令行带 --install-dir，或 --console）
            if (!string.IsNullOrEmpty(dirArg) || headless)
            {
                string dir = NormalizeInstallDir(string.IsNullOrEmpty(dirArg) ? defaultDir : dirArg);
                try
                {
                    string exePath = InstallTo(dir, payload, hash, null);
                    Log("已安装到：" + dir);
                    if (!noLaunch)
                    {
                        try { Process.Start(new ProcessStartInfo(exePath) { UseShellExecute = true }); }
                        catch (Exception ex) { Log("启动安装后的程序失败：" + ex.Message); }
                    }
                    return 0;
                }
                catch (Exception ex)
                {
                    Log("安装失败：" + ex.Message);
                    Report("安装失败：\r\n" + ex.Message, headless, true);
                    return 1;
                }
            }

            // 图形化安装向导
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            string chosen = defaultDir;
            string installedExe = "";
            using (InstallWizard wiz = new InstallWizard(defaultDir))
            {
                if (wiz.ShowDialog() != DialogResult.OK) return 0;
                chosen = wiz.SelectedDir;
                installedExe = wiz.InstalledExePath;
                noLaunch = !wiz.RunAfterInstall;
            }
            try
            {
                // 以 InstallTo 实际返回的路径为准：选中「桌面/文档」时安装目录会被
                // 自动下钻一层（<dir>\QQ导出助手），用原始输入拼路径会启动不存在的文件。
                string exePath = string.IsNullOrEmpty(installedExe)
                    ? Path.Combine(NormalizeInstallDir(chosen), AppTitle + ".exe")
                    : installedExe;
                Log("已安装到：" + chosen);
                if (!noLaunch)
                {
                    try { Process.Start(new ProcessStartInfo(exePath) { UseShellExecute = true }); }
                    catch (Exception ex) { Log("启动安装后的程序失败：" + ex.Message); }
                }
                return 0;
            }
            catch (Exception ex)
            {
                Log("安装后启动失败：" + ex.Message);
                return 0;
            }
        }

        /// <summary>
        /// 真正的安装动作（向导与静默安装共用）。
        /// report 为 null 表示不需要进度回调。返回安装后的 exe 路径。
        /// </summary>
        internal static string InstallTo(string dir, byte[] payload, string hash, Action<int, string> report)
        {
            // 安全护栏：如果用户把「桌面 / 下载 / 文档」这类文件夹本身选成安装目录，
            // 自动下钻一层（<该文件夹>\QQ导出助手），避免卸载时把整个文件夹当成安装目录删掉。
            dir = NormalizeInstallDir(dir);

            if (report != null) report(3, "正在准备安装目录…");
            Directory.CreateDirectory(dir);

            string exePath = Path.Combine(dir, AppTitle + ".exe");
            string self = Assembly.GetExecutingAssembly().Location;
            if (!string.IsNullOrEmpty(self) &&
                !string.Equals(Path.GetFullPath(self), Path.GetFullPath(exePath), StringComparison.OrdinalIgnoreCase))
            {
                if (report != null) report(8, "正在复制程序文件…");
                File.Copy(self, exePath, true);
            }

            // 运行目录：<安装目录>\runtime\app-<hash12>
            string runtime = Path.Combine(dir, "runtime");
            string appDir = Path.Combine(runtime, "app-" + hash.Substring(0, 12));
            if (!File.Exists(Path.Combine(appDir, ".payload.ok")))
            {
                if (report != null) report(18, "正在释放运行文件（约 60 MB，首次安装需要一会儿）…");
                string error = "";
                string ok = Extract(payload, appDir, out error);
                if (ok == null) throw new IOException("释放运行文件失败：" + error);
                File.WriteAllText(Path.Combine(ok, ".payload.ok"), DateTime.Now.ToString("s"));
            }
            else if (report != null)
            {
                report(60, "运行文件已存在，跳过释放…");
            }
            try { File.WriteAllText(Path.Combine(runtime, "current.txt"), Path.GetFileName(appDir)); }
            catch { }

            // 安装标记：卸载时只有看到它，才允许删除整个安装目录。
            try
            {
                File.WriteAllText(Path.Combine(dir, ".qqexport-install"),
                    AppVersion + " " + DateTime.Now.ToString("s") + Environment.NewLine);
            }
            catch { }

            if (report != null) report(72, "正在创建桌面和开始菜单快捷方式…");
            string desktop = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                                          AppTitle + ".lnk");
            string menu = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs),
                                       AppTitle + ".lnk");
            CreateShortcut(desktop, exePath);
            CreateShortcut(menu, exePath);

            if (report != null) report(88, "正在登记卸载信息…");
            WriteUninstallEntry(dir, exePath);

            if (report != null) report(100, "安装完成");
            return exePath;
        }

        /// <summary>供安装向导调用（向导是另一个类，不能访问 private 成员）</summary>
        internal static byte[] ReadPayloadForInstall()
        {
            return ReadPayload();
        }

        /// <summary>供安装向导调用</summary>
        internal static string HashForInstall(byte[] payload)
        {
            return Sha256Hex(payload);
        }

        private static int RunUninstall(bool headless)
        {
            string dir = "";
            try { dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? ""; } catch { }
            try
            {
                string desktop = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                                              AppTitle + ".lnk");
                string menu = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs),
                                           AppTitle + ".lnk");
                DeleteFile(desktop);
                DeleteFile(menu);
            }
            catch { }
            try { Microsoft.Win32.Registry.CurrentUser.DeleteSubKeyTree(UninstallKey, false); } catch { }

            if (!string.IsNullOrEmpty(dir))
            {
                // 只有「安装时写过 .qqexport-install 标记」且不是桌面/下载/文档这类
                // 已知用户文件夹时，才允许把整个目录删掉；否则只删本程序自己的东西。
                // 绿色单文件放在别的文件夹里运行时，--uninstall 绝不能把那个文件夹清空。
                bool owned = false;
                try { owned = File.Exists(Path.Combine(dir, ".qqexport-install")); } catch { }
                bool safe = owned && !IsKnownUserFolder(dir);
                if (safe) Log("卸载：删除安装目录 " + dir);
                else Log("卸载：只清理运行文件与程序本体，保留目录 " + dir + (owned ? "" : "（未找到安装标记）"));

                // 自己删不掉正在运行的 exe：复制一份到临时目录，由它在原进程退出后再删。
                // 不依赖 cmd.exe 的命令行解析（中文路径在无控制台进程里会被代码页搞坏）。
                try
                {
                    string self = Assembly.GetExecutingAssembly().Location;
                    string tmpDir = Path.GetTempPath();
                    if (string.IsNullOrEmpty(tmpDir) || !Directory.Exists(tmpDir)) tmpDir = LocalAppData();
                    string helper = Path.Combine(tmpDir, "qqexport-cleanup-" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".exe");
                    File.Copy(self, helper, true);
                    string helperArgs = "--uninstall-cleanup --dir \"" + dir + "\" --wait-pid "
                                      + Process.GetCurrentProcess().Id + (safe ? " --whole" : "");
                    ProcessStartInfo hpsi = new ProcessStartInfo(helper, helperArgs);
                    hpsi.UseShellExecute = false;
                    hpsi.CreateNoWindow = true;
                    Process.Start(hpsi);
                    Log("卸载：已安排延迟清理（" + (safe ? "整目录" : "仅程序文件") + "）");
                }
                catch (Exception ex) { Log("安排延迟清理失败：" + ex.Message); }
            }
            if (!headless)
            {
                MessageBox.Show("已卸载。\r\n\r\n" + AppTitle + " 已经从本机移除。", AppTitle,
                                MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
            return 0;
        }

        /// <summary>
        /// 卸载收尾进程：等原进程退出后，删掉运行文件与程序本体（--whole 时删整个安装目录），
        /// 最后安排删除自己。用 .NET API 而不是 cmd 的 del/rmdir，避免中文路径被代码页破坏。
        /// </summary>
        private static int RunUninstallCleanup(string[] args)
        {
            string dir = GetOption(args, "--dir") ?? "";
            bool whole = HasFlag(args, "--whole");
            int pid = 0;
            try { int.TryParse(GetOption(args, "--wait-pid") ?? "0", out pid); } catch { }
            if (pid > 0)
            {
                try
                {
                    Process p = Process.GetProcessById(pid);
                    p.WaitForExit(30000);
                }
                catch { }
            }
            Thread.Sleep(1200);
            if (string.IsNullOrEmpty(dir)) return 0;

            try
            {
                string runtime = Path.Combine(dir, "runtime");
                if (Directory.Exists(runtime))
                {
                    Directory.Delete(runtime, true);
                    Log("清理：已删除 " + runtime);
                }
            }
            catch (Exception ex) { Log("清理 runtime 失败：" + ex.Message); }

            try
            {
                string exe = Path.Combine(dir, AppTitle + ".exe");
                if (File.Exists(exe))
                {
                    File.Delete(exe);
                    Log("清理：已删除 " + exe);
                }
            }
            catch (Exception ex) { Log("清理程序本体失败：" + ex.Message); }

            try
            {
                if (Directory.Exists(dir))
                {
                    if (whole) Directory.Delete(dir, true);
                    else if (Directory.GetFileSystemEntries(dir).Length == 0) Directory.Delete(dir);
                }
            }
            catch (Exception ex) { Log("清理目录失败：" + ex.Message); }

            // 临时副本留在 %TEMP%，下次启动时由 CleanupStaleHelpers 清理。
            // （不再调用 cmd.exe 自删除：那种写法是恶意软件的典型特征，容易被 AV 误判。）
            return 0;
        }

        /// <summary>清理 %TEMP% 里残留的卸载临时副本（超过 6 小时的）。</summary>
        private static void CleanupStaleHelpers()
        {
            try
            {
                string tmp = Path.GetTempPath();
                if (string.IsNullOrEmpty(tmp) || !Directory.Exists(tmp)) return;
                foreach (string f in Directory.GetFiles(tmp, "qqexport-cleanup-*.exe"))
                {
                    try
                    {
                        if (DateTime.UtcNow - File.GetLastWriteTimeUtc(f) > TimeSpan.FromHours(6))
                            File.Delete(f);
                    }
                    catch { }
                }
            }
            catch { }
        }

        /// <summary>
        /// 如果选中的安装目录本身是「桌面 / 下载 / 文档 / 用户主目录 / 盘符根」这类
        /// 已知用户文件夹，就在它下面再套一层 QQ导出助手，避免卸载时误删整层。
        /// </summary>
        private static string NormalizeInstallDir(string dir)
        {
            try
            {
                string full = Path.GetFullPath(dir).TrimEnd('\\');
                if (IsKnownUserFolder(full))
                {
                    string sub = Path.Combine(full, AppTitle);
                    Log("安装目录是已知用户文件夹，改为：" + sub);
                    return sub;
                }
                // 统一返回绝对路径：相对路径写进注册表后，换工作目录卸载会失败
                return full;
            }
            catch { }
            return dir;
        }

        /// <summary>判断是不是「不该被整体删除」的常见文件夹。</summary>
        private static bool IsKnownUserFolder(string dir)
        {
            try
            {
                string full = Path.GetFullPath(dir).TrimEnd('\\');
                if (full.Length <= 3) return true;                       // C:\ 之类的盘符根
                string[] specials = new string[]
                {
                    Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                    Environment.GetFolderPath(Environment.SpecialFolder.Desktop),
                    Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                    Environment.GetFolderPath(Environment.SpecialFolder.MyMusic),
                    Environment.GetFolderPath(Environment.SpecialFolder.MyPictures),
                    Environment.GetFolderPath(Environment.SpecialFolder.MyVideos),
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                    Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
                    Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
                    Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                    Environment.GetFolderPath(Environment.SpecialFolder.System),
                    Path.GetTempPath(),
                    // 用户主目录下的 Downloads（SpecialFolder 没有对应枚举）
                    Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads"),
                };
                foreach (string s in specials)
                {
                    if (string.IsNullOrEmpty(s)) continue;
                    if (string.Equals(full, Path.GetFullPath(s).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                        return true;
                }
            }
            catch { }
            return false;
        }

        /// <summary>写注册表里的"卸载程序"条目</summary>
        private static void WriteUninstallEntry(string dir, string exePath)
        {
            long size = 0;
            try
            {
                foreach (string f in Directory.GetFiles(dir, "*", SearchOption.AllDirectories))
                {
                    try { size += new FileInfo(f).Length; } catch { }
                }
            }
            catch { }
            try
            {
                using (Microsoft.Win32.RegistryKey key = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(UninstallKey))
                {
                    key.SetValue("DisplayName", AppTitle);
                    key.SetValue("DisplayVersion", AppVersion);
                    key.SetValue("Publisher", AppTitle);
                    key.SetValue("InstallLocation", dir);
                    key.SetValue("UninstallString", "\"" + exePath + "\" --uninstall");
                    key.SetValue("QuietUninstallString", "\"" + exePath + "\" --uninstall --console");
                    key.SetValue("DisplayIcon", exePath);
                    key.SetValue("NoModify", 1);
                    key.SetValue("NoRepair", 1);
                    key.SetValue("EstimatedSize", (int)(size / 1024));
                }
                Log("已写入卸载信息（注册表）。");
            }
            catch (Exception ex)
            {
                Log("写卸载信息失败：" + ex.GetType().Name + ": " + ex.Message +
                    (ex.InnerException != null ? " / 内层: " + ex.InnerException.Message : "") +
                    "（不再回退到 PowerShell：该回退会被安全软件当作可疑行为）");
            }
        }

        /// <summary>创建桌面/开始菜单快捷方式（用 WScript.Shell 后期绑定，免去 COM 引用）</summary>
        private static void CreateShortcut(string linkPath, string target)
        {
            try
            {
                string dir = Path.GetDirectoryName(linkPath);
                if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                Type shellType = Type.GetTypeFromProgID("WScript.Shell");
                if (shellType == null) { Log("没有 WScript.Shell，无法创建快捷方式：" + linkPath); return; }
                object shell = Activator.CreateInstance(shellType);
                object link = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod,
                                                    null, shell, new object[] { linkPath });
                Type linkType = link.GetType();
                linkType.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { target });
                linkType.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link,
                                      new object[] { Path.GetDirectoryName(target) });
                linkType.InvokeMember("Description", BindingFlags.SetProperty, null, link,
                                      new object[] { "QQ 聊天记录导出助手" });
                linkType.InvokeMember("IconLocation", BindingFlags.SetProperty, null, link,
                                      new object[] { target + ",0" });
                linkType.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
                Log("已创建快捷方式：" + linkPath);
            }
            catch (Exception ex)
            {
                Log("创建快捷方式失败（" + linkPath + "）：" + ex.GetType().Name + ": " + ex.Message +
                    (ex.InnerException != null ? " / 内层: " + ex.InnerException.Message : "") +
                    (ex.InnerException != null && ex.InnerException.InnerException != null
                        ? " / 更内层: " + ex.InnerException.InnerException.Message : ""));
            }
        }

        private static void DeleteFile(string path)
        {
            try { if (File.Exists(path)) File.Delete(path); } catch { }
        }

        // ------------------------------------------------------------------
        // 准备阶段提示窗口
        // ------------------------------------------------------------------
        private static void ShowPreparing()
        {
            _prepStatus = "正在准备 " + AppTitle + "…";
            Thread th = new Thread(delegate ()
            {
                try
                {
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    Form form = new Form();
                    form.Text = AppTitle;
                    form.FormBorderStyle = FormBorderStyle.FixedDialog;
                    form.StartPosition = FormStartPosition.CenterScreen;
                    form.ControlBox = false;
                    form.TopMost = true;
                    form.ClientSize = new Size(440, 110);
                    Label label = new Label();
                    label.Dock = DockStyle.Fill;
                    label.TextAlign = ContentAlignment.MiddleCenter;
                    label.Text = _prepStatus;
                    form.Controls.Add(label);
                    System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
                    timer.Interval = 300;
                    timer.Tick += delegate (object s, EventArgs e)
                    {
                        label.Text = _prepStatus;
                        if (_prepDone) { timer.Stop(); form.Close(); }
                    };
                    timer.Start();
                    Application.Run(form);
                }
                catch { }
            });
            th.SetApartmentState(ApartmentState.STA);
            th.IsBackground = true;
            th.Start();
        }

        private static void ClosePreparing()
        {
            _prepDone = true;
        }

        /// <summary>
        /// 非阻塞提示：弹一个自己的小窗（不阻塞主流程），到时间自动关掉。
        /// MessageBox 是模态的，卡在这里会让整个程序退不出去，所以不用它。
        /// </summary>
        private static void ShowNotice(string text, int seconds)
        {
            Thread th = new Thread(delegate ()
            {
                try
                {
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    Form form = new Form();
                    form.Text = AppTitle;
                    form.FormBorderStyle = FormBorderStyle.FixedDialog;
                    form.StartPosition = FormStartPosition.CenterScreen;
                    form.TopMost = true;
                    form.ClientSize = new Size(480, 170);
                    Label label = new Label();
                    label.Dock = DockStyle.Fill;
                    label.TextAlign = ContentAlignment.MiddleCenter;
                    label.Text = text;
                    form.Controls.Add(label);
                    Button ok = new Button();
                    ok.Text = "知道了";
                    ok.DialogResult = DialogResult.OK;
                    ok.Size = new Size(90, 28);
                    ok.Location = new Point(185, 128);
                    ok.Click += delegate (object s, EventArgs e) { form.Close(); };
                    form.Controls.Add(ok);
                    System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
                    timer.Interval = 1000;
                    int left = seconds;
                    timer.Tick += delegate (object s, EventArgs e)
                    {
                        left--;
                        if (left <= 0) { timer.Stop(); form.Close(); }
                    };
                    timer.Start();
                    Application.Run(form);
                }
                catch { }
            });
            th.SetApartmentState(ApartmentState.STA);
            th.IsBackground = true;
            th.Start();
        }

        /// <summary>等待"界面已就绪"信号（ui-ready.txt）或子进程退出，结束后关掉准备提示窗口</summary>
        private static void StartReadyWatchdog(string readyFile, Process proc)
        {
            Thread th = new Thread(delegate ()
            {
                while (true)
                {
                    try { if (!string.IsNullOrEmpty(readyFile) && File.Exists(readyFile)) break; } catch { }
                    try { if (proc.HasExited) break; } catch { }
                    Thread.Sleep(300);
                }
                ClosePreparing();
            });
            th.IsBackground = true;
            th.Start();
        }

        // ------------------------------------------------------------------
        // 杂项
        // ------------------------------------------------------------------
        private static bool HasFlag(string[] args, string flag)
        {
            foreach (string a in args)
            {
                if (string.Equals(a, flag, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        /// <summary>
        /// 文件名里带「安装 / 安装程序 / setup / install」时，双击直接进安装向导。
        /// 装完之后复制过去的本体叫「QQ导出助手.exe」，不含这些词，所以照常启动程序。
        /// </summary>
        private static bool LooksLikeInstaller()
        {
            try
            {
                string self = Assembly.GetExecutingAssembly().Location;
                if (string.IsNullOrEmpty(self)) return false;
                string name = Path.GetFileNameWithoutExtension(self).ToLowerInvariant();
                return name.Contains("安装") || name.Contains("setup") || name.Contains("install");
            }
            catch { return false; }
        }

        /// <summary>读取 --xxx value 形式的选项值（也支持 --xxx=value）</summary>
        private static string GetOption(string[] args, string name)
        {
            for (int i = 0; i < args.Length; i++)
            {
                string a = args[i] ?? "";
                if (a.StartsWith(name + "=", StringComparison.OrdinalIgnoreCase))
                {
                    return a.Substring(name.Length + 1).Trim('"');
                }
                if (string.Equals(a, name, StringComparison.OrdinalIgnoreCase) && i + 1 < args.Length)
                {
                    return (args[i + 1] ?? "").Trim('"');
                }
            }
            return "";
        }

        /// <summary>错误提示：headless（--console）时写到 stderr，不弹窗卡住</summary>
        private static void Report(string text, bool headless, bool error)
        {
            try
            {
                if (headless)
                {
                    TextWriter w = error ? Console.Error : Console.Out;
                    w.WriteLine(text);
                    w.Flush();
                    return;
                }
            }
            catch { }
            try { MessageBox.Show(text, AppTitle, MessageBoxButtons.OK,
                error ? MessageBoxIcon.Error : MessageBoxIcon.Information); }
            catch { }
        }

        /// <summary>本地应用数据目录：优先用 LOCALAPPDATA 环境变量，其次系统已知文件夹</summary>
        private static string LocalAppData()
        {
            string dir = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            if (!string.IsNullOrEmpty(dir)) return dir;
            return Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        }

        /// <summary>把启动器自己的日志写到本地应用数据目录；写不进去就退回 exe 同目录</summary>
        internal static void Log(string message)
        {
            string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message
                          + Environment.NewLine;
            string[] targets = {
                Path.Combine(LocalAppData(), AppFolder, "launcher.log"),
                Path.Combine(AppDir(), "launcher.log"),
                Path.Combine(Path.GetTempPath(), "QQExportHelper-launcher.log")
            };
            if (!string.IsNullOrEmpty(_homeDir))
            {
                targets = new string[] { Path.Combine(_homeDir, "launcher.log") }
                           .Concat(targets).ToArray();
            }
            foreach (string path in targets)
            {
                try
                {
                    string dir = Path.GetDirectoryName(path);
                    if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                    File.AppendAllText(path, line, new UTF8Encoding(false));
                    return;
                }
                catch { }
            }
        }

        /// <summary>exe 自身所在目录（用于日志兜底）</summary>
        private static string AppDir()
        {
            try
            {
                string p = Assembly.GetExecutingAssembly().Location;
                if (!string.IsNullOrEmpty(p)) return Path.GetDirectoryName(p);
            }
            catch { }
            return AppFolder;
        }

        /// <summary>目录是否可写（写一个临时文件试试）</summary>
        private static bool IsWritableDir(string dir)
        {
            if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir)) return false;
            string probe = Path.Combine(dir, ".qqexport_probe_" + DateTime.Now.Ticks);
            try
            {
                using (FileStream fs = new FileStream(probe, FileMode.Create, FileAccess.Write))
                {
                    fs.WriteByte(1);
                }
                File.Delete(probe);
                return true;
            }
            catch { return false; }
        }

        /// <summary>读取编译进程序集的 payload.zip</summary>
        private static byte[] ReadPayload()
        {
            Assembly asm = Assembly.GetExecutingAssembly();
            string name = PayloadResource;
            Stream s = asm.GetManifestResourceStream(name);
            if (s == null)
            {
                foreach (string n in asm.GetManifestResourceNames())
                {
                    if (n.EndsWith("payload.zip", StringComparison.OrdinalIgnoreCase))
                    {
                        name = n;
                        s = asm.GetManifestResourceStream(n);
                        break;
                    }
                }
            }
            if (s == null)
            {
                throw new InvalidOperationException(
                    "内嵌资源缺失：" + PayloadResource + "；可用资源：" +
                    string.Join(", ", asm.GetManifestResourceNames()));
            }
            using (s)
            using (MemoryStream ms = new MemoryStream())
            {
                byte[] buf = new byte[65536];
                int n;
                while ((n = s.Read(buf, 0, buf.Length)) > 0) ms.Write(buf, 0, n);
                return ms.ToArray();
            }
        }

        /// <summary>
        /// 候选运行目录（按优先级）：%LOCALAPPDATA%\QQExportHelper → %TEMP%\QQExportHelper
        /// → exe 同目录下的 runtime\。第一个可写的就用它。
        /// </summary>
        private static string[] CandidateBaseDirs()
        {
            List<string> list = new List<string>();
            try { list.Add(Path.Combine(LocalAppData(), AppFolder)); } catch { }
            try { string t = Path.GetTempPath(); if (!string.IsNullOrEmpty(t)) list.Add(Path.Combine(t, AppFolder)); } catch { }
            try { list.Add(Path.Combine(AppDir(), "runtime")); } catch { }
            return list.ToArray();
        }

        /// <summary>读取 current.txt：上次真正用起来的运行目录（要与本次 payload 对得上）</summary>
        private static string ReadPointer(string pointerFile, string hash)
        {
            try
            {
                if (!File.Exists(pointerFile)) return null;
                string name = File.ReadAllText(pointerFile).Trim();
                if (name.Length == 0) return null;
                if (!name.StartsWith("app-" + hash.Substring(0, 12))) return null;
                string full = Path.Combine(Path.GetDirectoryName(pointerFile) ?? "", name);
                if (!Directory.Exists(full)) return null;
                return full;
            }
            catch
            {
                return null;
            }
        }

        /// <summary>
        /// 就地解压 payload，返回真正使用的目录名。
        /// 正常路径：直接解到 appDir（目录名固定，.payload.ok 标记也写在这里，
        /// 下次启动命中标记即可跳过解压，秒开）。
        /// 只有第一次解压真的失败（目录被占用 / 权限不足）时，才换唯一目录重试一次。
        /// </summary>
        private static string Extract(byte[] payload, string appDir, out string error)
        {
            error = "";
            string first = Unzip(payload, appDir, out error);
            if (first != null) return first;

            // 解压失败 → 换唯一目录重试一次（例如上一个实例还占着 appDir）
            string alt = appDir + "-" + DateTime.Now.ToString("HHmmssfff");
            string altError;
            string ok = Unzip(payload, alt, out altError);
            if (ok != null) { error = altError; return ok; }
            if (!string.IsNullOrEmpty(altError)) error = altError;

            return null;                        // 两次都失败
        }

        /// <summary>解压到指定目录；全部成功返回目录，失败返回 null 并给出原因</summary>
        private static string Unzip(byte[] payload, string target, out string error)
        {
            bool allOk = true;
            error = "";
            try
            {
                Directory.CreateDirectory(target);
            }
            catch (Exception ex)
            {
                error = "创建目录失败（" + target + "）：" + ex.GetType().Name + ": " + ex.Message;
                return null;
            }
            try
            {
                using (MemoryStream ms = new MemoryStream(payload))
                using (ZipArchive zip = new ZipArchive(ms, ZipArchiveMode.Read))
                {
                    foreach (ZipArchiveEntry entry in zip.Entries)
                    {
                        string rel = entry.FullName.Replace('/', Path.DirectorySeparatorChar);
                        if (rel.Length == 0) continue;
                        string dest = Path.Combine(target, rel);
                        try
                        {
                            if (entry.FullName.EndsWith("/") || entry.FullName.EndsWith("\\"))
                            {
                                Directory.CreateDirectory(dest);
                                continue;
                            }
                            string dir = Path.GetDirectoryName(dest);
                            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);
                            using (Stream src = entry.Open())
                            using (FileStream dst = new FileStream(dest, FileMode.Create, FileAccess.Write))
                            {
                                src.CopyTo(dst);
                            }
                        }
                        catch (Exception ex)
                        {
                            allOk = false;
                            if (error == "")
                                error = "写入失败（" + dest + "）：" + ex.GetType().Name + ": " + ex.Message;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                error = "解压失败（" + target + "）：" + ex.GetType().Name + ": " + ex.Message;
                return null;
            }
            if (!allOk)
            {
                if (error == "") error = "部分文件写入失败（" + target + "）";
                return null;
            }
            return target;
        }

        /// <summary>删除除当前版本外的旧目录</summary>
        private static void CleanupOldVersions(string baseDir, string keep)
        {
            try
            {
                foreach (string dir in Directory.GetDirectories(baseDir, "app-*"))
                {
                    if (string.Equals(dir, keep, StringComparison.OrdinalIgnoreCase)) continue;
                    try { Directory.Delete(dir, true); } catch { }
                }
            }
            catch { }
        }

        /// <summary>读取文件末尾若干字符（容错）</summary>
        private static string ReadTail(string path, int max)
        {
            try
            {
                if (!File.Exists(path)) return "";
                string txt = File.ReadAllText(path);
                if (txt.Length > max) txt = txt.Substring(txt.Length - max);
                return txt;
            }
            catch { return ""; }
        }

        private static string Sha256Hex(byte[] data)
        {
            using (SHA256 sha = SHA256.Create())
            {
                byte[] hash = sha.ComputeHash(data);
                StringBuilder sb = new StringBuilder(hash.Length * 2);
                foreach (byte b in hash) sb.Append(b.ToString("x2"));
                return sb.ToString();
            }
        }
    }

    // ======================================================================
    // 原生窗口：WebView2 承载界面（不经过浏览器）
    // ======================================================================
    internal static class NativeUi
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr LoadLibraryW(string path);

        /// <summary>打开应用窗口并阻塞到窗口关闭</summary>
        public static int Run(string url, string homeDir, string appDir, Process child)
        {
            // 先把程序集解析钩子装好，再去 JIT 含 WebView2 类型的方法
            Prepare(appDir);
            return RunWindow(url, homeDir, appDir, child);
        }

        private static int RunWindow(string url, string homeDir, string appDir, Process child)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            Form f = new Form();
            f.Text = "QQ导出助手";
            f.ClientSize = new Size(1180, 780);
            f.MinimumSize = new Size(900, 620);
            f.StartPosition = FormStartPosition.CenterScreen;
            f.BackColor = Color.FromArgb(17, 19, 23);
            f.ForeColor = Color.White;

            Microsoft.Web.WebView2.WinForms.WebView2 wv = new Microsoft.Web.WebView2.WinForms.WebView2();
            wv.Dock = DockStyle.Fill;
            f.Controls.Add(wv);

            int state = 0;   // 0=运行中 1=正在关闭 2=已请求关闭
            int initFailed = 0;

            // 窗口关闭 → 请本地服务退出 → 等子进程结束 → 真正关窗
            f.FormClosing += delegate (object s, FormClosingEventArgs e)
            {
                // WebView2 起不来时窗口是我们自己关掉的，**不能**顺手把服务也停了：
                // 上层还要拿同一个地址改用默认浏览器兜底。这里直接放行关闭即可。
                if (Volatile.Read(ref initFailed) != 0) return;
                if (Volatile.Read(ref state) != 0) return;
                e.Cancel = true;
                Interlocked.Exchange(ref state, 1);
                Thread th = new Thread(delegate ()
                {
                    RequestShutdown(url);
                    try { child.WaitForExit(15000); } catch { }
                    try { if (!child.HasExited) child.Kill(); } catch { }
                    Interlocked.Exchange(ref state, 2);
                    try
                    {
                        f.BeginInvoke(new MethodInvoker(delegate { f.Close(); }));
                    }
                    catch { }
                });
                th.IsBackground = true;
                th.Start();
            };

            // 服务自己退了（例如界面里点了「退出」）→ 把窗口也关掉
            Thread watcher = new Thread(delegate ()
            {
                try { child.WaitForExit(); } catch { return; }
                if (Volatile.Read(ref state) == 0)
                {
                    Interlocked.Exchange(ref state, 2);
                    try
                    {
                        f.BeginInvoke(new MethodInvoker(delegate { f.Close(); }));
                    }
                    catch { }
                }
            });
            watcher.IsBackground = true;
            watcher.Start();

            f.Shown += async (s, e) =>
            {
                try
                {
                    string udf = Path.Combine(string.IsNullOrEmpty(homeDir) ? appDir : homeDir, "webview2");
                    Directory.CreateDirectory(udf);
                    Microsoft.Web.WebView2.Core.CoreWebView2Environment env =
                        await Microsoft.Web.WebView2.Core.CoreWebView2Environment.CreateAsync(null, udf);
                    await wv.EnsureCoreWebView2Async(env);
                    wv.CoreWebView2.Navigate(url);
                }
                catch (Exception ex)
                {
                    Program.Log("WebView2 初始化失败：" + ex.GetType().Name + ": " + ex.Message);
                    Interlocked.Exchange(ref initFailed, 1);
                    try { f.BeginInvoke(new MethodInvoker(delegate { f.Close(); })); } catch { }
                }
            };

            Application.Run(f);
            // 1 = WebView2 没起来（服务还活着，交给上层用默认浏览器兜底）
            return Volatile.Read(ref initFailed) != 0 ? 1 : 0;
        }

        /// <summary>准备 WebView2 组件：程序集解析 + 原生 loader 预加载</summary>
        private static void Prepare(string appDir)
        {
            string dir = Path.Combine(appDir ?? "", "WebView2");
            if (!Directory.Exists(dir))
            {
                Program.Log("WebView2 目录不存在：" + dir);
                return;
            }

            // 原生 loader（WebView2Loader.dll）先 LoadLibrary 进来，
            // 之后 Core 程序集里的 P/Invoke 就能按名字找到它
            string loader = Path.Combine(dir, "WebView2Loader.dll");
            if (File.Exists(loader))
            {
                try { LoadLibraryW(loader); Program.Log("已预加载 WebView2Loader.dll"); }
                catch (Exception ex) { Program.Log("预加载 WebView2Loader 失败：" + ex.Message); }
            }

            // 先把 Core 装进来：WinForms 程序集依赖它。
            // 注意：Assembly.LoadFrom / LoadFile 在本机会抛
            // FileLoadException(0x80131515「不支持操作」)，所以改成从字节加载
            try
            {
                Assembly.Load(File.ReadAllBytes(Path.Combine(dir, "Microsoft.Web.WebView2.Core.dll")));
                Program.Log("已加载 WebView2 Core 程序集");
            }
            catch (Exception ex)
            {
                Program.Log("加载 WebView2 Core 失败：" + ex.GetType().Name + ": " + ex.Message);
            }

            AppDomain.CurrentDomain.AssemblyResolve += delegate (object sender, ResolveEventArgs args)
            {
                try
                {
                    string simple = new AssemblyName(args.Name).Name;
                    if (string.IsNullOrEmpty(simple)) return null;
                    if (!simple.StartsWith("Microsoft.Web.WebView2")) return null;
                    string path = Path.Combine(dir, simple + ".dll");
                    if (!File.Exists(path))
                    {
                        Program.Log("程序集解析失败（文件不存在）：" + simple);
                        return null;
                    }
                    Assembly a = Assembly.Load(File.ReadAllBytes(path));
                    Program.Log("程序集已解析：" + simple);
                    return a;
                }
                catch (Exception ex)
                {
                    Program.Log("程序集解析出错：" + ex.GetType().Name + ": " + ex.Message);
                }
                return null;
            };
        }

        private static void RequestShutdown(string baseUrl)
        {
            try
            {
                string url = (baseUrl ?? "").TrimEnd('/') + "/api/shutdown";
                HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
                req.Method = "POST";
                req.Timeout = 5000;
                req.ReadWriteTimeout = 5000;
                using (Stream s = req.GetResponse().GetResponseStream()) { }
            }
            catch { }
        }
    }

    // ======================================================================
    // 安装向导窗口（4 页：欢迎 → 选择位置 → 安装中 → 完成）
    // ======================================================================
    internal sealed class InstallWizard : Form
    {
        private readonly Panel _pageWelcome;
        private readonly Panel _pageDir;
        private readonly Panel _pageBusy;
        private readonly Panel _pageDone;
        private readonly TextBox _path;
        private readonly ProgressBar _bar;
        private readonly Label _status;
        private readonly Label _donePath;
        private readonly CheckBox _runNow;
        private readonly Button _btnBack;
        private readonly Button _btnNext;
        private readonly Button _btnCancel;
        private readonly Label _err;

        private int _page;
        private bool _installing;

        public string SelectedDir { get; private set; }
        /// <summary>InstallTo 实际写出的 exe 全路径（安装目录可能被下钻一层）。</summary>
        public string InstalledExePath { get; private set; }
        public bool RunAfterInstall { get { return _runNow.Checked; } }

        public InstallWizard(string defaultDir)
        {
            SelectedDir = defaultDir;
            Text = "安装 " + Program.AppTitle;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(600, 400);
            MaximizeBox = false;
            MinimizeBox = false;
            BackColor = Color.White;
            Font = new Font("Microsoft YaHei UI", 9f);

            // ---------------- 第 1 页：欢迎 ----------------
            _pageWelcome = NewPage();
            AddTitle(_pageWelcome, "欢迎使用 " + Program.AppTitle + " 安装向导");
            AddText(_pageWelcome,
                "这个向导会把「" + Program.AppTitle + "」安装到你的电脑上。\r\n\r\n" +
                "· 安装完成后，可以从桌面或「开始菜单」启动它；\r\n" +
                "· 随时可以在「设置 → 应用」里卸载；\r\n" +
                "· 程序为单文件自包含，不修改系统里的 QQ 本身。",
                18, 70, 560, 190);
            Controls.Add(_pageWelcome);

            // ---------------- 第 2 页：选择安装位置 ----------------
            _pageDir = NewPage();
            AddTitle(_pageDir, "选择安装位置");
            AddText(_pageDir, "程序将被安装到下面这个文件夹：", 18, 64, 560, 22);

            _path = new TextBox();
            _path.Text = defaultDir;
            _path.Location = new Point(18, 88);
            _path.Size = new Size(440, 24);
            _pageDir.Controls.Add(_path);

            Button browse = new Button();
            browse.Text = "浏览…";
            browse.Location = new Point(466, 87);
            browse.Size = new Size(96, 26);
            browse.Click += delegate
            {
                using (FolderBrowserDialog dlg = new FolderBrowserDialog())
                {
                    dlg.Description = "选择安装位置";
                    dlg.ShowNewFolderButton = true;
                    try { dlg.SelectedPath = _path.Text.Trim(); } catch { }
                    if (dlg.ShowDialog(this) == DialogResult.OK) _path.Text = dlg.SelectedPath;
                }
            };
            _pageDir.Controls.Add(browse);

            AddText(_pageDir,
                "需要约 90 MB 磁盘空间（程序本体 + 解包后的运行文件）。\r\n" +
                "如果选的是桌面、下载、文档这类文件夹，程序会自动在里面再建一个「" + Program.AppTitle + "」子文件夹。",
                18, 126, 560, 60);

            _err = new Label();
            _err.ForeColor = Color.Firebrick;
            _err.AutoSize = false;
            _err.Location = new Point(18, 196);
            _err.Size = new Size(560, 60);
            _err.Text = "";
            _pageDir.Controls.Add(_err);
            Controls.Add(_pageDir);

            // ---------------- 第 3 页：安装中 ----------------
            _pageBusy = NewPage();
            AddTitle(_pageBusy, "正在安装…");
            _bar = new ProgressBar();
            _bar.Location = new Point(18, 100);
            _bar.Size = new Size(560, 22);
            _bar.Minimum = 0;
            _bar.Maximum = 100;
            _pageBusy.Controls.Add(_bar);
            _status = new Label();
            _status.AutoSize = false;
            _status.Location = new Point(18, 136);
            _status.Size = new Size(560, 60);
            _status.Text = "正在准备…";
            _pageBusy.Controls.Add(_status);
            Controls.Add(_pageBusy);

            // ---------------- 第 4 页：完成 ----------------
            _pageDone = NewPage();
            AddTitle(_pageDone, "安装完成");
            AddText(_pageDone, "「" + Program.AppTitle + "」已经安装到：", 18, 64, 560, 22);
            _donePath = new Label();
            _donePath.AutoSize = false;
            _donePath.Location = new Point(18, 88);
            _donePath.Size = new Size(560, 44);
            _donePath.Font = new Font("Microsoft YaHei UI", 9f, FontStyle.Bold);
            _pageDone.Controls.Add(_donePath);
            AddText(_pageDone, "桌面和「开始菜单」里都已经放好快捷方式。", 18, 138, 560, 22);
            _runNow = new CheckBox();
            _runNow.Text = "立即运行 " + Program.AppTitle;
            _runNow.Checked = true;
            _runNow.AutoSize = true;
            _runNow.Location = new Point(18, 176);
            _pageDone.Controls.Add(_runNow);
            Controls.Add(_pageDone);

            // ---------------- 底部按钮 ----------------
            _btnBack = new Button();
            _btnBack.Text = "< 上一步";
            _btnBack.Size = new Size(96, 30);
            _btnBack.Location = new Point(288, 350);
            _btnBack.Click += delegate { ShowPage(_page - 1); };
            Controls.Add(_btnBack);

            _btnNext = new Button();
            _btnNext.Text = "下一步 >";
            _btnNext.Size = new Size(96, 30);
            _btnNext.Location = new Point(392, 350);
            _btnNext.Click += delegate { OnNext(); };
            Controls.Add(_btnNext);

            _btnCancel = new Button();
            _btnCancel.Text = "取消";
            _btnCancel.Size = new Size(96, 30);
            _btnCancel.Location = new Point(496, 350);
            _btnCancel.Click += delegate
            {
                if (_installing) return;
                DialogResult = DialogResult.Cancel;
                Close();
            };
            Controls.Add(_btnCancel);

            AcceptButton = _btnNext;
            CancelButton = _btnCancel;
            // 页面面板是 Dock=Top，按钮必须压在它上面（否则可能被面板遮住）
            _btnBack.BringToFront();
            _btnNext.BringToFront();
            _btnCancel.BringToFront();
            ShowPage(0);
        }

        private Panel NewPage()
        {
            Panel p = new Panel();
            p.Dock = DockStyle.Top;
            p.Height = 330;
            p.BackColor = Color.White;
            p.Visible = false;
            return p;
        }

        private void AddTitle(Control page, string text)
        {
            Label l = new Label();
            l.Text = text;
            l.AutoSize = false;
            l.Location = new Point(18, 20);
            l.Size = new Size(560, 34);
            l.Font = new Font("Microsoft YaHei UI", 13f, FontStyle.Bold);
            page.Controls.Add(l);
        }

        private void AddText(Control page, string text, int x, int y, int width, int height)
        {
            Label l = new Label();
            l.Text = text;
            l.AutoSize = false;
            l.Location = new Point(x, y);
            l.Size = new Size(width, height);
            page.Controls.Add(l);
        }

        private void ShowPage(int index)
        {
            if (index < 0 || index > 3) return;
            _page = index;
            _pageWelcome.Visible = index == 0;
            _pageDir.Visible = index == 1;
            _pageBusy.Visible = index == 2;
            _pageDone.Visible = index == 3;

            _btnBack.Visible = index == 1;
            _btnCancel.Visible = index <= 1;
            _btnNext.Visible = index != 2;

            if (index == 0) _btnNext.Text = "下一步 >";
            else if (index == 1) _btnNext.Text = "安装";
            else if (index == 3) { _btnNext.Text = "完成"; _btnNext.Location = new Point(496, 350); }

            if (index == 1) _path.Focus();
            if (index == 3) _btnNext.Focus();
        }

        private void OnNext()
        {
            if (_page == 0) { ShowPage(1); return; }
            if (_page == 1) { StartInstall(); return; }
            if (_page == 3) { DialogResult = DialogResult.OK; Close(); }
        }

        private void StartInstall()
        {
            string dir = _path.Text.Trim();
            if (dir.Length == 0) { _err.Text = "请填写安装位置。"; return; }
            try
            {
                Directory.CreateDirectory(dir);
            }
            catch (Exception ex)
            {
                _err.Text = "无法创建这个文件夹：" + ex.Message +
                            "\r\n请换一个你有写入权限的位置（例如 C:\\Users\\你的用户名\\AppData\\Local\\Programs）。";
                return;
            }
            _err.Text = "";
            SelectedDir = dir;

            byte[] payload = null;
            string hash = "";
            try
            {
                payload = Program.ReadPayloadForInstall();
                hash = Program.HashForInstall(payload);
            }
            catch (Exception ex)
            {
                _err.Text = "读取安装数据失败：" + ex.Message;
                return;
            }

            _installing = true;
            ShowPage(2);
            _bar.Value = 0;
            _status.Text = "正在准备…";

            Thread t = new Thread(delegate ()
            {
                try
                {
                    string installed = Program.InstallTo(dir, payload, hash, delegate (int pct, string text)
                    {
                        try
                        {
                            BeginInvoke((MethodInvoker)delegate
                            {
                                _bar.Value = Math.Max(0, Math.Min(100, pct));
                                _status.Text = text;
                            });
                        }
                        catch { }
                    });
                    InstalledExePath = installed;
                    try
                    {
                        BeginInvoke((MethodInvoker)delegate
                        {
                            _installing = false;
                            _donePath.Text = string.IsNullOrEmpty(installed) ? dir : installed;
                            ShowPage(3);
                        });
                    }
                    catch { }
                }
                catch (Exception ex)
                {
                    string msg = ex.Message;
                    try
                    {
                        BeginInvoke((MethodInvoker)delegate
                        {
                            _installing = false;
                            ShowPage(1);
                            _err.Text = "安装失败：" + msg +
                                        "\r\n如果是「拒绝访问」，请换一个可写目录，或右键本程序选择「以管理员身份运行」。";
                        });
                    }
                    catch { }
                }
            });
            t.IsBackground = true;
            t.SetApartmentState(ApartmentState.STA);
            t.Start();
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            if (_installing) { e.Cancel = true; return; }
            base.OnFormClosing(e);
        }
    }
}
