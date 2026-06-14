from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .engine import OpenShellError, run_openshell

__all__ = [
    "SandboxConfig",
    "create_sandbox",
    "setup_workspace",
    "run_claude",
    "collect_output",
    "configure_claude_settings",
    "delete_sandbox",
]


_DEFAULT_SANDBOX_IMAGE = "ghcr.io/nvidia/openshell-community/sandboxes/base:latest"

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")

_RESERVED_ENV_VARS = {
    "ANTHROPIC_BASE_URL": "https://inference.local",
    "ANTHROPIC_API_KEY": "unused",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    "PATH": "/home/user/.local/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin",
}


def _validate_repo_components(
    owner: str,
    repo: str,
    branch: str | None = None,
    pr_num: str | None = None,
) -> None:
    if not _SAFE_NAME_RE.match(owner):
        raise ValueError(
            f"Invalid repository owner: {owner!r} "
            f"(allowed: letters, digits, '.', '_', '-')"
        )
    if not _SAFE_NAME_RE.match(repo) or repo in (".", ".."):
        raise ValueError(
            f"Invalid repository name: {repo!r} "
            f"(allowed: letters, digits, '.', '_', '-')"
        )
    if pr_num is not None and not pr_num.isdigit():
        raise ValueError(
            f"Invalid PR number: {pr_num!r} (must be a positive integer)"
        )
    if branch is not None:
        if not _SAFE_BRANCH_RE.match(branch):
            raise ValueError(
                f"Invalid branch name: {branch!r} "
                f"(allowed: letters, digits, '.', '_', '/', '-')"
            )
        if ".." in branch:
            raise ValueError(
                f"Invalid branch name: {branch!r} (contains '..')"
            )
        if branch.endswith("/"):
            raise ValueError(
                f"Invalid branch name: {branch!r} (cannot end with '/')"
            )


@dataclass
class SandboxConfig:
    provider_names: list[str]
    policy_path: Path
    name: str | None = None
    image: str | None = None
    pull: bool = False
    gpu: bool = False
    cpu: str | None = None
    memory: str | None = None
    upload_path: Path | None = None
    upload_filter: str = "safe"
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    forward: str | None = None
    editor: str | None = None
    driver_config: str | None = None
    verbose: int = 0
    dry_run: bool = False


def _pull_container_image(
    image: str,
    *,
    dry_run: bool = False,
) -> None:
    """Pull a container image via the configured engine (docker/podman)."""
    engine = os.environ.get("AGENTLOOP_ENGINE", "docker")
    cmd = [engine, "pull", image]
    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}", file=sys.stderr)
        return
    print(f"  Pulling {image} ...", file=sys.stderr)
    result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        print(f"WARNING: Failed to pull {image}: {(result.stderr or '').strip()}", file=sys.stderr)


def create_sandbox(config: SandboxConfig) -> str:
    """Create an OpenShell sandbox. Returns the sandbox name."""
    cmd = ["sandbox", "create"]

    if config.name:
        cmd.extend(["--name", config.name])

    if config.image:
        cmd.extend(["--from", config.image])

    if config.pull:
        pull_image = config.image or _DEFAULT_SANDBOX_IMAGE
        _pull_container_image(pull_image, dry_run=config.dry_run)

    for provider in config.provider_names:
        cmd.extend(["--provider", provider])

    cmd.extend(["--policy", str(config.policy_path)])

    if config.upload_path:
        cmd.extend(["--upload", str(config.upload_path)])
        if config.upload_filter == "all":
            cmd.append("--no-git-ignore")

    if config.gpu:
        cmd.append("--gpu")

    if config.cpu:
        cmd.extend(["--cpu", config.cpu])

    if config.memory:
        cmd.extend(["--memory", config.memory])

    for key, value in config.env.items():
        if key.upper() in _RESERVED_ENV_VARS:
            print(f"WARNING: Ignoring reserved environment variable: {key}", file=sys.stderr)
            continue
        cmd.extend(["--env", f"{key}={value}"])

    # Security-critical env vars — always last so they cannot be overridden
    for key, value in _RESERVED_ENV_VARS.items():
        cmd.extend(["--env", f"{key}={value}"])

    for key, value in config.labels.items():
        cmd.extend(["--label", f"{key}={value}"])

    if config.forward:
        cmd.extend(["--forward", config.forward])

    if config.editor:
        cmd.extend(["--editor", config.editor])

    if config.driver_config:
        cmd.extend(["--driver-config-json", config.driver_config])

    cmd.append("--no-auto-providers")
    cmd.append("--no-tty")

    # Run a quick command so create returns immediately; sandbox stays alive
    cmd.extend(["--", "echo", "ready"])

    result = run_openshell(
        cmd,
        dry_run=config.dry_run,
        verbose=config.verbose,
    )

    if result is None:
        return config.name or "dry-run-sandbox"

    # Parse sandbox name from "Created sandbox: <name>" in output
    raw = (result.stdout or "") + (result.stderr or "")
    # Strip ANSI escape codes
    clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    match = re.search(r"Created sandbox:\s*(\S+)", clean)
    sandbox_name = match.group(1) if match else config.name
    if not sandbox_name:
        raise OpenShellError("Failed to parse sandbox name from openshell output")

    # "safe" mode: .gitignore is respected so .git/ may have been excluded —
    # upload it separately so the sandbox has a proper git workspace.
    # "strict" mode: .git/ is intentionally excluded.
    # "all" mode: --no-git-ignore already included everything.
    if (
        sandbox_name
        and config.upload_filter == "safe"
        and config.upload_path
        and (config.upload_path / ".git").is_dir()
    ):
        dirname = config.upload_path.name
        run_openshell(
            ["sandbox", "upload", sandbox_name,
             str(config.upload_path / ".git"), f"/sandbox/{dirname}/.git"],
            check=False,
            dry_run=config.dry_run,
            verbose=config.verbose,
        )

    return sandbox_name


