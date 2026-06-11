"""AgentLoop: Sandboxed Claude Code agent powered by OpenShell."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("agentloop")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
