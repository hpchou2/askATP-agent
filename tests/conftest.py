"""
tests/conftest.py — shared fixtures.

Each test gets a fresh SQLite database seeded relative to today, so the
deterministic tools can be tested without an API key or the LLM. Workflow
tests use a scripted chat model in place of Claude.
"""

import os
import sqlite3
import sys
import uuid
from datetime import date, timedelta

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import setup_db
from tools import sap_tools


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Seeds a temporary database and points the SAP tools at it."""
    path = tmp_path / "sap_mock.db"
    conn = sqlite3.connect(path)
    setup_db.create_tables(conn)
    setup_db.seed_data(conn, date.today())
    conn.close()
    monkeypatch.setattr(sap_tools, "DB_PATH", str(path))
    return path


def day(n: int) -> str:
    """Date n days from today, as YYYY-MM-DD."""
    return (date.today() + timedelta(days=n)).isoformat()


# ─── Scripted LLM ─────────────────────────────────────────────────────────────
# Lets the full LangGraph workflow run without an API key: every LLM call
# (supervisor, SAP Agent, Policy Agent) takes the next message from a script.

class ScriptedChatModel(BaseChatModel):
    responses: list

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])

    def bind_tools(self, tools, **kwargs):
        return self


def route(next_node: str, message: str = "") -> AIMessage:
    """A supervisor decision (structured output arrives as a tool call)."""
    return call("SupervisorDecision", next=next_node, message=message or f"go to {next_node}")


def call(tool_name: str, **args) -> AIMessage:
    """An agent turn that calls one tool."""
    return AIMessage(content="", tool_calls=[
        {"name": tool_name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}"}
    ])


def say(text: str) -> AIMessage:
    """An agent turn that replies with text and hands control back."""
    return AIMessage(content=text)


def air_escalation_script(matnr: str, qty: float, requested_date: str) -> list:
    """LLM turns for the late-SEA → policy permitted → propose AIR path, up to the pause."""
    return [
        route("sap_agent", f"Run calculate_atp for {matnr} at TW01, qty {qty}"),
        call("calculate_atp", matnr=matnr, werks="TW01",
             requested_qty=qty, requested_date=requested_date),
        say("Fulfillable but SEA is late."),
        route("policy_agent", "Is air permitted for TW01?"),
        call("read_air_shipping_policy"),
        say("PERMITTED for TW01."),
        route("sap_agent", f"Switch POs to AIR for {matnr} at TW01, qty {qty}"),
        call("po_logistic_method_change", matnr=matnr, werks="TW01",
             requested_qty=qty, method="AIR"),
    ]


def after_approval_script(final_answer: str) -> list:
    """LLM turns after the planner decides: SAP Agent reports, supervisor finishes."""
    return [say("Reported the result."), route("FINISH", final_answer)]


@pytest.fixture
def make_graph():
    """Builds the real supervisor graph with a scripted LLM and an in-memory checkpointer."""
    from agents.supervisor import build_graph

    def _make(script: list):
        return build_graph(checkpointer=InMemorySaver(), llm=ScriptedChatModel(responses=script))
    return _make
