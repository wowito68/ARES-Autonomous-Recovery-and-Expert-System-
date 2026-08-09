"""Replaceable capability and plugin provider boundaries."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from ares.capabilities.models import CapabilityMetadata, PluginManifest
from ares.workflows import WorkflowDefinition


class Capability(Protocol):
    """Validate semantic I/O contracts and build a private workflow.

    Capabilities never execute a command and never accept an action/tool
    identifier from a client or LLM.
    """

    @property
    def metadata(self) -> CapabilityMetadata:
        """Return immutable, self-documenting capability metadata."""

    @property
    def input_model(self) -> type[BaseModel]:
        """Return the Pydantic model used for semantic input validation."""

    @property
    def output_model(self) -> type[BaseModel]:
        """Return the Pydantic model used to validate the public result."""

    def build_workflow(self, payload: BaseModel) -> WorkflowDefinition:
        """Return a server-owned plan from already validated semantic input."""


class CapabilityPlugin(Protocol):
    """Installable provider of one or more capabilities."""

    @property
    def manifest(self) -> PluginManifest:
        """Return the immutable provider manifest."""

    def capabilities(self) -> tuple[Capability, ...]:
        """Return the provider's capability objects."""
