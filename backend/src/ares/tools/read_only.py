"""Hard read-only policy wrapper for storage process probes."""

from __future__ import annotations

from ares.tools.storage import ProcessResult, ProcessRunner, ToolAvailability

_LSBLK_ARGS = (
    "--json",
    "--bytes",
    "--output",
    "NAME,PATH,TYPE,SIZE,RO,RM,MODEL,VENDOR,TRAN,PKNAME,FSTYPE,FSVER,UUID,LABEL,MOUNTPOINTS,SERIAL,WWN",
)
_FINDMNT_ARGS = ("--json", "--bytes", "--output", "SOURCE,TARGET,FSTYPE,OPTIONS")
_DF_ARGS = ("-B1", "--output=source,size,used,avail,pcent,target")
_READ_ONLY_INVOCATIONS: dict[str, tuple[str, ...]] = {
    "lsblk": _LSBLK_ARGS,
    "findmnt": _FINDMNT_ARGS,
    "df": _DF_ARGS,
}
_INSPECTABLE_TOOLS = frozenset((*_READ_ONLY_INVOCATIONS, "blkid", "smartctl"))


class ReadOnlyStorageProcessRunner:
    """Reject every process invocation except the exact passive probe allowlist.

    ``blkid`` and ``smartctl`` may be inspected for availability so the
    capability can report a precise degradation reason, but they cannot be
    executed through this runner. Device-opening access remains at the
    privileged broker boundary defined by ADR-0002.
    """

    def __init__(self, delegate: ProcessRunner) -> None:
        self.delegate = delegate

    def inspect(self, tool: str) -> ToolAvailability:
        if tool not in _INSPECTABLE_TOOLS:
            return ToolAvailability(tool=tool, available=False, reason="read_only_policy_rejected")
        return self.delegate.inspect(tool)

    async def run(
        self,
        tool: str,
        args: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        expected = _READ_ONLY_INVOCATIONS.get(tool)
        if expected is None or args != expected:
            raise PermissionError("read_only_policy_rejected")
        return await self.delegate.run(tool, args, timeout_seconds=timeout_seconds)
