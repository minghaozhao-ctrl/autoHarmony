# autoharmony × Cursor

## Install

```bash
pip install autoharmony
hdc list targets
```

## Wire in: project rules (new-style)

Create `.cursor/rules/autoharmony.mdc`:

```markdown
---
description: Verify HarmonyOS UI changes on-device with autoharmony before done
globs: *.ets, *.ts
---
Always verify ArkTS UI changes on device before finishing:
- `autoharmony ui click-by-text "目标文字" --expect-route "期望路由" --json`
- Only `"status": "SUCCESS"` (exit 0) counts as pass. Parse JSON, never guess.
- statuses: SUCCESS | NO_CHANGE | BLOCKED_BY_DIALOG | CRASHED.
- Record once, replay forever:
  - `autoharmony script record start` → run ui commands → `autoharmony script record stop --output flow.json`
  - after code change: `autoharmony script run flow.json --json`
- Inspect before acting: `autoharmony tree dump --overview --json`
- `autoharmony ui dismiss-dialogs` clears blocking overlay popups.
```

## Legacy `.cursorrules`

Put the same markdown content (without frontmatter) in `.cursorrules` at repo root.

## Use in Cursor agent mode

Ask Cursor to "verify the UI change on device" — it now auto-runs the autoharmony loop and reports verdicts.