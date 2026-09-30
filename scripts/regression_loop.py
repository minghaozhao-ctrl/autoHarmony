#!/usr/bin/env python3
"""
scripts/regression_loop.py

迭代回归循环辅助脚本：主 agent 改完 bug 后调用此脚本跑 pytest + 解析报告 + 输出建议

用法：
  python3 ${SKILL_DIR}/scripts/regression_loop.py [--max-rounds N]

设计目标：
- 一键跑 pytest + 解析 + 总结（不替代主 agent 改 bug 决策）
- 安全阀：N 轮后强制停止
- 输出：哪些 pass / 哪些 fail / 失败原因 / 下一步建议

注意：
- 主 agent 改 bug 是手动的，脚本不自动改代码
- 脚本只负责"跑测试 + 报告 + 建议"
- 终止条件：全部 PASS / 达到 N 轮 / 用户主动中断
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

def _find_project_root() -> Path:
    """向上查找 HarmonyOS 工程根（含 oh-package.json5 / build-profile.json5 的目录）

    skill 可能随仓库迁移（如 .trae/skills/<skill>/scripts/），不能依赖固定层级回溯。
    """
    cur = Path(__file__).resolve().parent
    for candidate in [cur, *cur.parents]:
        if (candidate / "oh-package.json5").exists() \
                or (candidate / "build-profile.json5").exists():
            return candidate
    raise SystemExit(
        f"❌ 未找到工程根（oh-package.json5/build-profile.json5），"
        f"起始路径: {cur}"
    )


_PROJECT_ROOT_CACHE: list = []


def _project_root() -> Path:
    """惰性定位工程根（进程内缓存）——不在模块导入时执行：
    skill 位于 ~/.config 下时无 oh-package.json5，导入即 SystemExit 会
    阻断 --help 等所有入口

    优先级：HMUITEST_PROJECT_ROOT 环境变量 > --project-root 参数 >
    向上找 oh-package.json5/build-profile.json5
    """
    if not _PROJECT_ROOT_CACHE:
        env = os.environ.get("HMUITEST_PROJECT_ROOT")
        if env:
            _PROJECT_ROOT_CACHE.append(Path(env))
        else:
            _PROJECT_ROOT_CACHE.append(_find_project_root())
    return _PROJECT_ROOT_CACHE[0]


def run_pytest(case: str = None) -> dict:
    """跑 pytest 并解析结果（case 非空时用 -k 过滤指定用例）"""
    root = _project_root()
    tests_dir = root / "tests" / "uitest"
    cmd = [
        "pytest",
        str(tests_dir),
        "--tb=short",
        "-q",
    ]
    if case:
        cmd += ["-k", case]

    print(f"\n{'=' * 70}")
    print(f"🚀 [regression_loop] 跑 pytest")
    print(f"{'=' * 70}")
    print(f"命令：{' '.join(cmd)}\n")

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=_project_root(),
    )

    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def parse_pytest_output(output: str) -> dict:
    """解析 pytest 输出，提取 PASS/FAIL 信息"""
    # 解析总结行："5 passed in 2.34s" / "3 failed, 2 passed in 5.67s" /
    # "N passed, 1 error"（errors? 支持单数输出）
    summary_pattern = re.compile(
        r"(?P<passed>\d+)\s+passed|"
        r"(?P<failed>\d+)\s+failed|"
        r"(?P<skipped>\d+)\s+skipped|"
        r"(?P<errors>\d+)\s+errors?"
    )

    passed = failed = skipped = errors = 0
    for match in summary_pattern.finditer(output):
        if match.group("passed"):
            passed = int(match.group("passed"))
        elif match.group("failed"):
            failed = int(match.group("failed"))
        elif match.group("skipped"):
            skipped = int(match.group("skipped"))
        elif match.group("errors"):
            errors = int(match.group("errors"))

    # 解析失败的 test 名称
    failed_tests = []
    fail_pattern = re.compile(r"^FAILED\s+(.+)$", re.MULTILINE)
    for match in fail_pattern.finditer(output):
        failed_tests.append(match.group(1).strip())

    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "errors": errors,
        "total": passed + failed + skipped + errors,
        "failed_tests": failed_tests,
        "all_passed": failed == 0 and errors == 0 and passed > 0,
    }


def extract_failure_reason(output: str) -> dict:
    """提取失败原因（每个失败用例的错误信息）"""
    failures = {}
    # 匹配 FAILED test_xxx - error_msg 格式
    pattern = re.compile(
        r"FAILED\s+(?P<name>\S+)\s+-\s+(?P<reason>.*?)(?=\n(?:FAILED|PASSED|=+))",
        re.DOTALL,
    )
    for match in pattern.finditer(output):
        name = match.group("name")
        reason = match.group("reason").strip()[:500]  # 截断
        failures[name] = reason
    return failures


def print_summary(parsed: dict, failure_reasons: dict) -> None:
    """打印本轮结果摘要"""
    print(f"\n{'=' * 70}")
    print(f"📊 [regression_loop] 本轮结果")
    print(f"{'=' * 70}")
    print(f"通过：{parsed['passed']}  失败：{parsed['failed']}  跳过：{parsed['skipped']}  错误：{parsed['errors']}")
    print(f"总计：{parsed['total']}")

    if parsed["all_passed"]:
        print(f"\n🎉 全部 PASS！开发-测试-修复闭环完成 ✅")
        return

    if parsed["failed_tests"]:
        print(f"\n❌ 失败用例：")
        for test in parsed["failed_tests"]:
            reason = failure_reasons.get(test, "无错误详情")
            print(f"\n  • {test}")
            print(f"    原因：{reason.split(chr(10))[0][:200]}")


def print_advice(parsed: dict, round_num: int, max_rounds: int) -> None:
    """打印下一步建议（基于本轮失败）"""
    if parsed["all_passed"]:
        return

    print(f"\n{'=' * 70}")
    print(f"💡 [regression_loop] 下一步建议")
    print(f"{'=' * 70}")

    print(f"\n当前轮次：{round_num}/{max_rounds}")

    if round_num >= max_rounds:
        print(f"\n⚠️  已达最大轮次（{max_rounds}），强制停止。")
        print(f"建议人介入排查：")
        print(f"  - 是不是 UI 整体大改？→ 调 subagent 重新探索")
        print(f"  - 是不是测试用例本身有歧义？→ 检查 cases.md")
        print(f"  - 是不是测试环境问题？→ 检查 HDC 设备连接")
        return

    print(f"\n主 agent 应执行：")
    print(f"\n  1. 看 conftest.py hook 自动输出的正反例对照")
    print(f"     （已通过 pytest stderr 输出）")
    print(f"\n  2. 判断失败原因（3 类）：")
    print(f"     A. 业务代码 bug → 改业务代码 → 再跑本脚本")
    print(f"     B. UI 改 id/text → 调 subagent 重跑该条")
    print(f"     C. 脚本硬编码过期 → 改 tests/uitest/*.py → 再跑")

    print(f"\n  3. 跑下一轮：")
    try:
        print(f"     python3 {Path(__file__).relative_to(_project_root())}")
    except ValueError:
        # skill 与工程根不在同一目录树（全局安装 + 指定工程根）时退回绝对路径
        print(f"     python3 {Path(__file__).resolve()}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="迭代回归循环：跑 pytest + 解析 + 输出建议"
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=5,
        help="最大轮次（防止无限循环，默认 5）",
    )
    parser.add_argument(
        "--case",
        type=str,
        default=None,
        help="只跑指定用例（pytest -k），不指定跑全部",
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="工程根目录（默认自动向上找 oh-package.json5，或设 HMUITEST_PROJECT_ROOT）",
    )
    args = parser.parse_args()
    if args.project_root:
        os.environ["HMUITEST_PROJECT_ROOT"] = args.project_root

    print(f"\n{'#' * 70}")
    print(f"# 迭代回归循环 - 测试运行")
    print(f"# 项目根：{_project_root()}")
    print(f"# 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"# 最大轮次：{args.max_rounds}")
    if args.case:
        print(f"# 指定用例：{args.case}")
    print(f"{'#' * 70}\n")

    for round_num in range(1, args.max_rounds + 1):
        print(f"\n\n{'▶' * 35}")
        print(f"▶  第 {round_num}/{args.max_rounds} 轮")
        print(f"{'▶' * 35}")

        result = run_pytest(case=args.case)
        parsed = parse_pytest_output(result["stdout"] + result["stderr"])
        failure_reasons = extract_failure_reason(result["stdout"] + result["stderr"])

        print_summary(parsed, failure_reasons)
        print_advice(parsed, round_num, args.max_rounds)

        if parsed["all_passed"]:
            print(f"\n\n{'🎉' * 35}")
            print(f"  🎉  全部 PASS，迭代完成！")
            print(f"{'🎉' * 35}\n")
            return 0

        if round_num >= args.max_rounds:
            print(f"\n\n⚠️ 达到最大轮次 {args.max_rounds}，停止。")
            return 1

        print(f"\n⏸  等待主 agent 改 bug 后跑下一轮...")
        if not args.case:
            # 自动重跑模式：等 5 秒
            print(f"  （自动重跑模式：5 秒后自动跑下一轮，Ctrl+C 中止）")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                print(f"\n用户中断")
                return 2
        else:
            # 指定用例模式：跑完就退出，让主 agent 决定下一步
            # 用 all_passed（含 passed>0 守卫）：--case 拼写错误/用例已改名时
            # pytest -k 无匹配（exit 5, "no tests ran"）→ 假绿会被误判回归通过
            return 0 if parsed["all_passed"] else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
