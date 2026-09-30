"""
conftest.py 模板：含正反例追踪 + 报告增强

subagent 生成 tests/uitest/conftest.py 时使用此模板。
模板来源：${SKILL_DIR}/scripts/conftest_template.py
"""
import json
import os
import pytest
from pathlib import Path
from typing import Optional, Any


# ============================================================
# 1. 探索日志加载（session 级）
# ============================================================

EXPLORE_LOGS_ROOT = Path(__file__).parent / ".explore_logs"


def _load_latest_explore_log() -> Optional[dict]:
    """找最新的 .explore_logs/<用例集名>_<时间戳>/explore_log.json"""
    if not EXPLORE_LOGS_ROOT.exists():
        return None
    candidates = sorted([d for d in EXPLORE_LOGS_ROOT.iterdir() if d.is_dir()], reverse=True)
    if not candidates:
        return None
    log_file = candidates[0] / "explore_log.json"
    if not log_file.exists():
        return None
    try:
        with open(log_file) as f:
            return json.load(f)
    except Exception as e:
        print(f"[警告] 加载探索日志失败：{e}")
        return None


# ============================================================
# 2. 探索日志 fixture（注入到 test 函数）
# ============================================================

@pytest.fixture(scope="session")
def explore_log(request: pytest.FixtureRequest) -> Optional[dict]:
    """注入正反例数据到 test 函数

    用法：
        def test_xxx(uitest_driver, explore_log):
            step = find_step_for_test(explore_log, "test_xxx")
            # step 包含 positive / negatives
    """
    return request.config._explore_log


# ============================================================
# 3. 失败时自动输出正反例对照（pytest hook）
# ============================================================

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """失败时自动输出正反例对照"""
    outcome = yield
    rep = outcome.get_result()

    if rep.when != "call" or not rep.failed:
        return

    log = getattr(item.session.config, "_explore_log", None)
    if not log:
        return

    step = _find_step_for_test(log, item.name)
    if not step:
        return

    # 找探索日志目录
    explore_log_dir = EXPLORE_LOGS_ROOT / log.get("cases_set_name", "default")

    _print_positive_negative_comparison(
        test_name=item.name,
        step=step,
        explore_log_dir=explore_log_dir,
        longrepr=rep.longrepr,
    )


def _find_step_for_test(log: dict, test_name: str) -> Optional[dict]:
    """根据 test 函数名找对应的最后一步"""
    for case in log.get("cases", []):
        if case.get("test_function") == test_name:
            steps = case.get("steps", [])
            if steps:
                return steps[-1]
    return None


def _print_positive_negative_comparison(
    test_name: str,
    step: dict,
    explore_log_dir: Path,
    longrepr: Any,
) -> None:
    """输出正反例对照到 stderr"""
    print(f"\n\n{'=' * 70}", flush=True)
    print(f"❌ 失败用例正反例对照：{test_name}", flush=True)
    print(f"{'=' * 70}", flush=True)

    # 错误信息
    if longrepr:
        print(f"\n🔴 pytest 报错：")
        # 截取关键错误
        err_text = str(longrepr).split("\n")[-3:]
        for line in err_text:
            if line.strip():
                print(f"   {line.strip()}", flush=True)

    # 正例
    positive = step.get("positive") or {}
    print(f"\n✅ 探索时正例（成功策略）：")
    print(f"   操作：{positive.get('input', {})}")
    print(f"   证据 dumpLayout：{explore_log_dir / str(positive.get('dump_layout') or '')}")
    print(f"   证据 screenshot：{explore_log_dir / str(positive.get('screenshot') or '')}")
    print(f"   当时断言：")
    for assertion in positive.get("assertions", []):
        status = "✓" if assertion.get("matched") else "✗"
        print(f"     {status} {assertion.get('kind')} = {assertion.get('value')}")

    # 反例
    negatives = step.get("negatives") or []
    if negatives:
        print(f"\n❌ 探索时反例（失败重试）：")
        for neg in negatives:
            print(f"\n   尝试 {neg.get('attempt')}：{neg.get('input', {})}")
            print(f"     失败原因：{neg.get('reason', '未知')}")
            print(f"     失败证据：{explore_log_dir / neg.get('dump_layout', '')}")
            print(f"     恢复策略：{neg.get('recovery', '无')}")
    else:
        print(f"\n📝 本步骤无反例记录（一次成功）")

    # 建议
    print(f"\n💡 修复建议：")
    print(f"   1. 对照探索时的正例 dumpLayout（路径已上）看现在 UI 变了什么")
    print(f"   2. 如果控件改名/改 id/改位置 → 改脚本里的硬编码")
    print(f"   3. 如果是断言错了 → 改 conftest.py 或脚本里的 assert 逻辑")
    print(f"   4. 如果 UI 整体大改 → 调 subagent 重新探索该条用例")
    print(f"{'=' * 70}\n", flush=True)


