"""Replaceable capability and plugin provider boundaries."""

from __future__ import annotations

from typing import Any, Protocol

from ares.capabilities.models import CapabilityMetadata, PluginManifest
from ares.workflows import WorkflowDefinition


class Capability(Protocol):
    """Build a workflow from validated semantic input."""

    metadata: CapabilityMetadata

    def build_workflow(self, payload: dict[str, Any]) -> WorkflowDefinition:
        """Return a server-owned plan; payload can never contain a command."""


class CapabilityPlugin(Protocol):
    """Installable provider of one or more capabilities."""

    manifest: PluginManifest

    def capabilities(self) -> tuple[Capability, ...]:
        """Return the provider's capability objects."""
