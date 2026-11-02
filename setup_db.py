"""
setup_db.py — Create and seed the mock SAP database (sap_mock.db).

Run once before starting the agent:
    python setup_db.py
"""

import sqlite3
import os

# Place the database file in the same folder as this script
DB_PATH = os.path.join(os.path.dirname(__file__), "sap_mock.db")


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

    conn.commit()


def seed_data(conn: sqlite3.Connection) -> None:
    """
    Populates mock data for a small electronics warehouse.

    Inventory snapshot:
      Material | Description       | Stock | SO Committed | Net Avail | POs
      ---------+-------------------+-------+--------------+-----------+------------------------------------------
      MAT001   | Server CPU Module |  150  |     120      |     30    | PO1001: 2026-07-10 +100 SEA
      MAT002   | Memory 32GB       |  300  |     250      |     50    | PO1002: 2026-07-15 +200 SEA
      MAT003   | NVMe SSD 1TB      |   80  |      80      |      0    | PO1003: 2026-07-20 +150 SEA
      MAT004   | Graphics Card 8GB |   50  |     200      |   -150    | PO1004: 2026-07-05 +100 SEA
               |                   |       |   (SO2004    |           | PO1005: 2026-07-15 +120 SEA
               |                   |       |  + SO2005)   |           | PO1006: 2026-07-25 +100 SEA

    MAT004 is the break-even demonstration: available_now = 50 − 200 = −150.
      PO walk for requested_qty=50:
        PO1004 (+100): running = −50  → still absorbing SO demand, skip (do NOT switch)
        PO1005 (+120): running = +70  → break-even (≥ 0); 70 ≥ 50 → also decisive → switch
        PO1006 (+100): running = +170 → coverage already reached, leave untouched
      switch_plan = ["PO1005"]; remaining_pos = ["PO1004", "PO1006"]
    """
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
        ("PO1001", "MAT001", "TW01", 100, "2026-07-10", "SEA"),
        ("PO1002", "MAT002", "TW01", 200, "2026-07-15", "SEA"),
        ("PO1003", "MAT003", "TW01", 150, "2026-07-20", "SEA"),
        # MAT004 — break-even scenario (running starts at −150):
        ("PO1004", "MAT004", "TW01", 100, "2026-07-05", "SEA"),  # running → −50  (SO-absorbed, skip)
        ("PO1005", "MAT004", "TW01", 120, "2026-07-15", "SEA"),  # running → +70  (break-even + decisive)
        ("PO1006", "MAT004", "TW01", 100, "2026-07-25", "SEA"),  # running → +170 (post-coverage, untouched)
    ]
    cur.executemany(
        "INSERT OR REPLACE INTO purchase_orders "
        "(po_number, matnr, werks, menge, eindt, logistic_method) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        pos,
    )

    # ── Sales orders — existing commitments to customers ─────────────────────
    sos = [
        ("SO2001", "MAT001", "TW01", 120, "2026-06-30", "OPEN"),
        ("SO2002", "MAT002", "TW01", 250, "2026-06-30", "OPEN"),
        ("SO2003", "MAT003", "TW01",  80, "2026-06-30", "OPEN"),
        # MAT004 — two SOs totalling 200, far exceeding current stock of 50
        ("SO2004", "MAT004", "TW01", 100, "2026-06-30", "OPEN"),
        ("SO2005", "MAT004", "TW01", 100, "2026-06-30", "OPEN"),
    ]
    cur.executemany(
        "INSERT OR REPLACE INTO sales_orders "
        "(so_number, matnr, werks, kwmeng, edatu, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        sos,
    )

    conn.commit()


def main() -> None:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"[setup] Removed old database: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    create_tables(conn)
    seed_data(conn)
    conn.close()

    print(f"[setup] Database created  : {DB_PATH}")
    print("[setup] Tables populated  : material_master, stock, purchase_orders, sales_orders")
    print()
    print("  Material | Description       | Stock | SO Committed | Net Avail | POs")
    print("  ---------+-------------------+-------+--------------+-----------+--------------------------------------------")
    print("  MAT001   | Server CPU Module |  150  |     120      |     30    | PO1001: 2026-07-10 +100 SEA")
    print("  MAT002   | Memory 32GB       |  300  |     250      |     50    | PO1002: 2026-07-15 +200 SEA")
    print("  MAT003   | NVMe SSD 1TB      |   80  |      80      |      0    | PO1003: 2026-07-20 +150 SEA")
    print("  MAT004   | Graphics Card 8GB |   50  |     200      |   -150    | PO1004: 2026-07-05 +100 SEA (SO-absorbed)")
    print("           |                   |       |              |           | PO1005: 2026-07-15 +120 SEA (break-even)")
    print("           |                   |       |              |           | PO1006: 2026-07-25 +100 SEA (post-coverage)")
    print()
    print("[setup] Done.")


if __name__ == "__main__":
    main()
