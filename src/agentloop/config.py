from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

import yaml

__all__ = [
    "GLOBAL_CONFIG_PATH",
    "LOCAL_CONFIG_DIR",
    "LOCAL_CONFIG_NAME",
    "BUILTIN_DEFAULTS",
    "load_global_config",
    "load_local_config",
    "merge_configs",
    "write_default_config",
    "write_local_template",
]

GLOBAL_CONFIG_DIR = Path.home() / ".agentloop"
GLOBAL_CONFIG_PATH = GLOBAL_CONFIG_DIR / "agentloop.yaml"
LOCAL_CONFIG_DIR = ".agentloop"
LOCAL_CONFIG_NAME = "agentloop.yaml"

BUILTIN_DEFAULTS: dict = {
    "claude": {
        "command": "claude",
        "default_args": [
            "--permission-mode",
            "dontAsk",
            "--settings",
            "/sandbox/.agentloop-settings.json",
            "--allowedTools",
            "Bash(*)",
            "Read",
            "Edit",
            "Write",
            "NotebookEdit",
            "WebFetch",
            "WebSearch",
        ],
    },
    "engine": {
        "backend": "docker",
        "image": None,
        "pull": False,
        "driver_config": None,
    },
    "providers": {
        "inference": "vertex",
        "github": True,
        "auto_providers": False,
        "vertex": {
            "project": None,
            "region": "global",
        },
    },
    "model": "claude-opus-4-6",
    "effort": "high",
    "inference": {
        "timeout": 120,
        "no_verify": True,
    },
    "network": {
        "github": True,
        "google_auth": True,
        "vertex": True,
        "allow_pypi": False,
        "allow_npm": False,
        "extra_hosts": [],
        "vertex_regions": [
            "global",
            "us-east-1",
            "us-central1",
            "us-west1",
            "us-west4",
            "europe-west1",
            "europe-west4",
            "asia-southeast1",
        ],
    },
    "output": {
        "enabled": True,
        "dir": None,
        "paths": ["/sandbox"],
        "on_error": True,
        "on_success": True,
    },
    "sandbox": {
        "keep": False,
        "gpu": False,
        "cpu": None,
        "memory": None,
        "timeout": 0,
        "upload_filter": "safe",
        "env": {},
        "labels": {"managed-by": "agentloop"},
        "forward": None,
        "editor": None,
    },
    "openshell": {
        "telemetry": True,
        "theme": "auto",
        "providers_v2": False,
    },
}

_DEFAULT_CONFIG_YAML = """\
# AgentLoop configuration
# CLI flags override these values. Credentials are NEVER stored here.

# ─── Claude Code CLI ───────────────────────────────────────────────
claude:
  command: claude                      # CLI binary to run inside the sandbox
  default_args:                        # args always passed to claude
    - --permission-mode
    - dontAsk
    - --settings
    - /sandbox/.agentloop-settings.json
    - --allowedTools
    - "Bash(*)"
    - Read
    - Edit
    - Write
    - NotebookEdit
    - WebFetch
    - WebSearch

# ─── Container Engine ──────────────────────────────────────────────
engine:
  backend: docker                      # docker | podman
  image: null                          # custom image/Dockerfile path (null = built-in)
  pull: false                          # always pull latest image before creating sandbox
  driver_config: null                  # JSON string for driver-specific config

# ─── Providers ─────────────────────────────────────────────────────
providers:
  inference: vertex                    # vertex | anthropic
  github: true                         # enable GitHub provider
  auto_providers: false                # let OpenShell auto-create providers from env
  vertex:
    project: null                      # GCP project ID (or ANTHROPIC_VERTEX_PROJECT_ID env)
    region: global                      # Vertex AI region (or CLOUD_ML_REGION env)

# ─── Model ─────────────────────────────────────────────────────────
model: claude-opus-4-6
effort: high                           # low | medium | high

# ─── Inference Routing ─────────────────────────────────────────────
inference:
  timeout: 120                         # per-request inference timeout (seconds)
  no_verify: true                      # skip endpoint liveness probe on setup

# ─── Network Policy ───────────────────────────────────────────────
network:
  github: true                         # github.com, api.github.com, *.githubusercontent.com
  google_auth: true                    # oauth2.googleapis.com, accounts.google.com
  vertex: true                         # Vertex AI regional endpoints
  allow_pypi: false                    # pypi.org, files.pythonhosted.org
  allow_npm: false                     # registry.npmjs.org
  extra_hosts: []                      # list of "host:port:access" strings
  vertex_regions:
    - global
    - us-east-1
    - us-central1
    - us-west1
    - us-west4
    - europe-west1
    - europe-west4
    - asia-southeast1

# ─── Output / Artifact Collection ──────────────────────────────────
output:
  enabled: true                        # save sandbox files on exit (--no-output to disable)
  dir: null                            # override output dir (default: .agentloop/sandboxes/<name>/)
  paths:
    - /sandbox
  on_error: true
  on_success: true

# ─── Sandbox Resources ────────────────────────────────────────────
sandbox:
  keep: false                           # keep sandbox alive after exit (default: delete)
  gpu: false
  cpu: null
  memory: null
  timeout: 0                           # exec timeout in seconds (0 = none)
  upload_filter: safe                   # safe: .gitignore respected, .git/ included (default)
                                       # strict: .gitignore respected, .git/ excluded too
                                       # all: no filtering, uploads everything (DANGEROUS)
  env: {}
  labels:
    managed-by: agentloop
  forward: null                        # port forwarding (e.g. "8080")
  editor: null                         # vscode | cursor | null

# ─── OpenShell Settings ───────────────────────────────────────────
openshell:
  telemetry: true
  theme: auto                          # auto | dark | light
  providers_v2: false
"""

