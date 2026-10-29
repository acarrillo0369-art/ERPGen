"""Engine tests: clean data passes, runs are reproducible, and the validator catches injected breakage."""
from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path

import pytest

from erpgen import model as m
from erpgen.config import load_config
from erpgen.outputs import dataset_hash
from erpgen.pipeline import generate
from erpgen.rng import Streams
from erpgen.validate import validate

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "manufacturing_v0.yaml"
SMALL = {"end_date": "2025-03-31", "demand": {"orders_per_business_day": 10}}


@pytest.fixture(scope="module")
def clean():
    return generate(load_config(CONFIG, SMALL))


def _revalidate(res, events=None, entries=None):
    return validate(res.cfg, res.md, res.cal, events or res.events, entries or res.entries)


def _first(events, etype):
    return next(e for e in events if e.type == etype)


def test_clean_run_has_no_violations(clean):
    assert clean.report.ok, clean.report.counts()
    assert clean.report.stats["events"] > 1000


def test_same_seed_is_reproducible(clean):
    again = generate(load_config(CONFIG, SMALL))
    assert dataset_hash(again) == dataset_hash(clean)


def test_different_seed_changes_data(clean):
    other = generate(load_config(CONFIG, {**SMALL, "seed": 7}))
    assert other.report.ok
    assert dataset_hash(other) != dataset_hash(clean)


def test_streams_are_independent_of_creation_order():
    a = Streams(42)
    a.get("noise").random(1000)               # consume another stream first
    x = a.get("sim.orders").random(5)
    y = Streams(42).get("sim.orders").random(5)
    assert list(x) == list(y)


def test_catches_unbalanced_entry(clean):
    entries = copy.deepcopy(clean.entries)
    entries[10].lines[0].amount += Decimal("1.00")
    assert "INV-001" in _revalidate(clean, entries=entries).counts()


def test_catches_billing_without_delivery(clean):
    events = copy.deepcopy(clean.events)
    _first(events, m.BILLING_CREATED).refs["delivery"] = "9999999999"
    assert "DOC-001" in _revalidate(clean, events=events).counts()


def test_catches_billing_before_goods_issue(clean):
    events = copy.deepcopy(clean.events)
    bill = _first(events, m.BILLING_CREATED)
    gi = next(e for e in events if e.type == m.GOODS_ISSUED and e.refs["delivery"] == bill.refs["delivery"])
    bill.ts = gi.ts.replace(hour=0, minute=1)       # entered before the goods left
    assert "DOC-001" in _revalidate(clean, events=events).counts()


def test_catches_broken_posting_date_chain(clean):
    from datetime import timedelta
    events = copy.deepcopy(clean.events)
    bill = _first(events, m.BILLING_CREATED)
    gi = next(e for e in events if e.type == m.GOODS_ISSUED and e.refs["delivery"] == bill.refs["delivery"])
    bill.posting_date = gi.posting_date - timedelta(days=3)   # billing dated before goods issue
    counts = _revalidate(clean, events=events).counts()
    assert "DOC-002" in counts and "DOC-003" in counts


def test_catches_backdated_entry(clean):
    from datetime import timedelta
    events = copy.deepcopy(clean.events)
    bill = _first(events, m.BILLING_CREATED)
    bill.posting_date = bill.ts.date() - timedelta(days=10)
    assert "DOC-003" in _revalidate(clean, events=events).counts()


def test_catches_credit_release_by_creator(clean):
    events = copy.deepcopy(clean.events)
    rel = _first(events, m.ORDER_RELEASED)
    creator = next(e for e in events if e.type == m.ORDER_CREATED and e.doc_no == rel.refs["order"]).user_id
    rel.user_id = creator
    assert "DOC-004" in _revalidate(clean, events=events).counts()


def test_catches_over_applied_payment(clean):
    events = copy.deepcopy(clean.events)
    pay = _first(events, m.PAYMENT_RECEIVED)
    pay.data["applications"][0]["applied"] += Decimal("5.00")
    assert "INV-006" in _revalidate(clean, events=events).counts()


def test_catches_wrong_account_determination(clean):
    entries = copy.deepcopy(clean.entries)
    je = next(j for j in entries if j.doc_type == "RV")
    rev = next(l for l in je.lines if l.dc == "C" and l.material_id)
    rev.account = "4100000" if rev.account == "4000000" else "4000000"
    assert "INV-008" in _revalidate(clean, entries=entries).counts()


def test_subledger_break_is_detected(clean):
    entries = copy.deepcopy(clean.entries)
    je = next(j for j in entries if j.doc_type == "DZ")
    ar = next(l for l in je.lines if l.account == "1200000")
    ar.customer_id = "C0001" if ar.customer_id != "C0001" else "C0002"
    assert "INV-005" in _revalidate(clean, entries=entries).counts()
