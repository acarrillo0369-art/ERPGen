"""World builder: master data (customers, materials, users) drawn from the sector profile."""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

from .config import Config
from .model import Customer, MasterData, Material, User
from .rng import Streams

CENT = Decimal("0.01")


def D(x: float) -> Decimal:
    return Decimal(str(round(float(x), 2))).quantize(CENT, ROUND_HALF_UP)


def _zipf_weights(n: int, s: float, rng: np.random.Generator) -> np.ndarray:
    w = 1.0 / np.arange(1, n + 1) ** s
    w = rng.permutation(w)
    return w / w.sum()


def build_world(cfg: Config, streams: Streams) -> MasterData:
    rng = streams.get("world")
    w = cfg.world
    md = MasterData()

    def add_users(prefix: str, n: int, role: str, hours: tuple[int, int]):
        for i in range(1, n + 1):
            uid = f"{prefix}{i:02d}"
            md.users[uid] = User(uid, (role,), hours)

    add_users("SALES", w.n_sales_users, "SALES", (8, 18))
    add_users("CRED", w.n_credit_users, "CREDIT", (8, 17))
    add_users("WH", w.n_warehouse_users, "WAREHOUSE", (6, 16))
    add_users("BILL", w.n_billing_users, "BILLING", (8, 17))
    add_users("AR", w.n_ar_users, "AR", (8, 17))
    md.users["BATCH_BILL"] = User("BATCH_BILL", ("BILLING",), (2, 3), True)
    md.users["BATCH_LCKBX"] = User("BATCH_LCKBX", ("AR",), (6, 7), True)
    md.users["SYSTEM"] = User("SYSTEM", ("SYSTEM",), (0, 1), True)

    # Materials
    groups = list(cfg.material_groups)
    gshare = np.array([cfg.material_groups[g]["share"] for g in groups], dtype=float)
    gshare /= gshare.sum()
    mw = _zipf_weights(w.n_materials, w.material_zipf, rng)
    for i in range(w.n_materials):
        g = groups[int(rng.choice(len(groups), p=gshare))]
        cost = float(np.clip(rng.lognormal(math.log(w.cost_median), w.cost_sigma), 1.0, 5000.0))
        margin = rng.uniform(*w.margin_range)
        mid = f"M{i + 1:04d}"
        md.materials[mid] = Material(mid, f"Material {i + 1:04d}", g, D(cost), D(cost * (1 + margin)), float(mw[i]))

    # Customers
    cw = _zipf_weights(w.n_customers, w.customer_zipf, rng)
    terms = list(cfg.payment_terms)
    tshare = np.array([cfg.payment_terms[t]["share"] for t in terms], dtype=float)
    tshare /= tshare.sum()
    sales_users = [u.id for u in md.users_with_role("SALES")]

    qty_mean = cfg.demand.qty_median * math.exp(cfg.demand.qty_sigma ** 2 / 2)
    avg_price = sum(m.list_price * Decimal(str(m.weight)) for m in md.materials.values())
    avg_order_net = float(avg_price) * qty_mean * cfg.demand.items_mean

    for i in range(w.n_customers):
        cid = f"C{i + 1:04d}"
        t = terms[int(rng.choice(len(terms), p=tshare))]
        has_disc = cfg.payment_terms[t]["disc_pct"] > 0
        daily_sales = cfg.demand.orders_per_business_day * float(cw[i]) * avg_order_net
        limit = max(5000.0, daily_sales * w.credit_limit_days * rng.lognormal(0.0, 0.3))
        md.customers[cid] = Customer(
            id=cid,
            name=f"Customer {i + 1:04d}",
            tax_class="EXEMPT" if rng.random() < w.exempt_share else "TAXABLE",
            terms=t,
            credit_limit=D(round(limit, -2)),
            weight=float(cw[i]),
            pay_delay_mean=float(rng.uniform(*w.pay_delay_range)),
            p_take_discount=float(rng.beta(2, 3)) if has_disc else 0.0,
            price_discount=D(rng.uniform(0, w.max_customer_discount)),
            owner=sales_users[int(rng.integers(0, len(sales_users)))],
        )
    return md
