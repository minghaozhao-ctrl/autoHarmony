# autoharmony × OpenAI Codex

## Install

```bash
pip install autoharmony
hdc list targets
```

## Wire in: `AGENTS.md` (project root)

Codex reads `AGENTS.md` (GitHub standard). Add:

```markdown
## HarmonyOS UI verification (autoharmony)

Before finalizing any ArkTS change, verify on device:
- `autoharmony ui click-by-text "目标文字" --expect-route "期望路由" --json`
- Pass requires `"status": "SUCCESS"` (exit 0). Never fabricate results.
- statuses: SUCCESS | NO_CHANGE | BLOCKED_BY_DIALOG | CRASHED
- Record flows once, replay forever:
  `autoharmony script record start` → ui cmds → `autoharmony script record stop --output flow.json`
  then `autoharmony script run flow.json --json` after changes.
- Inspect before acting: `autoharmony tree dump --overview --json`
- `autoharmony ui dismiss-dialogs` clears blocking overlays.
```

## Use in Codex CLI/IDE

Prompt: "Verify the UI changes on the device and report verdicts." Codex follows the loop end-to-end.