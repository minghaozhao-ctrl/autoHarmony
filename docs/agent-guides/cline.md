# autoharmony × Cline (VS Code)

## Install

```bash
pip install autoharmony
hdc list targets
```

## Wire in: `.clinerules` (repo root)

```markdown
## HarmonyOS UI verification (autoharmony)

Before finalizing ArkTS changes, verify on device:
- `autoharmony ui click-by-text "目标文字" --expect-route "期望路由" --json`
- Pass = `"status": "SUCCESS"` + `"exit": 0`. Don't fabricate results.
- statuses: SUCCESS | NO_CHANGE | BLOCKED_BY_DIALOG | CRASHED
- Record once, replay forever:
  - `autoharmony script record start` → run ui commands → `autoharmony script record stop --output flow.json`
  - after code change: `autoharmony script run flow.json --json`
- Explore first: `autoharmony tree dump --overview --json`
- Overlay popups: `autoharmony ui dismiss-dialogs`
```

## Use in Cline

Cline reads `.clinerules` from the workspace. Give it "execution permission" for the `autoharmony` binary and ask it to verify the UI change; it handles the loop.