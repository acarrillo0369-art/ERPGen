"""Canonical, ERP-neutral data model. Exporters reshape this into ERP-specific files."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

# Event types (the event log is the source of truth; the ledger is derived from it)
OPENING_STOCK = "OPENING_STOCK"
ORDER_CREATED = "ORDER_CREATED"
ORDER_RELEASED = "ORDER_RELEASED"
ORDER_REJECTED = "ORDER_REJECTED"
DELIVERY_CREATED = "DELIVERY_CREATED"
GOODS_ISSUED = "GOODS_ISSUED"
BILLING_CREATED = "BILLING_CREATED"
PAYMENT_RECEIVED = "PAYMENT_RECEIVED"

# Role required to execute each event type (basis for the later access / SoD layer)
EVENT_ROLES = {
    OPENING_STOCK: "SYSTEM",
    ORDER_CREATED: "SALES",
    ORDER_RELEASED: "CREDIT",
    ORDER_REJECTED: "CREDIT",
    DELIVERY_CREATED: "WAREHOUSE",
    GOODS_ISSUED: "WAREHOUSE",
    BILLING_CREATED: "BILLING",
    PAYMENT_RECEIVED: "AR",
}

# Number-range key used by events that create a new document
EVENT_RANGE = {
    OPENING_STOCK: "MD",
    ORDER_CREATED: "SO",
    DELIVERY_CREATED: "DL",
    GOODS_ISSUED: "MD",
    BILLING_CREATED: "BL",
    PAYMENT_RECEIVED: "PAY",
}


@dataclass(slots=True)
class Customer:
    id: str
    name: str
    tax_class: str            # TAXABLE | EXEMPT
    terms: str
    credit_limit: Decimal
    weight: float             # share of order volume
    pay_delay_mean: float     # average days paid after due date
    p_take_discount: float
    price_discount: Decimal
    owner: str                # sales user who owns the account


@dataclass(slots=True)
class Material:
    id: str
    name: str
    group: str                # key into config.material_groups
    std_cost: Decimal
    list_price: Decimal
    weight: float             # popularity


@dataclass(slots=True)
class User:
    id: str
    roles: tuple[str, ...]
    hours: tuple[int, int]
    is_batch: bool = False


@dataclass
class MasterData:
    customers: dict[str, Customer] = field(default_factory=dict)
    materials: dict[str, Material] = field(default_factory=dict)
    users: dict[str, User] = field(default_factory=dict)

    def users_with_role(self, role: str, batch: bool = False) -> list[User]:
        return [u for u in self.users.values() if role in u.roles and u.is_batch == batch]


@dataclass(slots=True)
class Event:
    event_id: int
    ts: datetime              # entry timestamp: when the user or batch job executed it
    type: str
    user_id: str
    company_code: str
    posting_date: date
    doc_no: str
    refs: dict
    data: dict


@dataclass(slots=True)
class JELine:
    line_no: int
    account: str
    dc: str                   # "D" or "C"
    amount: Decimal           # always positive
    customer_id: str | None = None
    material_id: str | None = None
    ref_doc: str | None = None
    text: str = ""


@dataclass(slots=True)
class JournalEntry:
    doc_no: str
    company_code: str
    fiscal_year: int
    period: int
    doc_type: str
    document_date: date
    posting_date: date
    entry_ts: datetime
    user_id: str
    reference: str
    source_event_id: int
    currency: str
    lines: list[JELine]
