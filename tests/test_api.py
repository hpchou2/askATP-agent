"""
tests/test_api.py — POST /atp-check and POST /approve/{thread_id}, with the
real supervisor graph driven by a scripted LLM (no API key needed).
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import api
from tests.conftest import after_approval_script, air_escalation_script, day, route, call, say

TEST5 = {"matnr": "MAT004", "werks": "TW01", "requested_qty": 50, "requested_date": day(10)}


@pytest.fixture
def client(make_graph, monkeypatch):
    """Returns a function that installs a graph with the given script and gives a test client."""
    def _client(script: list) -> TestClient:
        monkeypatch.setattr(api, "_graph", make_graph(script))
        return TestClient(api.app)   # not used as a context manager, so lifespan doesn't run
    return _client


def _method(db, po_number: str) -> str:
    conn = sqlite3.connect(db)
    (method,) = conn.execute(
        "SELECT logistic_method FROM purchase_orders WHERE po_number = ?", (po_number,)
    ).fetchone()
    conn.close()
    return method


def test_on_time_order_completes_in_one_call(db, client):
    c = client([
        route("sap_agent"),
        call("calculate_atp", matnr="MAT001", werks="TW01", requested_qty=20, requested_date=day(10)),
        say("Fulfillable from stock."),
        route("FINISH", "Order confirmed from current stock."),
    ])
    r = c.post("/atp-check", json={**TEST5, "matnr": "MAT001", "requested_qty": 20})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "COMPLETED"
    assert body["answer"] == "Order confirmed from current stock."
    assert body["proposal"] is None


def test_air_switch_waits_for_approval_then_executes(db, client):
    c = client(air_escalation_script("MAT004", 50, day(10))
               + after_approval_script("PO1005 switched to AIR, approved by angela."))

    r = c.post("/atp-check", json=TEST5)
    body = r.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert [p["po_number"] for p in body["proposal"]["proposed_pos"]] == ["PO1005"]
    assert _method(db, "PO1005") == "SEA"

    r = c.post(f"/approve/{body['thread_id']}", json={"approved": True, "approver": "angela"})
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "COMPLETED"
    assert body["answer"] == "PO1005 switched to AIR, approved by angela."
    assert _method(db, "PO1005") == "AIR"


def test_approving_twice_returns_409(db, client):
    c = client(air_escalation_script("MAT004", 50, day(10)) + after_approval_script("Done."))
    thread_id = c.post("/atp-check", json=TEST5).json()["thread_id"]
    c.post(f"/approve/{thread_id}", json={"approved": False, "approver": "angela"})

    r = c.post(f"/approve/{thread_id}", json={"approved": True, "approver": "angela"})
    assert r.status_code == 409


def test_unknown_thread_returns_404(db, client):
    c = client([])
    r = c.post("/approve/no-such-run", json={"approved": True, "approver": "angela"})
    assert r.status_code == 404


def test_approver_is_required(db, client):
    c = client([])
    r = c.post("/approve/any", json={"approved": True})
    assert r.status_code == 422
