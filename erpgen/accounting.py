"""Accounting engine: deterministic posting rules that derive journal entries from events.

Because every journal entry comes from an event, subledgers tie to the GL by
construction, and the posting rules can be changed and the books regenerated.
Document types follow SAP conventions as I recall them (AB, WL, RV, DZ);
verify against your target system before relying on them.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from . import model as m
from .bizcal import BizCalendar
from .config import Config
from .model import Event, JELine, JournalEntry, MasterData

DOC_TYPES = {
    m.OPENING_STOCK: "AB",
    m.GOODS_ISSUED: "WL",
    m.BILLING_CREATED: "RV",
    m.PAYMENT_RECEIVED: "DZ",
}


class AccountingEngine:
    def __init__(self, cfg: Config, md: MasterData, cal: BizCalendar):
        self.cfg, self.md, self.cal = cfg, md, cal
        self.acct = cfg.accounts
        self._rules = {
            m.OPENING_STOCK: self._opening_stock,
            m.GOODS_ISSUED: self._goods_issue,
            m.BILLING_CREATED: self._billing,
            m.PAYMENT_RECEIVED: self._payment,
        }

    def post(self, events: list[Event]) -> list[JournalEntry]:
        counters: dict[tuple[str, int], int] = defaultdict(lambda: self.cfg.number_ranges["FI"])
        out: list[JournalEntry] = []
        for e in sorted(events, key=lambda ev: (ev.ts, ev.event_id)):
            rule = self._rules.get(e.type)
            if rule is None:
                continue
            fy, period = self.cal.period(e.posting_date)
            key = (e.company_code, fy)
            counters[key] += 1
            lines = rule(e)
            for n, ln in enumerate(lines, start=1):
                ln.line_no = n
            out.append(JournalEntry(
                doc_no=f"{counters[key]:010d}", company_code=e.company_code, fiscal_year=fy, period=period,
                doc_type=DOC_TYPES[e.type], document_date=e.ts.date(), posting_date=e.posting_date,
                entry_ts=e.ts, user_id=e.user_id, reference=e.doc_no, source_event_id=e.event_id,
                currency=self.cfg.currency, lines=lines,
            ))
        return out

    def _grp(self, material_id: str) -> dict:
        return self.cfg.material_groups[self.md.materials[material_id].group]

    def _opening_stock(self, e: Event) -> list[JELine]:
        lines, total = [], Decimal("0")
        for it in e.data["items"]:
            amt = it["qty"] * it["unit_cost"]
            total += amt
            lines.append(JELine(0, self._grp(it["material_id"])["inventory"], "D", amt, material_id=it["material_id"]))
        lines.append(JELine(0, self.acct["opening_equity"], "C", total, text="Opening balance"))
        return lines

    def _goods_issue(self, e: Event) -> list[JELine]:
        lines: list[JELine] = []
        for it in e.data["items"]:
            amt = it["qty"] * it["unit_cost"]
            g = self._grp(it["material_id"])
            lines.append(JELine(0, g["cogs"], "D", amt, material_id=it["material_id"], ref_doc=e.refs["delivery"]))
            lines.append(JELine(0, g["inventory"], "C", amt, material_id=it["material_id"], ref_doc=e.refs["delivery"]))
        return lines

    def _billing(self, e: Event) -> list[JELine]:
        d = e.data
        cust = d["customer_id"]
        lines = [JELine(0, self.acct["ar"], "D", d["gross"], customer_id=cust, ref_doc=e.doc_no)]
        for it in d["items"]:
            lines.append(JELine(0, self._grp(it["material_id"])["revenue"], "C", it["net_amount"],
                                customer_id=cust, material_id=it["material_id"], ref_doc=e.doc_no))
        if d["tax"] > 0:
            lines.append(JELine(0, self.acct["tax_payable"], "C", d["tax"], customer_id=cust, ref_doc=e.doc_no))
        return lines

    def _payment(self, e: Event) -> list[JELine]:
        d = e.data
        cust = d["customer_id"]
        lines = [JELine(0, self.acct["bank"], "D", d["bank_amount"], customer_id=cust)]
        discount = sum((a["discount"] for a in d["applications"]), Decimal("0"))
        if discount > 0:
            lines.append(JELine(0, self.acct["cash_discount"], "D", discount, customer_id=cust))
        for a in d["applications"]:
            lines.append(JELine(0, self.acct["ar"], "C", a["applied"], customer_id=cust, ref_doc=a["invoice"]))
        return lines
