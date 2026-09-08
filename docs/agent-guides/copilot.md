# autoharmony × GitHub Copilot

## Install

```bash
pip install autoharmony
hdc list targets
```

## Wire in

Copilot reads both `AGENTS.md` and `.github/copilot-instructions.md`. Prefer `AGENTS.md` (shared with Codex, Claude, Goose). Add:

```markdown
# HarmonyOS UI verification (autoharmony)

Verify on device before claiming an ArkTS change is done:
- `autoharmony ui click-by-text "目标文字" --expect-route "期望路由" --json`
- Pass = `"status": "SUCCESS"` and `"exit": 0`. Read JSON, do not invent passes.
- Record → replay:
  - `autoharmony script record start` → run ui commands → `autoharmony script record stop --output flow.json`
  - after code change: `autoharmony script run flow.json --json`
- Explore first: `autoharmony tree dump --overview --json`
- `autoharmony ui dismiss-dialogs` unblocks overlay popups.
```

## VS Code / JetBrains Chat

Enable the model to run terminal commands (VS Code: @terminal agent). Then ask "verify this UI change on device" — Copilot follows the instructions above.