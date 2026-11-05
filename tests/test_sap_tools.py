"""
tests/test_sap_tools.py — the five README scenarios, checked against the
deterministic SAP tools (calculate_atp and po_logistic_method_change).
"""

import sqlite3

import pytest

from tools.sap_tools import calculate_atp, po_logistic_method_change
from tests.conftest import day


def _method(db, po_number: str) -> str:
    conn = sqlite3.connect(db)
    (method,) = conn.execute(
        "SELECT logistic_method FROM purchase_orders WHERE po_number = ?", (po_number,)
    ).fetchone()
    conn.close()
    return method


# ─── calculate_atp ────────────────────────────────────────────────────────────

def test1_fulfillable_from_current_stock(db):
    r = calculate_atp("MAT001", "TW01", 20, day(10))
    assert r["available_now"] == 30
    assert r["fulfillable"] and r["on_time"]
    assert r["earliest_date"] == day(0)
    assert r["pos_checked"] == []


def test2_fulfillable_via_incoming_po_on_time(db):
    r = calculate_atp("MAT002", "TW01", 100, day(45))
    assert r["available_now"] == 50
    assert r["fulfillable"] and r["on_time"]
    assert r["earliest_date"] == day(45)
    assert r["days_late"] == 0


def test3_fulfillable_but_sea_too_late(db):
    r = calculate_atp("MAT001", "TW01", 100, day(10))
    assert r["fulfillable"] and not r["on_time"]
    assert r["earliest_date"] == day(40)
    assert r["days_late"] == 30


def test4_quantity_shortage(db):
    r = calculate_atp("MAT003", "TW01", 200, day(50))
    assert not r["fulfillable"]
    assert r["max_available"] == 150
    assert r["max_available_date"] == day(50)


def test5_negative_free_stock_still_fulfillable_late(db):
    r = calculate_atp("MAT004", "TW01", 50, day(10))
    assert r["available_now"] == -150
    assert r["fulfillable"] and not r["on_time"]
    assert r["earliest_date"] == day(45)
    assert r["days_late"] == 35


# ─── po_logistic_method_change ────────────────────────────────────────────────

def test3_switches_the_single_decisive_po(db):
    r = po_logistic_method_change("MAT001", "TW01", 100, "AIR")
    assert [p["po_number"] for p in r["updated_pos"]] == ["PO1001"]
    assert r["coverage_qty"] == 130
    assert _method(db, "PO1001") == "AIR"


def test5_skips_po_absorbed_by_existing_orders(db):
    r = po_logistic_method_change("MAT004", "TW01", 50, "AIR")
    assert [p["po_number"] for p in r["updated_pos"]] == ["PO1005"]
    assert r["remaining_pos"] == ["PO1004", "PO1006"]
    assert _method(db, "PO1004") == "SEA"
    assert _method(db, "PO1005") == "AIR"
    assert _method(db, "PO1006") == "SEA"


def test4_shortage_switches_nothing(db):
    r = po_logistic_method_change("MAT003", "TW01", 200, "AIR")
    assert r["updated_count"] == 0
    assert r["updated_pos"] == []
    assert r["remaining_pos"] == ["PO1003"]
    assert _method(db, "PO1003") == "SEA"


def test_invalid_method_is_rejected(db):
    with pytest.raises(ValueError):
        po_logistic_method_change("MAT001", "TW01", 100, "TRUCK")
