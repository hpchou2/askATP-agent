"""
tests/test_approval.py — the planner approval step, run through the real
supervisor graph with a scripted LLM (no API key needed).

The graph must pause before any PO is changed, then execute only what the
planner approved, and record every decision in approval_log.
"""

import sqlite3

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from tests.conftest import after_approval_script, air_escalation_script, day

CONFIG = {"configurable": {"thread_id": "run-1"}}


def _methods(db, matnr: str) -> dict:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT po_number, logistic_method FROM purchase_orders WHERE matnr = ?", (matnr,)
    ).fetchall()
    conn.close()
    return dict(rows)


def _log(db) -> list:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT thread_id, po_numbers, decision, approver FROM approval_log"
    ).fetchall()
    conn.close()
    return rows


def _start_test5(make_graph):
    """Runs Test 5 (MAT004, 50 units) up to the approval pause."""
    graph = make_graph(
        air_escalation_script("MAT004", 50, day(10)) + after_approval_script("Done.")
    )
    graph.invoke({"messages": [HumanMessage(content="Test 5")]}, CONFIG)
    return graph


def test_pauses_before_changing_any_po(db, make_graph):
    graph = _start_test5(make_graph)

    (pending,) = graph.get_state(CONFIG).interrupts
    assert [p["po_number"] for p in pending.value["proposed_pos"]] == ["PO1005"]
    assert pending.value["remaining_pos"] == ["PO1004", "PO1006"]
    assert _methods(db, "MAT004") == {"PO1004": "SEA", "PO1005": "SEA", "PO1006": "SEA"}
    assert _log(db) == []


def test_approve_switches_the_proposed_po(db, make_graph):
    graph = _start_test5(make_graph)
    graph.invoke(Command(resume={
        "approved": True, "approver": "angela", "approved_pos": ["PO1005"],
    }), CONFIG)

    state = graph.get_state(CONFIG)
    assert not state.interrupts and not state.next          # run finished
    assert state.values["messages"][-1].content == "Done."
    assert _methods(db, "MAT004") == {"PO1004": "SEA", "PO1005": "AIR", "PO1006": "SEA"}
    assert _log(db) == [("run-1", "PO1005", "APPROVED", "angela")]


def test_reject_changes_nothing(db, make_graph):
    graph = _start_test5(make_graph)
    graph.invoke(Command(resume={
        "approved": False, "approver": "angela", "approved_pos": ["PO1005"],
    }), CONFIG)

    assert _methods(db, "MAT004") == {"PO1004": "SEA", "PO1005": "SEA", "PO1006": "SEA"}
    assert _log(db) == [("run-1", "PO1005", "REJECTED", "angela")]


def test_stale_proposal_is_not_executed(db, make_graph):
    graph = _start_test5(make_graph)

    # While the run is paused, PO1005 is cut to 10 units. Its units are now
    # all absorbed by existing orders (−150 + 100 + 10 = −40), so the plan
    # becomes PO1006 — not what the planner saw.
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE purchase_orders SET menge = 10 WHERE po_number = 'PO1005'")
    conn.close()

    graph.invoke(Command(resume={
        "approved": True, "approver": "angela", "approved_pos": ["PO1005"],
    }), CONFIG)

    assert _methods(db, "MAT004") == {"PO1004": "SEA", "PO1005": "SEA", "PO1006": "SEA"}
    assert _log(db) == [("run-1", "PO1006", "STALE_PROPOSAL", "angela")]
