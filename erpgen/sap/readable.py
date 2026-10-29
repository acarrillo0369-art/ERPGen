"""Readable layer: joins the raw SAP tables into analyst-friendly tables.

It reads ONLY from a RawStore (never from simulator events), exactly as an analyst would
join BKPF, ACDOCA, SKAT, BUT000 and friends. Keys stay as SAP keys so every row can be traced
back to the raw tables; what changes is names, ISO dates, labels, and signs.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .spec import RawStore

DOC_TYPES = {"AB": "Opening balance", "WL": "Goods issue (delivery)", "RV": "Billing document transfer",
             "DZ": "Customer payment"}
REF_TYPES = {"VBRK": "Billing document", "MKPF": "Material document", "BKPF": "Accounting document"}
FLOW_TYPES = {"C": "Sales order", "J": "Delivery", "M": "Invoice", "R": "Goods movement"}
MOVEMENTS = {"561": "Initial stock entry", "601": "Goods issue for delivery"}
PROCESSING = {"A": "Not processed", "B": "Partially processed", "C": "Completely processed"}
CREDIT = {"A": "Credit OK", "B": "Credit blocked", "D": "Credit released"}


EMPTY: dict = defaultdict(str)          # missing join partner behaves like a left join: blanks


def _date(s: str) -> str:
    return "" if s in ("", "00000000") else f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _ts(d: str, t: str) -> str:
    return "" if d in ("", "00000000") else f"{_date(d)}T{t[:2]}:{t[2:4]}:{t[4:]}"


def _int(s: str) -> int:
    return int(s) if s else 0


def _index(rows: list[dict], *keys: str) -> dict:
    return {tuple(r[k] for k in keys) if len(keys) > 1 else r[keys[0]]: r for r in rows}


def build_readable(raw: RawStore) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    skat = _index(raw["SKAT"], "SAKNR")
    partners = _index(raw["BUT000"], "PARTNER")
    makt = _index(raw["MAKT"], "MATNR")
    bkpf = _index(raw["BKPF"], "BUKRS", "GJAHR", "BELNR")
    bseg = _index(raw["BSEG"], "BUKRS", "GJAHR", "BELNR", "BUZEI")
    mat_name = lambda n: makt[n]["MAKTX"] if n in makt else ""            # noqa: E731
    cust_name = lambda k: partners[k]["NAME_ORG1"] if k in partners else ""  # noqa: E731

    # --- journal lines: ACDOCA + BKPF + SKAT + BUT000 + MAKT -------------------------------------
    rows = []
    for a in raw["ACDOCA"]:
        h = bkpf.get((a["RBUKRS"], a["GJAHR"], a["BELNR"]), EMPTY)
        signed = Decimal(a["HSL"])
        rows.append({
            "company_code": a["RBUKRS"], "fiscal_year": int(a["GJAHR"]), "doc_no": a["BELNR"],
            "line_no": int(a["DOCLN"]), "doc_type": a["BLART"], "doc_type_text": DOC_TYPES.get(a["BLART"], ""),
            "posting_date": _date(a["BUDAT"]), "document_date": _date(a["BLDAT"]),
            "entry_timestamp": _ts(h["CPUDT"], h["CPUTM"]), "entry_user": h["USNAM"],
            "transaction_code": h["TCODE"], "account": a["RACCT"],
            "account_name": skat[a["RACCT"]]["TXT50"] if a["RACCT"] in skat else "",
            "debit_credit": "D" if a["DRCRK"] == "S" else "C", "amount": str(abs(signed)),
            "signed_amount": str(signed), "customer_no": a["KUNNR"], "customer_name": cust_name(a["KUNNR"]),
            "material_no": a["MATNR"], "material": mat_name(a["MATNR"]), "plant": a["WERKS"],
            "quantity": a["MSL"], "profit_center": a["PRCTR"], "source_document": a["AWREF"],
            "source_type": REF_TYPES.get(a["AWTYP"], a["AWTYP"]), "clearing_doc": a["AUGBL"],
            "clearing_date": _date(a["AUGDT"]),
        })
    out["journal_lines"] = rows

    # --- sales order items: VBAK + VBAP + BUT000 + MAKT ------------------------------------------
    vbak = _index(raw["VBAK"], "VBELN")
    out["sales_order_items"] = [{
        "order_no": p["VBELN"], "item_no": int(p["POSNR"]), "order_date": _date(vbak.get(p["VBELN"], EMPTY)["AUDAT"]),
        "created_by": vbak.get(p["VBELN"], EMPTY)["ERNAM"], "customer_no": vbak.get(p["VBELN"], EMPTY)["KUNNR"],
        "customer_name": cust_name(vbak.get(p["VBELN"], EMPTY)["KUNNR"]), "material_no": p["MATNR"], "material": p["ARKTX"],
        "quantity": p["KWMENG"], "net_value": p["NETWR"], "order_net_value": vbak.get(p["VBELN"], EMPTY)["NETWR"],
        "credit_status": CREDIT.get(vbak.get(p["VBELN"], EMPTY)["CMGST"], ""),
        "overall_status": PROCESSING.get(vbak.get(p["VBELN"], EMPTY)["GBSTK"], ""),
        "delivery_status": PROCESSING.get(vbak.get(p["VBELN"], EMPTY)["LFSTK"], ""),
        "billing_status": PROCESSING.get(vbak.get(p["VBELN"], EMPTY)["FKSTK"], ""),
        "rejected": p["ABGRU"] != "",
    } for p in raw["VBAP"]]

    # --- delivery items: LIKP + LIPS ----------------------------------------------------------------
    likp = _index(raw["LIKP"], "VBELN")
    out["delivery_items"] = [{
        "delivery_no": i["VBELN"], "item_no": int(i["POSNR"]), "order_no": i["VGBEL"],
        "order_item_no": int(i["VGPOS"]), "delivery_date": _date(likp.get(i["VBELN"], EMPTY)["LFDAT"]),
        "goods_issue_date": _date(likp.get(i["VBELN"], EMPTY)["WADAT_IST"]), "created_by": likp.get(i["VBELN"], EMPTY)["ERNAM"],
        "customer_no": likp.get(i["VBELN"], EMPTY)["KUNNR"], "customer_name": cust_name(likp.get(i["VBELN"], EMPTY)["KUNNR"]),
        "material_no": i["MATNR"], "material": i["ARKTX"], "quantity": i["LFIMG"],
        "goods_movement_status": PROCESSING.get(i["WBSTA"], ""),
    } for i in raw["LIPS"]]

    # --- billing items: VBRK + VBRP ------------------------------------------------------------------
    vbrk = _index(raw["VBRK"], "VBELN")
    out["billing_items"] = [{
        "billing_no": b["VBELN"], "item_no": int(b["POSNR"]), "billing_date": _date(vbrk.get(b["VBELN"], EMPTY)["FKDAT"]),
        "created_by": vbrk.get(b["VBELN"], EMPTY)["ERNAM"], "customer_no": vbrk.get(b["VBELN"], EMPTY)["KUNAG"],
        "customer_name": cust_name(vbrk.get(b["VBELN"], EMPTY)["KUNAG"]), "material_no": b["MATNR"], "material": b["ARKTX"],
        "quantity": b["FKIMG"], "net_value": b["NETWR"], "tax": b["MWSBP"],
        "delivery_no": b["VGBEL"], "order_no": b["AUBEL"], "accounting_doc": vrk_belnr(vbrk, b),
        "fiscal_year": _int(vbrk.get(b["VBELN"], EMPTY)["GJAHR"]), "payment_terms": vbrk.get(b["VBELN"], EMPTY)["ZTERM"],
    } for b in raw["VBRP"]]

    # --- document flow: VBFA ------------------------------------------------------------------------
    out["document_flow"] = [{
        "preceding_doc": f["VBELV"], "preceding_item": int(f["POSNV"]),
        "preceding_type": FLOW_TYPES.get(f["VBTYP_V"], f["VBTYP_V"]), "subsequent_doc": f["VBELN"],
        "subsequent_item": int(f["POSNN"]), "subsequent_type": FLOW_TYPES.get(f["VBTYP_N"], f["VBTYP_N"]),
        "quantity": f["RFMNG"], "value": f["RFWRT"], "date": _date(f["ERDAT"]),
    } for f in raw["VBFA"]]

    # --- AR items: customer lines of the universal journal, open vs cleared --------------------------
    items = []
    for a in raw["ACDOCA"]:
        if a["KOART"] != "D":
            continue
        seg = bseg.get((a["RBUKRS"], a["GJAHR"], a["BELNR"], a["DOCLN"][-3:]), {})
        items.append({
            "customer_no": a["KUNNR"], "customer_name": cust_name(a["KUNNR"]), "fiscal_year": int(a["GJAHR"]),
            "doc_no": a["BELNR"], "line_no": int(a["DOCLN"]), "doc_type": a["BLART"],
            "posting_date": _date(a["BUDAT"]), "signed_amount": a["HSL"],
            "status": "Cleared" if a["AUGBL"] else "Open", "clearing_doc": a["AUGBL"],
            "clearing_date": _date(a["AUGDT"]), "billing_no": seg.get("VBELN", ""),
            "payment_terms": seg.get("ZTERM", ""), "baseline_date": _date(seg.get("ZFBDT", "00000000")),
        })
    out["ar_items"] = items

    # --- master data ----------------------------------------------------------------------------------
    kna1, knb1, knvv = _index(raw["KNA1"], "KUNNR"), _index(raw["KNB1"], "KUNNR"), _index(raw["KNVV"], "KUNNR")
    knvi, ukm = _index(raw["KNVI"], "KUNNR"), _index(raw["UKM_BP_CMS_SGM"], "PARTNER")
    out["customers"] = [{
        "customer_no": p["PARTNER"], "name": p["NAME_ORG1"], "city": kna1.get(p["PARTNER"], EMPTY)["ORT01"],
        "region": kna1.get(p["PARTNER"], EMPTY)["REGIO"], "country": kna1.get(p["PARTNER"], EMPTY)["LAND1"],
        "payment_terms": knvv.get(p["PARTNER"], EMPTY)["ZTERM"], "credit_limit": ukm.get(p["PARTNER"], EMPTY)["CREDIT_LIMIT"],
        "tax_status": "Taxable" if knvi.get(p["PARTNER"], EMPTY)["TAXKD"] == "1" else "Exempt",
        "reconciliation_account": knb1.get(p["PARTNER"], EMPTY)["AKONT"], "sales_org": knvv.get(p["PARTNER"], EMPTY)["VKORG"],
    } for p in raw["BUT000"]]
    mara, mbew = _index(raw["MARA"], "MATNR"), {r["MATNR"]: r for r in raw["MBEW"]}
    out["materials"] = [{
        "material_no": n, "description": mat_name(n), "material_type": mara.get(n, EMPTY)["MTART"],
        "material_group": mara.get(n, EMPTY)["MATKL"], "standard_price": mbew.get(n, EMPTY)["STPRS"],
        "valuation_class": mbew.get(n, EMPTY)["BKLAS"], "stock_quantity": mbew.get(n, EMPTY)["LBKUM"], "stock_value": mbew.get(n, EMPTY)["SALK3"],
    } for n in mara]
    skb1 = _index(raw["SKB1"], "SAKNR")
    out["chart_of_accounts"] = [{
        "account": a["SAKNR"], "name": skat[a["SAKNR"]]["TXT50"] if a["SAKNR"] in skat else "",
        "account_type": "Balance sheet" if a["XBILK"] == "X" else "P&L",
        "reconciliation_type": "Customer" if skb1.get(a["SAKNR"], EMPTY)["MITKZ"] == "D" else "",
        "open_item_managed": skb1.get(a["SAKNR"], EMPTY)["XOPVW"] == "X",
    } for a in raw["SKA1"]]
    usr = _index(raw["USR02"], "BNAME")
    out["users"] = [{
        "user": r["UNAME"], "user_type": "System/batch" if usr.get(r["UNAME"], EMPTY)["USTYP"] == "B" else "Dialog",
        "role": r["AGR_NAME"], "valid_from": _date(r["FROM_DAT"]), "valid_to": _date(r["TO_DAT"]),
    } for r in raw["AGR_USERS"]]
    out["material_movements"] = [{
        "material_doc": r["MBLNR"], "item": int(r["ZEILE"]), "posting_date": _date(r["BUDAT"]),
        "movement_type": r["BWART"], "movement_text": MOVEMENTS.get(r["BWART"], ""), "material_no": r["MATNR"],
        "material": mat_name(r["MATNR"]),
        "quantity": r["MENGE"] if r["SHKZG"] == "S" else str(-Decimal(r["MENGE"])),
        "value": r["DMBTR"] if r["SHKZG"] == "S" else str(-Decimal(r["DMBTR"])),
        "delivery_no": r["VBELN_IM"], "entered_by": r["USNAM"],
    } for r in raw["MATDOC"]]
    return out


def vrk_belnr(vbrk: dict, item: dict) -> str:
    return vbrk.get(item["VBELN"], EMPTY)["BELNR"]


def write_readable(tables: dict[str, list[dict]], out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, rows in tables.items():
        with (out_dir / f"{name}.csv").open("w", newline="") as fh:
            if rows:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        counts[name] = len(rows)
    return counts
