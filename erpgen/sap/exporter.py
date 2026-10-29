"""S/4HANA-style raw layer: reshapes the canonical events and journals into SAP tables.

Everything SAP-specific (table and field names, key formats, document categories, posting
keys, status codes) lives here and in specs_s4_o2c.yaml. Codes marked "unverified" below come
from recall; check them against SAP documentation before relying on them.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from .. import model as m
from ..config import Config
from ..model import Event, JournalEntry, MasterData
from .keys import KeyMap
from .spec import RawStore, load_spec

ZERO = Decimal("0")
CITIES = [("Dallas", "TX"), ("Houston", "TX"), ("Chicago", "IL"), ("Atlanta", "GA"),
          ("Denver", "CO"), ("Phoenix", "AZ"), ("Columbus", "OH"), ("Charlotte", "NC")]

# Per event type: transaction type (VORGN), reference procedure (AWTYP). Unverified.
VORGN = {m.OPENING_STOCK: "RMWE", m.GOODS_ISSUED: "RMWA", m.BILLING_CREATED: "SD00", m.PAYMENT_RECEIVED: "RFBU"}
AWTYP = {m.OPENING_STOCK: "MKPF", m.GOODS_ISSUED: "MKPF", m.BILLING_CREATED: "VBRK", m.PAYMENT_RECEIVED: "BKPF"}


class S4Exporter:
    def __init__(self, cfg: Config, md: MasterData, events: list[Event], entries: list[JournalEntry]):
        self.cfg, self.md, self.sap = cfg, md, cfg.sap
        self.keys = KeyMap(cfg)
        self.events = sorted(events, key=lambda e: (e.ts, e.event_id))
        self.entries = entries
        self.client = cfg.sap.client
        self.cur = cfg.currency
        self.je_of = {je.source_event_id: je for je in entries}
        self.ev: dict[str, list[Event]] = defaultdict(list)
        for e in self.events:
            self.ev[e.type].append(e)
        self.orders = {e.doc_no: e for e in self.ev[m.ORDER_CREATED]}
        self.decision = {e.refs["order"]: e.type for e in self.ev[m.ORDER_RELEASED] + self.ev[m.ORDER_REJECTED]}
        self.dels_of_order: dict[str, list[Event]] = defaultdict(list)
        for e in self.ev[m.DELIVERY_CREATED]:
            self.dels_of_order[e.refs["order"]].append(e)
        self.gi_of = {e.refs["delivery"]: e for e in self.ev[m.GOODS_ISSUED]}
        self.bill_of = {e.refs["delivery"]: e for e in self.ev[m.BILLING_CREATED]}
        self.bills = {e.doc_no: e for e in self.ev[m.BILLING_CREATED]}
        self.clearing = self._clearing()

    # ---- helpers ---------------------------------------------------------------------------
    def _clearing(self) -> dict[str, tuple[str, int, date]]:
        """invoice -> (clearing FI doc, fiscal year, clearing date), once fully paid."""
        paid: dict[str, Decimal] = defaultdict(lambda: ZERO)
        out: dict[str, tuple[str, int, date]] = {}
        for e in self.ev[m.PAYMENT_RECEIVED]:
            for a in e.data["applications"]:
                inv = a["invoice"]
                paid[inv] += a["applied"]
                if paid[inv] == self.bills[inv].data["gross"]:
                    je = self.je_of[e.event_id]
                    out[inv] = (je.doc_no, je.fiscal_year, e.posting_date)
        return out

    def _row(self, store: RawStore, table: str, **kw):
        store.add(table, kw)

    # ---- build -----------------------------------------------------------------------------
    def build(self) -> RawStore:
        store = RawStore(load_spec())
        self._org_and_gl(store)
        self._customers(store)
        self._materials(store)
        self._sales_docs(store)
        self._deliveries(store)
        self._billing(store)
        self._doc_flow(store)
        self._fi_docs(store)
        self._mat_docs(store)
        self._users(store)
        return store

    # ---- organisation and G/L master ---------------------------------------------------------
    def _org_and_gl(self, s: RawStore):
        c, sap = self.cfg, self.sap
        self._row(s, "T001", MANDT=self.client, BUKRS=c.company_code, BUTXT=sap.company_name,
                  ORT01=sap.company_city, LAND1=sap.country, WAERS=self.cur, KTOPL=sap.chart_of_accounts,
                  PERIV=sap.fiscal_variant)
        for acct, name in sorted(c.chart.items()):
            sak = self.keys.saknr(acct)
            pnl = acct[0] in "45"
            self._row(s, "SKA1", MANDT=self.client, KTOPL=sap.chart_of_accounts, SAKNR=sak,
                      XBILK="" if pnl else "X", KTOKS="SAKO", GVTYP="X" if pnl else "")
            self._row(s, "SKB1", MANDT=self.client, BUKRS=c.company_code, SAKNR=sak, WAERS=self.cur,
                      MITKZ="D" if acct == c.accounts["ar"] else "", XOPVW="X" if acct == c.accounts["ar"] else "")
            self._row(s, "SKAT", MANDT=self.client, SPRAS=sap.language, KTOPL=sap.chart_of_accounts,
                      SAKNR=sak, TXT20=name[:20], TXT50=name[:50])

    # ---- customers -------------------------------------------------------------------------
    def _customers(self, s: RawStore):
        sap, c = self.sap, self.cfg
        for cust in self.md.customers.values():
            k = self.keys.kunnr(cust.id)
            city, region = CITIES[int(cust.id[1:]) % len(CITIES)]
            self._row(s, "BUT000", CLIENT=self.client, PARTNER=k, TYPE="2", BU_GROUP="CUST",
                      BU_SORT1=cust.name.upper()[:20], NAME_ORG1=cust.name, CRDAT=c.start_date, CRUSR="SYSTEM")
            self._row(s, "KNA1", MANDT=self.client, KUNNR=k, NAME1=cust.name, ORT01=city, LAND1=sap.country,
                      REGIO=region, KTOKD="0001", ERDAT=c.start_date, ERNAM="SYSTEM")
            self._row(s, "KNB1", MANDT=self.client, KUNNR=k, BUKRS=c.company_code,
                      AKONT=self.keys.saknr(c.accounts["ar"]), ZTERM=self.keys.zterm(cust.terms),
                      ERDAT=c.start_date, ERNAM="SYSTEM")
            self._row(s, "KNVV", MANDT=self.client, KUNNR=k, VKORG=sap.sales_org, VTWEG=sap.distribution_channel,
                      SPART=sap.division, ZTERM=self.keys.zterm(cust.terms), KALKS="1", KDGRP="01", WAERS=self.cur)
            self._row(s, "KNVI", MANDT=self.client, KUNNR=k, ALAND=sap.country, TATYP="MWST",
                      TAXKD="1" if cust.tax_class == "TAXABLE" else "0")
            self._row(s, "UKM_BP_CMS_SGM", CLIENT=self.client, PARTNER=k, CREDIT_SGMNT=sap.credit_segment,
                      CREDIT_LIMIT=cust.credit_limit)

    # ---- materials -------------------------------------------------------------------------
    def _materials(self, s: RawStore):
        sap, c = self.sap, self.cfg
        qty: dict[str, int] = defaultdict(int)
        for e in self.ev[m.OPENING_STOCK]:
            for it in e.data["items"]:
                qty[it["material_id"]] += it["qty"]
        for e in self.ev[m.GOODS_ISSUED]:
            for it in e.data["items"]:
                qty[it["material_id"]] -= it["qty"]
        for mat in self.md.materials.values():
            n = self.keys.matnr(mat.id)
            self._row(s, "MARA", MANDT=self.client, MATNR=n, MTART=sap.material_types[mat.group], MATKL=mat.group,
                      MEINS=sap.unit, ERSDA=c.start_date, ERNAM="SYSTEM")
            self._row(s, "MAKT", MANDT=self.client, MATNR=n, SPRAS=sap.language, MAKTX=mat.name)
            self._row(s, "MBEW", MANDT=self.client, MATNR=n, BWKEY=sap.plant, BWTAR="", VPRSV="S",
                      STPRS=mat.std_cost, VERPR=mat.std_cost, PEINH=1, BKLAS=sap.valuation_classes[mat.group],
                      LBKUM=qty[mat.id], SALK3=qty[mat.id] * mat.std_cost)

    # ---- sales documents -------------------------------------------------------------------
    def _sales_docs(self, s: RawStore):
        sap, kp = self.sap, self.keys
        for so, e in self.orders.items():
            d = e.data
            dec = self.decision.get(so)
            delivered: dict[int, int] = defaultdict(int)
            billed: dict[int, int] = defaultdict(int)
            for de in self.dels_of_order.get(so, []):
                for it in de.data["items"]:
                    delivered[it["item_no"]] += it["qty"]
                bill = self.bill_of.get(de.doc_no)
                if bill:
                    for it in bill.data["items"]:
                        billed[it["item_no"]] += it["qty"]
            ordered = sum(i["qty"] for i in d["items"])
            n_deliv, n_bill = sum(delivered.values()), sum(billed.values())
            lfstk = "C" if n_deliv == ordered else ("B" if n_deliv else "A")
            fkstk = "C" if n_bill == ordered else ("B" if n_bill else "A")
            gbstk = "C" if (fkstk == "C" or dec == m.ORDER_REJECTED) else ("A" if not n_deliv else "B")
            if not d["credit_blocked"]:
                cmgst = "A"
            else:
                cmgst = "D" if dec == m.ORDER_RELEASED else "B"
            self._row(s, "VBAK", MANDT=self.client, VBELN=so, ERDAT=e.ts.date(), ERZET=e.ts.time(), ERNAM=e.user_id,
                      AUDAT=e.posting_date, VBTYP="C", AUART=sap.order_type, VKORG=sap.sales_org,
                      VTWEG=sap.distribution_channel, SPART=sap.division, NETWR=d["net_value"], WAERK=self.cur,
                      KUNNR=kp.kunnr(d["customer_id"]), KNUMV=kp.knumv(so), CMGST=cmgst, GBSTK=gbstk,
                      LFSTK=lfstk, FKSTK=fkstk)
            cust = self.md.customers[d["customer_id"]]
            for it in d["items"]:
                mat = self.md.materials[it["material_id"]]
                pos = kp.posnr(it["item_no"])
                net = it["qty"] * it["net_price"]
                self._row(s, "VBAP", MANDT=self.client, VBELN=so, POSNR=pos, MATNR=kp.matnr(mat.id), ARKTX=mat.name,
                          PSTYV=sap.item_category, KWMENG=it["qty"], VRKME=sap.unit, NETWR=net, WAERK=self.cur,
                          WERKS=sap.plant, LGORT=sap.storage_location,
                          ABGRU="50" if dec == m.ORDER_REJECTED else "")
                list_val = it["qty"] * mat.list_price
                self._row(s, "PRCD_ELEMENTS", CLIENT=self.client, KNUMV=kp.knumv(so), KPOSN=pos, STUNR=10, ZAEHK=1,
                          KAPPL="V", KSCHL=sap.price_condition, KRECH="C", KBETR=mat.list_price, WAERS=self.cur,
                          KPEIN=1, KMEIN=sap.unit, KAWRT=it["qty"], KWERT=list_val)
                self._row(s, "PRCD_ELEMENTS", CLIENT=self.client, KNUMV=kp.knumv(so), KPOSN=pos, STUNR=20, ZAEHK=1,
                          KAPPL="V", KSCHL=sap.discount_condition, KRECH="A", KBETR=-cust.price_discount * 100,
                          WAERS="%", KPEIN=1, KMEIN=sap.unit, KAWRT=list_val, KWERT=net - list_val)

    # ---- deliveries ------------------------------------------------------------------------
    def _deliveries(self, s: RawStore):
        sap, kp = self.sap, self.keys
        for e in self.ev[m.DELIVERY_CREATED]:
            gi = self.gi_of.get(e.doc_no)
            k = kp.kunnr(e.data["customer_id"])
            self._row(s, "LIKP", MANDT=self.client, VBELN=e.doc_no, ERDAT=e.ts.date(), ERZET=e.ts.time(),
                      ERNAM=e.user_id, BLDAT=e.posting_date, LFDAT=e.posting_date, WADAT=e.posting_date,
                      WADAT_IST=gi.posting_date if gi else None, LFART=sap.delivery_type, VSTEL=sap.shipping_point,
                      VKORG=sap.sales_org, KUNNR=k, KUNAG=k, WBSTK="C" if gi else "A")
            for it in e.data["items"]:
                mat = self.md.materials[it["material_id"]]
                self._row(s, "LIPS", MANDT=self.client, VBELN=e.doc_no, POSNR=kp.posnr(it["item_no"]),
                          MATNR=kp.matnr(mat.id), ARKTX=mat.name, LFIMG=it["qty"], MEINS=sap.unit, VRKME=sap.unit,
                          WERKS=sap.plant, LGORT=sap.storage_location, VGBEL=e.refs["order"],
                          VGPOS=kp.posnr(it["item_no"]), VGTYP="C", PSTYV=sap.item_category,
                          WBSTA="C" if gi else "A")

    # ---- billing ---------------------------------------------------------------------------
    def _billing(self, s: RawStore):
        sap, kp = self.sap, self.keys
        for e in self.ev[m.BILLING_CREATED]:
            d, je = e.data, self.je_of[e.event_id]
            k = kp.kunnr(d["customer_id"])
            self._row(s, "VBRK", MANDT=self.client, VBELN=e.doc_no, FKART=sap.billing_type, VBTYP="M", WAERK=self.cur,
                      VKORG=sap.sales_org, VTWEG=sap.distribution_channel, SPART=sap.division, FKDAT=e.posting_date,
                      BELNR=je.doc_no, GJAHR=je.fiscal_year, BUKRS=e.company_code, KUNRG=k, KUNAG=k,
                      NETWR=d["net"], MWSBK=d["tax"], ZTERM=kp.zterm(d["terms"]), ERNAM=e.user_id,
                      ERDAT=e.ts.date(), ERZET=e.ts.time(), FKSTO="", RFBSK="C")
            items = d["items"]
            tax_left = d["tax"]
            for i, it in enumerate(items):
                last = i == len(items) - 1
                share = tax_left if last else (d["tax"] * it["net_amount"] / d["net"]).quantize(Decimal("0.01"))
                tax_left -= share
                mat = self.md.materials[it["material_id"]]
                self._row(s, "VBRP", MANDT=self.client, VBELN=e.doc_no, POSNR=kp.posnr(it["item_no"]),
                          MATNR=kp.matnr(mat.id), ARKTX=mat.name, FKIMG=it["qty"], VRKME=sap.unit,
                          NETWR=it["net_amount"], MWSBP=share, VGBEL=e.refs["delivery"],
                          VGPOS=kp.posnr(it["item_no"]), VGTYP="J", AUBEL=e.refs["order"],
                          AUPOS=kp.posnr(it["item_no"]), WERKS=sap.plant, PSTYV=sap.item_category)

    # ---- document flow ---------------------------------------------------------------------
    def _doc_flow(self, s: RawStore):
        kp = self.keys
        price = {(so, it["item_no"]): it["net_price"] for so, e in self.orders.items() for it in e.data["items"]}
        for e in self.ev[m.DELIVERY_CREATED]:
            so = e.refs["order"]
            for it in e.data["items"]:
                pos = kp.posnr(it["item_no"])
                self._row(s, "VBFA", MANDT=self.client, VBELV=so, POSNV=pos, VBELN=e.doc_no, POSNN=pos,
                          VBTYP_N="J", VBTYP_V="C", RFMNG=it["qty"], MEINS=self.sap.unit,
                          RFWRT=it["qty"] * price[(so, it["item_no"])], WAERS=self.cur, PLMIN="+",
                          ERDAT=e.ts.date(), ERZET=e.ts.time())
        for e in self.ev[m.GOODS_ISSUED]:
            for z, it in enumerate(e.data["items"], start=1):
                self._row(s, "VBFA", MANDT=self.client, VBELV=e.refs["delivery"], POSNV=kp.posnr(it["item_no"]),
                          VBELN=e.doc_no, POSNN=z, VBTYP_N="R", VBTYP_V="J", RFMNG=it["qty"], MEINS=self.sap.unit,
                          RFWRT=it["qty"] * it["unit_cost"], WAERS=self.cur, PLMIN="+",
                          ERDAT=e.ts.date(), ERZET=e.ts.time())
        for e in self.ev[m.BILLING_CREATED]:
            for it in e.data["items"]:
                pos = kp.posnr(it["item_no"])
                self._row(s, "VBFA", MANDT=self.client, VBELV=e.refs["delivery"], POSNV=pos, VBELN=e.doc_no, POSNN=pos,
                          VBTYP_N="M", VBTYP_V="J", RFMNG=it["qty"], MEINS=self.sap.unit, RFWRT=it["net_amount"],
                          WAERS=self.cur, PLMIN="+", ERDAT=e.ts.date(), ERZET=e.ts.time())

    # ---- accounting documents (BKPF, ACDOCA, BSEG) ------------------------------------------
    _BSCHL = {  # (event type, debit/credit, line kind) -> posting key. Unverified.
        (m.OPENING_STOCK, "D"): "89", (m.OPENING_STOCK, "C"): "50",
        (m.GOODS_ISSUED, "D"): "81", (m.GOODS_ISSUED, "C"): "99",
        (m.BILLING_CREATED, "D"): "01", (m.BILLING_CREATED, "C"): "50",
        (m.PAYMENT_RECEIVED, "D"): "40", (m.PAYMENT_RECEIVED, "C"): "15",
    }

    def _fi_docs(self, s: RawStore):
        c, sap, kp = self.cfg, self.sap, self.keys
        ev_of = {e.event_id: e for e in self.events}
        ar_acct = c.accounts["ar"]
        for je in self.entries:
            e = ev_of[je.source_event_id]
            user = self.md.users[je.user_id]
            mblnr_key = f"{e.doc_no}{e.posting_date.year}"
            tcode = {m.BILLING_CREATED: "VF04" if user.is_batch else "VF01", m.GOODS_ISSUED: "VL02N",
                     m.PAYMENT_RECEIVED: "FLB2" if user.is_batch else "F-28", m.OPENING_STOCK: "LSMW"}[e.type]
            if e.type == m.BILLING_CREATED:
                awkey, xblnr = e.doc_no, e.doc_no
            elif e.type in (m.GOODS_ISSUED, m.OPENING_STOCK):
                awkey, xblnr = mblnr_key, e.refs.get("delivery", "")
            else:
                awkey, xblnr = "", ""
            self._row(s, "BKPF", MANDT=self.client, BUKRS=je.company_code, BELNR=je.doc_no, GJAHR=je.fiscal_year,
                      BLART=je.doc_type, BLDAT=je.document_date, BUDAT=je.posting_date, MONAT=je.period,
                      CPUDT=je.entry_ts.date(), CPUTM=je.entry_ts.time(), USNAM=je.user_id, TCODE=tcode,
                      XBLNR=xblnr, WAERS=je.currency, KURSF=1, AWTYP=AWTYP[e.type], AWKEY=awkey, STBLG="",
                      BSTAT="", LDGRP="")
            item_of = {}   # material -> item number / line (for AWITEM and quantities)
            qty_of = {}
            if e.type in (m.BILLING_CREATED, m.GOODS_ISSUED, m.OPENING_STOCK):
                for z, it in enumerate(e.data["items"], start=1):
                    item_of[it["material_id"]] = it.get("item_no", z) if e.type == m.BILLING_CREATED else z
                    qty_of[it["material_id"]] = it["qty"]
            if e.type == m.BILLING_CREATED:
                awref, aworg = e.doc_no, ""
            elif e.type in (m.GOODS_ISSUED, m.OPENING_STOCK):
                awref, aworg = e.doc_no, str(e.posting_date.year)
            else:
                awref, aworg = je.doc_no, f"{je.company_code}{je.fiscal_year}"
            for ln in je.lines:
                sign = 1 if ln.dc == "D" else -1
                amt = sign * ln.amount
                is_ar = ln.account == ar_acct
                ktosl = ""
                if e.type == m.BILLING_CREATED and ln.dc == "C":
                    ktosl = "ERL" if ln.material_id else "MWS"
                elif e.type == m.GOODS_ISSUED:
                    ktosl = "GBB" if ln.dc == "D" else "BSX"
                elif e.type == m.OPENING_STOCK and ln.dc == "D":
                    ktosl = "BSX"
                koart = "D" if is_ar else ("M" if ktosl == "BSX" else "S")
                grp = self.md.materials[ln.material_id].group if ln.material_id else None
                clr = None
                if is_ar:
                    inv = ln.ref_doc
                    clr = self.clearing.get(inv)
                bschl = self._BSCHL[(e.type, ln.dc)]
                msl = sign * qty_of[ln.material_id] if ln.material_id in qty_of else 0
                self._row(
                    s, "ACDOCA", RCLNT=self.client, RLDNR=sap.ledger, RBUKRS=je.company_code, GJAHR=je.fiscal_year,
                    BELNR=je.doc_no, DOCLN=ln.line_no, RYEAR=je.fiscal_year, RRCTY="0", VORGN=VORGN[e.type],
                    KOART=koart, RACCT=kp.saknr(ln.account), PRCTR=sap.profit_centers[grp] if grp else "",
                    KOKRS=sap.controlling_area, RHCUR=self.cur, RWCUR=self.cur, RKCUR=self.cur,
                    HSL=amt, WSL=amt, KSL=amt, DRCRK="S" if ln.dc == "D" else "H", POPER=je.period,
                    BUDAT=je.posting_date, BLDAT=je.document_date, BLART=je.doc_type,
                    TIMESTAMP=int(je.entry_ts.strftime("%Y%m%d%H%M%S")), USNAM=je.user_id, AWTYP=AWTYP[e.type],
                    AWREF=awref, AWORG=aworg, AWITEM=item_of.get(ln.material_id, 0) if ln.material_id else 0,
                    KUNNR=kp.kunnr(ln.customer_id) if is_ar else "", MATNR=kp.matnr(ln.material_id) if grp else "",
                    WERKS=sap.plant if grp else "", MSL=msl, RUNIT=sap.unit if msl else "",
                    AUGDT=clr[2] if clr else None, AUGBL=clr[0] if clr else "", AUGGJ=clr[1] if clr else 0,
                    KTOSL=ktosl, BSCHL=bschl)
                if is_ar:
                    terms = kp.zterm(e.data["terms"]) if e.type == m.BILLING_CREATED else ""
                    self._row(
                        s, "BSEG", MANDT=self.client, BUKRS=je.company_code, BELNR=je.doc_no, GJAHR=je.fiscal_year,
                        BUZEI=ln.line_no, BSCHL=bschl, KOART="D", SHKZG="S" if ln.dc == "D" else "H",
                        DMBTR=ln.amount, WRBTR=ln.amount, PSWSL=self.cur, HKONT=kp.saknr(ar_acct),
                        KUNNR=kp.kunnr(ln.customer_id), ZTERM=terms,
                        ZFBDT=e.posting_date if e.type == m.BILLING_CREATED else None,
                        VBELN=e.doc_no if e.type == m.BILLING_CREATED else "",
                        AUGDT=clr[2] if clr else None, AUGBL=clr[0] if clr else "", AUGGJ=clr[1] if clr else 0)

    # ---- material documents ----------------------------------------------------------------
    def _mat_docs(self, s: RawStore):
        sap, kp = self.sap, self.keys
        for e in self.ev[m.OPENING_STOCK] + self.ev[m.GOODS_ISSUED]:
            opening = e.type == m.OPENING_STOCK
            for z, it in enumerate(e.data["items"], start=1):
                self._row(s, "MATDOC", MANDT=self.client, MBLNR=e.doc_no, MJAHR=e.posting_date.year, ZEILE=z,
                          BLDAT=e.posting_date, BUDAT=e.posting_date, CPUDT=e.ts.date(), CPUTM=e.ts.time(),
                          USNAM=e.user_id, BWART="561" if opening else "601", MATNR=kp.matnr(it["material_id"]),
                          WERKS=sap.plant, LGORT=sap.storage_location, MENGE=it["qty"], MEINS=sap.unit,
                          DMBTR=it["qty"] * it["unit_cost"], SHKZG="S" if opening else "H",
                          KUNNR="" if opening else kp.kunnr(e.data["customer_id"]),
                          VBELN_IM="" if opening else e.refs["delivery"],
                          VBELP_IM=0 if opening else it["item_no"])

    # ---- users and roles ---------------------------------------------------------------------
    def _users(self, s: RawStore):
        for u in self.md.users.values():
            self._row(s, "USR02", MANDT=self.client, BNAME=u.id, USTYP="B" if u.is_batch else "A",
                      ERDAT=self.cfg.start_date)
            for role in u.roles:
                self._row(s, "AGR_USERS", MANDT=self.client, AGR_NAME=self.sap.roles[role], UNAME=u.id,
                          FROM_DAT=self.cfg.start_date, TO_DAT=date(9999, 12, 31))
