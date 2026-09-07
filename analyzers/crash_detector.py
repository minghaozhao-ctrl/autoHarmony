#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闪退检测器

通过 pidof 进程存活检查（~0.1s）和 faultlog 文件变化检测闪退。
hidumper -e（13s+）仅在确认闪退后用于获取崩溃详情。
"""
from typing import List, Optional
from utils.common import run_hdc_command


class CrashCheckResult:
    """闪退检测结果"""

    def __init__(self):
        self.crashed: bool = False
        self.hidumper_records: List[str] = []
        self.faultlog_files: List[str] = []
        self.internal_restart: bool = False

    def summary(self) -> str:
        parts = []
        if self.crashed:
            parts.append("⚠️ 检测到异常")
        if self.internal_restart:
            parts.append("⚠️ 检测到 App 内部重启")
        if self.hidumper_records:
            parts.append(f"hidumper: {len(self.hidumper_records)} 条")
        if self.faultlog_files:
            parts.append(f"faultlog: {len(self.faultlog_files)} 个")
        return "; ".join(parts) if parts else "✅ 正常"


class CrashDetector:
    """闪退检测器

    自动维护基线，通过 pidof 进程存活检查（快）和 faultlog 文件变化检测闪退。
    hidumper -e（慢）仅在确认闪退后用于获取崩溃详情。
    """

    CRASH_KEYWORDS = [
        "CRASH", "Fatal", "signal", "backtrace",
        "SIGSEGV", "SIGABRT", "SIGBUS", "SIGFPE",
        "js crash", "cpp crash", "appfreeze",
        "native_crash", "jscrash",
    ]

    def __init__(self, device: Optional[str] = None,
                 bundle: str = "com.cmcc.DigitalHome"):
        self.device = device
        self.bundle = bundle
        self._baseline_app_alive: Optional[bool] = None
        self._baseline_faultlog_count: int = 0
        self._baseline_max_acc: int = 0
        self._last_result: Optional[CrashCheckResult] = None

    def _hdc_cmd(self, cmd_parts: List[str], timeout: int = 30) -> tuple:
        cmd = ["hdc"]
        if self.device:
            cmd.extend(["-t", self.device])
        cmd.extend(cmd_parts)
        return run_hdc_command(cmd, timeout)

    def _is_app_alive(self) -> bool:
        """检查 App 进程是否存活（~0.1s，远快于 hidumper -e 的 13s+）"""
        success, output = self._hdc_cmd(["shell", "pidof", self.bundle], timeout=5)
        return success and bool(output.strip())

    def _init_baseline(self):
        if self._baseline_app_alive is None:
            self._baseline_app_alive = self._is_app_alive()
            self._baseline_faultlog_count = self._count_faultlog_files()

    def prime_baseline(self) -> None:
        """主动建立基线

        在测试/操作开始时调用，确保后续 detect() 能正确发现"新建基线之后"发生的崩溃。
        若不主动调用，detect() 会惰性建基线并立即对比，导致已发生的崩溃被吞掉（漏报）。
        """
        self._init_baseline()

    def _count_faultlog_files(self) -> int:
        success, output = self._hdc_cmd(
            ["shell", "ls", "/data/log/faultlog/faultlogger/"], timeout=10
        )
        if not success:
            return 0
        return len([f for f in output.strip().split('\n') if f.strip()])

    @staticmethod
    def _extract_max_acc(widgets) -> int:
        max_acc = 0
        for w in widgets:
            acc = w.get('accessibilityId', '')
            if isinstance(acc, str) and acc.isdigit():
                max_acc = max(max_acc, int(acc))
            elif isinstance(acc, int) and acc > 0:
                max_acc = max(max_acc, acc)
        return max_acc

    def record_acc(self, widgets) -> None:
        """记录 accessibilityId 基线（从控件树 widgets 中提取最大值）

        ArkUI accessibilityId 单调递增不回收，正常情况下只会增不会降。
        若后续 dump 发现 acc 骤降 → App 内部重启（同 pid 但 UI 重建）。
        """
        max_acc = self._extract_max_acc(widgets)
        if max_acc > 0:
            self._baseline_max_acc = max_acc

    def detect_internal_restart(self, widgets) -> bool:
        """检测 App 内部重启（pidof 检测不到的盲区）

        pidof 只检查进程是否存活，无法发现同 pid 的内部重启（如 ArkUI 引擎
        崩溃后自动恢复）。但 acc 会从高位骤降到低位（重启后从头分配）。
        """
        if self._baseline_max_acc < 100:
            return False
        current_max = self._extract_max_acc(widgets)
        if current_max > 0 and current_max < self._baseline_max_acc * 0.1:
            return True
        return False

    def has_crashed(self) -> bool:
        if self._last_result is not None:
            return self._last_result.crashed
        result = self.detect()
        self._last_result = result
        return result.crashed

    def detect(self) -> CrashCheckResult:
        self._init_baseline()
        result = CrashCheckResult()

        current_alive = self._is_app_alive()
        if self._baseline_app_alive and not current_alive:
            result.crashed = True
            result.hidumper_records = [
                f"App 进程 {self.bundle} 已退出（基线存活，当前不存在）"
            ]

        current_count = self._count_faultlog_files()
        if current_count > self._baseline_faultlog_count:
            result.faultlog_files = [f"新增 {current_count - self._baseline_faultlog_count} 个文件"]
            result.crashed = True

        self._last_result = result
        return result

    def detect_and_report(self) -> CrashCheckResult:
        result = self.detect()
        if not result.crashed:
            return result

        # 仅在确认闪退时才运行慢速 hidumper -e 获取崩溃详情
        try:
            success, output = self._hdc_cmd(["shell", "hidumper", "-e"], timeout=60)
            if success:
                records = [r for r in output.strip().split('\n') if r.strip()]
                crash_related = [
                    r for r in records
                    if any(kw.lower() in r.lower() for kw in self.CRASH_KEYWORDS)
                ]
                if crash_related:
                    result.hidumper_records = crash_related
        except Exception:
            pass

        print()
        print("=" * 60)
        print("🔍 闪退检测")
        print("=" * 60)
        print(f"结果: {result.summary()}")
        print()

        if result.hidumper_records:
            print("📋 崩溃记录:")
            for rec in result.hidumper_records[:10]:
                print(f"  {rec}")
            if len(result.hidumper_records) > 10:
                print(f"  ... 共 {len(result.hidumper_records)} 条")
            print()

        if result.faultlog_files:
            print("📁 faultlog 文件:")
            for f in result.faultlog_files:
                print(f"  - {f}")
            print()

        print("💡 导出崩溃日志: hdc file recv /data/log/faultlog/faultlogger/ ./crash_logs/")
        print()
        return result
