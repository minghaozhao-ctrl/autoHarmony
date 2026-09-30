#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""失败留证（failure artifacts）

UI 动作失败时把"当时现场"落盘，便于事后/离线定位而无需复现：
  - 截屏 PNG（hdc uitest screenCap + file recv）
  - 设备日志切片（hilog，保留末尾若干行）
  - 崩溃详情（可选 hidumper -e）
  - failure.json（裁决原因 + 各产物路径 + 设备/日志路径）

目录：<日志目录>/artifacts/<YYYYMMDD>/<HHMMSS.mmm>-pid<pid>-<tag>/
默认开启；HMUITEST_ARTIFACTS=0/off/false/no 关闭（CLI 侧 --no-artifacts 同义）。
留证失败一律吞掉：绝不影响主流程，也不改变退出码。
总耗时受 TOTAL_BUDGET_SEC 约束（默认 12s），持续输出型命令（hilog）靠短超时截断并保留已捕获的部分输出。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

from engines.logger import current_log_path, log_dir_path, log_line
from utils.common import run_hdc_command

# hilog 切片保留的最大行数（尾部为最近日志）
HILOG_TAIL_LINES = 300

# 各产物超时（秒）：留证是"尽力而为"，绝不能拖慢主流程
SCREENSHOT_TIMEOUT_SEC = 6
HILOG_TIMEOUT_SEC = 2
FAULT_TIMEOUT_SEC = 8
# 留证总耗时预算（秒）：超出后放弃剩余产物
TOTAL_BUDGET_SEC = 12

# hilog 行首时间戳（MM-DD HH:MM:SS.mmm），用于过滤命令错误输出
_HILOG_LINE_RE = re.compile(r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}")

_disabled = False


def enabled() -> bool:
    """留证是否启用（环境变量可关闭）。"""
    if _disabled:
        return False
    flag = os.environ.get("HMUITEST_ARTIFACTS", "1").strip().lower()
    return flag not in ("0", "off", "false", "no")


def disable() -> None:
    """进程内关闭留证（CLI --no-artifacts 调用）。"""
    global _disabled
    _disabled = True


def _safe(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "failure")
    return cleaned[:60] or "failure"


def _run_dir(tag: str) -> str:
    base = log_dir_path() or os.path.join(os.path.expanduser("~"), ".hmuitest")
    day = datetime.now().strftime("%Y%m%d")
    ts = datetime.now().strftime("%H%M%S.%f")[:-3]
    path = os.path.join(base, "artifacts", day,
                        "%s-pid%d-%s" % (ts, os.getpid(), _safe(tag)))
    os.makedirs(path, exist_ok=True)
    return path


def _hdc(device: Optional[str], args: List[str]) -> List[str]:
    cmd = ["hdc"]
    if device:
        cmd += ["-t", device]
    return cmd + args


def _run_hdc_capture(args: List[str], timeout: float) -> str:
    """执行 hdc 命令并返回输出；超时则杀掉进程并返回已捕获的部分输出。

    持续输出型命令（如不带 -x 的 hilog）不会自行退出，靠超时截断，
    已写入管道缓冲的日志仍可保留，避免"空手而归 + 长时间阻塞"。
    """
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
    except Exception:
        return ""
    try:
        out, _ = proc.communicate(timeout=timeout)
        return out or ""
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = ""
        return out or ""
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return ""


def _looks_like_hilog(out: str) -> bool:
    """输出中是否含 hilog 日志行（过滤 hilog 不支持某参数时的报错文本）。"""
    for line in out.splitlines():
        if _HILOG_LINE_RE.match(line.strip()):
            return True
    return False


def capture_screenshot(device: Optional[str], out_path: str,
                       timeout: float = SCREENSHOT_TIMEOUT_SEC) -> Optional[str]:
    """截屏到 out_path；成功返回路径，失败返回 None。"""
    remote = "/data/local/tmp/_screenshot_tmp.png"
    ok, _ = run_hdc_command(
        _hdc(device, ["shell", "uitest", "screenCap", "-p", remote]), max(1, int(timeout)))
    if not ok:
        return None
    ok2, _ = run_hdc_command(_hdc(device, ["file", "recv", remote, out_path]),
                             max(1, int(timeout)))
    if ok2 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    return None


