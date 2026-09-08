# autoharmony × Claude Code

## Install

```bash
pip install autoharmony
hdc list targets
```

## Wire in: `CLAUDE.md` (project root)

Append this block, then restart Claude Code so it reloads the file:

```markdown
## HarmonyOS UI verification (autoharmony)

Whenever you change ArkTS/UI code, verify it ON the device before saying "done":
- Run: `autoharmony ui click-by-text "目标文字" --expect-route "期望路由" --json`
- Parse `{"status": "SUCCESS", "exit": 0}` — never fake a pass.
- `status` values: SUCCESS | NO_CHANGE | BLOCKED_BY_DIALOG | CRASHED. Only SUCCESS (+ optionally NO_CHANGE when expected) means pass.
- Record a flow once, replay forever:
  - `autoharmony script record start`
  - ... run ui commands ...
  - `autoharmony script record stop --output flow.json`
  - after code changes: `autoharmony script run flow.json --json`
- First explore: `autoharmony tree dump --overview --json` to see the widget tree.
- `autoharmony ui dismiss-dialogs` clears overlay popups that block actions.
- Never uninstall shells: if a step fails, re-dump the tree and read the verdict reason before retrying.
```

## Recommended skill

Install the repo's `SKILL.md` into your Claude skills dir for tool-shaped instructions:

```bash
mkdir -p ~/.claude/skills
cp /path/to/autoharmony/SKILL.md ~/.claude/skills/autoharmony/SKILL.md
```

Then Claude Code auto-loads it when UI-verification tasks come up.