# ============================================================
# 4. uitest_driver fixture（HDC + 控件树封装）
# ============================================================

@pytest.fixture(scope="module")
def uitest_driver():
    """HDC 操作 + 控件树分析封装

    用法：
        def test_xxx(uitest_driver, explore_log):
            uitest_driver.tap_by_type("Tab", index=2)
            uitest_driver.wait_for_text("登录", timeout=5000)
            uitest_driver.assert_text("登录")
    """
    driver = UITestDriver()
    yield driver
    # 模块结束清理
    try:
        driver.back()
    except Exception:
        pass


def _find_skill_dir() -> Path:
    """定位 autoharmony 工具目录

    优先级：
      1. UITEST_SKILL_DIR 环境变量
      2. opencode 全局 skill 目录（~/.config/opencode/skills/skills/...）
      3. 从 conftest 位置向上找工程根下的 .trae/.claude/.opencode skills 目录，
         或包含 autoharmony.py 的目录
    """
    env = os.environ.get("UITEST_SKILL_DIR")
    if env:
        return Path(env)
    # opencode 全局安装位置
    home = Path.home() / ".config" / "opencode" / "skills" / "skills"
    candidate = home / "hm-navigation-uitest"
    if (candidate / "autoharmony.py").exists():
        return candidate
    # 工程内安装位置（从当前文件位置向上找）
    for parent in Path(__file__).resolve().parents:
        for base in (".trae", ".claude", ".opencode"):
            skill = parent / base / "skills" / "hm-navigation-uitest"
            if (skill / "autoharmony.py").exists():
                return skill
        if (parent / "autoharmony.py").exists():
            return parent
    raise RuntimeError(
        "未找到 autoharmony 目录，请设置 UITEST_SKILL_DIR"
    )


_SKILL_DIR_CACHE: list = []


def _skill_dir() -> Path:
    """惰性定位 skill 目录（进程内缓存）——不在模块导入时执行，
    避免 conftest 加载即崩（不用 UITestDriver 的会话也受影响）"""
    if not _SKILL_DIR_CACHE:
        _SKILL_DIR_CACHE.append(_find_skill_dir())
    return _SKILL_DIR_CACHE[0]


