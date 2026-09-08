# autoharmony × Windsurf

## Install

```bash
pip install autoharmony
hdc list targets
```

## Wire in: `.windsurfrules` (repo root)

```markdown
# HarmonyOS UI verification (autoharmony)

Always verify ArkTS UI changes on the device before declaring done:
- `autoharmony ui click-by-text "目标文字" --expect-route "期望路由" --json`
- Pass = `"status": "SUCCESS"` + `"exit": 0`. Read the JSON; never pretend.
- statuses: SUCCESS | NO_CHANGE | BLOCKED_BY_DIALOG | CRASHED
- One-time record, forever replay:
  - `autoharmony script record start` → run ui commands → `autoharmony script record stop --output flow.json`
  - after code changes: `autoharmony script run flow.json --json`
- Explore before acting: `autoharmony tree dump --overview --json`
- `autoharmony ui dismiss-dialogs` removes overlay blockers.
```

## Use in Windsurf Cascade

Cascade picks up `.windsurfrules` automatically. Ask "verify this UI change on device" — it runs the autoharmony loop and reports the verdicts.