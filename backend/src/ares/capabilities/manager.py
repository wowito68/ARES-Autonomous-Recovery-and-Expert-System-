"""Discovery, validation, search and execution of capability plugins."""

from __future__ import annotations

from typing import Any

from ares.capabilities.base import Capability, CapabilityPlugin
from ares.capabilities.models import CapabilityCategory, CapabilityMetadata
from ares.workflows import WorkflowEngine, WorkflowExecution

_CORE_API_VERSION = "2.0"


class CapabilityManager:
    """Policy boundary between reasoning and private workflow actions."""

    def __init__(
        self,
        workflow_engine: WorkflowEngine,
        *,
        os_family: str = "debian",
        os_version: str = "13",
        architecture: str = "amd64",
        live_mode: str = "live",
    ) -> None:
        self.workflow_engine = workflow_engine
        self.os_family = os_family
        self.os_version = os_version
        self.architecture = architecture
        self.live_mode = live_mode
        self._capabilities: dict[str, Capability] = {}
        self._plugins: dict[str, CapabilityPlugin] = {}
        self._sealed = False

    def load(self, plugins: tuple[CapabilityPlugin, ...]) -> None:
        """Discover and validate trusted plugin providers at service startup."""

        if self._sealed:
            raise RuntimeError("capability registry is sealed")
        for plugin in plugins:
            manifest = plugin.manifest
            if manifest.id in self._plugins:
                raise ValueError("duplicate plugin id")
            if manifest.core_api_version != _CORE_API_VERSION:
                raise ValueError("plugin targets an incompatible core API")
            self._validate_compatibility(
                manifest.os_compatibility.families,
                manifest.os_compatibility.architectures,
                manifest.os_compatibility.live_modes,
                manifest.os_compatibility.minimum_version,
            )
            discovered = plugin.capabilities()
            if tuple(capability.metadata.id for capability in discovered) != manifest.capabilities:
                raise ValueError("plugin manifest does not match discovered capabilities")
            for capability in discovered:
                metadata = capability.metadata
                if metadata.id in self._capabilities:
                    raise ValueError("duplicate capability id")
                self._validate_compatibility(
                    metadata.os_compatibility.families,
                    metadata.os_compatibility.architectures,
                    metadata.os_compatibility.live_modes,
                    metadata.os_compatibility.minimum_version,
                )
                declared_permissions = set(manifest.permissions)
                required_permissions = {item.id for item in metadata.permissions}
                if not required_permissions.issubset(declared_permissions):
                    raise ValueError("capability permission is absent from plugin manifest")
                self._capabilities[metadata.id] = capability
            self._plugins[manifest.id] = plugin

    def seal(self) -> None:
        """Resolve dependencies once and make the startup registry immutable."""

        for plugin in self._plugins.values():
            if not set(plugin.manifest.dependencies).issubset(self._plugins):
                raise ValueError("plugin dependency is unavailable")
        for capability in self._capabilities.values():
            if not set(capability.metadata.dependencies).issubset(self._capabilities):
                raise ValueError("capability dependency is unavailable")
        self._sealed = True

    def catalog(
        self,
        *,
        query: str | None = None,
        category: CapabilityCategory | None = None,
    ) -> tuple[CapabilityMetadata, ...]:
        """Search self-documenting metadata, never private action implementations."""

        terms = tuple((query or "").casefold().split())
        matches: list[CapabilityMetadata] = []
        for capability in self._capabilities.values():
            metadata = capability.metadata
            if category is not None and metadata.category is not category:
                continue
            haystack = " ".join(
                (
                    metadata.id,
                    metadata.name,
                    metadata.description,
                    metadata.objective,
                    *metadata.keywords,
                )
            ).casefold()
            if terms and not all(term in haystack for term in terms):
                continue
            matches.append(metadata)
        return tuple(sorted(matches, key=lambda item: item.id))

    def get(self, capability_id: str) -> CapabilityMetadata | None:
        capability = self._capabilities.get(capability_id)
        return capability.metadata if capability is not None else None

    async def execute(
        self,
        capability_id: str,
        payload: dict[str, Any],
    ) -> WorkflowExecution:
        if not self._sealed:
            raise RuntimeError("capability registry is not sealed")
        capability = self._capabilities.get(capability_id)
        if capability is None:
            raise KeyError(capability_id)
        return await self.workflow_engine.execute(capability.build_workflow(payload))

    def _validate_compatibility(
        self,
        families: tuple[str, ...],
        architectures: tuple[str, ...],
        live_modes: tuple[str, ...],
        minimum_version: str | None,
    ) -> None:
        if (
            self.os_family not in families
            or self.architecture not in architectures
            or self.live_mode not in live_modes
            or (
                minimum_version is not None
                and _version_tuple(self.os_version) < _version_tuple(minimum_version)
            )
        ):
            raise ValueError("plugin or capability is incompatible with this system")


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as exc:
        raise ValueError("operating-system version is invalid") from exc
