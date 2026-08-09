"""Process entry points for independent ARES runtime services."""

from __future__ import annotations

import argparse
import asyncio

from ares.audit import serve_audit_writer
from ares.runtime.broker import serve_tool_broker
from ares.runtime.consent import serve_consent_agent


def main() -> int:
    parser = argparse.ArgumentParser(prog="ares-runtime")
    parser.add_argument("service", choices=("audit", "broker", "consent"))
    args = parser.parse_args()
    if args.service == "audit":
        asyncio.run(serve_audit_writer())
    elif args.service == "broker":
        asyncio.run(serve_tool_broker())
    else:
        asyncio.run(serve_consent_agent())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