def configure_claude_settings(
    sandbox_name: str,
    workdir: str = "/sandbox",
    *,
    dry_run: bool = False,
    verbose: int = 0,
) -> None:
    """Write Claude Code settings to skip the first-run wizard.

    Uses sandbox upload (not exec) to avoid shell quoting issues.
    """
    import tempfile

    # Pre-trust the workspace and common parent paths
    trusted_project = {"hasTrustDialogAccepted": True, "allowedTools": []}
    claude_json = json.dumps({
        "numStartups": 1,
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": "9.9.9",
        "lastReleaseNotesSeen": "9.9.9",
        "customApiKeyResponses": {"approved": ["unused"], "rejected": []},
        "bypassPermissionsAccepted": True,
        "projects": {
            "/sandbox": trusted_project,
            workdir: trusted_project,
        },
    })
    settings_json = json.dumps({
        "theme": "dark",
        "hasCompletedOnboarding": True,
        "hasDismissedAnnouncement": True,
        "permissions": {
            "defaultMode": "bypassPermissions",
            "skipDangerousModePermissionPrompt": True,
        },
    })

    uploads = [
        (claude_json, "/sandbox/.claude.json"),
        (settings_json, "/sandbox/.agentloop-settings.json"),
    ]

    for content, dest in uploads:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        try:
            tmp.write(content)
            tmp.close()
            run_openshell(
                ["sandbox", "upload", sandbox_name, tmp.name, dest],
                check=False, dry_run=dry_run, verbose=verbose,
            )
        finally:
            os.unlink(tmp.name)

    # Ensure ~/.local/bin is on PATH inside interactive/login shells too.
    # The --env PATH we set at create time can be overridden by .bashrc/.profile,
    # so we prepend it in both profile files to make it stick.
    run_openshell(
        ["sandbox", "exec", "-n", sandbox_name, "--no-tty", "--",
         "bash", "-c",
         'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.bashrc && '
         'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.profile'],
        check=False, dry_run=dry_run, verbose=verbose,
    )


def _normalize_spec(spec: str) -> str:
    """Strip GitHub URL prefix if present.

    Accepts:
      owner/repo
      github.com/owner/repo
      https://github.com/owner/repo
      https://github.com/owner/repo/pull/42
      https://github.com/owner/repo/tree/branch
    Returns the path portion after github.com/.
    """
    spec = spec.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if spec.startswith(prefix):
            spec = spec[len(prefix):]
            break
    return spec


def _parse_repo_spec(spec: str) -> tuple[str, str]:
    """Parse OWNER/REPO from a pre-normalized repo spec."""
    match = re.match(r"^([^/@#]+)/([^/@#]+)", spec)
    if not match:
        raise ValueError(f"Invalid repo spec: {spec}")
    return match.group(1), match.group(2)


def _parse_remote(spec: str) -> tuple[str, str, str | None, str | None]:
    """Parse a remote spec into (owner, repo, branch_or_none, pr_num_or_none).

    Auto-detects format:
      owner/repo                              -> clone default branch
      owner/repo@ref                          -> clone + checkout ref
      owner/repo#42                           -> clone + checkout PR
      https://github.com/owner/repo           -> clone default branch
      https://github.com/owner/repo/tree/ref  -> clone + checkout ref
      https://github.com/owner/repo/pull/42   -> clone + checkout PR
      github.com/owner/repo                   -> clone default branch
    """
    normalized = _normalize_spec(spec)

    # URL: owner/repo/pull/NUM
    if (pull_match := re.match(r"^([^/]+)/([^/]+)/pull/(\d+)", normalized)):
        result = pull_match.group(1), pull_match.group(2), None, pull_match.group(3)

    # URL: owner/repo/tree/REF
    elif (tree_match := re.match(r"^([^/]+)/([^/]+)/tree/(.+)$", normalized)):
        result = tree_match.group(1), tree_match.group(2), tree_match.group(3), None

    # owner/repo#NUM
    elif "#" in normalized:
        hash_idx = normalized.index("#")
        owner, repo = _parse_repo_spec(normalized[:hash_idx])
        result = owner, repo, None, normalized[hash_idx + 1:]

    # owner/repo@REF
    elif "@" in normalized:
        at_idx = normalized.index("@")
        owner, repo = _parse_repo_spec(normalized[:at_idx])
        result = owner, repo, normalized[at_idx + 1:], None

    # owner/repo (default branch)
    else:
        owner, repo = _parse_repo_spec(normalized)
        result = owner, repo, None, None

    _validate_repo_components(*result)
    return result


