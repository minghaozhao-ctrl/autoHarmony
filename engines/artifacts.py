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
"""

import json
import os
import re
import shutil
import sys
from datetime import datetime
from typing import Dict, List, Optional

from engines.logger import current_log_path, log_dir_path, log_line
from utils.common import run_hdc_command

# hilog 切片保留的最大行数（尾部为最近日志）
HILOG_TAIL_LINES = 300

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


def capture_screenshot(device: Optional[str], out_path: str) -> Optional[str]:
    """截屏到 out_path；成功返回路径，失败返回 None。"""
    remote = "/data/local/tmp/_screenshot_tmp.png"
    ok, _ = run_hdc_command(
        _hdc(device, ["shell", "uitest", "screenCap", "-p", remote]), 30)
    if not ok:
        return None
    ok2, _ = run_hdc_command(_hdc(device, ["file", "recv", remote, out_path]), 30)
    if ok2 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    return None


def _capture_hilog(device: Optional[str], out_path: str) -> Optional[str]:
    for args in (["shell", "hilog", "-x"], ["shell", "hilog"]):
        ok, out = run_hdc_command(_hdc(device, args), 20)
        if ok and out:
            lines = out.splitlines()[-HILOG_TAIL_LINES:]
            with open(out_path, "w", encoding="utf-8", errors="replace") as f:
                f.write("\n".join(lines) + "\n")
            return out_path
    return None


def _capture_fault(device: Optional[str], out_path: str) -> Optional[str]:
    ok, out = run_hdc_command(_hdc(device, ["shell", "hidumper", "-e"]), 60)
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
        found: Dict[str, Optional[str]] = {
            "screenshot": capture_screenshot(
                device, os.path.join(run_dir, "screenshot.png")),
            "hilog": _capture_hilog(device, os.path.join(run_dir, "hilog.txt")),
        }
        if crash:
            found["faultlog"] = _capture_fault(
                device, os.path.join(run_dir, "hidumper-e.txt"))

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
