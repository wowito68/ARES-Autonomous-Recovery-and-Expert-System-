"""Small typed and durable graph of the system observed by ARES."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

_MAX_GRAPH_BYTES = 8_000_000


class GraphKind(StrEnum):
    """Node types supported by the ARES v2 schema."""

    SYSTEM = "system"
    HARDWARE = "hardware"
    CPU = "cpu"
    RAM = "ram"
    GPU = "gpu"
    DISK = "disk"
    PARTITION = "partition"
    FILESYSTEM = "filesystem"
    MOUNT_POINT = "mount_point"
    SMART_STATUS = "smart_status"
    OPERATING_SYSTEM = "operating_system"
    BACKUP_SOURCE = "backup_source"
    BACKUP = "backup"
    BACKUP_DESTINATION = "backup_destination"
    BACKUP_ENTRY = "backup_entry"
    BACKUP_VERIFICATION = "backup_verification"
    KERNEL = "kernel"
    DRIVER = "driver"
    SERVICE = "service"
    PROCESS = "process"
    USER = "user"
    NETWORK = "network"
    FIREWALL = "firewall"
    PACKAGE = "package"
    LOG = "log"
    ERROR = "error"
    EVENT = "event"


class GraphNode(BaseModel):
    """One sanitized system fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.:/-]{2,191}$")]
    kind: GraphKind
    attributes: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GraphEdge(BaseModel):
    """Directed relationship between two nodes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Annotated[str, Field(min_length=3, max_length=192)]
    relation: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")]
    target: Annotated[str, Field(min_length=3, max_length=192)]


class GraphSnapshot(BaseModel):
    """Versioned persistence and API contract for the graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    revision: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()


class KnowledgeGraph:
    """Atomic graph store; updates are serialized and replace facts by ID."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._nodes: dict[str, GraphNode] = {}
        self._edges: set[tuple[str, str, str]] = set()
        self._revision = 0

    def prepare(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        if not self.path.exists():
            return
        if self.path.is_symlink() or not self.path.is_file():
            raise OSError("knowledge graph is not a regular file")
        if self.path.stat().st_size > _MAX_GRAPH_BYTES:
            raise OSError("knowledge graph exceeds its safety limit")
        try:
            snapshot = GraphSnapshot.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OSError("knowledge graph is invalid") from exc
        self._nodes = {node.id: node for node in snapshot.nodes}
        self._edges = {(edge.source, edge.relation, edge.target) for edge in snapshot.edges}
        self._revision = snapshot.revision
        self.path.chmod(0o600)

    async def apply(
        self,
        nodes: tuple[GraphNode, ...],
        edges: tuple[GraphEdge, ...],
    ) -> GraphSnapshot:
        """Upsert facts and persist one new revision."""

        async with self._lock:
            next_nodes = dict(self._nodes)
            next_edges = set(self._edges)
            for node in nodes:
                next_nodes[node.id] = node
            for edge in edges:
                if edge.source not in next_nodes or edge.target not in next_nodes:
                    raise ValueError("graph edge references an unknown node")
                next_edges.add((edge.source, edge.relation, edge.target))
            next_revision = self._revision + 1
            snapshot = self._snapshot_from(next_nodes, next_edges, next_revision)
            await asyncio.to_thread(self._write_snapshot, snapshot)
            self._nodes = next_nodes
            self._edges = next_edges
            self._revision = next_revision
            return snapshot

    async def snapshot(self) -> GraphSnapshot:
        async with self._lock:
            return self._snapshot()

    def _snapshot(self) -> GraphSnapshot:
        return self._snapshot_from(self._nodes, self._edges, self._revision)

    @staticmethod
    def _snapshot_from(
        node_map: dict[str, GraphNode],
        edge_set: set[tuple[str, str, str]],
        revision: int,
    ) -> GraphSnapshot:
        nodes = tuple(node_map[key] for key in sorted(node_map))
        edges = tuple(
            GraphEdge(source=source, relation=relation, target=target)
            for source, relation, target in sorted(edge_set)
        )
        return GraphSnapshot(revision=revision, nodes=nodes, edges=edges)

    def _write_snapshot(self, snapshot: GraphSnapshot) -> None:
        encoded = (
            json.dumps(
                snapshot.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > _MAX_GRAPH_BYTES:
            raise ValueError("knowledge graph exceeds its safety limit")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
