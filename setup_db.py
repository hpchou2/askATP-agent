"""
setup_db.py — Create and seed the mock SAP database (sap_mock.db).

Run once before starting the agent:
    python setup_db.py

All dates are relative to the day you run this script, so the test scenarios
in the README give the same results whenever they are run.
"""

import json
import sqlite3
import os
from datetime import date, timedelta

# Place the database file in the same folder as this script
DB_PATH = os.path.join(os.path.dirname(__file__), "sap_mock.db")

# LangGraph checkpoints (workflow runs paused for planner approval) — same path
# as CHECKPOINT_DB_PATH in api.py
CHECKPOINT_DB_PATH = os.path.join(os.path.dirname(__file__), "checkpoints.db")

# Days after the seed date for each PO arrival and SO due date
PO_ARRIVAL_DAYS = {
    "PO1001": 40, "PO1002": 45, "PO1003": 50,
    "PO1004": 35, "PO1005": 45, "PO1006": 55,
}
SO_DUE_DAYS = 30

# The five README test scenarios: (matnr, requested_qty, days from seed date)
TEST_REQUESTS = [
    ("Test 1 - from current stock",       "MAT001",  20, 10),
    ("Test 2 - via incoming PO, on time", "MAT002", 100, 45),
    ("Test 3 - PO late -> AIR",            "MAT001", 100, 10),
    ("Test 4 - quantity shortage",        "MAT003", 200, 50),
    ("Test 5 - skip SO-absorbed PO",      "MAT004",  50, 10),
]


def _day(base: date, days: int) -> str:
    return (base + timedelta(days=days)).isoformat()


def create_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    # material_master: one row per product — its name, unit of measure, and category
    cur.execute("""
        CREATE TABLE IF NOT EXISTS material_master (
            matnr TEXT PRIMARY KEY,  -- Material number: unique product ID (e.g. 'MAT001')
            maktx TEXT,              -- Description: human-readable product name
            meins TEXT,              -- Unit of measure (e.g. 'PC' = pieces, 'KG' = kilograms)
            mtart TEXT               -- Material type (e.g. 'FERT' = finished manufactured good)
        )
    """)

    # stock: physical inventory on hand, by plant and storage location
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            matnr TEXT,   -- Material number
            werks TEXT,   -- Plant: the facility that holds this stock (e.g. 'TW01' = Taiwan site 1)
            lgort TEXT,   -- Storage location within the plant (e.g. 'WH01' = main warehouse)
            labst REAL,   -- Unrestricted-use stock: quantity freely available to sell or ship
            PRIMARY KEY (matnr, werks, lgort)
        )
    """)

    # purchase_orders: future supply arriving from vendors on a scheduled date
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_orders (
            po_number        TEXT PRIMARY KEY,  -- Purchase order ID (e.g. 'PO1001')
            matnr            TEXT,              -- Material being purchased
            werks            TEXT,              -- Plant that will receive the delivery
            menge            REAL,              -- Quantity ordered from the vendor
            eindt            TEXT,              -- Expected goods receipt date (YYYY-MM-DD)
            logistic_method  TEXT DEFAULT 'SEA' -- Shipping method: 'SEA' (default) or 'AIR'
        )
    """)

    # sales_orders: customer orders already confirmed — stock promised but not yet shipped
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sales_orders (
            so_number TEXT PRIMARY KEY,  -- Sales order ID (e.g. 'SO2001')
            matnr     TEXT,              -- Material the customer ordered
            werks     TEXT,              -- Plant responsible for fulfillment
            kwmeng    REAL,              -- Confirmed quantity: amount promised to the customer
            edatu     TEXT,              -- Customer's requested delivery date (YYYY-MM-DD)
            status    TEXT               -- 'OPEN' = confirmed but not yet shipped
        )
    """)

    # approval_log: append-only record of planner decisions on proposed PO
    # switches (the fulfillment case record required by policy rule 4)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS approval_log (
            log_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id     TEXT,  -- Workflow run ID returned by POST /atp-check
            matnr         TEXT,  -- Material number
            werks         TEXT,  -- Plant
            requested_qty REAL,  -- Quantity the new order needs
            method        TEXT,  -- Target logistic method (e.g. 'AIR')
            po_numbers    TEXT,  -- Comma-separated POs in the proposal
            decision      TEXT,  -- 'APPROVED', 'REJECTED' or 'STALE_PROPOSAL'
            approver      TEXT,  -- Who made the decision
            comment       TEXT,  -- Optional note from the approver
            decided_at    TEXT   -- UTC timestamp (YYYY-MM-DD HH:MM:SS)
        )
    """)

    conn.commit()


