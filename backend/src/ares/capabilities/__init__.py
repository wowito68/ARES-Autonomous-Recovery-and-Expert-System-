"""Discoverable, policy-rich ARES capabilities."""

from ares.capabilities.base import Capability, CapabilityPlugin
from ares.capabilities.discovery import PluginDiscoveryError, discover_plugins
from ares.capabilities.manager import CapabilityManager
from ares.capabilities.models import (
    AuditPolicy,
    CapabilityCategory,
    CapabilityDescriptor,
    CapabilityMetadata,
    OperationClass,
    OSCompatibility,
    PermissionRequirement,
    PluginManifest,
    RiskLevel,
    RollbackPolicy,
)

__all__ = [
    "AuditPolicy",
    "Capability",
    "CapabilityCategory",
    "CapabilityDescriptor",
    "CapabilityManager",
    "CapabilityMetadata",
    "CapabilityPlugin",
    "OSCompatibility",
    "OperationClass",
    "PermissionRequirement",
    "PluginDiscoveryError",
    "PluginManifest",
    "RiskLevel",
    "RollbackPolicy",
    "discover_plugins",
]
