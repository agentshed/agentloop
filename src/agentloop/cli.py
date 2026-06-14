from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click
from . import __version__
from .config import (
    GLOBAL_CONFIG_PATH,
    LOCAL_CONFIG_DIR,
    LOCAL_CONFIG_NAME,
    load_global_config,
    load_local_config,
    merge_configs,
    resolved_config_summary,
    write_default_config,
    write_local_template,
)
from .engine import OpenShellError, PreflightError, configure_compute_driver, preflight_checks
from .policy import build_policy_rules, generate_policy_yaml, write_policy_tempfile
from .providers import (
    configure_inference_route,
    ensure_anthropic_provider,
    ensure_github_provider,
    ensure_vertex_provider,
    list_existing_providers,
)
from .sandbox import (
    SandboxConfig,
    configure_claude_settings,
    collect_output,
    create_sandbox,
    delete_sandbox,
    run_claude,
    setup_workspace,
)

SUBCOMMANDS = {"init", "config", "run"}
CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cli_overrides(**kwargs) -> dict:
    """Build a config override dict from non-None Click parameters."""
    overrides: dict = {}

    if kwargs.get("provider") is not None:
        overrides.setdefault("providers", {})["inference"] = kwargs["provider"]
    if kwargs.get("model") is not None:
        overrides["model"] = kwargs["model"]
    if kwargs.get("engine") is not None:
        overrides.setdefault("engine", {})["backend"] = kwargs["engine"]
    if kwargs.get("image") is not None:
        overrides.setdefault("engine", {})["image"] = kwargs["image"]
    if kwargs.get("no_github"):
        overrides.setdefault("providers", {})["github"] = False
    if kwargs.get("no_google_auth"):
        overrides.setdefault("network", {})["google_auth"] = False
    if kwargs.get("no_vertex"):
        overrides.setdefault("network", {})["vertex"] = False
    if kwargs.get("allow_pypi"):
        overrides.setdefault("network", {})["allow_pypi"] = True
    if kwargs.get("allow_npm"):
        overrides.setdefault("network", {})["allow_npm"] = True
    if kwargs.get("extra_host"):
        overrides.setdefault("network", {})["extra_hosts"] = list(kwargs["extra_host"])
    if kwargs.get("keep"):
        overrides.setdefault("sandbox", {})["keep"] = True
    if kwargs.get("gpu"):
        overrides.setdefault("sandbox", {})["gpu"] = True
    if kwargs.get("cpu") is not None:
        overrides.setdefault("sandbox", {})["cpu"] = kwargs["cpu"]
    if kwargs.get("memory") is not None:
        overrides.setdefault("sandbox", {})["memory"] = kwargs["memory"]
    if kwargs.get("timeout") is not None:
        overrides.setdefault("sandbox", {})["timeout"] = kwargs["timeout"]
    if kwargs.get("pull"):
        overrides.setdefault("engine", {})["pull"] = True
    if kwargs.get("no_gitignore"):
        overrides.setdefault("sandbox", {})["upload_filter"] = "all"
    elif kwargs.get("enforce_gitignore"):
        overrides.setdefault("sandbox", {})["upload_filter"] = "strict"
    if kwargs.get("forward") is not None:
        overrides.setdefault("sandbox", {})["forward"] = kwargs["forward"]
    if kwargs.get("editor") is not None:
        overrides.setdefault("sandbox", {})["editor"] = kwargs["editor"]
    if kwargs.get("output") is not None:
        overrides.setdefault("output", {})["dir"] = kwargs["output"]
    if kwargs.get("discard_output"):
        overrides.setdefault("output", {})["enabled"] = False
    if kwargs.get("output_paths"):
        overrides.setdefault("output", {})["paths"] = list(kwargs["output_paths"])
    if kwargs.get("output_on_error"):
        overrides.setdefault("output", {})["on_error"] = True
    if kwargs.get("env"):
        env_dict = {}
        for pair in kwargs["env"]:
            k, _, v = pair.partition("=")
            env_dict[k] = v
        overrides.setdefault("sandbox", {})["env"] = env_dict
    if kwargs.get("label"):
        label_dict = {}
        for pair in kwargs["label"]:
            k, _, v = pair.partition("=")
            label_dict[k] = v
        overrides.setdefault("sandbox", {})["labels"] = label_dict

    return overrides


def _resolve_prompt(prompt: str | None, prompt_file: str | None) -> str | None:
    if prompt is not None:
        return prompt
    if prompt_file:
        p = Path(prompt_file)
        if not p.is_file():
            raise click.BadParameter(
                f"File not found: {prompt_file}", param_hint="--prompt-file"
            )
        return p.read_text().strip()
    return None


def _resolve_project_dir(directory: str | None) -> Path:
    if directory:
        p = Path(directory).resolve()
        if not p.is_dir():
            raise click.BadParameter(
                f"Directory not found: {directory}", param_hint="DIRECTORY"
            )
        return p
    return Path.cwd()


