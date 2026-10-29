"""Order-to-cash discrete-event simulator.

Each document type behaves like a small state machine that only allows legal
transitions (billing requires goods issue, delivery requires a released order),
so clean data is correct by construction. The simulator emits *events*; the
accounting engine derives the books from them.
"""
from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from . import model as m
from .bizcal import BizCalendar
from .config import Config
from .model import Event, MasterData, User
from .rng import Streams
from .world import CENT

ZERO = Decimal("0")


@dataclass
class _Item:
    no: int
    material: str
    qty: int
    net_price: Decimal
    delivered: int = 0


@dataclass
class _Order:
    no: str
    customer: str
    creator: str
    items: list[_Item]
    net: Decimal
    n_deliveries: int = 0
    status: str = "OPEN"      # OPEN | BLOCKED | REJECTED


@dataclass
class _Delivery:
    no: str
    order_no: str
    items: dict[int, int]     # item_no -> qty


@dataclass
class _Invoice:
    no: str
    customer: str
    gross: Decimal
    open_amount: Decimal
    due_date: date
    terms: str


class _Range:
    def __init__(self, start: int):
        self.n = start

    def next(self) -> str:
        self.n += 1
        return f"{self.n:010d}"


class Simulator:
    def __init__(self, cfg: Config, md: MasterData, cal: BizCalendar, streams: Streams):
        self.cfg, self.md, self.cal = cfg, md, cal
        self.r_ord = streams.get("sim.orders")
        self.r_proc = streams.get("sim.process")
        self.r_pay = streams.get("sim.payments")
        self.r_t = streams.get("sim.time")
        self.events: list[Event] = []
        self._heap: list = []
        self._seq = 0
        self.num = {k: _Range(v) for k, v in cfg.number_ranges.items()}
        self.orders: dict[str, _Order] = {}
        self.deliveries: dict[str, _Delivery] = {}
        self.invoices: dict[str, _Invoice] = {}
        self.opening = self._opening_quantities()
        self.stock = dict(self.opening)
        self.reserved: dict[str, int] = defaultdict(int)
        self.open_orders: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self.open_ar: dict[str, Decimal] = defaultdict(lambda: ZERO)
        self.wh = md.users_with_role("WAREHOUSE")
        self.credit = md.users_with_role("CREDIT")
        self.billing = md.users_with_role("BILLING")
        self.ar = md.users_with_role("AR")
        self.batch_bill = md.users["BATCH_BILL"]
        self.lockbox = md.users["BATCH_LCKBX"]

    # ---- plumbing -------------------------------------------------------------------
    def _push(self, ts: datetime, kind: str, **payload):
        self._seq += 1
        heapq.heappush(self._heap, (ts, self._seq, kind, payload))

    def _emit(self, ts: datetime, etype: str, user: str, doc_no: str, refs: dict, data: dict) -> Event:
        ev = Event(len(self.events) + 1, ts, etype, user, self.cfg.company_code, ts.date(), doc_no, refs, data)
        self.events.append(ev)
        return ev

    def _int(self, rng, rng_range: list[int]) -> int:
        return int(rng.integers(rng_range[0], rng_range[1] + 1))

    def _pick(self, rng, users: list[User]) -> User:
        return users[int(rng.integers(0, len(users)))]

    def _when(self, d: date, user: User) -> datetime:
        s, e = user.hours
        if user.is_batch:
            return datetime.combine(d, time(s, int(self.r_t.integers(0, 30)), int(self.r_t.integers(0, 60))))
        return datetime.combine(d, time()) + timedelta(seconds=int(self.r_t.integers(s * 3600, e * 3600)))

    def _after(self, ts: datetime, user: User, lo: int, hi: int) -> datetime:
        """A time shortly after ts within the user's working hours, else next business day."""
        t = ts + timedelta(minutes=int(self.r_t.integers(lo, hi + 1)))
        if t.date() != ts.date() or t.hour >= user.hours[1]:
            return self._when(self.cal.add_bd(ts.date(), 1), user)
        return t

    # ---- run ------------------------------------------------------------------------
    def run(self) -> list[Event]:
        self._opening_stock()
        self._schedule_orders()
        end = self.cfg.end_date
        while self._heap:
            ts, _, kind, p = heapq.heappop(self._heap)
            if ts.date() > end:
                break
            getattr(self, f"_on_{kind}")(ts, **p)
        return self.events

    def _opening_quantities(self) -> dict[str, int]:
        """Size opening stock from expected demand (receipts arrive with the P2P slice later)."""
        cfg, dem = self.cfg, self.cfg.demand
        push = self.cal.quarter_push_days(cfg.start_date, cfg.end_date, dem.quarter_end_push_days)
        orders = sum(
            dem.orders_per_business_day * dem.seasonality[d.month - 1] * (dem.quarter_end_push if d in push else 1.0)
            for d in self.cal.business_days(cfg.start_date, cfg.end_date)
        )
        qty_mean = dem.qty_median * math.exp(dem.qty_sigma ** 2 / 2)
        return {
            mat.id: max(50, math.ceil(orders * dem.items_mean * mat.weight * qty_mean * cfg.world.opening_stock_cover))
            for mat in self.md.materials.values()
        }

    def _opening_stock(self):
        items = [
            {"material_id": mat.id, "qty": self.opening[mat.id], "unit_cost": mat.std_cost}
            for mat in self.md.materials.values()
        ]
        ts = datetime.combine(self.cfg.start_date, time(0, 5))
        self._emit(ts, m.OPENING_STOCK, "SYSTEM", self.num["MD"].next(), {}, {"items": items})

    def _schedule_orders(self):
        cfg, md, dem = self.cfg, self.md, self.cfg.demand
        push_days = self.cal.quarter_push_days(cfg.start_date, cfg.end_date, dem.quarter_end_push_days)
        cust = list(md.customers.values())
        cp = [c.weight for c in cust]
        mats = list(md.materials.values())
        mp = [x.weight for x in mats]
        for d in self.cal.business_days(cfg.start_date, cfg.end_date):
            lam = dem.orders_per_business_day * dem.seasonality[d.month - 1]
            if d in push_days:
                lam *= dem.quarter_end_push
            for _ in range(int(self.r_ord.poisson(lam))):
                c = cust[int(self.r_ord.choice(len(cust), p=cp))]
                k = min(len(mats), 6, 1 + int(self.r_ord.poisson(dem.items_mean - 1)))
                idxs = self.r_ord.choice(len(mats), size=k, replace=False, p=mp)
                items = []
                for j, idx in enumerate(idxs, start=1):
                    mat = mats[int(idx)]
                    qty = max(1, int(round(self.r_ord.lognormal(math.log(dem.qty_median), dem.qty_sigma))))
                    price = (mat.list_price * (1 - c.price_discount)).quantize(CENT)
                    items.append(_Item(j * 10, mat.id, qty, price))
                self._push(self._when(d, md.users[c.owner]), "order", customer=c.id, items=items)

    # ---- order ----------------------------------------------------------------------
    def _on_order(self, ts: datetime, customer: str, items: list[_Item]):
        c = self.md.customers[customer]
        net = sum((i.qty * i.net_price for i in items), ZERO)
        no = self.num["SO"].next()
        blocked = self.open_orders[c.id] + self.open_ar[c.id] + net > c.credit_limit
        self.orders[no] = _Order(no, c.id, c.owner, items, net, status="BLOCKED" if blocked else "OPEN")
        self.open_orders[c.id] += net
        self._emit(ts, m.ORDER_CREATED, c.owner, no, {}, {
            "customer_id": c.id, "currency": self.cfg.currency, "terms": c.terms, "net_value": net,
            "credit_blocked": blocked,
            "items": [{"item_no": i.no, "material_id": i.material, "qty": i.qty, "net_price": i.net_price} for i in items],
        })
        if blocked:
            d = self.cal.add_bd(ts.date(), self._int(self.r_proc, self.cfg.process.credit_decision_lag_days))
            u = self._pick(self.r_proc, self.credit)
            self._push(self._when(d, u), "credit", order=no, user=u.id)
        else:
            self._plan_delivery(no, ts.date(), self.cfg.process.delivery_lag_days)

    def _on_credit(self, ts: datetime, order: str, user: str):
        o = self.orders[order]
        if self.r_proc.random() < self.cfg.process.credit_release_prob:
            o.status = "OPEN"
            self._emit(ts, m.ORDER_RELEASED, user, order, {"order": order}, {"customer_id": o.customer})
            self._plan_delivery(order, ts.date(), self.cfg.process.delivery_lag_days)
        else:
            o.status = "REJECTED"
            self.open_orders[o.customer] -= o.net
            self._emit(ts, m.ORDER_REJECTED, user, order, {"order": order}, {"customer_id": o.customer})

    # ---- delivery & goods issue -----------------------------------------------------
    def _plan_delivery(self, order_no: str, base: date, lag_range: list[int]):
        d = self.cal.add_bd(base, self._int(self.r_proc, lag_range))
        u = self._pick(self.r_proc, self.wh)
        self._push(self._when(d, u), "delivery", order=order_no, user=u.id)

    def _on_delivery(self, ts: datetime, order: str, user: str):
        o = self.orders[order]
        # one decision per order: is the first shipment a partial delivery?
        partial = o.n_deliveries == 0 and self.r_proc.random() < self.cfg.process.partial_delivery_prob
        lines: dict[int, int] = {}
        for it in o.items:
            remaining = it.qty - it.delivered
            if remaining <= 0:
                continue
            want = remaining
            if partial and remaining > 1:
                want = max(1, min(remaining - 1, int(remaining * self.r_proc.uniform(0.4, 0.8))))
            q = min(want, self.stock[it.material] - self.reserved[it.material])
            if q > 0:
                lines[it.no] = q
        if not lines:
            if any(it.delivered < it.qty for it in o.items):          # nothing available: retry later
                self._plan_delivery(order, ts.date(), [3, 3])
            return
        no = self.num["DL"].next()
        self.deliveries[no] = _Delivery(no, order, lines)
        o.n_deliveries += 1
        by_no = {it.no: it for it in o.items}
        for item_no, q in lines.items():
            by_no[item_no].delivered += q
            self.reserved[by_no[item_no].material] += q
        self._emit(ts, m.DELIVERY_CREATED, user, no, {"order": order}, {
            "customer_id": o.customer,
            "items": [{"item_no": n, "material_id": by_no[n].material, "qty": q} for n, q in lines.items()],
        })
        wh = self._pick(self.r_proc, self.wh)
        if self.r_proc.random() < self.cfg.process.gi_same_day_prob:
            gi_ts = self._after(ts, wh, 30, 180)
        else:
            gi_ts = self._when(self.cal.add_bd(ts.date(), 1), wh)
        self._push(gi_ts, "gi", delivery=no, user=wh.id)
        if any(it.delivered < it.qty for it in o.items):             # second delivery for the remainder
            self._plan_delivery(order, ts.date(), self.cfg.process.next_delivery_lag_days)

    def _on_gi(self, ts: datetime, delivery: str, user: str):
        dl = self.deliveries[delivery]
        o = self.orders[dl.order_no]
        by_no = {it.no: it for it in o.items}
        items = []
        for item_no, q in dl.items.items():
            mat = self.md.materials[by_no[item_no].material]
            self.stock[mat.id] -= q
            self.reserved[mat.id] -= q
            items.append({"item_no": item_no, "material_id": mat.id, "qty": q, "unit_cost": mat.std_cost})
        self._emit(ts, m.GOODS_ISSUED, user, self.num["MD"].next(),
                   {"delivery": delivery, "order": o.no}, {"customer_id": o.customer, "items": items})
        # schedule billing: nightly batch (never same night) or a billing clerk
        pr = self.cfg.process
        lag = self._int(self.r_proc, pr.billing_lag_days)
        if self.r_proc.random() < pr.batch_billing_prob:
            d = self.cal.add_bd(ts.date(), max(lag, 1))
            self._push(self._when(d, self.batch_bill), "billing", delivery=delivery, user=self.batch_bill.id)
        else:
            clerk = self._pick(self.r_proc, self.billing)
            bts = self._after(ts, clerk, 30, 240) if lag == 0 else self._when(self.cal.add_bd(ts.date(), lag), clerk)
            self._push(bts, "billing", delivery=delivery, user=clerk.id)

    # ---- billing & payment ----------------------------------------------------------
    def _on_billing(self, ts: datetime, delivery: str, user: str):
        dl = self.deliveries[delivery]
        o = self.orders[dl.order_no]
        c = self.md.customers[o.customer]
        by_no = {it.no: it for it in o.items}
        items, net = [], ZERO
        for item_no, q in dl.items.items():
            amt = (q * by_no[item_no].net_price).quantize(CENT)
            net += amt
            items.append({"item_no": item_no, "material_id": by_no[item_no].material, "qty": q, "net_amount": amt})
        rate = Decimal(str(self.cfg.world.tax_rate)) if c.tax_class == "TAXABLE" else ZERO
        tax = (net * rate).quantize(CENT)
        gross = net + tax
        terms = self.cfg.payment_terms[c.terms]
        due = ts.date() + timedelta(days=int(terms["net_days"]))
        no = self.num["BL"].next()
        self.invoices[no] = _Invoice(no, c.id, gross, gross, due, c.terms)
        self.open_orders[c.id] -= net
        self.open_ar[c.id] += gross
        self._emit(ts, m.BILLING_CREATED, user, no, {"delivery": delivery, "order": o.no}, {
            "customer_id": c.id, "items": items, "net": net, "tax": tax, "gross": gross,
            "tax_rate": rate, "tax_class": c.tax_class, "terms": c.terms, "due_date": due,
            "disc_days": int(terms["disc_days"]), "disc_pct": terms["disc_pct"],
        })
        self._plan_payments(self.invoices[no], c, ts.date())

    def _plan_payments(self, inv: _Invoice, c, bill_date: date):
        r, pr = self.r_pay, self.cfg.process
        terms = self.cfg.payment_terms[inv.terms]
        if terms["disc_pct"] > 0 and r.random() < c.p_take_discount:
            pay = self.cal.next_bd(bill_date + timedelta(days=int(r.integers(3, int(terms["disc_days"]) + 1))))
            disc = (inv.gross * Decimal(str(terms["disc_pct"]))).quantize(CENT)
            self._push_payment(pay, inv, inv.gross, disc)
            return
        delay = max(-5, int(round(r.normal(c.pay_delay_mean, c.pay_delay_mean * 0.6 + 2))))
        pay = self.cal.next_bd(inv.due_date + timedelta(days=delay))
        if r.random() < pr.partial_payment_prob:
            first = (inv.gross * Decimal(str(round(float(r.uniform(0.4, 0.8)), 2)))).quantize(CENT)
            self._push_payment(pay, inv, first, ZERO)
            later = self.cal.next_bd(pay + timedelta(days=int(r.integers(15, 41))))
            self._push_payment(later, inv, inv.gross - first, ZERO)
        else:
            self._push_payment(pay, inv, inv.gross, ZERO)

    def _push_payment(self, d: date, inv: _Invoice, applied: Decimal, discount: Decimal):
        if self.r_pay.random() < self.cfg.process.lockbox_prob:
            u = self.lockbox
        else:
            u = self._pick(self.r_pay, self.ar)
        self._push(self._when(d, u), "payment", invoice=inv.no, applied=applied, discount=discount, user=u.id)

    def _on_payment(self, ts: datetime, invoice: str, applied: Decimal, discount: Decimal, user: str):
        inv = self.invoices[invoice]
        inv.open_amount -= applied
        self.open_ar[inv.customer] -= applied
        self._emit(ts, m.PAYMENT_RECEIVED, user, self.num["PAY"].next(), {"invoice": invoice}, {
            "customer_id": inv.customer, "bank_amount": applied - discount,
            "applications": [{"invoice": invoice, "applied": applied, "discount": discount}],
        })
