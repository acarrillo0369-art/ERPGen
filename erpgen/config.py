"""Typed, versionable run configuration (loaded from a YAML profile)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import typing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml


@dataclass
class WorldCfg:
    n_customers: int = 120
    n_materials: int = 60
    n_sales_users: int = 6
    n_credit_users: int = 2
    n_warehouse_users: int = 4
    n_billing_users: int = 2
    n_ar_users: int = 3
    customer_zipf: float = 1.1
    material_zipf: float = 0.9
    exempt_share: float = 0.55
    tax_rate: float = 0.0825
    credit_limit_days: float = 120.0       # credit limit ~ this many days of expected sales
    cost_median: float = 40.0
    cost_sigma: float = 1.0
    margin_range: list[float] = field(default_factory=lambda: [0.20, 0.60])
    max_customer_discount: float = 0.12
    pay_delay_range: list[float] = field(default_factory=lambda: [0.0, 18.0])
    opening_stock_cover: float = 1.6     # opening stock = this multiple of expected demand over the run


@dataclass
class DemandCfg:
    orders_per_business_day: float = 40.0
    seasonality: list[float] = field(
        default_factory=lambda: [0.9, 0.9, 1.0, 1.0, 1.05, 1.05, 0.95, 0.95, 1.05, 1.1, 1.1, 0.9]
    )
    quarter_end_push: float = 1.25
    quarter_end_push_days: int = 5
    items_mean: float = 2.2
    qty_median: float = 12.0
    qty_sigma: float = 0.9


@dataclass
class ProcessCfg:
    credit_release_prob: float = 0.9
    credit_decision_lag_days: list[int] = field(default_factory=lambda: [1, 3])
    delivery_lag_days: list[int] = field(default_factory=lambda: [1, 4])
    next_delivery_lag_days: list[int] = field(default_factory=lambda: [2, 5])
    partial_delivery_prob: float = 0.12
    gi_same_day_prob: float = 0.8
    billing_lag_days: list[int] = field(default_factory=lambda: [0, 2])
    batch_billing_prob: float = 0.85
    lockbox_prob: float = 0.4
    partial_payment_prob: float = 0.06
    over_delivery_tolerance: float = 0.0
    max_backdate_days: int = 0
    period_close_lag_days: int = 5
    allow_number_gaps: bool = False


@dataclass
class SapCfg:
    """SAP-specific constants for the raw layer (one sales org, one plant, for now)."""
    client: str = "100"
    ledger: str = "0L"
    chart_of_accounts: str = "YCOA"
    fiscal_variant: str = "K4"
    controlling_area: str = "A000"
    company_name: str = "Erpgen Manufacturing Inc."
    company_city: str = "Springfield"
    country: str = "US"
    sales_org: str = "1000"
    distribution_channel: str = "10"
    division: str = "00"
    plant: str = "1000"
    storage_location: str = "0001"
    shipping_point: str = "1000"
    credit_segment: str = "0000"
    unit: str = "ST"
    language: str = "E"
    order_type: str = "OR"
    delivery_type: str = "LF"
    billing_type: str = "F2"
    item_category: str = "TAN"
    price_condition: str = "PR00"
    discount_condition: str = "K007"
    customer_number_offset: int = 100000
    material_number_offset: int = 200000
    profit_centers: dict[str, str] = field(default_factory=lambda: {"FG": "0000001000", "SP": "0000002000"})
    material_types: dict[str, str] = field(default_factory=lambda: {"FG": "FERT", "SP": "ERSA"})
    valuation_classes: dict[str, str] = field(default_factory=lambda: {"FG": "7920", "SP": "3040"})
    terms_keys: dict[str, str] = field(
        default_factory=lambda: {"NT30": "N030", "NT45": "N045", "NT60": "N060", "2/10NT30": "D030"})
    roles: dict[str, str] = field(default_factory=lambda: {
        "SALES": "Z_SD_SALES", "CREDIT": "Z_CREDIT_MGR", "WAREHOUSE": "Z_WM_SHIPPING",
        "BILLING": "Z_SD_BILLING", "AR": "Z_FI_AR_CLERK", "SYSTEM": "Z_SYSTEM"})


@dataclass
class Config:
    profile: str = "manufacturing_v0"
    seed: int = 42
    company_code: str = "1000"
    currency: str = "USD"
    start_date: date = date(2025, 1, 1)
    end_date: date = date(2025, 12, 31)
    world: WorldCfg = field(default_factory=WorldCfg)
    demand: DemandCfg = field(default_factory=DemandCfg)
    process: ProcessCfg = field(default_factory=ProcessCfg)
    sap: SapCfg = field(default_factory=SapCfg)
    number_ranges: dict[str, int] = field(default_factory=dict)
    accounts: dict[str, str] = field(default_factory=dict)
    material_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    payment_terms: dict[str, dict[str, float]] = field(default_factory=dict)
    chart: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.start_date, str):
            self.start_date = date.fromisoformat(self.start_date)
        if isinstance(self.end_date, str):
            self.end_date = date.fromisoformat(self.end_date)
        if self.end_date < self.start_date:
            raise ValueError("end_date before start_date")


def _build(cls, data: dict):
    hints = typing.get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"Unknown config keys for {cls.__name__}: {sorted(unknown)}")
    kwargs = {}
    for name in names & set(data):
        t, v = hints[name], data[name]
        if dataclasses.is_dataclass(t) and isinstance(v, dict):
            v = _build(t, v)
        kwargs[name] = v
    return cls(**kwargs)


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Config:
    data: dict = {}
    if path:
        data = yaml.safe_load(Path(path).read_text()) or {}
    if overrides:
        data = _deep_merge(data, overrides)
    return _build(Config, data)


def config_hash(cfg: Config) -> str:
    blob = json.dumps(dataclasses.asdict(cfg), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
