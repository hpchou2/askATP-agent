"""
tests/test_sap_tools.py — the five README scenarios, checked against the
deterministic SAP tools (calculate_atp, plan_po_logistic_change,
po_logistic_method_change), plus the approval log.
"""

import sqlite3

import pytest

from tools.sap_tools import (
    calculate_atp,
    log_approval,
    plan_po_logistic_change,
    po_logistic_method_change,
)
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


# ─── plan_po_logistic_change ──────────────────────────────────────────────────

def test5_plan_proposes_without_writing(db):
    r = plan_po_logistic_change("MAT004", "TW01", 50, "AIR")
    assert r["covered"]
    assert [p["po_number"] for p in r["switch_pos"]] == ["PO1005"]
    assert r["remaining_pos"] == ["PO1004", "PO1006"]
    assert _method(db, "PO1005") == "SEA"   # nothing written


def test4_plan_for_shortage_proposes_nothing(db):
    r = plan_po_logistic_change("MAT003", "TW01", 200, "AIR")
    assert not r["covered"]
    assert r["switch_pos"] == []
    assert r["remaining_pos"] == ["PO1003"]


# ─── log_approval ─────────────────────────────────────────────────────────────

def test_log_approval_appends_a_row(db):
    log_approval("t1", "MAT004", "TW01", 50, "AIR", ["PO1005"], "APPROVED", "angela", "ok")
    log_approval("t2", "MAT001", "TW01", 100, "AIR", ["PO1001"], "REJECTED", "bob", None)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT thread_id, po_numbers, decision, approver FROM approval_log ORDER BY log_id"
    ).fetchall()
    conn.close()
    assert rows == [("t1", "PO1005", "APPROVED", "angela"), ("t2", "PO1001", "REJECTED", "bob")]


def test_approved_switch_and_log_are_one_transaction(db):
    # If the approval can't be logged, the PO must not be switched either.
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE approval_log")
    conn.close()
    with pytest.raises(sqlite3.OperationalError):
        po_logistic_method_change("MAT004", "TW01", 50, "AIR",
                                  approval={"thread_id": "t1", "approver": "angela"})
    assert _method(db, "PO1005") == "SEA"
