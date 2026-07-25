"""Replaceable capability and plugin provider boundaries."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from ares.capabilities.models import CapabilityMetadata, PluginManifest
from ares.workflows import WorkflowDefinition


class Capability(Protocol):
    """Validate semantic input and build a private workflow.

    Capabilities expose a typed input model to the core. They never execute a
    command and never accept an action/tool identifier from a client or LLM.
    """

    metadata: CapabilityMetadata
    input_model: type[BaseModel]

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        """Return a server-owned plan from already validated semantic input."""


class CapabilityPlugin(Protocol):
    """Installable provider of one or more capabilities."""

    manifest: PluginManifest

    def capabilities(self) -> tuple[Capability, ...]:
        """Return the provider's capability objects."""
