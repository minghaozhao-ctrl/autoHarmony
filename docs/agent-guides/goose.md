# autoharmony × Goose

## Install

```bash
pip install autoharmony
hdc list targets
```

## Wire in: `AGENTS.md`

Goose respects `AGENTS.md` (project root or global `~/.config/goose/AGENTS.md`). Add:

```markdown
## HarmonyOS UI verification (autoharmony)

Verify ArkTS/UI changes on device before saying done:
- `autoharmony ui click-by-text "目标文字" --expect-route "期望路由" --json`
- Pass = `"status": "SUCCESS"` (exit 0). Never fake a pass.
- statuses: SUCCESS | NO_CHANGE | BLOCKED_BY_DIALOG | CRASHED
- Record once, replay forever:
  - `autoharmony script record start` → ui commands → `autoharmony script record stop --output flow.json`
  - after code change: `autoharmony script run flow.json --json`
- Explore first: `autoharmony tree dump --overview --json`
- `autoharmony ui dismiss-dialogs` clears blocking overlays.
```

## Use in Goose

Start Goose in the repo, then: "Verify the UI changes on the device and report the verdicts."