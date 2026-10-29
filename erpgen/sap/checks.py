"""Two kinds of checks on the SAP layers.

raw_checks    what an analyst could run on the raw tables alone (document balance, header/line
              completeness, document-flow integrity). These are also the detectors for the
              anomaly scenarios the injector will create later (e.g. billing without delivery).
reconcile     proves the layers are complete: the readable layer, built only from raw tables,
              must match what the simulator actually did.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from .. import model as m
from ..config import Config
from ..model import Event, JournalEntry, MasterData
from .keys import KeyMap
from .spec import RawStore

ZERO = Decimal("0")


@dataclass
class Finding:
    check: str
    ref: str
    detail: str


# ---- checks that need only the raw tables ---------------------------------------------------------
def raw_checks(raw: RawStore) -> list[Finding]:
    f: list[Finding] = []
    # R-01 every universal-journal document balances
    bal: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
    for a in raw["ACDOCA"]:
        bal[(a["RBUKRS"], a["GJAHR"], a["BELNR"])] += Decimal(a["HSL"])
    for k, v in bal.items():
        if v != 0:
            f.append(Finding("R-01", "/".join(k), f"ACDOCA document does not balance ({v})"))
    # R-02 header / line completeness in both directions
    headers = {(b["BUKRS"], b["GJAHR"], b["BELNR"]) for b in raw["BKPF"]}
    for k in bal.keys() - headers:
        f.append(Finding("R-02", "/".join(k), "ACDOCA lines without a BKPF header"))
    for k in headers - bal.keys():
        f.append(Finding("R-02", "/".join(k), "BKPF header without ACDOCA lines"))
    # R-03 billing items must reference an existing delivery item, and that delivery must be goods-issued
    likp = {r["VBELN"]: r for r in raw["LIKP"]}
    lips = {(r["VBELN"], r["POSNR"]): r for r in raw["LIPS"]}
    vbak = {r["VBELN"] for r in raw["VBAK"]}
    for r in raw["LIPS"]:
        if r["VGBEL"] not in vbak:
            f.append(Finding("R-03", r["VBELN"], f"delivery references missing order {r['VGBEL']}"))
    billed: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
    for r in raw["VBRP"]:
        ref = (r["VGBEL"], r["VGPOS"])
        if ref not in lips:
            f.append(Finding("R-03", f"{r['VBELN']}/{r['POSNR']}", "billing item without a delivery item"))
            continue
        if likp[r["VGBEL"]]["WADAT_IST"] == "00000000":
            f.append(Finding("R-03", f"{r['VBELN']}/{r['POSNR']}", "billed before goods issue"))
        billed[ref] += Decimal(r["FKIMG"])
    # R-08 billed quantity never exceeds delivered quantity
    for ref, q in billed.items():
        if ref in lips and q > Decimal(lips[ref]["LFIMG"]):
            f.append(Finding("R-08", "/".join(ref), f"billed {q} > delivered {lips[ref]['LFIMG']}"))
    # R-04 billing header points to an existing accounting document
    for r in raw["VBRK"]:
        if (r["BUKRS"], r["GJAHR"], r["BELNR"]) not in headers:
            f.append(Finding("R-04", r["VBELN"], "billing document without an accounting document"))
    # R-05 document flow carries a link for every delivery item and billing item
    flow = {(r["VBELV"], r["POSNV"], r["VBELN"], r["POSNN"], r["VBTYP_N"]) for r in raw["VBFA"]}
    for r in raw["LIPS"]:
        if (r["VGBEL"], r["VGPOS"], r["VBELN"], r["POSNR"], "J") not in flow:
            f.append(Finding("R-05", f"{r['VBELN']}/{r['POSNR']}", "delivery item missing from document flow"))
    for r in raw["VBRP"]:
        if (r["VGBEL"], r["VGPOS"], r["VBELN"], r["POSNR"], "M") not in flow:
            f.append(Finding("R-05", f"{r['VBELN']}/{r['POSNR']}", "billing item missing from document flow"))
    # R-06 customer lines of the universal journal have a BSEG segment line; customers exist
    bseg = {(r["BUKRS"], r["GJAHR"], r["BELNR"], r["BUZEI"]) for r in raw["BSEG"]}
    kna1 = {r["KUNNR"] for r in raw["KNA1"]}
    for a in raw["ACDOCA"]:
        if a["KOART"] == "D":
            if (a["RBUKRS"], a["GJAHR"], a["BELNR"], a["DOCLN"][-3:]) not in bseg:
                f.append(Finding("R-06", f"{a['BELNR']}/{a['DOCLN']}", "customer line without BSEG segment"))
            if a["KUNNR"] not in kna1:
                f.append(Finding("R-06", f"{a['BELNR']}/{a['DOCLN']}", f"unknown customer {a['KUNNR']}"))
    return f


# ---- reconciliation of the readable layer against the simulator ------------------------------------
def _diff(f: list[Finding], check: str, expected: set, got: set, what: str) -> None:
    for x in sorted(expected - got, key=str)[:3]:
        f.append(Finding(check, str(x)[:80], f"{what}: expected but missing from readable layer"))
    for x in sorted(got - expected, key=str)[:3]:
        f.append(Finding(check, str(x)[:80], f"{what}: in readable layer but not produced by simulator"))
    if len(expected) != len(got):
        f.append(Finding(check, what, f"count mismatch: simulator {len(expected)} vs readable {len(got)}"))


def reconcile(cfg: Config, md: MasterData, events: list[Event], entries: list[JournalEntry],
              readable: dict[str, list[dict]]) -> list[Finding]:
    k, f = KeyMap(cfg), []
    ev: dict[str, list[Event]] = defaultdict(list)
    for e in sorted(events, key=lambda x: (x.ts, x.event_id)):
        ev[e.type].append(e)
    ar_acct = cfg.accounts["ar"]

    # 1. journal lines
    canon = {(je.fiscal_year, je.doc_no, ln.line_no, ln.account, (1 if ln.dc == "D" else -1) * ln.amount,
              je.posting_date.isoformat(), k.kunnr(ln.customer_id) if ln.account == ar_acct else "")
             for je in entries for ln in je.lines}
    got = {(r["fiscal_year"], r["doc_no"], r["line_no"], k.acct(r["account"]), Decimal(r["signed_amount"]),
            r["posting_date"], r["customer_no"]) for r in readable["journal_lines"]}
    _diff(f, "X-01", canon, got, "journal lines")

    # 2. sales order items and order values
    canon = {(e.doc_no, i["item_no"], i["material_id"], i["qty"], i["qty"] * i["net_price"])
             for e in ev[m.ORDER_CREATED] for i in e.data["items"]}
    got = {(r["order_no"], r["item_no"], k.mid(r["material_no"]), int(Decimal(r["quantity"])), Decimal(r["net_value"]))
           for r in readable["sales_order_items"]}
    _diff(f, "X-02", canon, got, "sales order items")
    _diff(f, "X-02", {(e.doc_no, e.data["net_value"]) for e in ev[m.ORDER_CREATED]},
          {(r["order_no"], Decimal(r["order_net_value"])) for r in readable["sales_order_items"]}, "order values")

    # 3. deliveries and goods issue dates
    gi = {e.refs["delivery"]: e for e in ev[m.GOODS_ISSUED]}
    canon = {(e.doc_no, i["item_no"], e.refs["order"], i["qty"]) for e in ev[m.DELIVERY_CREATED] for i in e.data["items"]}
    got = {(r["delivery_no"], r["item_no"], r["order_no"], int(Decimal(r["quantity"]))) for r in readable["delivery_items"]}
    _diff(f, "X-03", canon, got, "delivery items")
    _diff(f, "X-03", {(e.doc_no, gi[e.doc_no].posting_date.isoformat() if e.doc_no in gi else "")
                      for e in ev[m.DELIVERY_CREATED]},
          {(r["delivery_no"], r["goods_issue_date"]) for r in readable["delivery_items"]}, "goods issue dates")

    # 4. billing items, header totals, accounting document link
    je_of = {je.source_event_id: je for je in entries}
    canon = {(e.doc_no, i["item_no"], i["material_id"], i["qty"], i["net_amount"])
             for e in ev[m.BILLING_CREATED] for i in e.data["items"]}
    got = {(r["billing_no"], r["item_no"], k.mid(r["material_no"]), int(Decimal(r["quantity"])), Decimal(r["net_value"]))
           for r in readable["billing_items"]}
    _diff(f, "X-04", canon, got, "billing items")
    tot: dict[str, list] = defaultdict(lambda: [ZERO, ZERO, set()])
    for r in readable["billing_items"]:
        t = tot[r["billing_no"]]
        t[0] += Decimal(r["net_value"])
        t[1] += Decimal(r["tax"])
        t[2].add(r["accounting_doc"])
    canon = {(e.doc_no, e.data["net"], e.data["tax"], je_of[e.event_id].doc_no) for e in ev[m.BILLING_CREATED]}
    got = {(b, t[0], t[1], next(iter(t[2]))) for b, t in tot.items() if len(t[2]) == 1}
    _diff(f, "X-04", canon, got, "billing totals and accounting document")

    # 5. document flow
    canon = set()
    for e in ev[m.DELIVERY_CREATED]:
        for i in e.data["items"]:
            canon.add((e.refs["order"], i["item_no"], e.doc_no, i["item_no"], "Delivery"))
    for e in ev[m.GOODS_ISSUED]:
        for z, i in enumerate(e.data["items"], start=1):
            canon.add((e.refs["delivery"], i["item_no"], e.doc_no, z, "Goods movement"))
    for e in ev[m.BILLING_CREATED]:
        for i in e.data["items"]:
            canon.add((e.refs["delivery"], i["item_no"], e.doc_no, i["item_no"], "Invoice"))
    got = {(r["preceding_doc"], r["preceding_item"], r["subsequent_doc"], r["subsequent_item"], r["subsequent_type"])
           for r in readable["document_flow"]}
    _diff(f, "X-05", canon, got, "document flow")

    # 6. open AR per customer (derived from clearing fields) vs simulator receivables
    ar: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for e in ev[m.BILLING_CREATED]:
        ar[k.kunnr(e.data["customer_id"])] += e.data["gross"]
    for e in ev[m.PAYMENT_RECEIVED]:
        ar[k.kunnr(e.data["customer_id"])] -= sum((a["applied"] for a in e.data["applications"]), ZERO)
    open_ar: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in readable["ar_items"]:
        if r["status"] == "Open":
            open_ar[r["customer_no"]] += Decimal(r["signed_amount"])
    for cid in md.customers:
        kn = k.kunnr(cid)
        if ar[kn] != open_ar[kn]:
            f.append(Finding("X-06", kn, f"open AR: simulator {ar[kn]} vs readable {open_ar[kn]}"))

    # 7. materials, stock, and the inventory tie-out between subledger (MBEW) and journal
    stock: dict[str, int] = defaultdict(int)
    for e in ev[m.OPENING_STOCK]:
        for i in e.data["items"]:
            stock[i["material_id"]] += i["qty"]
    for e in ev[m.GOODS_ISSUED]:
        for i in e.data["items"]:
            stock[i["material_id"]] -= i["qty"]
    canon = {(k.matnr(mat.id), Decimal(stock[mat.id]), mat.std_cost, Decimal(stock[mat.id]) * mat.std_cost)
             for mat in md.materials.values()}
    got = {(r["material_no"], Decimal(r["stock_quantity"]), Decimal(r["standard_price"]), Decimal(r["stock_value"]))
           for r in readable["materials"]}
    _diff(f, "X-07", canon, got, "materials and stock")
    inv_accts = {k.saknr(g["inventory"]) for g in cfg.material_groups.values()}
    gl_inv = sum((Decimal(r["signed_amount"]) for r in readable["journal_lines"] if r["account"] in inv_accts), ZERO)
    sub_inv = sum((Decimal(r["stock_value"]) for r in readable["materials"]), ZERO)
    if gl_inv != sub_inv:
        f.append(Finding("X-07", "inventory", f"GL {gl_inv} != material valuation {sub_inv}"))

    # 8. customers, chart of accounts, users and roles
    canon = {(k.kunnr(c.id), c.credit_limit, k.zterm(c.terms), "Taxable" if c.tax_class == "TAXABLE" else "Exempt")
             for c in md.customers.values()}
    got = {(r["customer_no"], Decimal(r["credit_limit"]), r["payment_terms"], r["tax_status"])
           for r in readable["customers"]}
    _diff(f, "X-08", canon, got, "customers")
    _diff(f, "X-08", {(k.saknr(a), n[:50]) for a, n in cfg.chart.items()},
          {(r["account"], r["name"]) for r in readable["chart_of_accounts"]}, "chart of accounts")
    _diff(f, "X-08", {(u.id, cfg.sap.roles[role], "System/batch" if u.is_batch else "Dialog")
                      for u in md.users.values() for role in u.roles},
          {(r["user"], r["role"], r["user_type"]) for r in readable["users"]}, "users and roles")
    return f
