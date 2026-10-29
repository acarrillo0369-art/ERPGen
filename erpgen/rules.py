"""The spec layer: every rule the data must follow, in one registry.

Tiers
-----
INVARIANT     never violated, even after anomaly injection. A failure is a generator bug.
INJECTABLE    clean data obeys it; the injector may violate it, and must write a label.
CONFIGURABLE  enforced against a profile parameter (sector / maturity dependent).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Tier(str, Enum):
    INVARIANT = "invariant"
    INJECTABLE = "injectable"
    CONFIGURABLE = "configurable"


@dataclass(frozen=True)
class Rule:
    id: str
    tier: Tier
    title: str
    param: str | None = None  # profile parameter that tunes the rule, if any


_RULES = [
    Rule("INV-001", Tier.INVARIANT, "Every journal entry balances (debits = credits)"),
    Rule("INV-002", Tier.INVARIANT, "Every predecessor reference resolves to an earlier document"),
    Rule("INV-003", Tier.INVARIANT, "Master data referenced by a transaction exists"),
    Rule("INV-004", Tier.INVARIANT, "Postings derive correctly from their source events"),
    Rule("INV-005", Tier.INVARIANT, "Subledgers tie to the GL (AR by customer, inventory value)"),
    Rule("INV-006", Tier.INVARIANT, "A payment never applies more than the open invoice amount"),
    Rule("INV-007", Tier.INVARIANT, "Stock on hand never goes negative"),
    Rule("INV-008", Tier.INVARIANT, "Account determination is consistent per material group"),
    Rule("INV-009", Tier.INVARIANT, "Document numbers ascend with entry time", "process.allow_number_gaps"),
    Rule("DOC-001", Tier.INJECTABLE, "Goods are billed only from a delivery with goods issue posted, up to the issued quantity"),
    Rule("DOC-002", Tier.INJECTABLE, "Posting-date chain: order <= delivery <= goods issue <= billing <= payment"),
    Rule("DOC-003", Tier.INJECTABLE, "Posting date within the backdating window and the period still open at entry", "process.max_backdate_days"),
    Rule("DOC-004", Tier.INJECTABLE, "Credit-blocked orders are released by someone other than the creator, before delivery"),
    Rule("DOC-005", Tier.INJECTABLE, "Each event is executed by a user holding the required role"),
    Rule("CFG-001", Tier.CONFIGURABLE, "Delivered quantity <= ordered quantity plus tolerance", "process.over_delivery_tolerance"),
]
RULES: dict[str, Rule] = {r.id: r for r in _RULES}

# Anomaly scenarios planned for the injector: scenario id -> (title, rule it violates).
# Not implemented yet; listed so the spec and the label scheme stay aligned.
PLANNED_SCENARIOS: dict[str, tuple[str, str]] = {
    "S-REV-01": ("Billing without delivery: goods item set to order-related billing", "DOC-001"),
    "S-REV-02": ("Direct FI invoice that bypasses SD (no document flow)", "DOC-001"),
    "S-REV-03": ("Billing proceeds although nothing was goods-issued", "DOC-001"),
    "S-DATE-01": ("Backdated entries and period-end cutoff manipulation", "DOC-003"),
    "S-REV-04": ("Credit memo issued just after period end", "DOC-003"),
}
