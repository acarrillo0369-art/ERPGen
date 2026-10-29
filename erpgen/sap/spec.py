"""Table specs (loaded from YAML), SAP-style value formatting, and the RawStore.

A RawStore holds tables exactly as a user would receive them: every value a string,
formatted the way an extract from the data dictionary types would look (NUMC zero-padded,
DATS as YYYYMMDD with 00000000 for empty, amounts with fixed decimals and a leading minus).
The readable layer reads only from a RawStore.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

SPEC_PATH = Path(__file__).with_name("specs_s4_o2c.yaml")
_TEXT_TYPES = {"CLNT", "CHAR", "LANG", "CUKY", "UNIT"}
_NUMERIC_TYPES = {"CURR", "QUAN", "DEC"}


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    length: int
    decimals: int
    key: bool


@dataclass
class TableSpec:
    name: str
    description: str
    verified: bool
    fields: list[Field]

    @property
    def columns(self) -> list[str]:
        return [f.name for f in self.fields]

    @property
    def keys(self) -> list[str]:
        return [f.name for f in self.fields if f.key]


_DEFAULT_LEN = {"CLNT": 3, "LANG": 1, "CUKY": 5, "UNIT": 3, "DATS": 8, "TIMS": 6}


def _parse_field(text: str) -> Field:
    parts = text.split()
    name, ftype = parts[0], parts[1]
    key = parts[-1] == "key"
    rest = [p for p in parts[2:] if p != "key"]
    length, decimals = _DEFAULT_LEN.get(ftype, 0), 0
    if rest:
        if "," in rest[0]:
            a, b = rest[0].split(",")
            length, decimals = int(a), int(b)
        else:
            length = int(rest[0])
    return Field(name, ftype, length, decimals, key)


def load_spec(path: Path | str = SPEC_PATH) -> dict[str, TableSpec]:
    raw = yaml.safe_load(Path(path).read_text())
    out: dict[str, TableSpec] = {}
    for name, t in raw["tables"].items():
        out[name] = TableSpec(name, t["description"], bool(t.get("verified", False)),
                              [_parse_field(f) for f in t["fields"]])
    return out


def fmt(f: Field, value: Any) -> str:
    """Format a Python value the way the field's data-dictionary type would appear in an extract."""
    t = f.type
    if t in _TEXT_TYPES:
        s = "" if value is None else str(value)
        if len(s) > f.length:
            raise ValueError(f"{f.name}: '{s}' exceeds length {f.length}")
        return s
    if t == "NUMC":
        s = "" if value is None else str(value)
        s = s.zfill(f.length) if s else "0" * f.length
        if not s.isdigit() or len(s) > f.length:
            raise ValueError(f"{f.name}: bad NUMC '{value}' (len {f.length})")
        return s
    if t == "DATS":
        if value in (None, ""):
            return "00000000"
        return value.strftime("%Y%m%d") if isinstance(value, (date, datetime)) else str(value)
    if t == "TIMS":
        if value in (None, ""):
            return "000000"
        return value.strftime("%H%M%S") if isinstance(value, (time, datetime)) else str(value)
    if t in _NUMERIC_TYPES:
        d = Decimal(0) if value in (None, "") else Decimal(str(value))
        q = Decimal(1).scaleb(-f.decimals)
        d = d.quantize(q, ROUND_HALF_UP)
        if len(str(abs(d)).split(".")[0]) > f.length - f.decimals:
            raise ValueError(f"{f.name}: {d} overflows {f.length},{f.decimals}")
        return f"{d:f}"
    raise ValueError(f"unsupported type {t}")


class RawStore:
    def __init__(self, specs: dict[str, TableSpec]):
        self.specs = specs
        self.tables: dict[str, list[dict[str, str]]] = {n: [] for n in specs}

    def add(self, table: str, row: dict[str, Any]) -> None:
        spec = self.specs[table]
        unknown = set(row) - set(spec.columns)
        if unknown:
            raise KeyError(f"{table}: fields not in spec: {sorted(unknown)}")
        self.tables[table].append({f.name: fmt(f, row.get(f.name)) for f in spec.fields})

    def __getitem__(self, table: str) -> list[dict[str, str]]:
        return self.tables[table]

    def write(self, out_dir: Path) -> dict[str, int]:
        out_dir.mkdir(parents=True, exist_ok=True)
        counts = {}
        for name, spec in self.specs.items():
            rows = self.tables[name]
            with (out_dir / f"{name}.csv").open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(spec.columns)
                w.writerows([r[c] for c in spec.columns] for r in rows)
            counts[name] = len(rows)
        return counts

    @classmethod
    def from_dir(cls, directory: Path | str, specs: dict[str, TableSpec] | None = None) -> "RawStore":
        specs = specs or load_spec()
        store = cls(specs)
        for name, spec in specs.items():
            with (Path(directory) / f"{name}.csv").open(newline="") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames != spec.columns:
                    raise ValueError(f"{name}: columns differ from spec")
                store.tables[name] = [dict(r) for r in reader]
        return store


_DATS = re.compile(r"^\d{8}$")
_TIMS = re.compile(r"^\d{6}$")


def check_against_spec(store: RawStore) -> list[str]:
    """Structural spec test: columns, key uniqueness, and value formats. Returns problems found."""
    problems: list[str] = []
    for name, spec in store.specs.items():
        seen = set()
        for i, row in enumerate(store[name]):
            if list(row) != spec.columns:
                problems.append(f"{name}: row {i} columns differ from spec")
                break
            key = tuple(row[k] for k in spec.keys)
            if key in seen:
                problems.append(f"{name}: duplicate key {key}")
                break
            seen.add(key)
            for f in spec.fields:
                v = row[f.name]
                if f.type == "NUMC" and not (v.isdigit() and len(v) == f.length):
                    problems.append(f"{name}.{f.name}: bad NUMC '{v}'")
                elif f.type == "DATS" and not _DATS.match(v):
                    problems.append(f"{name}.{f.name}: bad DATS '{v}'")
                elif f.type == "TIMS" and not _TIMS.match(v):
                    problems.append(f"{name}.{f.name}: bad TIMS '{v}'")
                elif f.type in _TEXT_TYPES and len(v) > f.length:
                    problems.append(f"{name}.{f.name}: too long")
            if problems and problems[-1].startswith(name):
                break
    return problems
