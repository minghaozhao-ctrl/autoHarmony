# -*- coding: utf-8 -*-
"""后台默认日志：把 stdout/stderr 同时追加写入日志文件（无需传任何参数）。

设计目标：不改变既有终端可读输出（仍原样打印），同时在后台把每次运行的
完整输出（含异常 traceback，因为 stderr 也一起进文件）追加落盘，方便事后
定位问题（弹窗误判、自愈重跑、阻塞、崩溃堆栈等）而无需反复真机操作。

默认日志路径：~/.hmuitest/logs/uitest-YYYYMMDD.log  （自动建目录、按天追加）
环境变量覆盖：
  HMUITEST_LOG_DIR    日志目录（默认 ~/.hmuitest/logs）
  HMUITEST_LOG_FILE   完整日志文件路径（优先级最高）
  HMUITEST_LOG        设 0/off/false/no 关闭文件落盘（仅保留终端输出）

用法（在 CLI 入口 main() 的**最开头**调用一次）：
    from engines.logger import init_logger   # hm 侧
    from logger import init_logger           # skill 侧（扁平）
    init_logger("autoharmony")
"""

import os
import sys
import time
from datetime import datetime

_DEFAULT_SUBDIR = "logs"

# 已初始化后记录日志文件路径，避免重复初始化/重复开文件
_ACTIVE = []
# 当前日志目录（archive_dump 归档用的根目录）
_LOG_DIR = None


class _Tee:
    """同时写终端（原样，不加前缀）与日志文件（每行行首带时间戳）。

    通过替换 sys.stdout / sys.stderr 实现；print / traceback / argparse
    的一切输出都会经由此对象转发，终端可读性不变，文件里带时间戳便于定位。
    """

    def __init__(self, orig, fh):
        self._orig = orig
        self._fh = fh
        self._at_line_start = True

    def write(self, s):
        try:
            self._orig.write(s)
        except Exception:
            pass
        try:
            if self._at_line_start and s:
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                self._fh.write("[%s] " % ts)
            self._fh.write(s)
            self._at_line_start = s.endswith(("\n", "\r"))
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass
        try:
            self._fh.flush()
        except Exception:
            pass

    def isatty(self):
        return False


def _default_log_file():
    d = os.environ.get("HMUITEST_LOG_DIR") or os.path.join(
        os.path.expanduser("~"), ".hmuitest", _DEFAULT_SUBDIR)
    return os.path.join(d, "uitest-%s.log" % datetime.now().strftime("%Y%m%d"))


def _cmdline_summary():
    """从 sys.argv 提取：程序名、完整参数、设备、页面定位线索。

    全部为纯静态字符串解析，不做任何 hdc 调用（零额外耗时）。
    页面标识取自命令自身的期望参数（--expect-route/--expect-text/...），
    代表“本次命令期望断言目标页面”；若命令不携带页面期望则留空。
    """
    argv = sys.argv
    prog = os.path.basename(argv[0]) if argv else "uitest"
    args = argv[1:]
    argline = " ".join(args)

    dev = os.environ.get("HARMONY_DEVICE_ID", "")
    for i, a in enumerate(args):
        if a in ("-d", "--device") and i + 1 < len(args):
            dev = args[i + 1]
            break

    hints = []
    for i, a in enumerate(args):
        if a.startswith("--expect") and i + 1 < len(args):
            hints.append("%s=%s" % (a, args[i + 1]))
    return prog, argline, dev, " ".join(hints)


def init_logger(prog="uitest", label=None):
    """在 CLI 入口最开头调用：默认后台把 stdout/stderr 追加写入日志文件。

    返回日志文件绝对路径（字符串）；若未启用文件日志则返回 None。
    重复调用安全（幂等：第二次直接返回已打开的日志路径）。
    """
    if _ACTIVE:
        return _ACTIVE[0]

    flag = os.environ.get("HMUITEST_LOG", "1").strip().lower()
    if flag in ("0", "off", "false", "no"):
        return None

    logfile = os.environ.get("HMUITEST_LOG_FILE") or _default_log_file()
    try:
        d = os.path.dirname(logfile)
        if d:
            os.makedirs(d, exist_ok=True)
        fh = open(logfile, "a", encoding="utf-8", errors="replace")
    except Exception:
        return None

    global _LOG_DIR
    _LOG_DIR = d or os.path.join(os.path.expanduser("~"), ".hmuitest", _DEFAULT_SUBDIR)

    who = label or prog
    fh.write("\n===== %s 会话开始 %s | pid=%d | cwd=%s =====\n"
             % (who, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                os.getpid(), os.getcwd()))

    _prog, _argline, _dev, _pagehint = _cmdline_summary()
    fh.write("[cmd]  %s %s\n" % (_prog, _argline))
    if _dev:
        fh.write("[dev]  %s\n" % _dev)
    if _pagehint:
        fh.write("[page] %s\n" % _pagehint)
    fh.flush()

    for name in ("stdout", "stderr"):
        orig = getattr(sys, name)
        setattr(sys, name, _Tee(orig, fh))

    _ACTIVE.append(logfile)
    return logfile


def archive_dump(local_json_path, tag="layout"):
    """归档一份已有的页面 dump JSON 到日志目录（复用产物，零额外 dump）。

    仅当文件日志已启用（init_logger 成功）且有源文件时复制；
    目标是 <日志目录>/dump/<HHMMSS>-<tag>.json。复制失败静默降级（返回 None），
    绝不影响主流程。
    """
    if not _LOG_DIR or not local_json_path:
        return None
    try:
        if not os.path.isfile(local_json_path):
            return None
        dump_dir = os.path.join(_LOG_DIR, "dump")
        os.makedirs(dump_dir, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S.%f")[:-3]
        dst = os.path.join(dump_dir, "%s-%s.json" % (ts, (tag or "layout")))
        import shutil
        shutil.copyfile(local_json_path, dst)
        return dst
    except Exception:
        return None