def setup_workspace(
    sandbox_name: str,
    *,
    repo_path: Path | None = None,
    remote: str | None = None,
    dry_run: bool = False,
    verbose: int = 0,
) -> str:
    """Prepare workspace inside the sandbox. Returns the workdir path."""

    if remote:
        owner, repo, branch, pr_num = _parse_remote(remote)

        run_openshell(
            ["sandbox", "exec", "-n", sandbox_name, "--no-tty",
             "--workdir", "/sandbox", "--",
             "gh", "repo", "clone", f"{owner}/{repo}"],
            dry_run=dry_run,
            verbose=verbose,
        )

        repo_workdir = f"/sandbox/{repo}"

        if pr_num:
            run_openshell(
                ["sandbox", "exec", "-n", sandbox_name, "--no-tty",
                 "--workdir", repo_workdir, "--",
                 "gh", "pr", "checkout", pr_num],
                dry_run=dry_run,
                verbose=verbose,
            )
        elif branch:
            run_openshell(
                ["sandbox", "exec", "-n", sandbox_name, "--no-tty",
                 "--workdir", repo_workdir, "--",
                 "git", "checkout", branch],
                dry_run=dry_run,
                verbose=verbose,
            )

        return repo_workdir

    elif repo_path:
        dirname = repo_path.name
        return f"/sandbox/{dirname}"

    else:
        return "/sandbox"


def run_claude(
    sandbox_name: str,
    workdir: str,
    *,
    prompt: str | None = None,
    claude_command: str = "claude",
    claude_args: list[str] | None = None,
    extra_args: list[str] | None = None,
    timeout: int = 0,
    capture_output: bool = False,
    dry_run: bool = False,
    verbose: int = 0,
) -> tuple[int, str | None]:
    """Run Claude Code. Unattended if prompt is given, interactive otherwise.

    Returns (exit_code, captured_stdout). captured_stdout is non-None only when
    capture_output is set on an unattended run (name-only mode): Claude's output
    is captured instead of streamed to the terminal.
    """
    unattended = prompt is not None
    quiet = unattended and capture_output
    cmd = ["sandbox", "exec", "-n", sandbox_name, "--workdir", workdir]

    if unattended:
        cmd.append("--no-tty")
        if timeout > 0:
            cmd.extend(["--timeout", str(timeout)])
    else:
        cmd.append("--tty")

    cmd.extend(["--", claude_command])
    if claude_args:
        cmd.extend(claude_args)
    if unattended:
        cmd.extend(["--print", prompt])
    if extra_args:
        cmd.extend(extra_args)

    result = run_openshell(
        cmd, check=False,
        passthrough=not unattended,
        capture=quiet,
        dry_run=dry_run, verbose=verbose,
    )
    if result is None:
        return 0, None
    return result.returncode, (result.stdout if quiet else None)


def collect_output(
    sandbox_name: str,
    output_dir: str,
    *,
    sandbox_paths: list[str] | None = None,
    exit_code: int = 0,
    on_error: bool = True,
    on_success: bool = True,
    prompt: str | None = None,
    start_time: float | None = None,
    dry_run: bool = False,
    verbose: int = 0,
) -> None:
    """Download sandbox paths to the local output directory."""
    should_save = (exit_code == 0 and on_success) or (exit_code != 0 and on_error)
    if not should_save:
        return

    paths = sandbox_paths or ["/sandbox"]
    os.makedirs(output_dir, exist_ok=True)

    for spath in paths:
        run_openshell(
            [
                "sandbox",
                "download",
                sandbox_name,
                spath,
                output_dir,
            ],
            check=False,
            dry_run=dry_run,
            verbose=verbose,
        )

    # Write metadata
    metadata = {
        "sandbox_name": sandbox_name,
        "exit_code": exit_code,
        "prompt": prompt,
        "duration_seconds": round(time.time() - start_time, 1) if start_time else None,
    }
    meta_path = os.path.join(output_dir, "_metadata.json")
    if not dry_run:
        with open(meta_path, "w") as f:
            json.dump(metadata, f, indent=2)
    else:
        print(f"[dry-run] Would write metadata to {meta_path}", file=sys.stderr)


def delete_sandbox(
    sandbox_name: str,
    *,
    dry_run: bool = False,
    verbose: int = 0,
) -> None:
    """Delete a sandbox."""
    run_openshell(
        ["sandbox", "delete", sandbox_name],
        check=False,
        dry_run=dry_run,
        verbose=verbose,
    )
