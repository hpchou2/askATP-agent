"""
agents/policy_agent.py — Policy Agent node for the LangGraph multi-agent system.

Wraps the policy file reader as a LangChain tool, then builds the ReAct agent.
All reasoning about whether a plant is permitted for air shipping is done by
the LLM — the tool only provides u with ability to read the raw policy document.
"""

import logging

from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

logger = logging.getLogger(__name__)

from tools.policy_tools import read_air_shipping_policy as _read_air_shipping_policy


# ─── Tool wrapper ─────────────────────────────────────────────────────────────

@tool
def read_air_shipping_policy() -> str:
    """
    Retrieve the full corporate air shipping policy document.

    Returns the raw markdown content listing each plant's authorization status
    for switching purchase order logistics from SEA to AIR freight, including
    any conditions or restrictions that apply.

    Call this tool whenever the Supervisor asks you to check whether a specific
    plant is permitted to use air shipping.
    """
    return _read_air_shipping_policy()


# ─── System prompt ────────────────────────────────────────────────────────────

POLICY_SYSTEM_PROMPT = """You are the Policy Agent, responsible for checking \
corporate logistics policy and advising the Supervisor on air shipping authorization.

You have one tool:

1. read_air_shipping_policy()
   - Call this to retrieve the full corporate air shipping policy document.
   - Read it carefully to find the policy entry for the specific plant the \
Supervisor has asked about.

After reading the policy, return a clear verdict to the Supervisor containing:
  - PERMITTED or NOT PERMITTED (stated explicitly)
  - The exact plant code and name
  - The relevant policy excerpt that supports your decision
  - Any conditions that apply (e.g. minimum quantity thresholds, date restrictions, \
escalation paths)

Be precise — the Supervisor will use your verdict to decide whether to instruct \
the SAP Agent to execute a logistics method change. Do not make assumptions beyond \
what the policy document states."""


# ─── Agent factory ────────────────────────────────────────────────────────────

def create_policy_agent(llm):
    """Returns a compiled ReAct agent with the policy read tool bound."""
    return create_react_agent(
        model=llm,
        tools=[read_air_shipping_policy],
        prompt=POLICY_SYSTEM_PROMPT,
    )
