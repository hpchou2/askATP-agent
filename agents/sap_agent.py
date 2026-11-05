"""
agents/sap_agent.py — SAP Agent node for the LangGraph multi-agent system.

Wraps three SAP tools with the @tool decorator so LangChain can pass their
schemas to the LLM, then builds the ReAct agent via create_react_agent.
"""

import logging

from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

logger = logging.getLogger(__name__)

from tools.sap_tools import (
    sap_extract as _sap_extract,
    calculate_atp as _calculate_atp,
    po_logistic_method_change as _po_logistic_method_change,
)


# ─── Tool wrappers ────────────────────────────────────────────────────────────
# Docstrings here become the tool descriptions the LLM reads when deciding
# which tool to call and with which arguments.

@tool
def sap_extract(matnr: str, werks: str) -> dict:
    """
    Extract all relevant SAP data for a material/plant in one call.

    Returns current unrestricted stock, all open sales orders (committed demand),
    and all open purchase orders (incoming supply) including their current
    logistic_method (SEA or AIR).

    Always call this first before any other SAP tool to get a complete
    snapshot of the supply/demand position.

    Args:
        matnr: Material number, e.g. 'MAT001'
        werks: Plant code, e.g. 'TW01'
    """
    return _sap_extract(matnr, werks)


@tool
def calculate_atp(
    matnr: str,
    werks: str,
    requested_qty: float,
    requested_date: str,
) -> dict:
    """
    Calculate Available-to-Promise (ATP) for a material/plant/quantity/date.

    Determines the earliest date the requested quantity can be fulfilled, and
    whether that date meets the customer's required delivery date.

    Key result fields:
    - fulfillable    : True if the quantity can ever be covered (regardless of timing)
    - on_time        : True if earliest_date <= requested_date (SEA is fast enough)
    - days_late      : days early (negative) or late (positive) vs requested_date
    - available_now  : stock minus committed SOs — may be negative when committed
                       demand exceeds current stock; report this to the Supervisor as
                       it explains why certain POs may be skipped during switching
    - pos_checked    : informational list of every PO walked, with its cumulative
                       available quantity after each step — useful context for the
                       Supervisor, but PO switching is handled automatically by
                       po_logistic_method_change and does not need to be done manually

    Routing guide for the Supervisor:
    - fulfillable=True  + on_time=True   → confirm order, no escalation needed
    - fulfillable=True  + on_time=False  → escalate to Policy Agent; SEA is too slow
    - fulfillable=False                  → quantity shortage; AIR cannot resolve this

    Args:
        matnr:          Material number, e.g. 'MAT001'
        werks:          Plant code, e.g. 'TW01'
        requested_qty:  Quantity the customer requires
        requested_date: Customer's required delivery date as 'YYYY-MM-DD';
                        used to compute on_time and days_late
    """
    return _calculate_atp(matnr, werks, requested_qty, requested_date)


@tool
def po_logistic_method_change(
    matnr: str,
    werks: str,
    requested_qty: float,
    method: str,
) -> dict:
    """
    Switch the minimum set of POs (from the break-even PO to the decisive PO)
    to the specified logistic method (AIR or SEA).

    Internally calls sap_extract to get current stock, committed sales orders,
    and open POs, then applies the break-even-to-decisive selection rule:

    1. Walk POs in earliest-delivery-date-first order accumulating:
           running = stock - committed_SOs + PO₁ + … + POᵢ
    2. Skip POs while running < 0  — these are entirely absorbed by existing SO
       commitments and must not be touched.
    3. The first PO that brings running >= 0 is the break-even PO; switch it.
       Continue switching subsequent POs until running >= requested_qty.

    If stock - committed >= 0 from the start (no SO deficit), switching starts
    with the very first PO.

    All-or-nothing: if the POs cannot reach requested_qty, no PO is switched
    (updated_count = 0) and the shortage must be reported instead.

    Call this ONLY after the Supervisor has confirmed that air shipping is
    permitted for the plant.

    Args:
        matnr:         Material number, e.g. 'MAT001'
        werks:         Plant code, e.g. 'TW01'
        requested_qty: The customer's quantity that must be covered
        method:        Target shipping method — 'AIR' or 'SEA'
    """
    return _po_logistic_method_change(matnr, werks, requested_qty, method)


# ─── System prompt ────────────────────────────────────────────────────────────

SAP_SYSTEM_PROMPT = """You are the SAP Agent, responsible for querying SAP supply \
chain data, calculating whether a request from the user can be fulfilled using the queried results, \
and executing logistics changes on purchase orders.

You have three tools:

1. sap_extract(matnr, werks)
   - Always call this FIRST to get a full snapshot: current unrestricted stock, \
all open sales orders (committed demand), and all open purchase orders (incoming \
supply) with their current logistic_method (SEA or AIR).

2. calculate_atp(matnr, werks, requested_qty, requested_date)
   - Call this to determine whether and when the order can be fulfilled.
   - Returns fulfillable, earliest_date, on_time, and days_late.
     - on_time=True  → SEA is fast enough; report this to the Supervisor.
     - on_time=False → SEA is too slow; report earliest_date and days_late so \
the Supervisor can escalate to the Policy Agent.
     - fulfillable=False → quantity shortage even with all POs; report max_available \
and max_available_date to the Supervisor.
   - The pos_checked list is informational context showing the full PO walkthrough; \
you do not need to act on it directly.

3. po_logistic_method_change(matnr, werks, requested_qty, method)
   - Call this ONLY when the Supervisor explicitly instructs you to switch POs \
to AIR shipping after the Policy Agent has confirmed it is permitted.
   - This tool automatically identifies the correct set of POs using the \
break-even-to-decisive rule: it skips POs whose units are entirely consumed by \
committed sales orders, then switches from the break-even PO (first where cumulative \
supply covers all SO commitments) through to the decisive PO (first where cumulative \
supply also covers requested_qty).
   - After calling it, report back to the Supervisor:
     * updated_pos   — which POs were switched, their delivery dates, and the \
running coverage quantity at each step
     * remaining_pos — which POs were NOT switched; distinguish between POs \
skipped because they were entirely absorbed by committed SO demand (running still \
negative) and POs left untouched because coverage was already reached
     * message       — the human-readable summary from the result

Always include matnr, werks, quantities, and dates in your responses so the \
Supervisor has full context for routing decisions."""


# ─── Agent factory ────────────────────────────────────────────────────────────

def create_sap_agent(llm):
    """Returns a compiled ReAct agent with the three SAP tools bound."""
    return create_react_agent(
        model=llm,
        tools=[sap_extract, calculate_atp, po_logistic_method_change],
        prompt=SAP_SYSTEM_PROMPT,
    )