# ---------------------------------------------------------------------------
# Shared options decorator
# ---------------------------------------------------------------------------

def run_options(fn):
    """Decorator that adds all run/config shared options."""
    decorators = [
        click.argument("directory", required=False, default=None),

        # Prompt (most common — short flags)
        click.option("-p", "--prompt", default=None, help="Prompt for Claude Code (switches to unattended mode)."),
        click.option("-f", "--prompt-file", default=None, type=click.Path(), help="Read prompt from file (unattended mode)."),

        # Model & provider
        click.option("-m", "--model", default=None, help="Model override (e.g., claude-opus-4-6)."),
        click.option("-P", "--provider", type=click.Choice(["vertex", "anthropic"]), default=None, help="API backend. [default: vertex]"),
        click.option("--no-github", is_flag=True, default=False, help="Skip GitHub provider setup."),

        # Engine
        click.option("-e", "--engine", type=click.Choice(["docker", "podman"]), default=None, help="Container engine. [default: docker]"),
        click.option("--image", default=None, help="Custom container image or Dockerfile path."),
        click.option("-l", "--pull", is_flag=True, default=False, help="Pull the latest container image before creating the sandbox."),

        # Workspace (local) — upload filtering (mutually exclusive)
        click.option("--enforce-gitignore", is_flag=True, default=False, help="Strict: enforce all .gitignore rules including .git/ exclusion."),
        click.option("--no-gitignore", is_flag=True, default=False, help="Permissive: ignore .gitignore rules, upload everything (DANGEROUS: may leak .env, keys)."),

        # Workspace (remote)
        click.option("-r", "--repo", default=None, help="Clone remote repo, branch, or PR. Auto-detects from format: owner/repo, owner/repo@branch, owner/repo#42, or any GitHub URL."),

        # Output
        click.option("-o", "--output", default=None, help="Save sandbox workspace to this dir on exit (default: .agentloop/sandboxes/<name>/)."),
        click.option("-D", "--discard-output", is_flag=True, default=False, help="Discard all sandbox files on exit — nothing is saved locally."),
        click.option("--output-paths", multiple=True, help="Sandbox paths to download (repeatable)."),
        click.option("--output-on-error", is_flag=True, default=False, help="Save output even on error/kill."),
        click.option("-N", "--name-only", is_flag=True, default=False,
                     help="Print ONLY the sandbox name to stdout, nothing else (unattended runs only). "
                          "Claude still runs; its output is saved to the output dir instead of streamed. "
                          "Pair with --keep to act on the sandbox afterward."),

        # Network
        click.option("--no-google-auth", is_flag=True, default=False, help="Disable Google OAuth/accounts endpoints."),
        click.option("--no-vertex", is_flag=True, default=False, help="Disable Vertex AI regional endpoints."),
        click.option("--allow-pypi", is_flag=True, default=False, help="Allow egress to pypi.org for pip/uv installs."),
        click.option("--allow-npm", is_flag=True, default=False, help="Allow egress to registry.npmjs.org."),
        click.option("--extra-host", multiple=True, help="Allow additional host. Format: HOST:PORT:ACCESS (repeatable)."),

        # Sandbox resources
        click.option("-n", "--name", default=None, help="Sandbox name (auto-generated if omitted)."),
        click.option("--keep", is_flag=True, default=False, help="Keep sandbox alive after exit (default: delete)."),
        click.option("--gpu", is_flag=True, default=False, help="Enable GPU passthrough."),
        click.option("--cpu", default=None, help="CPU limit (e.g., 2, 500m)."),
        click.option("--memory", default=None, help="Memory limit (e.g., 4Gi, 8G)."),
        click.option("--env", multiple=True, help="Inject env var into sandbox. Format: KEY=VALUE (repeatable)."),
        click.option("--label", multiple=True, help="Add sandbox label. Format: KEY=VALUE (repeatable)."),
        click.option("--forward", default=None, help="Forward local port to sandbox (e.g., 8080)."),
        click.option("--editor", type=click.Choice(["vscode", "cursor"]), default=None, help="Open remote editor after sandbox is ready."),

        # Debug
        click.option("--dry-run", is_flag=True, default=False, help="Print openshell commands without executing."),
        click.option("-t", "--timeout", type=int, default=None, help="Unattended execution timeout in seconds."),
        click.option("-v", "--verbose", count=True, help="Increase verbosity (-v, -vv, -vvv)."),
    ]
    for dec in reversed(decorators):
        fn = dec(fn)
    return fn


# ---------------------------------------------------------------------------
# CLI group (minimal — subcommands carry their own options)
# ---------------------------------------------------------------------------

