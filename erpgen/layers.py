"""Build the SAP layers for a finished run: raw tables -> readable layer, plus all checks."""
from __future__ import annotations

from dataclasses import dataclass, field

from .pipeline import Result
from .sap.checks import Finding, raw_checks, reconcile
from .sap.exporter import S4Exporter
from .sap.readable import build_readable
from .sap.spec import RawStore, check_against_spec


@dataclass
class LayerResult:
    raw: RawStore
    readable: dict[str, list[dict]]
    spec_problems: list[str] = field(default_factory=list)
    raw_findings: list[Finding] = field(default_factory=list)
    recon_findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.spec_problems or self.raw_findings or self.recon_findings)


def build_layers(res: Result) -> LayerResult:
    raw = S4Exporter(res.cfg, res.md, res.events, res.entries).build()
    readable = build_readable(raw)            # reads only the raw tables
    return LayerResult(
        raw=raw,
        readable=readable,
        spec_problems=check_against_spec(raw),
        raw_findings=raw_checks(raw),
        recon_findings=reconcile(res.cfg, res.md, res.events, res.entries, readable),
    )
