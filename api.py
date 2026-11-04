"""
api.py — FastAPI entrypoint for the ATP Fulfillment Multi-Agent System.

Endpoints:
    GET  /health                 — liveness check
    POST /atp-check              — run the full LangGraph supervisor workflow
    POST /approve/{thread_id}    — approve or reject a proposed PO switch and
                                   resume the paused workflow
"""

import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── Graph singleton ──────────────────────────────────────────────────────────
# Compiled once at startup; reused across all requests.

# Workflow checkpoints, so runs paused for planner approval survive a server
# restart. setup_db.py deletes this file when it resets the mock SAP data.
CHECKPOINT_DB_PATH = os.path.join(os.path.dirname(__file__), "checkpoints.db")

_graph = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. "
            "Create a .env file from .env.example and fill in your Anthropic API key."
        )
    from agents.supervisor import build_graph
    logger.info("Building LangGraph multi-agent graph…")
    # Sync endpoints run in a thread pool, so the connection is shared across
    # threads; SqliteSaver serialises access with its own lock.
    conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
    _graph = build_graph(checkpointer=SqliteSaver(conn))
    logger.info("Graph ready — ATP Fulfillment Agent is online")
    yield
    _graph = None
    conn.close()


app = FastAPI(
    title="ATP Fulfillment Agent API",
    description="LangGraph multi-agent system for SAP ATP checks and logistics escalation.",
    version="1.0.0",
    lifespan=lifespan,
)


# ─── Request / Response schemas ───────────────────────────────────────────────

class ATPCheckRequest(BaseModel):
    matnr: str = Field(..., description="Material number, e.g. 'MAT001'")
    werks: str = Field("TW01", description="Plant code, e.g. 'TW01'")
    requested_qty: float = Field(..., gt=0, description="Quantity the customer requires")
    requested_date: str = Field(
        default_factory=lambda: date.today().isoformat(),
        description="Customer's desired delivery date as YYYY-MM-DD",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "matnr": "MAT001",
                "werks": "TW01",
                "requested_qty": 100,
                "requested_date": "2026-07-01",
            }
        }
    }


class MessageTrace(BaseModel):
    role: str = Field(description="Message role: human / ai / tool")
    name: str | None = Field(None, description="Named sender, e.g. 'supervisor'")
    content: str = Field(description="Text content of the message")


class ATPCheckResponse(BaseModel):
    status: Literal["COMPLETED", "PENDING_APPROVAL"] = Field(
        description="COMPLETED, or PENDING_APPROVAL when a PO switch awaits a planner decision"
    )
    thread_id: str = Field(description="Workflow run ID; pass it to POST /approve/{thread_id}")
    answer: str = Field(description="Final supervisor summary, or the approval question if pending")
    proposal: dict | None = Field(
        None, description="The proposed PO switch awaiting approval (only when PENDING_APPROVAL)"
    )
    trace: list[MessageTrace] = Field(description="Full agent message trace for observability")


class ApproveRequest(BaseModel):
    approved: bool = Field(..., description="true to execute the proposed PO switch, false to reject it")
    approver: str = Field(..., min_length=1, description="Name or ID of the planner deciding")
    comment: str | None = Field(None, description="Optional note, stored in the approval log")

    model_config = {
        "json_schema_extra": {
            "example": {"approved": True, "approver": "angela.chou", "comment": "Customer is strategic"}
        }
    }


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_final_answer(messages: list) -> str:
    """Return the last message posted by the supervisor (name='supervisor')."""
    for msg in reversed(messages):
        if getattr(msg, "name", None) == "supervisor":
            content = msg.content
            return content if isinstance(content, str) else str(content)
    # Fallback: return the last message's content
    last = messages[-1]
    content = last.content
    return content if isinstance(content, str) else str(content)


def _build_trace(messages: list) -> list[MessageTrace]:
    """Convert LangChain message objects to serialisable trace entries."""
    trace = []
    for msg in messages:
        role = type(msg).__name__.lower().replace("message", "")
        raw = msg.content
        if isinstance(raw, str):
            content = raw
        elif isinstance(raw, list):
            # Tool-use blocks arrive as a list of dicts; flatten to readable text
            parts = []
            for block in raw:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        parts.append(
                            f"[tool_use] {block.get('name')}({block.get('input', {})})"
                        )
                    else:
                        parts.append(str(block))
                else:
                    parts.append(str(block))
            content = "\n".join(parts)
        else:
            content = str(raw)

        trace.append(MessageTrace(
            role=role,
            name=getattr(msg, "name", None),
            content=content,
        ))
    return trace


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health", summary="Liveness check")
def health():
    return {"message": "service is running"}