@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(__version__, prog_name="agentloop")
def cli():
    """Run Claude Code in a secure OpenShell sandbox with Vertex AI and GitHub.

    \b
    Quick start:
      agentloop                                  Interactive on current dir
      agentloop /path/to/project                 Interactive on specific dir
      agentloop -p "fix the tests"               Unattended with prompt
      agentloop -r owner/repo                    Clone remote repo
      agentloop -r owner/repo@feature            Clone specific branch
      agentloop -r owner/repo#42                 Checkout a PR
      agentloop -r https://github.com/o/r/pull/1 From GitHub URL
    """


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--force", is_flag=True, help="Overwrite existing config")
@click.option("--local", is_flag=True, help="Create per-project config in current directory")
def init(force, local):
    """Create default config file."""
    if local:
        written = write_local_template(Path.cwd(), force=force)
        target = Path.cwd() / LOCAL_CONFIG_DIR / LOCAL_CONFIG_NAME
    else:
        written = write_default_config(GLOBAL_CONFIG_PATH, force=force)
        target = GLOBAL_CONFIG_PATH

    if written:
        click.echo(f"Config written to {target}")
    else:
        click.echo(f"Config already exists at {target} (use --force to overwrite)")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@cli.command("config")
@run_options
def show_config(directory, **kwargs):
    """Show resolved config."""
    project_dir = _resolve_project_dir(directory)
    global_config = load_global_config()
    local_config = load_local_config(project_dir)
    cli_over = _cli_overrides(**kwargs)
    merged = merge_configs(cli_over, local_config, global_config)

    click.echo(f"# Global: {GLOBAL_CONFIG_PATH}")
    local_path = project_dir / LOCAL_CONFIG_DIR / LOCAL_CONFIG_NAME
    if local_path.is_file():
        click.echo(f"# Local:  {local_path}")
    click.echo("# Resolved config:")
    click.echo(resolved_config_summary(merged))


# ---------------------------------------------------------------------------
# run (also the default when no subcommand is given)
# ---------------------------------------------------------------------------

@cli.command()
@run_options
@click.pass_context
def run(ctx, directory, **kwargs):
    """Create a sandboxed environment and run Claude Code inside it.

    \b
    No arguments starts interactive mode on the current directory.
    Use -p/--prompt for unattended mode (runs prompt and exits).
    See 'agentloop --help' for the full quick-start guide.
    """
    extra_args = (ctx.obj or {}).get("extra_args")
    _do_run(directory=directory, extra_args=extra_args, **kwargs)


def _do_run(*, directory, **kwargs):
    """Core run logic."""
    try:
        _do_run_inner(directory=directory, **kwargs)
    except PreflightError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    except OpenShellError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)
    except ValueError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        sys.exit(1)


