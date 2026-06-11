from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

__all__ = [
    "PolicyRule",
    "build_policy_rules",
    "generate_policy_yaml",
    "write_policy_tempfile",
]


@dataclass
class PolicyEndpoint:
    host: str
    port: int = 443
    protocol: str = "rest"
    enforcement: str = "enforce"
    access: str = "read-only"


@dataclass
class PolicyRule:
    key: str
    endpoints: list[PolicyEndpoint] = field(default_factory=list)
    binaries: list[dict[str, str]] | None = None


def _endpoints(targets: list[str], access: str) -> list[PolicyEndpoint]:
    """Build endpoint list from host:port strings."""
    result = []
    for target in targets:
        parts = target.rsplit(":", 1)
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 443
        result.append(PolicyEndpoint(host=host, port=port, access=access))
    return result


def build_policy_rules(config: dict) -> list[PolicyRule]:
    """Build network policy rules from the resolved config."""
    net = config.get("network", {})
    rules: list[PolicyRule] = []

    if net.get("github", True):
        rules.append(PolicyRule(
            key="github",
            endpoints=[
                PolicyEndpoint(host="github.com", port=443, protocol="rest", access="read-write"),
                PolicyEndpoint(host="api.github.com", port=443, protocol="rest", access="read-write"),
                PolicyEndpoint(host="raw.githubusercontent.com", port=443, protocol="rest", access="read-only"),
                PolicyEndpoint(host="objects.githubusercontent.com", port=443, protocol="rest", access="read-only"),
            ],
            binaries=[
                {"path": "/usr/bin/gh"},
                {"path": "/usr/local/bin/gh"},
                {"path": "/usr/bin/git"},
                {"path": "/usr/local/bin/git"},
                {"path": "**/node"},
                {"path": "**/claude"},
            ],
        ))

    if net.get("google_auth", True):
        rules.append(PolicyRule(
            key="google_oauth",
            endpoints=_endpoints(["oauth2.googleapis.com:443"], "read-write"),
        ))
        rules.append(PolicyRule(
            key="google_accounts",
            endpoints=_endpoints(["accounts.google.com:443"], "read-only"),
        ))
        rules.append(PolicyRule(
            key="google_apis",
            endpoints=_endpoints(["www.googleapis.com:443"], "read-only"),
        ))

    if net.get("vertex", True):
        regions = net.get("vertex_regions", ["global"])
        targets = [f"{region}-aiplatform.googleapis.com:443" for region in regions]
        rules.append(PolicyRule(
            key="vertex_ai",
            endpoints=_endpoints(targets, "read-write"),
        ))

    if net.get("allow_pypi", False):
        rules.append(PolicyRule(
            key="pypi",
            endpoints=_endpoints([
                "pypi.org:443",
                "files.pythonhosted.org:443",
            ], "read-only"),
        ))

    if net.get("allow_npm", False):
        rules.append(PolicyRule(
            key="npm",
            endpoints=_endpoints(["registry.npmjs.org:443"], "read-only"),
        ))

    for host_spec in net.get("extra_hosts", []):
        parts = host_spec.split(":")
        if len(parts) == 3:
            host, port, access = parts
        elif len(parts) == 2:
            host, port = parts
            access = "read-only"
        else:
            print(
                f"WARNING: Ignoring malformed --extra-host: {host_spec!r} "
                f"(expected HOST:PORT or HOST:PORT:ACCESS)",
                file=sys.stderr,
            )
            continue
        safe_key = f"extra_{host.replace('.', '_').replace('-', '_')}"
        rules.append(PolicyRule(
            key=safe_key,
            endpoints=_endpoints([f"{host}:{port}"], access),
        ))

    return rules


def generate_policy_yaml(rules: list[PolicyRule]) -> str:
    """Serialize policy rules to OpenShell YAML policy format."""
    network_policies = {}
    for rule in rules:
        entry: dict = {
            "name": rule.key,
            "endpoints": [
                {
                    "host": ep.host,
                    "port": ep.port,
                    "protocol": ep.protocol,
                    "enforcement": ep.enforcement,
                    "access": ep.access,
                }
                for ep in rule.endpoints
            ],
        }
        if rule.binaries:
            entry["binaries"] = rule.binaries
        network_policies[rule.key] = entry

    policy = {
        "version": 1,
        "filesystem_policy": {
            "include_workdir": True,
            "read_write": ["/sandbox", "/tmp", "/dev/null", "/dev/urandom"],
            "read_only": ["/usr", "/lib", "/proc", "/etc", "/bin", "/sbin", "/opt"],
        },
        "network_policies": network_policies,
    }
    return yaml.dump(policy, default_flow_style=False, sort_keys=False)


def write_policy_tempfile(yaml_content: str) -> Path:
    """Write policy YAML to a temp file. Caller must clean up."""
    fd, path = tempfile.mkstemp(
        prefix="agentloop-policy-",
        suffix=".yaml",
    )
    with open(fd, "w") as f:
        f.write(yaml_content)
    return Path(path)