class UITestDriver:
    """HDC UI 测试驱动封装

    所有 UI 操作都基于 HDC shell uitest 命令 + autoharmony.py。
    """

    def __init__(self):
        self._dump_counter = 0
        self.UITEST = str(_skill_dir() / "autoharmony.py")

    def dump_layout(self, target: str = "/tmp/layout.json") -> bool:
        """dumpLayout + 拉取 + 铁律分析"""
        import subprocess
        self._dump_counter += 1

        # 1. 设备端 dump
        subprocess.run(
            ["hdc", "shell", "uitest", "dumpLayout", "-a", "-p", "/data/local/tmp/layout.json"],
            check=True,
        )
        # 2. 拉取
        subprocess.run(
            ["hdc", "file", "recv", "/data/local/tmp/layout.json", target],
            check=True,
        )
        # 3. 铁律：立即调 analyzer（确认 dump 有效，列出类型统计）
        try:
            subprocess.run(
                ["python3", self.UITEST, "tree", "show", target, "--list-types"],
                check=False,  # 分析失败不阻塞
            )
        except FileNotFoundError:
            pass
        return True

    def _find_centers(self, search_args: list) -> list:
        """dump 后按条件搜索控件，返回中心坐标列表 [(x, y), ...]"""
        import subprocess
        self.dump_layout()
        result = subprocess.run(
            ["python3", self.UITEST, "tree", "show", "/tmp/layout.json", "--json"] + search_args,
            capture_output=True, text=True,
        )
        # 区分"搜索无命中"与"tree show 本身失败"：后者误报断言失败会误导排查
        if result.returncode != 0:
            print(f"⚠️ tree show 失败（exit {result.returncode}）："
                  f"{(result.stderr or '').strip()[:200]}")
            return []
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = []
        centers = []
        for item in data:
            center = item.get("center") or {}
            if center.get("x") is not None and center.get("y") is not None:
                centers.append((center["x"], center["y"]))
        return centers

    def tap_by_id(self, component_id: str) -> None:
        """通过 ID 点击（无需坐标）"""
        centers = self._find_centers(["--id", component_id])
        if not centers:
            raise AssertionError(f"未找到 id '{component_id}' 对应的控件")
        self.tap_xy(*centers[0])

    def tap_by_text(self, text: str) -> None:
        """通过文本匹配点击"""
        centers = self._find_centers(["--text", text])
        if not centers:
            raise AssertionError(f"未找到文本 '{text}' 对应的控件")
        self.tap_xy(*centers[0])

    def tap_by_type(self, widget_type: str, index: int = 0) -> None:
        """通过类型 + 索引点击"""
        centers = self._find_centers(["--type", widget_type])
        if len(centers) <= index:
            raise AssertionError(f"类型 {widget_type} 索引 {index} 未找到")
        self.tap_xy(*centers[index])

    def tap_xy(self, x: int, y: int) -> None:
        """坐标点击"""
        import subprocess
        subprocess.run(
            ["hdc", "shell", "uitest", "uiInput", "click", str(x), str(y)],
            check=True,
        )

    def wait_for_text(self, text: str, timeout: int = 5000, interval: int = 200) -> None:
        """轮询直到文本出现"""
        import time
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            if self._find_centers(["--text", text]):
                return
            time.sleep(interval / 1000)
        raise AssertionError(f"等待 {timeout}ms 后未找到文本 '{text}'")

    def assert_text(self, text: str) -> None:
        """断言文本存在"""
        if not self._find_centers(["--text", text]):
            raise AssertionError(f"断言失败：未找到文本 '{text}'")

    def assert_id(self, component_id: str) -> None:
        """断言 ID 存在"""
        if not self._find_centers(["--id", component_id]):
            raise AssertionError(f"断言失败：未找到 id '{component_id}'")

    def assert_type(self, widget_type: str) -> None:
        """断言类型存在"""
        if not self._find_centers(["--type", widget_type]):
            raise AssertionError(f"断言失败：未找到类型 '{widget_type}'")

    def back(self) -> None:
        """返回键"""
        import subprocess
        subprocess.run(
            ["hdc", "shell", "uitest", "uiInput", "keyEvent", "Back"],
            check=False,
        )

    def input_text(self, text: str) -> None:
        """输入文本（uiInput text 免坐标；API 18+。inputText 需 point 坐标）。

        shlex.quote：hdc 会把参数 join 后交设备 shell 解释，含空格文本被拆
        """
        import subprocess
        import shlex
        subprocess.run(
            ["hdc", "shell", "uitest", "uiInput", "text", shlex.quote(text)],
            check=True,
        )

    def assert_component_exist(self, matcher: str) -> None:
        """通用断言（支持 id:xxx / text:xxx / type:xxx 格式）

        用法：
            driver.assert_component_exist("text:登录")
            driver.assert_component_exist("id:login_button")
            driver.assert_component_exist("type:Swiper")
        """
        kind, value = matcher.split(":", 1)
        if kind == "text":
            self.assert_text(value)
        elif kind == "id":
            self.assert_id(value)
        elif kind == "type":
            self.assert_type(value)
        else:
            raise ValueError(f"不支持的 matcher 格式：{matcher}")


# ============================================================
# 5. 命令行选项
# ============================================================

def pytest_addoption(parser: pytest.Parser) -> None:
    """添加 --explore-log 命令行参数，允许指定特定探索日志"""
    parser.addoption(
        "--explore-log",
        action="store",
        default=None,
        help="指定探索日志目录名（默认用最新的）",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """session 开始时加载探索日志（支持 --explore-log 覆盖默认）"""
    specified = config.getoption("--explore-log", default=None)
    if specified:
        log_dir = EXPLORE_LOGS_ROOT / specified
        log_file = log_dir / "explore_log.json"
        if log_file.exists():
            with open(log_file) as f:
                config._explore_log = json.load(f)
            cases_count = len(config._explore_log.get("cases", []))
            print(f"\n[正反例追踪] 已加载指定探索日志：{specified}（{cases_count} 条用例）\n")
        else:
            print(f"\n[警告] 指定的探索日志不存在：{log_file}，回退到最新日志\n")
            config._explore_log = _load_latest_explore_log()
    else:
        config._explore_log = _load_latest_explore_log()

    # 打印正反例统计
    if config._explore_log:
        cases_count = len(config._explore_log.get("cases", []))
        positive = sum(
            1 for c in config._explore_log.get("cases", [])
            for s in c.get("steps", [])
        )
        negative = sum(
            1 for c in config._explore_log.get("cases", [])
            for s in c.get("steps", [])
            for _ in s.get("negatives", [])
        )
        print(f"[正反例追踪] 统计：{cases_count} 条用例，"
              f"{positive} 个正例步骤，{negative} 个反例步骤\n")
