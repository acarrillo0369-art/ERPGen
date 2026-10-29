"""Validation gate: replays the event log (in entry order) with its own state tracking and checks every rule.

It does not trust the simulator. Clean data must produce zero violations; once the
anomaly injector exists, violations must match the injected labels exactly.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from . import model as m
from .bizcal import BizCalendar
from .config import Config
from .model import Event, JournalEntry, MasterData

ZERO = Decimal("0")


@dataclass
class Violation:
    rule_id: str
    ref: str
    message: str


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violations

    def counts(self) -> dict[str, int]:
        return dict(Counter(v.rule_id for v in self.violations))


def validate(cfg: Config, md: MasterData, cal: BizCalendar, events: list[Event],
             entries: list[JournalEntry]) -> Report:
    evs = sorted(events, key=lambda e: (e.ts, e.event_id))
    v: list[Violation] = []
    v += _check_entries(cfg, entries)
    replay_v, state = _replay(cfg, md, cal, evs)
    v += replay_v
    v += _check_postings(cfg, md, evs, entries, state)
    v += _check_numbers(cfg, evs, entries)
    return Report(v, _stats(evs, entries, state))


# ---- journal-level checks --------------------------------------------------------------
def _check_entries(cfg: Config, entries: list[JournalEntry]) -> list[Violation]:
    out = []
    for je in entries:
        d = sum((l.amount for l in je.lines if l.dc == "D"), ZERO)
        c = sum((l.amount for l in je.lines if l.dc == "C"), ZERO)
        if d != c or len(je.lines) < 2:
            out.append(Violation("INV-001", je.doc_no, f"debits {d} != credits {c}"))
        for l in je.lines:
            if l.account not in cfg.chart:
                out.append(Violation("INV-003", je.doc_no, f"account {l.account} not in chart"))
            if l.amount <= 0:
                out.append(Violation("INV-001", je.doc_no, f"non-positive amount on line {l.line_no}"))
    return out


# ---- event replay ----------------------------------------------------------------------
@dataclass
class _State:
    ar: dict = field(default_factory=lambda: defaultdict(lambda: ZERO))
    stock: dict = field(default_factory=lambda: defaultdict(int))


def _replay(cfg: Config, md: MasterData, cal: BizCalendar, evs: list[Event]):
    v: list[Violation] = []
    st = _State()
    orders: dict[str, dict] = {}
    dels: dict[str, dict] = {}
    invs: dict[str, dict] = {}
    gi_by_del: dict[str, dict] = {}
    pr = cfg.process

    def bad(rule, e, msg):
        v.append(Violation(rule, f"{e.type}:{e.doc_no}", msg))

    for e in evs:
        # who executed it (DOC-005) and entry vs posting date / period (DOC-003)
        u = md.users.get(e.user_id)
        if u is None:
            bad("INV-003", e, f"unknown user {e.user_id}")
        elif m.EVENT_ROLES[e.type] not in u.roles:
            bad("DOC-005", e, f"user {e.user_id} lacks role {m.EVENT_ROLES[e.type]}")
        lag = (e.ts.date() - e.posting_date).days
        fy, per = cal.period(e.posting_date)
        if lag < 0 or lag > pr.max_backdate_days:
            bad("DOC-003", e, f"posting date {e.posting_date} vs entry date {e.ts.date()}")
        elif e.ts.date() > cal.period_close_date(fy, per, pr.period_close_lag_days):
            bad("DOC-003", e, f"period {fy}/{per} already closed at entry")

        d = e.data
        if e.type == m.OPENING_STOCK:
            for it in d["items"]:
                st.stock[it["material_id"]] += it["qty"]

        elif e.type == m.ORDER_CREATED:
            if d["customer_id"] not in md.customers:
                bad("INV-003", e, "unknown customer")
            items = {}
            for it in d["items"]:
                if it["material_id"] not in md.materials:
                    bad("INV-003", e, f"unknown material {it['material_id']}")
                items[it["item_no"]] = {"qty": it["qty"], "price": it["net_price"], "material": it["material_id"],
                                        "delivered": 0, "issued": 0, "billed": 0}
            orders[e.doc_no] = {"pd": e.posting_date, "creator": e.user_id, "blocked": d["credit_blocked"],
                                "released": False, "rejected": False, "items": items}

        elif e.type in (m.ORDER_RELEASED, m.ORDER_REJECTED):
            o = orders.get(e.refs.get("order"))
            if o is None:
                bad("INV-002", e, "order not found")
                continue
            if not o["blocked"]:
                bad("DOC-004", e, "release/reject of an order that was not blocked")
            if e.user_id == o["creator"]:
                bad("DOC-004", e, "credit decision by the order creator")
            if e.posting_date < o["pd"]:
                bad("DOC-002", e, "decision dated before order")
            o["released" if e.type == m.ORDER_RELEASED else "rejected"] = True

        elif e.type == m.DELIVERY_CREATED:
            o = orders.get(e.refs.get("order"))
            if o is None:
                bad("INV-002", e, "order not found")
                continue
            if o["rejected"] or (o["blocked"] and not o["released"]):
                bad("DOC-004", e, "delivery for a blocked or rejected order")
            if e.posting_date < o["pd"]:
                bad("DOC-002", e, "delivery dated before order")
            for it in d["items"]:
                oi = o["items"].get(it["item_no"])
                if oi is None:
                    bad("INV-002", e, f"order item {it['item_no']} not found")
                    continue
                oi["delivered"] += it["qty"]
                if oi["delivered"] > oi["qty"] * (1 + pr.over_delivery_tolerance):
                    bad("CFG-001", e, f"item {it['item_no']} over-delivered")
            dels[e.doc_no] = {"order": e.refs["order"], "pd": e.posting_date, "items": {i["item_no"]: i["qty"] for i in d["items"]}}

        elif e.type == m.GOODS_ISSUED:
            dl = dels.get(e.refs.get("delivery"))
            if dl is None:
                bad("INV-002", e, "delivery not found")
                continue
            if e.posting_date < dl["pd"]:
                bad("DOC-002", e, "goods issue dated before delivery")
            o = orders[dl["order"]]
            for it in d["items"]:
                o["items"][it["item_no"]]["issued"] += it["qty"]
                st.stock[it["material_id"]] -= it["qty"]
                if st.stock[it["material_id"]] < 0:
                    bad("INV-007", e, f"negative stock for {it['material_id']}")
            gi_by_del[e.refs["delivery"]] = {"pd": e.posting_date}

        elif e.type == m.BILLING_CREATED:
            dl = dels.get(e.refs.get("delivery"))
            if dl is None:
                bad("DOC-001", e, "billing without a delivery")
                dl_items, o = {}, None
            else:
                o = orders[dl["order"]]
                gi = gi_by_del.get(e.refs["delivery"])
                if gi is None:
                    bad("DOC-001", e, "billing before goods issue")
                elif e.posting_date < gi["pd"]:
                    bad("DOC-002", e, "billing dated before goods issue")
            net = ZERO
            for it in d["items"]:
                net += it["net_amount"]
                if o is not None:
                    oi = o["items"].get(it["item_no"])
                    if oi is None:
                        bad("INV-002", e, f"order item {it['item_no']} not found")
                        continue
                    oi["billed"] += it["qty"]
                    if oi["billed"] > oi["issued"]:
                        bad("DOC-001", e, f"item {it['item_no']} billed beyond goods issued")
            if net != d["net"] or d["net"] + d["tax"] != d["gross"]:
                bad("INV-004", e, "net/tax/gross do not add up")
            st.ar[d["customer_id"]] += d["gross"]
            invs[e.doc_no] = {"pd": e.posting_date, "open": d["gross"]}

        elif e.type == m.PAYMENT_RECEIVED:
            for a in d["applications"]:
                inv = invs.get(a["invoice"])
                if inv is None:
                    bad("INV-002", e, f"invoice {a['invoice']} not found")
                    continue
                if e.posting_date < inv["pd"]:
                    bad("DOC-002", e, "payment dated before invoice")
                inv["open"] -= a["applied"]
                if inv["open"] < 0:
                    bad("INV-006", e, f"over-application on {a['invoice']}")
                st.ar[d["customer_id"]] -= a["applied"]
            applied = sum((a["applied"] for a in d["applications"]), ZERO)
            disc = sum((a["discount"] for a in d["applications"]), ZERO)
            if d["bank_amount"] != applied - disc:
                bad("INV-004", e, "bank amount != applied - discount")
    return v, st


# ---- postings vs events, account determination, subledger ties -------------------------
def _check_postings(cfg, md, evs, entries, st: _State) -> list[Violation]:
    v: list[Violation] = []
    by_event: dict[int, list[JournalEntry]] = defaultdict(list)
    for je in entries:
        by_event[je.source_event_id].append(je)
    ar_acct = cfg.accounts["ar"]
    for e in evs:
        if e.type not in (m.OPENING_STOCK, m.GOODS_ISSUED, m.BILLING_CREATED, m.PAYMENT_RECEIVED):
            continue
        jes = by_event.get(e.event_id, [])
        if len(jes) != 1:
            v.append(Violation("INV-002", f"{e.type}:{e.doc_no}", f"{len(jes)} journal entries for event"))
            continue
        je = jes[0]
        dsum = lambda acct, dc: sum((l.amount for l in je.lines if l.account == acct and l.dc == dc), ZERO)  # noqa: E731
        ref = f"{je.doc_type}:{je.doc_no}"
        if e.type == m.GOODS_ISSUED:
            val = sum((it["qty"] * it["unit_cost"] for it in e.data["items"]), ZERO)
            if sum((l.amount for l in je.lines if l.dc == "D"), ZERO) != val:
                v.append(Violation("INV-004", ref, "goods issue value != qty x cost"))
        elif e.type == m.BILLING_CREATED:
            if dsum(ar_acct, "D") != e.data["gross"]:
                v.append(Violation("INV-004", ref, "AR debit != invoice gross"))
            if dsum(cfg.accounts["tax_payable"], "C") != e.data["tax"]:
                v.append(Violation("INV-004", ref, "tax credit != invoice tax"))
        elif e.type == m.PAYMENT_RECEIVED:
            if dsum(cfg.accounts["bank"], "D") != e.data["bank_amount"]:
                v.append(Violation("INV-004", ref, "bank debit != bank amount"))
        # INV-008: account determination per material group
        for l in je.lines:
            if l.material_id is None:
                continue
            g = cfg.material_groups[md.materials[l.material_id].group]
            if e.type == m.BILLING_CREATED:
                ok = l.account == g["revenue"]
            elif e.type == m.GOODS_ISSUED:
                ok = l.account == (g["cogs"] if l.dc == "D" else g["inventory"])
            else:
                ok = l.account == g["inventory"]
            if not ok:
                v.append(Violation("INV-008", ref, f"{l.account} wrong for {l.material_id}"))
    # INV-005: subledger ties
    ar_je: dict[str, Decimal] = defaultdict(lambda: ZERO)
    inv_accts = {g["inventory"] for g in cfg.material_groups.values()}
    inv_je = ZERO
    for je in entries:
        for l in je.lines:
            signed = l.amount if l.dc == "D" else -l.amount
            if l.account == ar_acct and l.customer_id:
                ar_je[l.customer_id] += signed
            if l.account in inv_accts:
                inv_je += signed
    for cid in set(ar_je) | set(st.ar):
        if ar_je.get(cid, ZERO) != st.ar.get(cid, ZERO):
            v.append(Violation("INV-005", cid, f"AR GL {ar_je.get(cid, ZERO)} != open items {st.ar.get(cid, ZERO)}"))
    inv_value = sum((qty * md.materials[mid].std_cost for mid, qty in st.stock.items()), ZERO)
    if inv_je != inv_value:
        v.append(Violation("INV-005", "inventory", f"GL {inv_je} != stock value {inv_value}"))
    return v


# ---- number ranges ---------------------------------------------------------------------
def _check_numbers(cfg, evs, entries) -> list[Violation]:
    v: list[Violation] = []
    last: dict[str, int] = {}
    for e in evs:
        key = m.EVENT_RANGE.get(e.type)
        if key is None:
            continue
        n = int(e.doc_no)
        prev = last.get(key, cfg.number_ranges[key])
        if n <= prev or (n != prev + 1 and not cfg.process.allow_number_gaps):
            v.append(Violation("INV-009", f"{key}:{e.doc_no}", f"expected {prev + 1}"))
        last[key] = n
    fi: dict[tuple[str, int], int] = {}
    for je in sorted(entries, key=lambda j: (j.entry_ts, j.source_event_id)):
        k = (je.company_code, je.fiscal_year)
        prev = fi.get(k, cfg.number_ranges["FI"])
        n = int(je.doc_no)
        if n <= prev or (n != prev + 1 and not cfg.process.allow_number_gaps):
            v.append(Violation("INV-009", f"FI:{je.doc_no}", f"expected {prev + 1}"))
        fi[k] = n
    return v


def _stats(evs, entries, st: _State) -> dict:
    c = Counter(e.type for e in evs)
    blocked = sum(1 for e in evs if e.type == m.ORDER_CREATED and e.data["credit_blocked"])
    n_orders = c[m.ORDER_CREATED] or 1
    per_order = Counter(e.refs["order"] for e in evs if e.type == m.DELIVERY_CREATED)
    return {
        "events": len(evs), "journal_entries": len(entries),
        "journal_lines": sum(len(j.lines) for j in entries),
        "by_event_type": dict(c),
        "credit_block_rate": round(blocked / n_orders, 4),
        "partial_delivery_rate": round(
            sum(1 for k in per_order.values() if k > 1) / max(1, len(per_order)), 4),
        "open_ar_total": str(sum(st.ar.values(), ZERO)),
    }