def _capture_hilog(device: Optional[str], out_path: str,
                   timeout: float = HILOG_TIMEOUT_SEC) -> Optional[str]:
    """抓取 hilog 末尾切片。

    优先 `hilog -x`（导出缓冲区后自行退出，正常 <1s）；老设备不支持时回退到
    持续输出模式 `hilog`，靠短超时截断并保留已捕获的部分日志。
    最长约 2×timeout，避免失败留证拖慢主流程。
    """
    for args in (["shell", "hilog", "-x"], ["shell", "hilog"]):
        out = _run_hdc_capture(_hdc(device, args), timeout)
        if out and _looks_like_hilog(out):
            lines = out.splitlines()[-HILOG_TAIL_LINES:]
            with open(out_path, "w", encoding="utf-8", errors="replace") as f:
                f.write("\n".join(lines) + "\n")
            return out_path
    return None


def _capture_fault(device: Optional[str], out_path: str,
                   timeout: float = FAULT_TIMEOUT_SEC) -> Optional[str]:
    ok, out = run_hdc_command(_hdc(device, ["shell", "hidumper", "-e"]),
                              max(1, int(timeout)))
    if ok and out:
        with open(out_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(out)
        return out_path
    return None


def capture_failure(device: Optional[str], reason: str = "",
                    analyzer=None, crash: bool = False,
                    tag: str = "failure",
                    extra: Optional[Dict] = None) -> Optional[Dict]:
    """把一次失败的现场落盘。

    Args:
        device: 目标设备序列号
        reason: 失败原因（写进 failure.json）
        analyzer: 可选的控件树分析器，用于归档当前 dump
        crash: 为 True 时额外抓取 hidumper -e 崩溃详情
        tag: 产物目录名后缀
        extra: 附加到 failure.json 的字段

    Returns:
        产物路径字典；未启用或出错时 None。
    """
    if not enabled():
        return None
    try:
        run_dir = _run_dir(tag)
        found: Dict[str, Optional[str]] = {}
        deadline = time.monotonic() + TOTAL_BUDGET_SEC
        # 进度提示走 stderr 并 flush，避免管道缓冲造成"卡死"观感
        print("📦 保存失败现场（截图/日志）...", file=sys.stderr, flush=True)

        def _left() -> float:
            return deadline - time.monotonic()

        if _left() > 0:
            found["screenshot"] = capture_screenshot(
                device, os.path.join(run_dir, "screenshot.png"),
                min(SCREENSHOT_TIMEOUT_SEC, _left()))
            if found["screenshot"]:
                # 截图成功 → 单独打印文件路径，方便直接查看/引用
                print("📸 截图: %s" % found["screenshot"],
                      file=sys.stderr, flush=True)
        if _left() > 0:
            found["hilog"] = _capture_hilog(
                device, os.path.join(run_dir, "hilog.txt"),
                min(HILOG_TIMEOUT_SEC, _left()))
        if crash and _left() > 0:
            found["faultlog"] = _capture_fault(
                device, os.path.join(run_dir, "hidumper-e.txt"),
                min(FAULT_TIMEOUT_SEC, _left()))

        dump_src = (getattr(analyzer, "_temp_file", None)
                    or getattr(analyzer, "json_file", None))
        if dump_src and os.path.isfile(dump_src):
            try:
                dst = os.path.join(run_dir, "widget_tree.json")
                shutil.copyfile(dump_src, dst)
                found["dump"] = dst
            except Exception:
                pass

        log_path = current_log_path()
        info: Dict = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "device": device,
            "reason": reason,
            "tag": tag,
            "log_path": log_path,
            "artifacts": {k: v for k, v in found.items() if v},
        }
        if extra:
            info.update(extra)
        info_path = os.path.join(run_dir, "failure.json")
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        found["failure_json"] = info_path

        log_line("[artifact] failure dir=%s reason=%s" % (run_dir, reason))
        # 走 stderr：保持 stdout（尤其 --json）尽量干净
        print("📦 失败留证: %s" % run_dir, file=sys.stderr)
        return found
    except Exception as e:
        try:
            log_line("[err] capture_failure: %s" % e)
        except Exception:
            pass
        return None
