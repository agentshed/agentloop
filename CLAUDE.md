# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run

```bash
uv pip install -e .            # dev install
uv run agentloop               # run from checkout
uv run agentloop -h            # show help
uv run agentloop --dry-run -p "test"  # preview commands without executing
```

## Dependencies

At the start of each session, check `pyproject.toml` dependencies against the latest available versions (use `uv pip index versions <package>`). If any dependency has a newer version available, suggest the upgrade to the user before proceeding with other work.

## Architecture

Python CLI (`click`) that wraps the `openshell` CLI via subprocess to create isolated OpenShell sandboxes running Claude Code. There is no OpenShell Python SDK — all interaction is through `engine.run_openshell()` which shells out to the `openshell` binary.

### Module responsibilities

- **`cli.py`** — Click-based CLI with `run` (default), `init`, `config` subcommands. Default subcommand detection happens in `main()` by checking if the first non-flag arg is a known subcommand; if not, `run` is prepended.
- **`config.py`** — YAML config loading and deep-merge. Resolution order: CLI flag > env var > `./agentloop.yaml` (local) > `~/.agentloop/agentloop.yaml` (global) > `BUILTIN_DEFAULTS`. The `_DEFAULT_CONFIG_YAML` string is the template written by `init`.
- **`engine.py`** — `run_openshell()` is the single subprocess gateway for ALL openshell calls. Also contains `preflight_checks()` which validates openshell, Docker/Podman, gcloud ADC, GitHub token, and Vertex project/region with actionable error messages.
- **`providers.py`** — Idempotent provider create/update for vertex, anthropic, github. GitHub provider uses `--from-existing` (reads `GITHUB_TOKEN` from process env). Vertex provider always updates config with `VERTEX_AI_PROJECT_ID` and `VERTEX_AI_REGION`.
- **`policy.py`** — Generates OpenShell network policy YAML. GitHub endpoints MUST be in the custom policy with `binaries` list — otherwise `--policy` at create time replaces provider-owned endpoints and breaks credential injection.
- **`sandbox.py`** — Sandbox lifecycle: create (with `-- echo ready` so it returns immediately), workspace setup (local upload or remote clone), `_configure_claude_settings()` (uploads `.claude.json` and settings files to skip onboarding), Claude exec (unattended via `--print` or interactive via `--tty`), output collection, cleanup.

### Key design decisions

- **Policy must include GitHub endpoints with binaries.** `--policy` at sandbox create time does a full replacement of all policy including provider-injected endpoints. The github provider's credential proxy only works when `api.github.com` and `github.com` are explicitly listed in the policy with `binaries: [/usr/bin/gh, /usr/local/bin/gh, /usr/bin/git, ...]`. Without this, the proxy denies all connections with "endpoint not allowed by any policy" or "binary not allowed."

- **`inference.local` for Vertex AI.** Claude Code connects to `https://inference.local` (set via `ANTHROPIC_BASE_URL`), not directly to Vertex. The OpenShell gateway proxy resolves the placeholder `ANTHROPIC_API_KEY` and forwards to Vertex AI. `CLAUDE_CODE_USE_VERTEX=1` must NOT be set — it bypasses inference.local and tries to load GCP credentials that don't exist in the sandbox.

- **`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`** is required because Vertex AI rejects the `context_management` field that newer Claude Code versions send.

- **Claude Code settings are uploaded via `openshell sandbox upload`** (not `exec` + shell commands) to avoid quoting issues. The `.claude.json` file pre-sets `hasCompletedOnboarding`, `bypassPermissionsAccepted`, `customApiKeyResponses` to skip all first-run prompts. The `settings.json` sets `permissions.skipDangerousModePermissionPrompt` and is passed via `--settings` flag.

- **`-r/--repo` auto-detects format.** A single flag handles `owner/repo`, `owner/repo@branch`, `owner/repo#42`, and full GitHub URLs (`https://github.com/owner/repo/pull/42`, `.../tree/fix/nested-branch`). Parsing happens in `_parse_remote()` which normalizes URLs then matches against PR, tree, `#`, `@`, or plain patterns.

- **GitHub token from `gh auth token`.** The preflight check tries `GITHUB_TOKEN` env var first, then `GH_TOKEN`, then falls back to running `gh auth token` and setting `os.environ["GITHUB_TOKEN"]` so `openshell provider create --from-existing` picks it up.

### OpenShell debugging

```bash
# See why a connection was denied (most useful)
openshell logs <sandbox> --source sandbox --level debug

# See effective policy on a running sandbox
openshell policy get <sandbox> --full

# Gateway-level logs
openshell logs --level debug --source gateway --tail
```
