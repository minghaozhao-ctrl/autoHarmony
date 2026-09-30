# EXAMPLES

> 完整工作流与 subagent 探索型测试流程。命令参数详见 [REFERENCE.md](REFERENCE.md)。

---

## 1. 自动化测试工作流

**前置条件**：应用已构建安装、设备已连接（`hdc list targets` 可查）

```bash
# 0. 创建日志目录 & 启动 hilog 收集
LOG_DIR="${LOG_DIR:-${PROJECT_ROOT}/test_logs/test_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "${LOG_DIR}"
hdc shell hilog -r
hdc shell hilog > "${LOG_DIR}/test_execution.log" 2>&1 &
HILOG_PID=$!

UITEST="${SKILL_DIR:-.}/autoharmony.py"

# 1. [可选] 登录（需先询问用户手机号）
python3 $UITEST app login "用户手机号"
python3 $UITEST app user-info

# 2. 导航到目标页面
python3 $UITEST app navigate "MobileSmartTalkPage"

# 3. 执行 UI 操作（自动返回差异报告）
python3 $UITEST ui click 540 550 --operation "点击登录按钮"
python3 $UITEST ui input "13800138000" --operation "输入手机号"

# 4. 截图留证
python3 $UITEST ui screenshot result.png

# 5. 测试结束，清理
python3 $UITEST app logout
kill ${HILOG_PID} 2>/dev/null || true

# 6. 提取错误日志
grep -E " E\/| F\/" "${LOG_DIR}/test_execution.log" > "${LOG_DIR}/error.log"
grep "com.cmcc.DigitalHome" "${LOG_DIR}/test_execution.log" > "${LOG_DIR}/app.log" || true
echo "测试日志已保存到: ${LOG_DIR}"
```

---

## 2. 迭代式开发-测试-修复循环（核心）

决策简表：

```
pytest tests/uitest/ -v --md-report
    ↓
有 FAIL？看 conftest.py hook 输出的正反例对照
    ↓
A. 业务代码 bug → 改业务代码 → pytest
B. UI 改 id/text/位置 → 调 subagent 重跑该条
C. 脚本硬编码过期 → 改 tests/uitest/*.py → pytest
    ↓
循环直到全 PASS（安全阀：5 轮仍 fail → 人介入）
```

**改 bug 阶段主 agent 自己跑 pytest 即可**——脚本已存在。只有 UI 大改或批量 fail 时才重调 subagent。

---

## 3. Subagent 探索型测试 + pytest 脚本生成

### 3.1 一句话能力

> 主 agent 拿一份自然语言用例文件 → 委托 `general-purpose` subagent 自动探索每条用例 → 生成 pytest 脚本 → 自验 → 交付给主 agent；主 agent 改完代码后跑同一份脚本，根据报告改代码。

### 3.2 入口调用

主 agent 在改完业务代码、build 验证通过后，主动委托 subagent：

```python
Agent(
    subagent_type="general",
    prompt="""你是 HarmonyOS UI 测试执行 subagent。
    请按用例文件执行探索型测试并生成 pytest 脚本。
    用例文件：tests/cases/首页.md
    工具说明：参考 autoharmony 的 docs/REFERENCE.md。"""
)
```

拿到 subagent 交付的脚本后跑回归：

```bash
pytest tests/uitest/ -v --md-report
```

### 3.3 交付物

- `tests/uitest/*.py`：pytest 脚本（按模块分文件、一文件多函数）
- `tests/reports/report_<时间戳>.md`：Markdown 测试报告（pytest-md-report）
- `tests/uitest/conftest.py`：用例前后置 fixture

### 3.4 关键约束

- 单 subagent 串行，单设备运行
- 用例按"同一页面"切片：同切片不重置 App，跨切片才重置
- 每步探索：dumpLayout 断言 + 截图，失败整条重试 3 次
- 失败 3 次标"探索失败"，不阻塞其他用例
- 脚本生成后跑 2 次自验一致才交付
- docstring 含原自然语言用例，便于追溯
