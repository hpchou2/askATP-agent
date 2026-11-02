"""
api.py — FastAPI entrypoint for the ATP Fulfillment Multi-Agent System.

Endpoints:
    GET  /health       — liveness check
    POST /atp-check    — run the full LangGraph supervisor workflow
"""

import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from langchain_core.messages import HumanMessage
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
    _graph = build_graph()
    logger.info("Graph ready — ATP Fulfillment Agent is online")
    yield
    _graph = None


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
    answer: str = Field(description="Final supervisor summary returned to the user")
    trace: list[MessageTrace] = Field(description="Full agent message trace for observability")


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

    try:
        result = _graph.invoke(
            {"messages": [HumanMessage(content=user_message)]}
        )
    except Exception as exc:
        logger.error("[POST /atp-check] failed — %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    answer = _extract_final_answer(result["messages"])
    elapsed = time.perf_counter() - t0
    logger.info("[POST /atp-check] completed in %.2fs — answer: %.120s%s",
                elapsed, answer, "…" if len(answer) > 120 else "")

    return ATPCheckResponse(
        answer=answer,
        trace=_build_trace(result["messages"]),
    )