def _do_run_inner(*, directory, **kwargs):
    project_dir = _resolve_project_dir(directory)

    global_config = load_global_config()
    local_config = load_local_config(project_dir)
    cli_over = _cli_overrides(**kwargs)
    config = merge_configs(cli_over, local_config, global_config)

    dry_run = kwargs.get("dry_run", False)
    verbose = kwargs.get("verbose", 0)
    name_only = bool(kwargs.get("name_only"))

    if kwargs.get("no_gitignore") and kwargs.get("enforce_gitignore"):
        raise click.UsageError("--no-gitignore and --enforce-gitignore are mutually exclusive.")

    if name_only and kwargs.get("prompt") is None and not kwargs.get("prompt_file"):
        raise click.UsageError(
            "--name-only requires -p/--prompt or -f/--prompt-file (unattended mode)."
        )

    has_remote = bool(kwargs.get("repo"))
    github_enabled = config.get("providers", {}).get("github", True)
    if has_remote and not github_enabled:
        raise click.UsageError(
            "--repo / -r requires the GitHub provider.\n"
            "Remove --no-github or set providers.github: true in config."
        )

    preflight_checks(config, verbose=verbose > 0)

    engine = config["engine"]["backend"]
    configure_compute_driver(engine, dry_run=dry_run, verbose=verbose)

    existing = list_existing_providers(dry_run=dry_run, verbose=verbose)
    provider_names: list[str] = []

    inference_provider = config["providers"]["inference"]
    if inference_provider == "vertex":
        vertex_cfg = config.get("providers", {}).get("vertex", {})
        name = ensure_vertex_provider(
            existing=existing,
            project=vertex_cfg.get("project"),
            region=vertex_cfg.get("region"),
            dry_run=dry_run,
            verbose=verbose,
        )
        provider_names.append(name)
    else:
        name = ensure_anthropic_provider(existing=existing, dry_run=dry_run, verbose=verbose)
        provider_names.append(name)

    if config["providers"]["github"]:
        name = ensure_github_provider(existing=existing, dry_run=dry_run, verbose=verbose)
        provider_names.append(name)

    configure_inference_route(
        provider_name=provider_names[0],
        model=config["model"],
        inference_timeout=config["inference"]["timeout"],
        no_verify=config["inference"]["no_verify"],
        dry_run=dry_run,
        verbose=verbose,
    )

    rules = build_policy_rules(config)
    policy_yaml = generate_policy_yaml(rules)
    if dry_run:
        click.echo("# Generated network policy:", err=True)
        click.echo(policy_yaml, err=True)
    policy_path = write_policy_tempfile(policy_yaml)

    upload_path = None if has_remote else project_dir
    prompt = _resolve_prompt(kwargs.get("prompt"), kwargs.get("prompt_file"))
    extra_args = kwargs.get("extra_args")

    sandbox_name: str | None = None
    claude_stdout: str | None = None
    start_time = time.time()
    exit_code = 1

    try:
        sandbox_cfg = SandboxConfig(
            name=kwargs.get("name"),
            provider_names=provider_names,
            policy_path=policy_path,
            image=config["engine"]["image"],
            pull=config["engine"]["pull"],
            gpu=config["sandbox"]["gpu"],
            cpu=config["sandbox"]["cpu"],
            memory=config["sandbox"]["memory"],
            upload_path=upload_path,
            upload_filter=config["sandbox"]["upload_filter"],
            env=config["sandbox"]["env"],
            labels=config["sandbox"]["labels"],
            forward=config["sandbox"]["forward"],
            editor=config["sandbox"]["editor"],
            driver_config=config["engine"]["driver_config"],
            verbose=verbose,
            dry_run=dry_run,
        )
        sandbox_name = create_sandbox(sandbox_cfg)

        workdir = setup_workspace(
            sandbox_name,
            repo_path=upload_path,
            remote=kwargs.get("repo"),
            dry_run=dry_run,
            verbose=verbose,
        )

        # Configure Claude Code settings (skip wizard, trust workspace, etc.)
        configure_claude_settings(
            sandbox_name, workdir,
            dry_run=dry_run, verbose=verbose,
        )

        claude_cmd = config["claude"]["command"]
        claude_args = list(config["claude"]["default_args"])
        claude_args.extend(["--model", config["model"]])
        if config.get("effort"):
            claude_args.extend(["--effort", config["effort"]])
        exec_timeout = config["sandbox"]["timeout"]

        exit_code, claude_stdout = run_claude(
            sandbox_name, workdir,
            prompt=prompt,
            claude_command=claude_cmd, claude_args=claude_args,
            extra_args=extra_args, timeout=exec_timeout,
            capture_output=name_only,
            dry_run=dry_run, verbose=verbose,
        )

    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)
        exit_code = 130

    finally:
        output_enabled = config["output"].get("enabled", True)
        output_dir = config["output"]["dir"]
        if not output_dir and sandbox_name:
            output_dir = str(project_dir / LOCAL_CONFIG_DIR / "sandboxes" / sandbox_name)
        if output_enabled and output_dir and sandbox_name:
            collect_output(
                sandbox_name, output_dir,
                sandbox_paths=config["output"]["paths"],
                exit_code=exit_code,
                on_error=config["output"]["on_error"],
                on_success=config["output"]["on_success"],
                prompt=prompt, start_time=start_time,
                dry_run=dry_run, verbose=verbose,
            )
            # In name-only mode Claude's output is captured, not streamed —
            # save it so nothing is lost while stdout stays clean.
            if name_only and claude_stdout and not dry_run:
                os.makedirs(output_dir, exist_ok=True)
                with open(os.path.join(output_dir, "claude_output.txt"), "w") as f:
                    f.write(claude_stdout)

        if policy_path.is_file():
            os.unlink(policy_path)

        if sandbox_name and not config["sandbox"].get("keep"):
            delete_sandbox(sandbox_name, dry_run=dry_run, verbose=verbose)

        # Surface the (possibly auto-generated) sandbox name for scripting.
        # This is the last thing written to stdout for unattended runs.
        if sandbox_name and prompt is not None:
            click.echo(sandbox_name if name_only else f"sandbox name: {sandbox_name}")

    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    argv = sys.argv[1:]

    # Split at -- to capture extra claude args
    extra_args: list[str] | None = None
    if "--" in argv:
        idx = argv.index("--")
        extra_args = argv[idx + 1:]
        argv = argv[:idx]

    # Default subcommand: if the first non-flag token isn't a known
    # subcommand, prepend "run" so Click routes correctly.
    # This lets `agentloop ../mydir --prompt "hello"` work.
    first_arg = None
    for a in argv:
        if not a.startswith("-"):
            first_arg = a
            break

    if first_arg is None or first_arg not in SUBCOMMANDS:
        argv.insert(0, "run")

    # Stash extra_args where _do_run can find them via Click context
    cli(args=argv, standalone_mode=True, obj={"extra_args": extra_args})
