# AI Agent Integration Guide

This guide explains how to integrate `hmuitest` with AI agents (Claude, GPT, custom agents) for autonomous HarmonyOS UI testing.

## Architecture

```
Agent (LLM)
    │
    ├── Writes code (ArkTS)
    │
    ├── Calls hmuitest CLI
    │     ├── ui click-by-text "设置" --json
    │     ├── tree dump --json
    │     └── script run regression.json --json
    │
    ├── Parses structured output (JSON)
    │     ├── status: SUCCESS / FAILED / CRASHED / BLOCKED_BY_DIALOG
    │     ├── reason: human-readable explanation
    │     └── exit: 0 or 1
    │
    └── Saves reusable scripts
          └── script record start/stop → script.json
```

## Structured Output (`--json`)

Every command supports `--json` flag. Agent parses the JSON object directly.

### Success response

```json
{
  "status": "SUCCESS",
  "reason": "route changed to LoginPage",
  "action": "click_by_text",
  "target": "登录",
  "route_before": "HomePage",
  "route_after": "LoginPage",
  "exit": 0
}
```

### Failure response

```json
{
  "status": "NOT_FOUND",
  "reason": "no widget with text '不存在'",
  "action": "click_by_text",
  "target": "不存在",
  "exit": 1
}
```

### Dialog blocked

```json
{
  "status": "BLOCKED_BY_DIALOG",
  "reason": "dialog '确认删除' detected",
  "suggestion": "dismiss dialog first with: hmuitest.py ui dismiss-dialogs",
  "exit": 1
}
```

### Crash detected

```json
{
  "status": "CRASHED",
  "reason": "process died after action",
  "crash_type": "CppCrash",
  "suggestion": "check crash logs with: hdc shell hidumper -e",
  "exit": 1
}
```

## Agent Workflow

### Phase 1: Exploration (first run)

Agent explores the app autonomously:

```bash
# 1. Launch app
python3 hmuitest.py aa start "myapp://home" --bundle com.example.app --json

# 2. Click through pages, record what works
python3 hmuitest.py ui click-by-text "设置" --json
python3 hmuitest.py ui scroll-find "关于" --json
python3 hmuitest.py ui click-by-text "关于" --json

# 3. Save exploration as reusable script
python3 hmuitest.py script record stop --output regression.json
```

### Phase 2: Regression (subsequent runs)

After code changes, agent reruns saved scripts:

```bash
python3 hmuitest.py script run regression.json --json
# → {"all_passed": true, "steps": 5, "failed": 0}
```

If a step fails, agent gets structured error and can:
1. Fix the code
2. Rerun the script
3. Or update the script if UI changed

### Phase 3: Self-healing scripts

When UI changes break scripts, agent can:

```bash
# 1. Run script, see which step fails
python3 hmuitest.py script run regression.json --json
# → step 3 failed: NOT_FOUND "旧按钮文本"

# 2. Agent investigates current UI
python3 hmuitest.py tree dump --overview --json

# 3. Agent finds new text, updates script
# (agent edits regression.json with new target)

# 4. Rerun
python3 hmuitest.py script run regression.json --json
# → all_passed: true
```

## Integration Examples

### Claude / Anthropic API

```python
import subprocess
import json

def run_hmuitest(args: list[str]) -> dict:
    """Run hmuitest and return structured result."""
    cmd = ["python3", "hmuitest.py"] + args + ["--json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    
    if result.returncode != 0 and not result.stdout.strip():
        return {"status": "ERROR", "reason": result.stderr, "exit": 1}
    
    return json.loads(result.stdout)

# Agent tool definition
tools = [{
    "name": "hmuitest",
    "description": "HarmonyOS UI testing. Returns structured verdict.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "click", "click_by_text", "click_by_id", "swipe",
                "input", "input_by_text", "back", "scroll_find",
                "wait_for", "screenshot", "dismiss_dialogs",
                "tree_dump", "tree_diff", "script_run"
            ]},
            "args": {"type": "array", "items": {"type": "string"}}
        }
    }
}]

# Agent uses it
result = run_hmuitest(["ui", "click-by-text", "设置", "--expect-route", "Settings"])
if result["status"] == "SUCCESS":
    # Continue exploring
    pass
elif result["status"] == "BLOCKED_BY_DIALOG":
    # Dismiss dialog first
    run_hmuitest(["ui", "dismiss-dialogs"])
    # Retry
    run_hmuitest(["ui", "click-by-text", "设置", "--expect-route", "Settings"])
```

### OpenAI Function Calling

```python
import subprocess
import json

def hmuitest_tool(action: str, args: list[str] = None) -> dict:
    cmd = ["python3", "hmuitest.py"] + action.split() + (args or []) + ["--json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        return json.loads(result.stdout) if result.stdout.strip() else {"error": result.stderr}
    except json.JSONDecodeError:
        return {"error": "Failed to parse output", "raw": result.stdout}

# In your agent loop
response = client.chat.completions.create(
    model="gpt-4",
    messages=messages,
    tools=[{
        "type": "function",
        "function": {
            "name": "hmuitest",
            "description": "Execute HarmonyOS UI action and return structured verdict",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}}
                }
            }
        }
    }]
)
```

## Verdict Parsing

Agent should parse the `status` field for decision-making:

| Status | Meaning | Agent action |
|--------|---------|-------------|
| `SUCCESS` | Action completed, expectations met | Continue to next step |
| `NO_CHANGE` | No UI change detected | Check if action was correct, retry with different approach |
| `NOT_FOUND` | Target widget not found | Search with `tree dump`, update target |
| `BLOCKED_BY_DIALOG` | Dialog overlay detected | Run `dismiss-dialogs`, retry |
| `CRASHED` | App process died | Stop, report crash, check logs |
| `BACK_INEFFECTIVE` | Back press had no effect | App may be at root, try different navigation |
| `TIMEOUT` | Assertion polling timed out | Check if UI is loading slowly, increase timeout |

## Error Recovery Pattern

```python
def robust_action(action_args: list[str], max_retries: int = 3) -> dict:
    """Execute action with automatic error recovery."""
    for attempt in range(max_retries):
        result = run_hmuitest(["ui"] + action_args + ["--json"])
        
        if result["status"] == "SUCCESS":
            return result
        
        if result["status"] == "BLOCKED_BY_DIALOG":
            run_hmuitest(["ui", "dismiss-dialogs"])
            continue
        
        if result["status"] == "CRASHED":
            return result  # Don't retry crashes
        
        if result["status"] == "NOT_FOUND":
            # Agent investigates and finds new target
            tree = run_hmuitest(["tree", "dump", "--overview", "--json"])
            # Agent analyzes tree and updates action_args
            break
    
    return result
```

## Tips for Agent Authors

1. **Always use `--json`** — structured output is faster to parse than human text
2. **Use `--expect-route`** — route changes are the most reliable success signal
3. **Use `--auto-handle-dialog`** — reduces agent error handling complexity
4. **Save scripts early** — exploration is expensive, reuse is cheap
5. **Check verdict before continuing** — never assume success, always verify
