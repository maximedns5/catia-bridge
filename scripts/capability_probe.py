"""Live capability probe against a running CATIA V5 or CATIA 3DEXPERIENCE.

Runs catia_mcp.capabilities.probe and adds the raw answers that only a real
CATIA can give, so the offline assumptions can be checked:

- SystemConfiguration: which of ProductCount / ProductNamesCount answers, what
  GetProductNames returns, and IsProductAuthorized for every candidate in BOTH
  name forms ("PDG" and "PDG.prd"), on V5 and on 3DEXPERIENCE;
- the 3DEXPERIENCE DatabaseSearch members from the type library (is there any
  way to clear search criteria?);
- the CATIA process names and main window titles (for the dialog watchdog).

It attaches to a running CATIA and never starts one. Read-only unless
--create (a scratch V5 Part or 3D Shape, closed without saving) or --risky
(3dpart: PLMCreate "3DPart", which on many roles opens a blocking "Operation
not authorized" dialog: close it by hand and the probe records the refusal;
step_export: a V5 STEP export of the scratch part to a temporary file, then
deleted). Nothing is ever saved, and no other document is closed.

Usage (Windows, CATIA started and, for 3DEXPERIENCE, logged in):
    python scripts/capability_probe.py
    python scripts/capability_probe.py --create --report capability_report.json
    python scripts/capability_probe.py --create --risky 3dpart
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from catia_mcp import capabilities
from catia_mcp.connection import CATIAConnection


def raw_licences(conn: CATIAConnection) -> dict[str, Any]:
    conf = conn.app.SystemConfiguration
    names = sorted(set(capabilities.V5_PRODUCTS) | set(capabilities.X3D_PRODUCTS))
    raw: dict[str, Any] = {}
    for member in ("ProductCount", "ProductNamesCount"):
        try:
            raw[member] = getattr(conf, member)
        except Exception as error:  # noqa: BLE001 -- recorded
            raw[member] = f"error: {error}"
    raw["IsProductAuthorized"] = capabilities._authorized(
        conf, [*names, *(f"{n}.prd" for n in names)])
    return raw


def search_members(conn: CATIAConnection) -> Any:
    if not conn.is_3dx:
        return None
    from catia_mcp.introspect import describe_members

    try:
        db_search = conn.app.GetSessionService("Search").DatabaseSearch
        return [m.get("name") for m in describe_members(db_search)]
    except Exception as error:  # noqa: BLE001 -- recorded
        return f"error: {error}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--platform", default="auto", help="auto, v5 or 3dx")
    parser.add_argument("--create", action="store_true",
                        help="build a scratch part, closed without saving")
    parser.add_argument("--risky", action="append", choices=list(capabilities.RISKY_PROBES),
                        help="also run this risky probe (repeatable)")
    parser.add_argument("--refresh", action="store_true", help="ignore cached results")
    parser.add_argument("--report", default="capability_report.json", help="JSON report path")
    args = parser.parse_args()

    conn = CATIAConnection(platform=args.platform, allow_launch=False)
    try:
        print(conn.connect())
    except RuntimeError as error:
        print(error)
        return 2
    extra: dict[str, Any] = {}
    for key, read in (("raw_licences", raw_licences), ("search_members", search_members)):
        try:
            extra[key] = read(conn)
        except Exception as error:  # noqa: BLE001 -- recorded
            extra[key] = f"error: {error}"
    report = capabilities.probe(conn, create=args.create, risky=args.risky or (),
                                use_cache=not args.refresh)
    report["live_checks"] = extra
    report["cache_dir"] = capabilities.cache_dir()
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, default=str)
    print(report["summary"])
    print(f"Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
