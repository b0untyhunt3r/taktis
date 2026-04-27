# Taktis — Multi-Agent Pipeline Engine for Claude Code

## Documentation lives in the vault

The authoritative architecture docs are an Obsidian vault at **`docs/vault/`**. Start at `docs/vault/Home.md` — it links every topic, and each note carries `file:line` citations into the code.

Don't duplicate vault content in this file. When something material changes, update the vault note and, if it affects a rule Claude must always follow, add a one-liner here with a link.

### Jump-offs
- `docs/vault/Home.md` — vault index
- `docs/vault/Architecture Overview.md` — layer map, control flow
- `docs/vault/Glossary.md` — terminology
- `docs/ERROR_HANDLING.md` — the 6 error-handling rules (also mirrored at `docs/vault/Error Handling/Six Rules.md`)

## Running

```bash
python3 run.py                  # Web UI at http://localhost:8080
python3 -m pytest tests/ -v     # Tests
```

Python 3.10+. Cross-platform. `run.py` sets `CLAUDE_CODE_STREAM_CLOSE_TIMEOUT=4h`.

## Top-level layout

```
taktis/
├── core/              # scheduler, graph executor, SDK process, services
├── web/               # Starlette app + Jinja templates
├── experts/           # 184 persona .md files
├── agent_templates/   # 7 DB-backed prompt templates (.md)
├── defaults/
│   └── pipeline_templates/   # Drawflow JSON seed files
├── config.py, db.py, models.py, repository.py, exceptions.py
tests/
run.py
docs/
├── vault/             # Obsidian vault — full architecture docs
└── ERROR_HANDLING.md
```

For module-by-module detail see `docs/vault/Home.md` → Core / Pipeline / Data Layer / Web.

## Git workflow

- Remote: GitHub at `github.com/b0untyhunt3r/taktis`.
- Commit + push after completing work, but **ask before pushing** (see memory `feedback_no_push_without_permission`).
- Commit messages explain *why*, not *what*.
- Bundle related changes; split unrelated changes.
- Never commit `taktis.db`, `__pycache__/`, or `.taktis/` (already in `.gitignore`).
- Run `timeout 45 python3 -m pytest tests/ -v` before committing (pytest hangs after finishing on this env — see memory `feedback_test_command`).

## Non-obvious operational rules

These will bite you:

- **Don't run `Taktis().initialize()` against a live server.** Stale recovery flips every running task to `failed`. See `docs/vault/Crash Recovery.md`.
- **Working dir must exist** when creating a project (or check "Create directory").
- **`delete_project()` only removes `.taktis/` subdir** — never touches the working dir itself.
- **All async.** No blocking calls anywhere; everything is `asyncio`.
- **Task IDs are 8-char hex** (`uuid4().hex[:8]`), see `docs/vault/Data Layer/Models.md`.
- **Tasks require a phase** — no standalone tasks.
- **Ctrl+C** force-exits after 2s if SSE connections block shutdown.
- **Prompts must explicitly forbid tools** when the output should be plain text (roadmapper, researchers).
- **`_enrich_project()` must include `planning_options`** for phase review to fire.
- **Kill the dev server** with `powershell Stop-Process` — `taskkill /F` times out on mingw64 Python (see memory `feedback_kill_server`).

## Artifacts & testing

- Never save screenshots, test outputs, or temp files in the project root.
- Use `/tmp/` or a gitignored `tmp/` directory.
- MCP Playwright screenshots go to `/tmp/` or `tmp/`, never project root.
- Keep project root clean — only committed source belongs here.

## Dependencies

`pyproject.toml` has the full list.

```bash
pip install -r requirements.txt        # runtime
pip install -r requirements-dev.txt    # + testing
```

## Deep dives

| Area | Start here |
|---|---|
| Runtime scheduling | `docs/vault/Core/WaveScheduler.md`, `GraphExecutor.md` |
| SDK integration | `docs/vault/Core/SDKProcess.md`, `ProcessManager.md` |
| Events & SSE | `docs/vault/Core/EventBus.md`, `docs/vault/Web/SSE Architecture.md` |
| Pipelines | `docs/vault/Pipeline/Node Types.md`, `Pipeline Factory.md`, `Agent Templates.md`, `AskUserQuestion Flow.md` |
| Data | `docs/vault/Data Layer/Database Schema.md`, `Repository.md`, `Models.md`, `Config.md`, `Migrations.md` |
| Context chain | `docs/vault/Context/Context Chain.md`, `Context Budget.md` |
| Experts | `docs/vault/Experts/Expert System.md` |
| Phase review | `docs/vault/Core/Phase Review.md` |
| Crash recovery | `docs/vault/Crash Recovery.md` |
| Errors | `docs/vault/Error Handling/Exception Hierarchy.md`, `Six Rules.md`, `docs/ERROR_HANDLING.md` |
| Facade + services | `docs/vault/Core/Engine and Services.md` |
| Web routes | `docs/vault/Web/Web App.md` |
