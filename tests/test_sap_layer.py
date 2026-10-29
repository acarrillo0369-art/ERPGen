"""SAP layers: spec compliance, raw checks, reconciliation, round-trip, and mutation tests."""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path

import pytest

from erpgen.config import load_config
from erpgen.layers import build_layers
from erpgen.pipeline import generate
from erpgen.sap.checks import raw_checks, reconcile
from erpgen.sap.readable import build_readable
from erpgen.sap.spec import RawStore, check_against_spec, load_spec

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "manufacturing_v0.yaml"
SMALL = {"end_date": "2025-03-31", "demand": {"orders_per_business_day": 10}}


@pytest.fixture(scope="module")
def run():
    res = generate(load_config(CONFIG, SMALL))
    return res, build_layers(res)


def _checks(layers_raw, res):
    """Raw-only checks plus reconciliation against the simulator, rebuilding the readable layer."""
    readable = build_readable(layers_raw)
    return raw_checks(layers_raw), reconcile(res.cfg, res.md, res.events, res.entries, readable)


def _ids(findings):
    return {f.check for f in findings}


# ---- clean data ------------------------------------------------------------------------------
def test_spec_matches_exported_columns_and_formats(run):
    _, layers = run
    assert layers.spec_problems == []


def test_every_table_is_populated(run):
    _, layers = run
    empty = [t for t, rows in layers.raw.tables.items() if not rows]
    assert empty == [], empty


def test_clean_run_passes_all_sap_checks(run):
    _, layers = run
    assert layers.raw_findings == []
    assert layers.recon_findings == []
    assert layers.ok


def test_sap_style_formats(run):
    _, layers = run
    raw = layers.raw
    assert all(re.fullmatch(r"\d{8}", r["BUDAT"]) for r in raw["BKPF"])
    assert all(len(r["MATNR"]) == 18 and r["MATNR"].isdigit() for r in raw["MARA"])
    assert all(len(r["KUNNR"]) == 10 and r["KUNNR"].isdigit() for r in raw["KNA1"])
    assert all(r["DOCLN"].isdigit() and len(r["DOCLN"]) == 6 for r in raw["ACDOCA"])
    # ACDOCA amounts are signed; the sign agrees with the debit/credit indicator
    assert all((r["DRCRK"] == "S") == (not r["HSL"].startswith("-")) for r in raw["ACDOCA"] if r["HSL"] != "0.00")
    assert all(r["RCLNT"] == "100" for r in raw["ACDOCA"])           # ACDOCA's client field is RCLNT
    assert all(r["AUGDT"] == "00000000" for r in raw["ACDOCA"] if r["AUGBL"] == "")


def test_readable_layer_rebuilds_from_csv_files(run, tmp_path):
    _, layers = run
    layers.raw.write(tmp_path)
    again = build_readable(RawStore.from_dir(tmp_path))
    assert again == layers.readable


def test_raw_layer_is_reproducible():
    def digest():
        res = generate(load_config(CONFIG, SMALL))
        raw = build_layers(res).raw
        h = hashlib.sha256()
        for name in sorted(raw.tables):
            for row in raw[name]:
                h.update("|".join(row.values()).encode())
        return h.hexdigest()
    assert digest() == digest()


def test_spec_loader_flags_unverified_tables():
    specs = load_spec()
    assert len(specs) == 27
    assert all(not t.verified for t in specs.values())      # nothing has been checked against SAP yet


# ---- mutation tests: corrupt the raw tables and confirm the checks notice -----------------------
def _mut(layers):
    return copy.deepcopy(layers.raw)


def test_detects_billing_item_without_delivery(run):
    res, layers = run
    raw = _mut(layers)
    victim = raw["VBRP"][0]
    raw.tables["LIKP"] = [r for r in raw["LIKP"] if r["VBELN"] != victim["VGBEL"]]
    raw.tables["LIPS"] = [r for r in raw["LIPS"] if r["VBELN"] != victim["VGBEL"]]
    raw_f, rec_f = _checks(raw, res)
    assert "R-03" in _ids(raw_f)
    assert rec_f                                    # readable layer no longer matches the simulator


def test_detects_billing_before_goods_issue(run):
    res, layers = run
    raw = _mut(layers)
    victim = raw["VBRP"][0]
    next(r for r in raw["LIKP"] if r["VBELN"] == victim["VGBEL"])["WADAT_IST"] = "00000000"
    raw_f, _ = _checks(raw, res)
    assert "R-03" in _ids(raw_f)


def test_detects_unbalanced_journal_document(run):
    res, layers = run
    raw = _mut(layers)
    raw["ACDOCA"][10]["HSL"] = "999999.99"
    raw_f, _ = _checks(raw, res)
    assert "R-01" in _ids(raw_f)


def test_detects_missing_header(run):
    res, layers = run
    raw = _mut(layers)
    raw.tables["BKPF"] = raw["BKPF"][1:]
    raw_f, _ = _checks(raw, res)
    assert "R-02" in _ids(raw_f)


def test_detects_missing_document_flow_link(run):
    res, layers = run
    raw = _mut(layers)
    raw.tables["VBFA"] = [r for r in raw["VBFA"] if r["VBTYP_N"] != "M"][:]
    raw_f, rec_f = _checks(raw, res)
    assert "R-05" in _ids(raw_f)
    assert "X-05" in _ids(rec_f)


def test_detects_billed_more_than_delivered(run):
    res, layers = run
    raw = _mut(layers)
    victim = raw["VBRP"][0]
    victim["FKIMG"] = "999999.000"
    raw_f, _ = _checks(raw, res)
    assert "R-08" in _ids(raw_f)


def test_detects_wrongly_cleared_receivable(run):
    res, layers = run
    raw = _mut(layers)
    line = next(r for r in raw["ACDOCA"] if r["KOART"] == "D" and r["AUGBL"] != "")
    line["AUGBL"], line["AUGDT"], line["AUGGJ"] = "", "00000000", "0000"      # reopen a cleared item
    _, rec_f = _checks(raw, res)
    assert "X-06" in _ids(rec_f)


def test_detects_altered_billing_amount(run):
    res, layers = run
    raw = _mut(layers)
    raw["VBRP"][3]["NETWR"] = "1.00"
    _, rec_f = _checks(raw, res)
    assert "X-04" in _ids(rec_f)


def test_detects_altered_master_data(run):
    res, layers = run
    raw = _mut(layers)
    row = raw["KNVI"][0]
    row["TAXKD"] = "0" if row["TAXKD"] == "1" else "1"
    _, rec_f = _checks(raw, res)
    assert "X-08" in _ids(rec_f)


def test_spec_check_catches_bad_format(run):
    _, layers = run
    raw = _mut(layers)
    raw["BKPF"][0]["BUDAT"] = "2025-01-06"
    assert any("BUDAT" in p for p in check_against_spec(raw))
