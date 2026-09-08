# autoharmony × AI Coding Agents

Plug `autoharmony` into your coding agent so it **verifies its own HarmonyOS work** instead of hand-waving "it looks fine".

## One-time setup (all agents)

```bash
pip install autoharmony
hdc list targets          # device must be visible
```

## Per-agent guides

| Agent | Config file | Guide |
|-------|-------------|-------|
| [Claude Code](claude-code.md) | `CLAUDE.md` | [→](claude-code.md) |
| [Cursor](cursor.md) | `.cursor/rules/*.mdc` | [→](cursor.md) |
| [GitHub Copilot](copilot.md) | `AGENTS.md` / `.github/copilot-instructions.md` | [→](copilot.md) |
| [OpenAI Codex](codex.md) | `AGENTS.md` | [→](codex.md) |
| [Windsurf](windsurf.md) | `.windsurfrules` | [→](windsurf.md) |
| [Cline](cline.md) | `.clinerules` | [→](cline.md) |
| [Goose](goose.md) | `AGENTS.md` | [→](goose.md) |
| [opencode](opencode.md) | `~/.config/opencode/skills/autoharmony/` | [→](opencode.md) |

## The loop every agent should follow

```
explore   → autoharmony ui click-by-text "登录" --json
record    → autoharmony script record start ... stop --output flow.json
verify    → autoharmony script run flow.json --json
```

Every command returns `exit 0/1` and `--json` verdict → agents parse, never regex.