@app.post(
    "/atp-check",
    response_model=ATPCheckResponse,
    summary="Run ATP fulfillment check",
    description=(
        "Submits a material/plant/quantity/date request to the LangGraph "
        "multi-agent supervisor. The supervisor orchestrates the SAP Agent "
        "(ATP check + optional logistics change) and the Policy Agent "
        "(air shipping authorization) and returns a final fulfillment verdict."
    ),
)
def atp_check(req: ATPCheckRequest):
    if _graph is None:
        raise HTTPException(status_code=503, detail="Agent graph is not initialised.")

    logger.info(
        "[POST /atp-check] matnr=%s  werks=%s  qty=%.0f  date=%s",
        req.matnr, req.werks, req.requested_qty, req.requested_date,
    )
    t0 = time.perf_counter()

    user_message = (
        f"Please check ATP and handle fulfillment for the following order:\n"
        f"- Material (matnr)   : {req.matnr}\n"
        f"- Plant (werks)      : {req.werks}\n"
        f"- Requested quantity : {req.requested_qty}\n"
        f"- Requested date     : {req.requested_date}"
    )

    return _run_graph(
        {"messages": [HumanMessage(content=user_message)]},
        thread_id=str(uuid.uuid4()),
        label="POST /atp-check",
        t0=t0,
    )


@app.post(
    "/approve/{thread_id}",
    response_model=ATPCheckResponse,
    summary="Approve or reject a proposed PO switch",
    description=(
        "Resumes a workflow run that POST /atp-check returned as PENDING_APPROVAL. "
        "If approved, the proposed POs are switched; if rejected, nothing changes. "
        "Either way the decision is written to the approval_log table and the "
        "supervisor returns the final answer."
    ),
)
def approve(thread_id: str, req: ApproveRequest):
    if _graph is None:
        raise HTTPException(status_code=503, detail="Agent graph is not initialised.")

    snapshot = _graph.get_state({"configurable": {"thread_id": thread_id}})
    if not snapshot.values:
        raise HTTPException(status_code=404, detail=f"No workflow run with thread_id '{thread_id}'.")
    if not snapshot.interrupts:
        raise HTTPException(status_code=409, detail="This workflow run is not waiting for approval.")

    proposal = snapshot.interrupts[0].value
    logger.info(
        "[POST /approve] thread=%s  approved=%s  approver=%s",
        thread_id, req.approved, req.approver,
    )
    return _run_graph(
        Command(resume={
            "approved": req.approved,
            "approver": req.approver,
            "comment":  req.comment,
            # The POs the planner actually saw, so the tool can detect a stale proposal
            "approved_pos": [p["po_number"] for p in proposal["proposed_pos"]],
        }),
        thread_id=thread_id,
        label="POST /approve",
        t0=time.perf_counter(),
    )


def _run_graph(graph_input, thread_id: str, label: str, t0: float) -> ATPCheckResponse:
    """
    Runs (or resumes) the graph for one workflow run, then reports either the
    final answer or the proposal the run is paused on.
    """
    config = {"configurable": {"thread_id": thread_id}}
    try:
        _graph.invoke(graph_input, config)
    except Exception as exc:
        logger.error("[%s] failed — %s", label, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    snapshot = _graph.get_state(config)
    messages = snapshot.values.get("messages", [])
    elapsed = time.perf_counter() - t0

    if snapshot.interrupts:
        proposal = snapshot.interrupts[0].value
        logger.info("[%s] paused for approval after %.2fs — thread=%s — %s",
                    label, elapsed, thread_id, proposal["message"])
        return ATPCheckResponse(
            status="PENDING_APPROVAL",
            thread_id=thread_id,
            answer=proposal["message"],
            proposal=proposal,
            trace=_build_trace(messages),
        )

    answer = _extract_final_answer(messages)
    logger.info("[%s] completed in %.2fs — answer: %.120s%s",
                label, elapsed, answer, "…" if len(answer) > 120 else "")
    return ATPCheckResponse(
        status="COMPLETED",
        thread_id=thread_id,
        answer=answer,
        trace=_build_trace(messages),
    )