def seed_data(conn: sqlite3.Connection, base: date | None = None) -> None:
    """
    Populates mock data for a small electronics warehouse.

    Dates are offsets from `base` (default: today), written below as T+n days.

    Inventory snapshot:
      Material | Description       | Stock | SO Committed | Net Avail | POs
      ---------+-------------------+-------+--------------+-----------+-------------------
      MAT001   | Server CPU Module |  150  |     120      |     30    | PO1001: T+40 +100
      MAT002   | Memory 32GB       |  300  |     250      |     50    | PO1002: T+45 +200
      MAT003   | NVMe SSD 1TB      |   80  |      80      |      0    | PO1003: T+50 +150
      MAT004   | Graphics Card 8GB |   50  |     200      |   -150    | PO1004: T+35 +100
               |                   |       |   (SO2004    |           | PO1005: T+45 +120
               |                   |       |  + SO2005)   |           | PO1006: T+55 +100

    MAT004 is the break-even demonstration: available_now = 50 − 200 = −150.
      PO walk for requested_qty=50:
        PO1004 (+100): running = −50  → still absorbing SO demand, skip (do NOT switch)
        PO1005 (+120): running = +70  → break-even (≥ 0); 70 ≥ 50 → also decisive → switch
        PO1006 (+100): running = +170 → coverage already reached, leave untouched
      switch_plan = ["PO1005"]; remaining_pos = ["PO1004", "PO1006"]
    """
    base = base or date.today()
    po_date = {po: _day(base, d) for po, d in PO_ARRIVAL_DAYS.items()}
    so_date = _day(base, SO_DUE_DAYS)
    cur = conn.cursor()

    # ── Material master ───────────────────────────────────────────────────────
    materials = [
        ("MAT001", "Server CPU Module", "PC", "FERT"),
        ("MAT002", "Memory 32GB",       "PC", "FERT"),
        ("MAT003", "NVMe SSD 1TB",      "PC", "FERT"),
        ("MAT004", "Graphics Card 8GB", "PC", "FERT"),  # break-even test case
    ]
    cur.executemany(
        "INSERT OR REPLACE INTO material_master (matnr, maktx, meins, mtart) "
        "VALUES (?, ?, ?, ?)",
        materials,
    )

    # ── Current stock — plant TW01 (Taiwan site 1), warehouse WH01 ───────────
    stocks = [
        ("MAT001", "TW01", "WH01", 150),
        ("MAT002", "TW01", "WH01", 300),
        ("MAT003", "TW01", "WH01",  80),
        ("MAT004", "TW01", "WH01",  50),  # stock < committed → available_now = −150
    ]
    cur.executemany(
        "INSERT OR REPLACE INTO stock (matnr, werks, lgort, labst) "
        "VALUES (?, ?, ?, ?)",
        stocks,
    )

    # ── Purchase orders — vendor deliveries scheduled in the future ───────────
    pos = [
        ("PO1001", "MAT001", "TW01", 100, po_date["PO1001"], "SEA"),
        ("PO1002", "MAT002", "TW01", 200, po_date["PO1002"], "SEA"),
        ("PO1003", "MAT003", "TW01", 150, po_date["PO1003"], "SEA"),
        # MAT004 — break-even scenario (running starts at −150):
        ("PO1004", "MAT004", "TW01", 100, po_date["PO1004"], "SEA"),  # running → −50  (SO-absorbed, skip)
        ("PO1005", "MAT004", "TW01", 120, po_date["PO1005"], "SEA"),  # running → +70  (break-even + decisive)
        ("PO1006", "MAT004", "TW01", 100, po_date["PO1006"], "SEA"),  # running → +170 (post-coverage, untouched)
    ]
    cur.executemany(
        "INSERT OR REPLACE INTO purchase_orders "
        "(po_number, matnr, werks, menge, eindt, logistic_method) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        pos,
    )

    # ── Sales orders — existing commitments to customers ─────────────────────
    sos = [
        ("SO2001", "MAT001", "TW01", 120, so_date, "OPEN"),
        ("SO2002", "MAT002", "TW01", 250, so_date, "OPEN"),
        ("SO2003", "MAT003", "TW01",  80, so_date, "OPEN"),
        # MAT004 — two SOs totalling 200, far exceeding current stock of 50
        ("SO2004", "MAT004", "TW01", 100, so_date, "OPEN"),
        ("SO2005", "MAT004", "TW01", 100, so_date, "OPEN"),
    ]
    cur.executemany(
        "INSERT OR REPLACE INTO sales_orders "
        "(so_number, matnr, werks, kwmeng, edatu, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        sos,
    )

    conn.commit()


def main() -> None:
    # Workflow runs waiting for approval refer to the old data, so clear them
    # too. Done first: if the API server still holds the file open, stop here
    # before anything has been deleted.
    try:
        for suffix in ("", "-wal", "-shm"):
            path = CHECKPOINT_DB_PATH + suffix
            if os.path.exists(path):
                os.remove(path)
                if not suffix:
                    print(f"[setup] Removed pending workflow runs: {CHECKPOINT_DB_PATH}")
    except PermissionError:
        raise SystemExit(
            f"[setup] {CHECKPOINT_DB_PATH} is in use. Stop the API server, then run this again."
        )

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"[setup] Removed old database: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    seed_data(conn, date.today())
    conn.close()

    base = date.today()
    print(f"[setup] Database created  : {DB_PATH}")
    print("[setup] Tables populated  : material_master, stock, purchase_orders, sales_orders")
    print("[setup] Table created     : approval_log (empty)")
    print(f"[setup] Dates are relative to today ({base.isoformat()})")
    print()
    print("  Material | Stock | SO Committed | Net Avail | POs (all SEA)")
    print("  ---------+-------+--------------+-----------+------------------------------")
    print(f"  MAT001   |  150  |     120      |     30    | PO1001: {_day(base, 40)} +100")
    print(f"  MAT002   |  300  |     250      |     50    | PO1002: {_day(base, 45)} +200")
    print(f"  MAT003   |   80  |      80      |      0    | PO1003: {_day(base, 50)} +150")
    print(f"  MAT004   |   50  |     200      |   -150    | PO1004: {_day(base, 35)} +100")
    print(f"           |       |              |           | PO1005: {_day(base, 45)} +120")
    print(f"           |       |              |           | PO1006: {_day(base, 55)} +100")
    print()
    print("  Request bodies for the README test scenarios (POST /atp-check):")
    for name, matnr, qty, days in TEST_REQUESTS:
        body = {"matnr": matnr, "werks": "TW01", "requested_qty": qty,
                "requested_date": _day(base, days)}
        print(f"  {name:<36} {json.dumps(body)}")
    print()
    print("[setup] Done.")


if __name__ == "__main__":
    main()
