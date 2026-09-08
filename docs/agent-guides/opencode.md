# autoharmony × opencode

## Install

```bash
pip install autoharmony
hdc list targets
```

## Wire in: install as an opencode skill

opencode loads skills from `SKILL.md` folders. Install `autoharmony` globally (all your projects) or per-project:

```bash
# Global — available in every opencode session
mkdir -p ~/.config/opencode/skills/autoharmony
cp /path/to/autoharmony/SKILL.md ~/.config/opencode/skills/autoharmony/SKILL.md

# Per-project — only this repo
mkdir -p .opencode/skill/autoharmony
cp /path/to/autoharmony/SKILL.md .opencode/skill/autoharmony/SKILL.md
```

The skill's frontmatter describes the trigger ("HarmonyOS UI 自动化测试… Use when 需要执行 UI 自动化测试…"), so opencode auto-loads it when you ask for UI verification.

## Wire in: AGENTS.md (project rules)

For tighter control, add to `AGENTS.md`:

```markdown
## HarmonyOS UI verification (autoharmony)

Before finalizing any ArkTS change, verify on device:
- `autoharmony ui click-by-text "目标文字" --expect-route "期望路由" --json`
- Pass = `"status": "SUCCESS"` + `"exit": 0`. Read JSON, never fake a pass.
- statuses: SUCCESS | NO_CHANGE | BLOCKED_BY_DIALOG | CRASHED
- Record once, replay forever:
  - `autoharmony script record start` → ui commands → `autoharmony script record stop --output flow.json`
  - after code change: `autoharmony script run flow.json --json`
- Explore first: `autoharmony tree dump --overview --json`
- `autoharmony ui dismiss-dialogs` clears blocking overlays.
```

## Use

In any opencode session: "验证一下 UI 改动" — the skill loads and the agent runs explore → record → replay, reading verdicts from `--json` output.