from __future__ import annotations

import os
import sys

from .engine import run_openshell

__all__ = [
    "list_existing_providers",
    "ensure_vertex_provider",
    "ensure_anthropic_provider",
    "ensure_github_provider",
    "configure_inference_route",
]


def list_existing_providers(
    *,
    dry_run: bool = False,
    verbose: int = 0,
) -> set[str]:
    """Return the set of existing provider names."""
    result = run_openshell(
        ["provider", "list", "--names"],
        check=False,
        dry_run=dry_run,
        verbose=verbose,
    )
    if result is None or result.returncode != 0:
        return set()
    return {
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip()
    }


def ensure_vertex_provider(
    name: str = "vertex",
    *,
    project: str | None = None,
    region: str | None = None,
    existing: set[str],
    dry_run: bool = False,
    verbose: int = 0,
) -> str:
    """Create Vertex AI provider from gcloud ADC if not already present.

    Always updates config (project/region) even if the provider exists,
    since the user may have changed ANTHROPIC_VERTEX_PROJECT_ID or CLOUD_ML_REGION.
    """
    project = project or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
    region = region or os.environ.get("CLOUD_ML_REGION") or "global"

    if name not in existing:
        run_openshell(
            [
                "provider",
                "create",
                "--name",
                name,
                "--type",
                "google-vertex-ai",
                "--from-gcloud-adc",
            ],
            dry_run=dry_run,
            verbose=verbose,
        )
    else:
        if verbose:
            print(f"  Provider '{name}' already exists, updating config.", file=sys.stderr)

    # Always set project and region config on the provider
    config_args = ["provider", "update", name]
    if project:
        config_args.extend(["--config", f"VERTEX_AI_PROJECT_ID={project}"])
    if region:
        config_args.extend(["--config", f"VERTEX_AI_REGION={region}"])

    if len(config_args) > 3:
        run_openshell(config_args, dry_run=dry_run, verbose=verbose)

    return name


def ensure_anthropic_provider(
    name: str = "anthropic",
    *,
    existing: set[str],
    dry_run: bool = False,
    verbose: int = 0,
) -> str:
    """Create Anthropic provider from ANTHROPIC_API_KEY if not already present."""
    if name in existing:
        if verbose:
            print(f"  Provider '{name}' already exists, skipping.", file=sys.stderr)
        return name

    run_openshell(
        [
            "provider",
            "create",
            "--name",
            name,
            "--type",
            "anthropic",
            "--from-existing",
        ],
        dry_run=dry_run,
        verbose=verbose,
    )
    return name


def ensure_github_provider(
    name: str = "github",
    *,
    existing: set[str],
    dry_run: bool = False,
    verbose: int = 0,
) -> str:
    """Create or update GitHub provider from GITHUB_TOKEN/GH_TOKEN.

    Always refreshes credentials since tokens can expire or rotate.
    """
    if name in existing:
        if verbose:
            print(f"  Provider '{name}' already exists, refreshing credentials.", file=sys.stderr)
        run_openshell(
            ["provider", "update", name, "--from-existing"],
            check=False, dry_run=dry_run, verbose=verbose,
        )
        return name

    run_openshell(
        [
            "provider",
            "create",
            "--name",
            name,
            "--type",
            "github",
            "--from-existing",
        ],
        dry_run=dry_run,
        verbose=verbose,
    )
    return name


def configure_inference_route(
    provider_name: str,
    model: str,
    *,
    inference_timeout: int = 120,
    no_verify: bool = True,
    dry_run: bool = False,
    verbose: int = 0,
) -> None:
    """Configure inference.local to route through the given provider and model."""
    cmd = [
        "inference",
        "set",
        "--provider",
        provider_name,
        "--model",
        model,
        "--timeout",
        str(inference_timeout),
    ]
    if no_verify:
        cmd.append("--no-verify")

    run_openshell(cmd, check=False, dry_run=dry_run, verbose=verbose)
