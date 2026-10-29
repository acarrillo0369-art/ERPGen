# erpgen v0.2: synthetic S/4HANA-style order-to-cash data

Generates a year of order-to-cash activity for a manufacturing company and delivers it in two layers:

* **Raw layer:** 27 SAP-style tables (ACDOCA, BKPF, BSEG, MATDOC, VBAK/VBAP, LIKP/LIPS, VBRK/VBRP, VBFA,
  BUT000/KNA1, MARA/MBEW, SKA1/SKAT, ...) as an analyst would receive an extract: technical field names,
  zero-padded keys, `YYYYMMDD` dates, signed universal-journal amounts.
* **Readable layer:** 11 analyst-friendly tables (journal lines, sales order items, delivery items, billing
  items, document flow, AR items with open/cleared status, customers, materials, chart of accounts, users,
  material movements). It is built **only from the raw tables**, never from the simulator.

Everything is synthetic. No real client data, no employer material, no SAP code or content.

```bash
pip install -e ".[dev]"
erpgen run --config configs/manufacturing_v0.yaml --out out --layer both    # about 25 seconds for a full year
erpgen run ... --layer raw | readable | both  [--internal]                  # --internal also writes the event log
erpgen readable --raw-dir out/<run>/raw --out out/readable_only             # readable layer from raw CSVs alone
pytest                                                                      # 30 tests
```

## Architecture

```
profile (YAML) -> world -> simulator (events) -> accounting engine (journals)   canonical, ERP-neutral
                                |                         |
                                +-> canonical validator <-+
                                |
                                +-> S/4 exporter -> RAW layer (27 tables) -> READABLE layer (11 tables)
                                                       |                         |
                                                  raw checks              reconciliation vs simulator
```

* **Simulator stays ERP-neutral.** Each ERP gets its own exporter; a Workday exporter later would not touch it.
* **Raw is the engine for readable.** `readable.py` joins raw tables only, so the readable layer is reproducible
  from the CSV files alone (tested), and picking `--layer readable` still builds raw first.
* **Reconciliation proves completeness.** The readable layer must match what the simulator actually did
  (journal lines, order/delivery/billing items, document flow, open AR per customer, stock, master data).
* **Specs are data.** Table and field definitions live in `erpgen/sap/specs_s4_o2c.yaml`. A spec check fails if
  exported columns, key uniqueness, or value formats drift from it.
* **Three independent checkpoints.** Canonical validator (generator bugs), raw checks (what an analyst could
  run on the raw tables), reconciliation (are the layers complete). Tests corrupt the data on purpose
  (billing without a delivery, billing before goods issue, unbalanced document, missing header, missing
  document-flow link, over-billing, wrongly cleared receivable, altered amounts) and assert each is caught.

## Reference run (seed 42, full year 2025)

About 10.2k orders, 54k events, 32k journal entries; 411k raw rows in 27 tables, 300k readable rows in 11 tables;
trial balance sums to zero; credit block rate about 4%; DSO about 57 days; all four checks PASS.

## Known limitations: read before demoing or relying on this

* **Every table spec is unverified.** Field lists, lengths, keys, and code values (posting keys, document types,
  VORGN, movement types, status codes, flow categories, rejection reason) come from recall and third-party
  descriptions, not SAP's data dictionary. The manifest reports `tables_verified_against_sap: 0`. Check each
  table in SE11/DD03L or SAP documentation and flip `verified` in the YAML.
* **S/4 details that sources disagreed on:** whether BSEG is populated for all lines (I emit customer lines only),
  how ACDOCA signs amounts and names its client field (RCLNT, signed), and whether KNA1/KNB1/KNVV remain physical
  tables next to BUT000. MATDOC is emitted with its business key (MBLNR/MJAHR/ZEILE), not its internal keys.
* **Simplifications:** one company code, sales org, plant, and currency; delivery item numbers equal order item
  numbers; pricing conditions (PR00, K007) on orders only; no change documents (CDHDR/CDPOS), no T052 payment terms
  table, no returns or credit memos, one payment per invoice, calendar fiscal year, placeholder master-data names.
* **Not calibrated** against public sector benchmarks (gross margin about 22%; inventory is heavy because receipts
  arrive with the P2P slice).
* **No anomaly injector yet,** so there are no labels. The raw checks (R-03 billing without delivery, R-08
  over-billing, R-05 flow gaps) are the detectors the injector's scenarios will target.
* **Performance:** about 25 s for a full year; value formatting dominates and can be optimized.

## Next steps

1. Verify the table specs against SAP documentation (or a trial system, after reading its terms).
2. Anomaly injector at the event level, with labels keyed on SAP primary keys and benign look-alikes:
   billing without delivery (config-driven, direct FI invoice, no goods issue), backdated entries and cutoff.
3. Change documents and the access layer (roles, SoD), then calibration and a demo dashboard.


## Desktop app (Windows)

A lightweight native desktop interface is included in `desktop_app.py`. It uses the
same generator, SAP exporter, output writer, and validation checks as the CLI.

From the project root in VS Code:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[desktop]"
erpgen-desktop
```

Alternatively, run `python desktop_app.py`. The app lets you set the seed, date range,
output layers, optional internal event log, and output folder. Generation runs in a
background thread so the window remains responsive.

This first desktop version exposes the existing S/4HANA manufacturing profile only.
It does not add new manufacturing scenarios or change the generator's underlying
data model.
