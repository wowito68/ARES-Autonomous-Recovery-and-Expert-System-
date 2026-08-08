"""Discovery, validation, documentation and execution of capability plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ares.capabilities.base import Capability, CapabilityPlugin
from ares.capabilities.models import (
    CapabilityCategory,
    CapabilityDescriptor,
    CapabilityMetadata,
    CapabilityMode,
    OperationClass,
)
from ares.workflows import ExecutionStatus, WorkflowDefinition, WorkflowEngine, WorkflowExecution

_CORE_API_VERSION = "2.0"


@dataclass(frozen=True, slots=True)
class _Registration:
    capability: Capability
    plugin_id: str
    plugin_version: str


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
        self._capabilities: dict[str, dict[str, _Registration]] = {}
        self._plugins: dict[tuple[str, str], CapabilityPlugin] = {}
        self._sealed = False

    def load(self, plugins: tuple[CapabilityPlugin, ...]) -> None:
        """Register and validate trusted plugin providers at service startup."""

        if self._sealed:
            raise RuntimeError("capability registry is sealed")
        for plugin in plugins:
            manifest = plugin.manifest
            plugin_key = (manifest.id, manifest.version)
            if plugin_key in self._plugins:
                raise ValueError("duplicate plugin id and version")
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
                self._register_capability(plugin, capability)
            self._plugins[plugin_key] = plugin

    def seal(self) -> None:
        """Resolve dependencies once and make the startup registry immutable."""

        available_plugins = {plugin_id for plugin_id, _ in self._plugins}
        for plugin in self._plugins.values():
            if not set(plugin.manifest.dependencies).issubset(available_plugins):
                raise ValueError("plugin dependency is unavailable")
        available_capabilities = set(self._capabilities)
        for versions in self._capabilities.values():
            for registration in versions.values():
                if not set(registration.capability.metadata.dependencies).issubset(
                    available_capabilities
                ):
                    raise ValueError("capability dependency is unavailable")
        for capability_id in available_capabilities:
            self.resolve_dependencies(capability_id)
        self._sealed = True

    def catalog(
        self,
        *,
        query: str | None = None,
        category: CapabilityCategory | None = None,
    ) -> tuple[CapabilityMetadata, ...]:
        return tuple(
            descriptor.metadata for descriptor in self.descriptors(query=query, category=category)
        )

    def descriptors(
        self,
        *,
        query: str | None = None,
        category: CapabilityCategory | None = None,
    ) -> tuple[CapabilityDescriptor, ...]:
        terms = tuple((query or "").casefold().split())
        matches: list[CapabilityDescriptor] = []
        for capability_id in self._capabilities:
            descriptor = self.descriptor(capability_id)
            if descriptor is None:
                continue
            metadata = descriptor.metadata
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
            matches.append(descriptor)
        return tuple(sorted(matches, key=lambda item: item.metadata.id))

    def descriptor(
        self,
        capability_id: str,
        *,
        version: str | None = None,
    ) -> CapabilityDescriptor | None:
        registration = self._registration(capability_id, version)
        if registration is None:
            return None
        metadata = registration.capability.metadata
        active = self._active_version(capability_id) == metadata.version
        return CapabilityDescriptor(
            metadata=metadata,
            input_schema=registration.capability.input_model.model_json_schema(),
            output_schema=registration.capability.output_model.model_json_schema(),
            plugin_id=registration.plugin_id,
            plugin_version=registration.plugin_version,
            active=active,
        )

    def versions(self, capability_id: str) -> tuple[CapabilityDescriptor, ...]:
        versions = self._capabilities.get(capability_id, {})
        ordered = sorted(versions, key=_version_tuple, reverse=True)
        return tuple(
            descriptor
            for version in ordered
            if (descriptor := self.descriptor(capability_id, version=version)) is not None
        )

    def get(
        self, capability_id: str, *, version: str | None = None
    ) -> CapabilityMetadata | None:
        descriptor = self.descriptor(capability_id, version=version)
        return descriptor.metadata if descriptor is not None else None

    def resolve_dependencies(self, capability_id: str) -> tuple[CapabilityMetadata, ...]:
        if capability_id not in self._capabilities:
            raise KeyError(capability_id)
        ordered: list[CapabilityMetadata] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(current_id: str) -> None:
            if current_id in visited:
                return
            if current_id in visiting:
                raise ValueError("capability dependency cycle detected")
            registration = self._registration(current_id, None)
            if registration is None:
                raise ValueError("capability dependency is unavailable")
            visiting.add(current_id)
            for dependency in registration.capability.metadata.dependencies:
                visit(dependency)
            visiting.remove(current_id)
            visited.add(current_id)
            ordered.append(registration.capability.metadata)

        visit(capability_id)
        return tuple(ordered)

    async def execute(
        self,
        capability_id: str,
        payload: dict[str, Any],
        *,
        version: str | None = None,
    ) -> WorkflowExecution:
        """Validate typed input, run the private workflow and validate typed output."""

        if not self._sealed:
            raise RuntimeError("capability registry is not sealed")
        registration = self._registration(capability_id, version)
        if registration is None:
            raise KeyError(capability_id)
        capability = registration.capability
        validated = capability.input_model.model_validate(payload)
        definition = capability.build_workflow(validated)
        self._validate_workflow_contract(capability.metadata, definition)
        execution = await self.workflow_engine.execute(definition)
        if execution.status is not ExecutionStatus.SUCCEEDED or execution.result is None:
            return execution
        validated_output = capability.output_model.model_validate(execution.result)
        return execution.model_copy(update={"result": validated_output.model_dump(mode="json")})

    def _register_capability(
        self,
        plugin: CapabilityPlugin,
        capability: Capability,
    ) -> None:
        metadata = capability.metadata
        for model, label in (
            (capability.input_model, "input_model"),
            (capability.output_model, "output_model"),
        ):
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                raise ValueError(f"capability {label} must be a Pydantic model")
        if (
            metadata.mode is CapabilityMode.READ_ONLY
            and metadata.operation is not OperationClass.OBSERVE
        ):
            raise ValueError("read-only capabilities must use observe operation class")
        self._validate_compatibility(
            metadata.os_compatibility.families,
            metadata.os_compatibility.architectures,
            metadata.os_compatibility.live_modes,
            metadata.os_compatibility.minimum_version,
        )
        declared_permissions = set(plugin.manifest.permissions)
        required_permissions = {item.id for item in metadata.permissions}
        if not required_permissions.issubset(declared_permissions):
            raise ValueError("capability permission is absent from plugin manifest")
        versions = self._capabilities.setdefault(metadata.id, {})
        if metadata.version in versions:
            raise ValueError("duplicate capability id and version")
        versions[metadata.version] = _Registration(
            capability=capability,
            plugin_id=plugin.manifest.id,
            plugin_version=plugin.manifest.version,
        )

    def _registration(
        self,
        capability_id: str,
        version: str | None,
    ) -> _Registration | None:
        versions = self._capabilities.get(capability_id)
        if not versions:
            return None
        selected = version or self._active_version(capability_id)
        return versions.get(selected)

    def _active_version(self, capability_id: str) -> str:
        versions = self._capabilities.get(capability_id)
        if not versions:
            raise KeyError(capability_id)
        return max(versions, key=_version_tuple)

    @staticmethod
    def _validate_workflow_contract(
        metadata: CapabilityMetadata,
        definition: WorkflowDefinition,
    ) -> None:
        if definition.capability_id != metadata.id:
            raise ValueError("workflow capability id does not match its registration")
        actual_actions = {step.action.id for stage in definition.stages for step in stage.steps}
        if actual_actions != set(metadata.internal_actions):
            raise ValueError("workflow actions do not match capability metadata")

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
        parts = tuple(int(part) for part in value.split("."))
        return parts + (0,) * max(0, 3 - len(parts))
    except ValueError as exc:
        raise ValueError("version is invalid") from exc
