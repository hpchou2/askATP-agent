"""
agents/supervisor.py — LangGraph multi-agent graph with supervisor routing.

Graph topology:
    START → supervisor → sap_agent  → supervisor (loop)
                       → policy_agent → supervisor (loop)
                       → END

The supervisor inspects the full message history after each subagent turn and
decides the next routing step via structured output (SupervisorDecision).
"""

import logging
import os
from typing import Annotated, Literal, TypedDict

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from agents.sap_agent import create_sap_agent
from agents.policy_agent import create_policy_agent

load_dotenv()


# ─── Shared state ─────────────────────────────────────────────────────────────

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    next: str  # routing target set by the supervisor each turn


# ─── Supervisor structured output ─────────────────────────────────────────────

class SupervisorDecision(BaseModel):
    next: Literal["sap_agent", "policy_agent", "FINISH"] = Field(
        description="Which node to route to next."
    )
    message: str = Field(
        description=(
            "Instruction for the next agent (if next is sap_agent or policy_agent), "
            "or the final answer summary for the user (if next is FINISH)."
        )
    )


# ─── Supervisor system prompt ─────────────────────────────────────────────────

SUPERVISOR_SYSTEM_PROMPT = """You are the Supervisor Agent orchestrating an ATP \
(Available-to-Promise) fulfillment workflow. You coordinate two sub-agents:
  - sap_agent:    queries SAP data, calculates whether user request can be fulfilled, \ 
                and executes purchase order logistics changes
  - policy_agent: checks corporate policy for air shipping authorization

════════════════════════════════════════════════════════════
WORKFLOW RULES
════════════════════════════════════════════════════════════

STEP 1 — Initial ATP check
  Route to: sap_agent
  Message:  Instruct it to run sap_extract then calculate_atp for the user's
            material (matnr), plant (werks), requested quantity, and date.

STEP 2 — Evaluate ATP result
  Check BOTH fulfillable AND on_time from the calculate_atp result.

  If fulfillable=True AND on_time=True:
    → Route to FINISH
    → Message: Confirm the order is fulfillable by earliest_date via sea shipping.
               Include matnr, werks, quantity, and fulfillment date.

  If fulfillable=True AND on_time=False:
    → Route to policy_agent
    → Message: State matnr, werks, requested_qty, requested_date, earliest SEA date
               (earliest_date), and how many days late that would be (days_late).
               Ask whether air shipping is permitted for this plant.

  If fulfillable=False:
    → Route to FINISH
    → Message: Inform the user the order cannot be fulfilled even with all scheduled
               POs. State max_available quantity vs. requested_qty.

STEP 3 — Evaluate policy verdict
  If PERMITTED (or permitted with conditions the order satisfies):
    → Route to sap_agent
    → Message: Instruct it to call po_logistic_method_change(matnr, werks,
               requested_qty, "AIR") to switch the minimum necessary POs to AIR.

  If NOT PERMITTED:
    → Route to FINISH
    → Message: Explain the shortage cannot be resolved — air shipping is not
               authorized for this plant — and provide the policy reason.

STEP 4 — After logistics change
  → Route to FINISH
  → Message: Summarize the outcome for the user:
             - Which POs were switched to AIR and their expected goods receipt
               dates (eindt — the vendor delivery date; note these still reflect
               the original SEA schedule until the actual AIR shipment is booked)
             - If any POs were left on SEA because they were entirely absorbed by
               committed SO demand (available_now was negative), explain this so
               the user understands why those POs were not switched
             - Coverage quantity achieved vs. requested quantity

════════════════════════════════════════════════════════════
IMPORTANT
════════════════════════════════════════════════════════════
- Always include matnr, werks, and requested_qty explicitly in every instruction
  you write to a sub-agent so it has full context.
- Do not call any tools yourself — delegate all SAP queries and policy lookups
  to the appropriate sub-agent.
- Keep your FINISH messages concise and user-facing (no internal jargon)."""


# ─── Graph builder ────────────────────────────────────────────────────────────

def build_graph():
    """
    Compiles and returns the full LangGraph multi-agent graph.

    Requires ANTHROPIC_API_KEY set in the environment (or a .env file).
    """
    llm = ChatAnthropic(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        temperature=0,
    )

    # Sub-agents (each is itself a compiled ReAct graph)
    sap_agent = create_sap_agent(llm)
    policy_agent = create_policy_agent(llm)

    # Supervisor LLM forced to return a structured routing decision
    supervisor_llm = llm.with_structured_output(SupervisorDecision)

    # ── Node definitions ──────────────────────────────────────────────────────

    def supervisor_node(state: AgentState) -> dict:
        """
        Reads full message history, decides next routing step, and appends
        its instruction/summary as a new HumanMessage so subagents always
        see a clear, contextual task at the top of the queue.
        """
        messages = [SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT)] + state["messages"]
        if isinstance(messages[-1], AIMessage):
            messages = messages + [HumanMessage(content="Based on the above results, decide the next step.")]
        decision: SupervisorDecision = supervisor_llm.invoke(messages)

        logger.info(
            "[supervisor] → routing to: %s | message: %.120s%s",
            decision.next,
            decision.message,
            "…" if len(decision.message) > 120 else "",
        )

        return {
            "messages": [HumanMessage(content=decision.message, name="supervisor")],
            "next": decision.next,
        }

    def sap_agent_node(state: AgentState) -> dict:
        """Runs the SAP ReAct agent and merges its new messages into state."""
        logger.info("[sap_agent] invoked")
        result = sap_agent.invoke({"messages": state["messages"]})
        logger.info("[sap_agent] completed — %d message(s) added", len(result["messages"]))
        return {"messages": result["messages"]}

    def policy_agent_node(state: AgentState) -> dict:
        """Runs the Policy ReAct agent and merges its new messages into state."""
        logger.info("[policy_agent] invoked")
        result = policy_agent.invoke({"messages": state["messages"]})
        logger.info("[policy_agent] completed — %d message(s) added", len(result["messages"]))
        return {"messages": result["messages"]}

    # ── Graph wiring ──────────────────────────────────────────────────────────

    graph = StateGraph(AgentState)

    graph.add_node("supervisor",    supervisor_node)
    graph.add_node("sap_agent",     sap_agent_node)
    graph.add_node("policy_agent",  policy_agent_node)

    # Entry point always hits the supervisor first
    graph.add_edge(START, "supervisor")

    # Supervisor decides where to go next
    graph.add_conditional_edges(
        "supervisor",
        lambda state: state["next"],
        {
            "sap_agent":    "sap_agent",
            "policy_agent": "policy_agent",
            "FINISH":       END,
        },
    )

    # Sub-agents always return control to the supervisor
    graph.add_edge("sap_agent",    "supervisor")
    graph.add_edge("policy_agent", "supervisor")

    return graph.compile()