_LOCAL_TEMPLATE_YAML = """\
# Project-specific AgentLoop overrides (.agentloop/agentloop.yaml)
# Only include keys you want to override from the global config.
# See ~/.agentloop/agentloop.yaml for all available keys.

# model: claude-opus-4-6
# network:
#   allow_pypi: true
# sandbox:
#   memory: 16Gi
#   gpu: true
#   env:
#     PROJECT_ENV: staging
"""


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*.

    Dict values merge recursively; scalars and lists replace entirely.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as exc:
        print(f"WARNING: Failed to parse {path}: {exc}", file=sys.stderr)
        return {}


def load_global_config() -> dict:
    return _load_yaml(GLOBAL_CONFIG_PATH)


def load_local_config(project_dir: Path) -> dict:
    return _load_yaml(project_dir / LOCAL_CONFIG_DIR / LOCAL_CONFIG_NAME)


def _apply_env_overrides(merged: dict) -> dict:
    """Apply environment variable overrides into the merged config dict."""
    env_map = {
        "AGENTLOOP_ENGINE": ("engine", "backend"),
        "AGENTLOOP_PROVIDER": ("providers", "inference"),
        "AGENTLOOP_MODEL": ("model",),
        "ANTHROPIC_VERTEX_PROJECT_ID": ("providers", "vertex", "project"),
        "CLOUD_ML_REGION": ("providers", "vertex", "region"),
    }
    for env_var, key_path in env_map.items():
        value = os.environ.get(env_var)
        if value is None:
            continue
        target = merged
        for part in key_path[:-1]:
            target = target.setdefault(part, {})
        target[key_path[-1]] = value
    return merged


def merge_configs(
    cli_overrides: dict,
    local_config: dict,
    global_config: dict,
) -> dict:
    """Merge configs in resolution order: CLI > env > local > global > builtin."""
    merged = copy.deepcopy(BUILTIN_DEFAULTS)
    merged = _deep_merge(merged, global_config)
    merged = _deep_merge(merged, local_config)
    merged = _apply_env_overrides(merged)
    merged = _deep_merge(merged, cli_overrides)
    return merged


def write_default_config(path: Path, *, force: bool = False) -> bool:
    """Write the default global config. Returns True if written."""
    if path.is_file() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_DEFAULT_CONFIG_YAML)
    return True


def write_local_template(directory: Path, *, force: bool = False) -> bool:
    """Write a minimal local config template. Returns True if written."""
    target = directory / LOCAL_CONFIG_DIR / LOCAL_CONFIG_NAME
    if target.is_file() and not force:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_LOCAL_TEMPLATE_YAML)
    return True


def resolved_config_summary(config: dict) -> str:
    """Return YAML representation of the resolved config for display."""
    return yaml.dump(config, default_flow_style=False, sort_keys=False)
