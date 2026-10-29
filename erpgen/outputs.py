"""Write a run to disk: raw SAP layer, readable layer, optional internal canonical files, manifest."""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from . import __version__
from .config import config_hash
from .layers import LayerResult
from .pipeline import Result
from .sap.readable import write_readable


def _default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(type(o))


def _j(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=_default)


def dataset_hash(res: Result) -> str:
    h = hashlib.sha256()
    for e in res.events:
        h.update(_j(dataclasses.asdict(e)).encode())
    for je in res.entries:
        h.update(_j(dataclasses.asdict(je)).encode())
    return h.hexdigest()


def _hash_dir(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.glob("*.csv"))}


def _write_internal(res: Result, d: Path) -> None:
    """Canonical (ERP-neutral) files: the event log is the simulator's ground truth."""
    d.mkdir(parents=True, exist_ok=True)
    with (d / "events.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["event_id", "ts", "type", "user_id", "company_code", "posting_date", "doc_no", "refs", "data"])
        for e in res.events:
            w.writerow([e.event_id, e.ts.isoformat(), e.type, e.user_id, e.company_code,
                        e.posting_date.isoformat(), e.doc_no, _j(e.refs), _j(e.data)])


def write_run(res: Result, layers: LayerResult, out_root: Path, which: str = "both",
              internal: bool = False) -> Path:
    cfg = res.cfg
    run_dir = out_root / f"{cfg.profile}-seed{cfg.seed}-{config_hash(cfg)[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "generator_version": __version__, "profile": cfg.profile, "seed": cfg.seed,
        "config_hash": config_hash(cfg), "dataset_hash": dataset_hash(res),
        "period": [cfg.start_date.isoformat(), cfg.end_date.isoformat()],
        "stats": res.report.stats,
        "validation": {"canonical_ok": res.report.ok, "violations_by_rule": res.report.counts(),
                       "spec_problems": len(layers.spec_problems), "raw_findings": len(layers.raw_findings),
                       "reconciliation_findings": len(layers.recon_findings)},
        "sap_layer": {"profile": "s4hana_o2c", "tables_verified_against_sap": 0,
                      "tables_total": len(layers.raw.specs),
                      "note": "All table specs are unverified; see erpgen/sap/specs_s4_o2c.yaml"},
        "layers": {},
    }
    if which in ("raw", "both"):
        counts = layers.raw.write(run_dir / "raw")
        manifest["layers"]["raw"] = {"row_counts": counts, "files_sha256": _hash_dir(run_dir / "raw")}
    if which in ("readable", "both"):
        counts = write_readable(layers.readable, run_dir / "readable")
        manifest["layers"]["readable"] = {"row_counts": counts, "files_sha256": _hash_dir(run_dir / "readable")}
    if internal:
        _write_internal(res, run_dir / "internal")
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=_default))
    return run_dir
