"""Command line.

  erpgen run --config configs/manufacturing_v0.yaml --out out --layer both
  erpgen readable --raw-dir out/<run>/raw --out out/readable_only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .layers import build_layers
from .outputs import write_run
from .pipeline import generate
from .sap.readable import build_readable, write_readable
from .sap.spec import RawStore


def _run(args) -> int:
    overrides = {}
    if args.seed is not None:
        overrides["seed"] = args.seed
    if args.end_date:
        overrides["end_date"] = args.end_date
    res = generate(load_config(args.config, overrides))
    layers = build_layers(res)
    run_dir = write_run(res, layers, Path(args.out), which=args.layer, internal=args.internal)
    s = res.report.stats
    print(f"run dir         : {run_dir}")
    print(f"events          : {s['events']:,}   journal entries: {s['journal_entries']:,}")
    print(f"raw tables      : {sum(len(v) for v in layers.raw.tables.values()):,} rows in {len(layers.raw.tables)} tables")
    print(f"canonical check : {'PASS' if res.report.ok else 'FAIL ' + str(res.report.counts())}")
    print(f"spec check      : {'PASS' if not layers.spec_problems else 'FAIL ' + str(layers.spec_problems[:3])}")
    print(f"raw checks      : {'PASS' if not layers.raw_findings else 'FAIL ' + str(len(layers.raw_findings))}")
    print(f"reconciliation  : {'PASS' if not layers.recon_findings else 'FAIL ' + str(len(layers.recon_findings))}")
    for fnd in (layers.raw_findings + layers.recon_findings)[:8]:
        print(f"  {fnd.check} {fnd.ref}: {fnd.detail}")
    return 0 if res.report.ok and layers.ok else 1


def _readable(args) -> int:
    raw = RawStore.from_dir(args.raw_dir)
    counts = write_readable(build_readable(raw), Path(args.out))
    print(f"readable layer built from raw tables only: {sum(counts.values()):,} rows in {len(counts)} tables")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="erpgen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="generate a dataset, build the SAP layers, and check everything")
    run.add_argument("--config", required=True)
    run.add_argument("--out", default="out")
    run.add_argument("--seed", type=int)
    run.add_argument("--end-date")
    run.add_argument("--layer", choices=["raw", "readable", "both"], default="both")
    run.add_argument("--internal", action="store_true", help="also write the canonical event log")
    rd = sub.add_parser("readable", help="build the readable layer from an existing raw-layer folder")
    rd.add_argument("--raw-dir", required=True)
    rd.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    return _run(args) if args.cmd == "run" else _readable(args)


if __name__ == "__main__":
    sys.exit(main())
