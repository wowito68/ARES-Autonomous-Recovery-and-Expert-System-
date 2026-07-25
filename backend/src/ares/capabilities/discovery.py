"""Trusted plugin discovery for built-ins and allow-listed entry points."""

from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import EntryPoint, entry_points
from typing import Any, cast

from ares.capabilities.base import CapabilityPlugin


class PluginDiscoveryError(RuntimeError):
    """Raised when an allow-listed provider cannot satisfy the plugin contract."""


def discover_plugins(
    builtins: Iterable[CapabilityPlugin] = (),
    *,
    entry_point_group: str = "ares.capabilities",
    allowed_entry_points: Iterable[str] = (),
) -> tuple[CapabilityPlugin, ...]:
    """Discover trusted providers without importing arbitrary writable paths.

    Built-ins come from the signed image. External providers are loaded only
    when their entry-point name is explicitly allow-listed by deployment
    policy. Installing a signed package and updating that policy does not
    require a core code change.
    """

    discovered = list(builtins)
    allowed = frozenset(allowed_entry_points)
    if not allowed:
        return tuple(discovered)

    candidates = entry_points().select(group=entry_point_group)
    indexed = {candidate.name: candidate for candidate in candidates}
    missing = sorted(allowed - indexed.keys())
    if missing:
        raise PluginDiscoveryError(
            f"allow-listed capability entry points are unavailable: {', '.join(missing)}"
        )
    for name in sorted(allowed):
        discovered.append(_load_plugin(indexed[name]))
    return tuple(discovered)


def _load_plugin(entry_point: EntryPoint) -> CapabilityPlugin:
    try:
        provider: Any = entry_point.load()
        if isinstance(provider, type):
            plugin = provider()
        elif hasattr(provider, "manifest"):
            plugin = provider
        elif callable(provider):
            plugin = provider()
        else:
            raise TypeError("entry point is not a plugin instance, class, or factory")
    except Exception as exc:
        raise PluginDiscoveryError(
            f"capability entry point {entry_point.name!r} could not be loaded"
        ) from exc
    if not hasattr(plugin, "manifest") or not callable(getattr(plugin, "capabilities", None)):
        raise PluginDiscoveryError(
            f"capability entry point {entry_point.name!r} has an invalid provider contract"
        )
    return cast(CapabilityPlugin, plugin)
