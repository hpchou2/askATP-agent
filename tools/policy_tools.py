"""
tools/policy_tools.py — Policy-facing tools for the Policy Agent.

One tool: read_air_shipping_policy()
Reads the corporate air shipping policy document and returns it as a plain
string. All reasoning about whether a specific plant is permitted is left
entirely to the Policy Agent LLM.
"""

import logging
import os

logger = logging.getLogger(__name__)

POLICY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "policies", "air_shipping_policy.md"
)


def read_air_shipping_policy() -> str:
    """
    Returns the full content of the corporate air shipping policy document.

    The Policy Agent should call this tool when the Supervisor reports that a
    material at a specific plant cannot be fulfilled on time via sea shipment, and asks
    whether air shipping is permitted for that plant.

    The agent is responsible for reading the returned text and reasoning about
    whether the given plant qualifies — this function performs no filtering or
    interpretation.

    Returns
    -------
    str — raw markdown content of policies/air_shipping_policy.md
    """
    policy_file = os.path.normpath(POLICY_PATH)
    if not os.path.exists(policy_file):
        raise FileNotFoundError(
            f"Policy file not found: {policy_file}\n"
            "Ensure 'policies/air_shipping_policy.md' exists in the project root."
        )
    with open(policy_file, "r", encoding="utf-8") as f:
        content = f.read()
    logger.info(
        "[read_air_shipping_policy] policy document loaded (%d chars)", len(content)
    )
    return content
