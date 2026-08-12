"""Explicit dependency graph and root-cause prioritization for recovery cases."""

from __future__ import annotations

from collections import defaultdict, deque

from ares.recovery.models import (
    RecoveryDependency,
    RecoveryIssue,
    RecoveryIssueCode,
    RootCauseHypothesis,
)


_CAUSAL_PRIORITY: dict[RecoveryIssueCode, int] = {
    RecoveryIssueCode.HARDWARE_DEGRADED: 100,
    RecoveryIssueCode.IO_ERROR: 95,
    RecoveryIssueCode.STORAGE_UNAVAILABLE: 90,
    RecoveryIssueCode.FILESYSTEM_DEGRADED: 85,
    RecoveryIssueCode.FSTAB_INVALID_REFERENCE: 82,
    RecoveryIssueCode.MOUNT_FAILURE: 75,
    RecoveryIssueCode.BOOT_DEGRADED: 70,
    RecoveryIssueCode.KERNEL_INITRAMFS_MISMATCH: 68,
    RecoveryIssueCode.INITRAMFS_MISSING: 67,
    RecoveryIssueCode.PACKAGE_DATABASE_INCONSISTENT: 65,
    RecoveryIssueCode.PACKAGE_INTERRUPTED: 64,
    RecoveryIssueCode.PACKAGE_BROKEN_DEPENDENCIES: 63,
    RecoveryIssueCode.SYSTEMD_DEGRADED: 50,
    RecoveryIssueCode.SERVICE_DEPENDENCY_FAILED: 40,
    RecoveryIssueCode.CRITICAL_SERVICE_FAILED: 35,
    RecoveryIssueCode.PERMISSION_ERROR: 30,
    RecoveryIssueCode.OOM: 25,
    RecoveryIssueCode.MISSING_DEVICE: 20,
    RecoveryIssueCode.EMERGENCY_MODE: 10,
    RecoveryIssueCode.UNKNOWN: 0,
    RecoveryIssueCode.PACKAGE_PENDING_CONFIGURATION: 60,
    RecoveryIssueCode.PACKAGE_REPAIR_EXTERNAL_DEPENDENCY: 5,
}


class RecoveryDependencyResolver:
    """Resolve explicit component dependencies and prefer evidence-backed root causes."""

    def base_graph(self) -> tuple[RecoveryDependency, ...]:
        pairs = (
            ("firmware", "bootloader", "loads"),
            ("bootloader", "kernel", "loads"),
            ("kernel", "initramfs", "requires"),
            ("initramfs", "root_filesystem", "mounts"),
            ("root_filesystem", "systemd", "hosts"),
            ("systemd", "critical_services", "starts"),
            ("critical_services", "application_environment", "supports"),
            ("root_filesystem", "package_manager", "hosts"),
            ("package_manager", "installed_packages", "manages"),
            ("installed_packages", "dependencies", "requires"),
            ("fstab", "mounts", "configures"),
            ("mounts", "systemd_mount_units", "materializes"),
            ("systemd_mount_units", "critical_services", "satisfies_dependencies_for"),
        )
        return tuple(
            RecoveryDependency(upstream=upstream, downstream=downstream, relation=relation)
            for upstream, downstream, relation in pairs
        )

    def hypotheses(self, issues: tuple[RecoveryIssue, ...]) -> tuple[RootCauseHypothesis, ...]:
        ranked = sorted(
            issues,
            key=lambda issue: (_CAUSAL_PRIORITY[issue.code], issue.confidence),
            reverse=True,
        )
        if not ranked:
            return ()
        hypotheses: list[RootCauseHypothesis] = []
        for index, issue in enumerate(ranked[:5]):
            alternatives = tuple(item.id for item in ranked if item.id != issue.id)[:4]
            confidence = min(0.99, issue.confidence * (1.0 if index == 0 else 0.85))
            hypotheses.append(
                RootCauseHypothesis(
                    hypothesis=self._hypothesis_text(issue),
                    evidence=issue.evidence_ids,
                    confidence=confidence,
                    affected_components=issue.affected_components,
                    alternative_hypotheses=alternatives,
                    root_issue_ids=(issue.id,),
                )
            )
        return tuple(hypotheses)

    def order_operations(
        self,
        operation_ids: tuple[str, ...],
        dependencies: tuple[RecoveryDependency, ...],
    ) -> tuple[str, ...]:
        known = set(operation_ids)
        indegree = {operation_id: 0 for operation_id in operation_ids}
        edges: dict[str, list[str]] = defaultdict(list)
        for dependency in dependencies:
            if dependency.upstream not in known or dependency.downstream not in known:
                continue
            edges[dependency.upstream].append(dependency.downstream)
            indegree[dependency.downstream] += 1
        queue = deque(operation_id for operation_id in operation_ids if indegree[operation_id] == 0)
        ordered: list[str] = []
        while queue:
            current = queue.popleft()
            ordered.append(current)
            for downstream in edges[current]:
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    queue.append(downstream)
        if len(ordered) != len(operation_ids):
            raise ValueError("recovery dependency graph contains a cycle")
        return tuple(ordered)

    @staticmethod
    def _hypothesis_text(issue: RecoveryIssue) -> str:
        mapping = {
            RecoveryIssueCode.FSTAB_INVALID_REFERENCE: (
                "An invalid filesystem identity in fstab is preventing a required mount and "
                "propagating dependency failures into systemd."
            ),
            RecoveryIssueCode.FILESYSTEM_DEGRADED: (
                "Filesystem integrity or mountability is the earliest observed failing layer."
            ),
            RecoveryIssueCode.PACKAGE_INTERRUPTED: (
                "An interrupted package transaction left packages pending configuration."
            ),
            RecoveryIssueCode.PACKAGE_BROKEN_DEPENDENCIES: (
                "The APT/dpkg dependency graph is inconsistent and blocks package configuration."
            ),
            RecoveryIssueCode.INITRAMFS_MISSING: (
                "The selected kernel does not have a corresponding initramfs artifact."
            ),
            RecoveryIssueCode.BOOT_DEGRADED: (
                "Boot-chain evidence is inconsistent before userspace services can start."
            ),
            RecoveryIssueCode.STORAGE_UNAVAILABLE: (
                "The target storage layer is unavailable or inconsistent, invalidating higher layers."
            ),
            RecoveryIssueCode.SYSTEMD_DEGRADED: (
                "Systemd reports a degraded userspace state, but lower-layer causes must remain preferred."
            ),
        }
        return mapping.get(issue.code, issue.summary)
