from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "OpenShellError",
    "OpenShellNotFoundError",
    "PreflightError",
    "run_openshell",
    "preflight_checks",
    "configure_compute_driver",
]


class OpenShellError(RuntimeError):
    """An openshell command failed."""


class OpenShellNotFoundError(OpenShellError):
    """The openshell binary was not found."""


class PreflightError(OpenShellError):
    """A preflight check failed."""

MIN_OPENSHELL_VERSION = (0, 0, 58)
MIN_PODMAN_VERSION = (5, 0, 0)

_OPENSHELL_NOT_FOUND_MSG = (
    "ERROR: openshell not found.\n\n"
    "Install with:\n"
    "  curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh\n\n"
    "Or:\n"
    "  uv tool install -U openshell"
)


def run_openshell(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    dry_run: bool = False,
    verbose: int = 0,
    timeout: int | None = None,
    passthrough: bool = False,
    stdin_data: str | None = None,
) -> subprocess.CompletedProcess | None:
    """Execute ``openshell <args>``.

    Central subprocess gateway used by every other module.
    """
    cmd = ["openshell"]
    for _ in range(verbose):
        cmd.append("-v")
    cmd.extend(args)

    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}", file=sys.stderr)
        return None

    kwargs: dict = {}
    if passthrough:
        kwargs.update(stdin=None, stdout=None, stderr=None)
    elif capture:
        kwargs.update(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    else:
        kwargs.update(stdout=None, stderr=None, text=True)

    if stdin_data is not None:
        kwargs["input"] = stdin_data

    if timeout and timeout > 0:
        kwargs["timeout"] = timeout

    try:
        result = subprocess.run(cmd, check=False, **kwargs)
    except subprocess.TimeoutExpired:
        raise OpenShellError(
            f"Command timed out after {timeout}s: {' '.join(cmd)}"
        )
    except FileNotFoundError:
        raise OpenShellNotFoundError(_OPENSHELL_NOT_FOUND_MSG)

    if check and result.returncode != 0:
        stderr_text = getattr(result, "stderr", "") or ""
        raise OpenShellError(
            f"openshell command failed (exit {result.returncode}):\n"
            f"  {' '.join(cmd)}\n"
            f"{stderr_text}"
        )

    return result


def _parse_version(version_str: str) -> tuple[int, ...]:
    match = re.search(r"(\d+\.\d+\.\d+)", version_str)
    if not match:
        return (0, 0, 0)
    return tuple(int(p) for p in match.group(1).split("."))


def _format_version(v: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in v)


def _run_check(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _ok(label: str, verbose: bool) -> None:
    if verbose:
        print(f"  [OK] {label}", file=sys.stderr)


def _stop(message: str) -> None:
    raise PreflightError(message)


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def preflight_checks(config: dict, *, verbose: bool = False) -> None:
    """Run all startup checks. Exits on first fatal error."""

    engine = config.get("engine", {}).get("backend", "docker")
    inference = config.get("providers", {}).get("inference", "vertex")
    github_enabled = config.get("providers", {}).get("github", True)

    # 1. OpenShell installed
    if not shutil.which("openshell"):
        _stop(_OPENSHELL_NOT_FOUND_MSG)
    _ok("openshell installed", verbose)

    # 2. OpenShell version
    result = _run_check(["openshell", "--version"])
    version = _parse_version(result.stdout + result.stderr)
    if version < MIN_OPENSHELL_VERSION:
        _warn(
            f"WARNING: openshell v{_format_version(version)} detected, "
            f"v{_format_version(MIN_OPENSHELL_VERSION)}+ recommended.\n"
            "  Run: openshell self-update"
        )
    _ok(f"openshell version {_format_version(version)}", verbose)

    # 3. OpenShell gateway reachable
    result = _run_check(["openshell", "status"])
    if result.returncode != 0:
        _warn(
            "WARNING: OpenShell gateway not reachable. It will be auto-bootstrapped\n"
            "on first sandbox creation. If this fails, try:\n"
            "  openshell sandbox create -- echo hello"
        )
    else:
        _ok("openshell gateway reachable", verbose)

    # 4. Container engine installed
    if not shutil.which(engine):
        if engine == "docker":
            _stop(
                "ERROR: docker not found.\n\n"
                "Install Docker Desktop:\n"
                "  https://docs.docker.com/get-docker/\n\n"
                "Or switch to podman:\n"
                "  agentloop --engine podman"
            )
        else:
            _stop(
                "ERROR: podman not found.\n\n"
                "Install Podman:\n"
                "  macOS:  brew install podman\n"
                "  Fedora: sudo dnf install podman\n"
                "  Ubuntu: sudo apt install podman\n\n"
                "Or switch to docker:\n"
                "  agentloop --engine docker"
            )
    _ok(f"{engine} installed", verbose)

    # 5. Container engine running
    result = _run_check([engine, "info"])
    if result.returncode != 0:
        is_mac = platform.system() == "Darwin"
        if engine == "docker":
            start_cmd = "open -a Docker" if is_mac else "sudo systemctl start docker"
        else:
            start_cmd = "podman machine start" if is_mac else "sudo systemctl start podman"
        _stop(
            f"ERROR: {engine} is installed but not running.\n\n"
            f"Start it with:\n  {start_cmd}"
        )
    _ok(f"{engine} running", verbose)

    # 6. Podman version check
    if engine == "podman":
        result = _run_check(["podman", "--version"])
        podman_version = _parse_version(result.stdout)
        if podman_version < MIN_PODMAN_VERSION:
            _stop(
                f"ERROR: Podman {_format_version(podman_version)} detected, "
                f"{_format_version(MIN_PODMAN_VERSION)}+ required for OpenShell.\n\n"
                "Upgrade:\n"
                "  macOS:  brew upgrade podman\n"
                "  Fedora: sudo dnf upgrade podman\n"
                "  Ubuntu: sudo apt upgrade podman"
            )
        _ok(f"podman version {_format_version(podman_version)}", verbose)

    # 7. Credentials
    if inference == "vertex":
        adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if not adc_path.is_file():
            _stop(
                "ERROR: Google Cloud Application Default Credentials not found.\n\n"
                "The Vertex AI provider requires gcloud ADC to authenticate.\n\n"
                "To fix, run:\n"
                "  gcloud auth application-default login\n\n"
                "This will open a browser to authenticate and create:\n"
                f"  {adc_path}"
            )
        _ok("gcloud ADC found", verbose)

        project = (
            os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
            or config.get("providers", {}).get("vertex", {}).get("project")
        )
        if not project:
            _stop(
                "ERROR: Google Cloud project ID not configured.\n\n"
                "Set it via environment variable:\n"
                "  export ANTHROPIC_VERTEX_PROJECT_ID=my-gcp-project\n\n"
                "Or add to ~/.agentloop/agentloop.yaml:\n"
                "  providers:\n"
                "    vertex:\n"
                "      project: my-gcp-project"
            )
        _ok(f"vertex project: {project}", verbose)

        region = (
            os.environ.get("CLOUD_ML_REGION")
            or config.get("providers", {}).get("vertex", {}).get("region")
        )
        if not region:
            _warn(
                "WARNING: CLOUD_ML_REGION not set, using default: global\n\n"
                "To change, set:\n"
                "  export CLOUD_ML_REGION=us-east-1\n\n"
                "Or add to config:\n"
                "  providers:\n"
                "    vertex:\n"
                "      region: us-east-1"
            )
        else:
            _ok(f"vertex region: {region}", verbose)

    elif inference == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            _stop(
                "ERROR: ANTHROPIC_API_KEY environment variable not set.\n\n"
                "The Anthropic provider requires an API key.\n\n"
                "To fix, run:\n"
                "  export ANTHROPIC_API_KEY=sk-ant-...\n\n"
                "Get your key at: https://console.anthropic.com/settings/keys"
            )
        _ok("ANTHROPIC_API_KEY set", verbose)

    if github_enabled:
        gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not gh_token:
            # Try to get token from gh CLI
            if shutil.which("gh"):
                result = _run_check(["gh", "auth", "token"])
                if result.returncode == 0 and result.stdout.strip():
                    gh_token = result.stdout.strip()
                    os.environ["GITHUB_TOKEN"] = gh_token
                    _ok("GitHub token resolved from gh CLI", verbose)
        if not gh_token:
            _stop(
                "ERROR: GitHub token not found.\n\n"
                "None of these sources provided a token:\n"
                "  - GITHUB_TOKEN env var\n"
                "  - GH_TOKEN env var\n"
                "  - gh auth token (gh CLI not logged in or not installed)\n\n"
                "To fix, either:\n"
                "  gh auth login\n\n"
                "Or export a token manually:\n"
                "  export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx\n"
                "  (create at https://github.com/settings/tokens with 'repo' scope)\n\n"
                "To skip GitHub (no git push / PR support), set:\n"
                "  --no-github flag, or providers.github: false in config"
            )
        else:
            _ok("GitHub token set", verbose)


def configure_compute_driver(
    engine: str,
    *,
    dry_run: bool = False,
    verbose: int = 0,
) -> None:
    """Set the OpenShell compute driver if needed."""
    run_openshell(
        [
            "settings",
            "set",
            "--global",
            "--key",
            "compute_driver",
            "--value",
            engine,
            "--yes",
        ],
        check=False,
        dry_run=dry_run,
        verbose=verbose,
    )
