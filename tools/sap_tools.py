"""
tools/sap_tools.py — SAP-facing tools for the SAP Agent.

Plain functions wrapped as LLM tools in agents/sap_agent.py:
  1. sap_extract()                — pull all relevant SAP data in one shot
  2. calculate_atp()              — determine fulfillability from current SAP data

PO shipping-method change, split into plan and execute so a planner can
approve in between (the po_logistic_method_change tool in agents/sap_agent.py
calls both around the approval step):
  - plan_po_logistic_change()     — which POs would be switched, without writing
  - execute_po_logistic_change()  — write the switch to the database; does NOT
                                    ask for approval itself
  - log_approval()                — append the planner's decision to approval_log
"""

import logging
import sqlite3
import os
from datetime import date

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "sap_mock.db")


def _get_conn() -> sqlite3.Connection:
    db = os.path.normpath(DB_PATH)
    if not os.path.exists(db):
        raise FileNotFoundError(
            f"Database not found: {db}\n"
            "Please run 'python setup_db.py' first."
        )
    return sqlite3.connect(db)


# ─────────────────────────────────────────────────────────────────────────────
# sap_extract
# ─────────────────────────────────────────────────────────────────────────────

def sap_extract(matnr: str, werks: str) -> dict:
    """
    Extracts all relevant SAP data for a material/plant combination in a single call.

    Replaces the three separate get_stock / get_sales_orders / get_po_schedule
    calls from the legacy agent. Returns everything the SAP Agent needs before
    running any ATP logic.

    Parameters
    ----------
    matnr : material number, e.g. 'MAT001'
    werks : plant code,      e.g. 'TW01'

    Returns
    -------
    dict with keys:
        matnr         (str)   — material number echoed back
        werks         (str)   — plant code echoed back
        stock         (float) — total unrestricted-use stock on hand
        sales_orders  (list)  — open sales orders, each: {so_number, kwmeng, edatu}
        purchase_orders (list)— open POs sorted by delivery date, each:
                                {po_number, menge, eindt, logistic_method}
    """
    conn = _get_conn()
    cur = conn.cursor()

    # Stock
    cur.execute(
        "SELECT SUM(labst) FROM stock WHERE matnr = ? AND werks = ?",
        (matnr, werks),
    )
    row = cur.fetchone()[0]
    stock = float(row) if row is not None else 0.0

    # Open sales orders
    cur.execute(
        """
        SELECT so_number, kwmeng, edatu
          FROM sales_orders
         WHERE matnr = ? AND werks = ? AND status = 'OPEN'
         ORDER BY edatu ASC
        """,
        (matnr, werks),
    )
    sales_orders = [
        {"so_number": r[0], "kwmeng": float(r[1]), "edatu": r[2]}
        for r in cur.fetchall()
    ]

    # Open purchase orders (include logistic_method for visibility)
    cur.execute(
        """
        SELECT po_number, menge, eindt, logistic_method
          FROM purchase_orders
         WHERE matnr = ? AND werks = ?
         ORDER BY eindt ASC
        """,
        (matnr, werks),
    )
    purchase_orders = [
        {
            "po_number":       r[0],
            "menge":           float(r[1]),
            "eindt":           r[2],
            "logistic_method": r[3],
        }
        for r in cur.fetchall()
    ]

    conn.close()

    logger.info(
        "[sap_extract] %s / %s → stock=%.0f, open SOs=%d, open POs=%d",
        matnr, werks, stock, len(sales_orders), len(purchase_orders),
    )
    return {
        "matnr":          matnr,
        "werks":          werks,
        "stock":          stock,
        "sales_orders":   sales_orders,
        "purchase_orders": purchase_orders,
    }


# ─────────────────────────────────────────────────────────────────────────────
# calculate_atp
# ─────────────────────────────────────────────────────────────────────────────

def calculate_atp(
    matnr: str,
    werks: str,
    requested_qty: float,
    requested_date: str,
) -> dict:
    """
    Calculates the earliest date when requested_qty units can be fulfilled, and
    whether that date meets the customer's requested_date.

    ATP Logic:
      1. available_now = stock - total committed from open sales orders
      2. If available_now >= requested_qty  →  fulfillable today
      3. Otherwise walk POs in chronological order, accumulating incoming supply
      4. Return the date of the first PO that pushes the running total >= requested_qty
      5. If stock + all POs still can't cover the request, report the shortfall

    The supervisor uses on_time to decide routing:
      - on_time=True        → confirm the order (SEA is sufficient)
      - on_time=False       → escalate to policy_agent to check AIR authorization
      - fulfillable=False   → report quantity shortage; AIR cannot resolve this

    Parameters
    ----------
    matnr         : material number
    werks         : plant code
    requested_qty : quantity the customer wants
    requested_date: customer's required delivery date as 'YYYY-MM-DD'; compared
                    against earliest_date to compute on_time and days_late

    Returns
    -------
    dict with keys:
        fulfillable         (bool)      — True if the order can ever be covered
                                          (quantity-wise, regardless of timing)
        earliest_date       (str|None)  — YYYY-MM-DD of earliest possible fulfillment;
                                          None if fulfillable=False
        on_time             (bool)      — True if earliest_date <= requested_date;
                                          False means SEA is too slow, consider AIR
        days_late           (int|None)  — earliest_date - requested_date in days;
                                          negative = arrives early, positive = arrives late,
                                          None if fulfillable=False
        stock               (float)     — raw unrestricted stock
        committed           (float)     — total qty committed to open sales orders
        available_now       (float)     — stock - committed (may be negative)
        max_available       (float)     — maximum qty reachable with current stock + all POs
        max_available_date  (str)       — date when max_available is first reached
        pos_checked         (list)      — informational: every PO walked, with
                                          {po_number, menge, eindt, logistic_method,
                                           cumulative_after}. PO selection is handled
                                          automatically by plan_po_logistic_change
                                          and does not need to be derived from this list.
    """
    today = date.today().isoformat()

    logger.info(
        "[calculate_atp] %s / %s — requested qty=%.0f, date=%s",
        matnr, werks, requested_qty, requested_date,
    )
    data = sap_extract(matnr, werks)
    stock = data["stock"]
    committed = sum(so["kwmeng"] for so in data["sales_orders"])
    available_now = stock - committed

    if available_now >= requested_qty:
        on_time = today <= requested_date
        days_late = (date.fromisoformat(today) - date.fromisoformat(requested_date)).days
        logger.info(
            "[calculate_atp] FULFILLABLE TODAY — available_now=%.0f >= requested=%.0f — on_time=%s",
            available_now, requested_qty, on_time,
        )
        return {
            "fulfillable":        True,
            "earliest_date":      today,
            "on_time":            on_time,
            "days_late":          days_late,
            "stock":              stock,
            "committed":          committed,
            "available_now":      available_now,
            "max_available":      available_now,
            "max_available_date": today,
            "pos_checked":        [],
        }

    cumulative = available_now
    max_available = available_now
    max_available_date = today
    pos_checked = []

    for po in data["purchase_orders"]:
        cumulative += po["menge"]
        pos_checked.append({
            "po_number":        po["po_number"],
            "menge":            po["menge"],
            "eindt":            po["eindt"],
            "logistic_method":  po["logistic_method"],
            "cumulative_after": cumulative,
        })

        if cumulative > max_available:
            max_available = cumulative
            max_available_date = po["eindt"]

        if cumulative >= requested_qty:
            on_time = po["eindt"] <= requested_date
            days_late = (date.fromisoformat(po["eindt"]) - date.fromisoformat(requested_date)).days
            logger.info(
                "[calculate_atp] FULFILLABLE via %s on %s — cumulative=%.0f >= requested=%.0f"
                " — on_time=%s (days_late=%d)",
                po["po_number"], po["eindt"], cumulative, requested_qty, on_time, days_late,
            )
            return {
                "fulfillable":        True,
                "earliest_date":      po["eindt"],
                "on_time":            on_time,
                "days_late":          days_late,
                "stock":              stock,
                "committed":          committed,
                "available_now":      available_now,
                "max_available":      max_available,
                "max_available_date": max_available_date,
                "pos_checked":        pos_checked,
            }

    logger.info(
        "[calculate_atp] CANNOT FULFILL — max_available=%.0f on %s, requested=%.0f",
        max_available, max_available_date, requested_qty,
    )
    return {
        "fulfillable":        False,
        "earliest_date":      None,
        "on_time":            False,
        "days_late":          None,
        "stock":              stock,
        "committed":          committed,
        "available_now":      available_now,
        "max_available":      max_available,
        "max_available_date": max_available_date,
        "pos_checked":        pos_checked,
    }


# ─────────────────────────────────────────────────────────────────────────────
# plan_po_logistic_change
# ─────────────────────────────────────────────────────────────────────────────

def plan_po_logistic_change(
    matnr: str,
    werks: str,
    requested_qty: float,
    method: str,
) -> dict:
    """
    Works out which POs would be switched to cover requested_qty, without
    writing anything to the database.

    Internally calls sap_extract() to get current stock, committed sales orders,
    and open POs sorted by earliest delivery date (eindt ASC).

    PO selection — break-even to decisive rule:
      Walk POs accumulating:
          running = stock − committed_SOs + PO₁ + PO₂ + … + POᵢ

      Phase 1 — skip pre-break-even POs:
        While running < 0, each PO's units are entirely consumed by committed SO
        demand. These POs are left untouched.

      Phase 2 — switch from break-even to coverage:
        The first PO that brings running >= 0 is the break-even PO — SO
        commitments are now fully covered and its remaining units (and all
        subsequent PO units) are available for the new order.
        Switch this PO and every subsequent PO until running >= requested_qty.

    If stock − committed >= 0 from the start (no SO deficit), Phase 1 is skipped
    and switching begins with the very first PO.

    All-or-nothing: if the POs cannot reach requested_qty, the plan switches
    nothing (covered=False).

    Parameters
    ----------
    matnr         : material number, e.g. 'MAT001'
    werks         : plant code, e.g. 'TW01'
    requested_qty : the customer's requested quantity that must be covered
    method        : target shipping method — must be 'AIR' or 'SEA'

    Returns
    -------
    dict with keys:
        covered         (bool)  — True if the switched POs reach requested_qty
        switch_pos      (list)  — POs to switch: {po_number, eindt, menge,
                                   old_method, new_method, cumulative_after};
                                   empty if covered=False
        remaining_pos   (list)  — PO numbers not switched
        coverage_qty    (float) — running total at the last PO walked
        requested_qty   (float) — echoed back
        method          (str)   — the target method
    """
    method = method.upper()
    if method not in ("AIR", "SEA"):
        raise ValueError(f"Invalid logistic method '{method}'. Must be 'AIR' or 'SEA'.")

    data = sap_extract(matnr, werks)
    committed = sum(so["kwmeng"] for so in data["sales_orders"])
    running = data["stock"] - committed

    switch_pos = []
    remaining_pos = []
    covered = False
    # True once running >= 0: SO commitments are fully covered and subsequent
    # PO units are available for the new order.
    switch_active = running >= 0

    for po in data["purchase_orders"]:
        if covered:
            # New order already covered; leave all remaining POs untouched.
            remaining_pos.append(po["po_number"])
            continue

        running += po["menge"]

        if not switch_active:
            if running < 0:
                # Still in SO-deficit territory — this PO's units are entirely
                # consumed by committed sales orders. Do not switch.
                remaining_pos.append(po["po_number"])
                continue
            # running just crossed 0: this is the break-even PO.
            # Committed SOs are now fully covered; start switching from here.
            switch_active = True

        # switch_active=True: this PO contributes to the new order.
        # Switch it, and keep switching until running >= requested_qty.
        switch_pos.append({
            "po_number":        po["po_number"],
            "eindt":            po["eindt"],
            "menge":            po["menge"],
            "old_method":       po["logistic_method"],
            "new_method":       method,
            "cumulative_after": running,
        })

        if running >= requested_qty:
            covered = True

    if not covered:
        # Partial coverage doesn't solve the order — switch nothing so no PO
        # is upgraded (and paid for) without fixing the shortage.
        remaining_pos = [p["po_number"] for p in switch_pos] + remaining_pos
        switch_pos = []

    return {
        "covered":       covered,
        "switch_pos":    switch_pos,
        "remaining_pos": remaining_pos,
        "coverage_qty":  running,
        "requested_qty": requested_qty,
        "method":        method,
    }


# ─────────────────────────────────────────────────────────────────────────────
# execute_po_logistic_change
# ─────────────────────────────────────────────────────────────────────────────

def execute_po_logistic_change(
    matnr: str,
    werks: str,
    requested_qty: float,
    method: str,
    approval: dict | None = None,
) -> dict:
    """
    Switches the minimum set of POs to the specified logistic method (AIR or SEA)
    to cover requested_qty, respecting existing committed sales order allocations.

    PO selection is done by plan_po_logistic_change() (break-even to decisive
    rule); this function writes the result to the database in one transaction.

    All-or-nothing: if the POs cannot reach requested_qty, no PO is switched
    and the database is left unchanged.

    This function does not ask for approval: it writes as soon as it is called.
    In the agent workflow it only runs after a planner approves the proposal
    (see the po_logistic_method_change tool in agents/sap_agent.py).

    Parameters
    ----------
    matnr         : material number, e.g. 'MAT001'
    werks         : plant code, e.g. 'TW01'
    requested_qty : the customer's requested quantity that must be covered
    method        : target shipping method — must be 'AIR' or 'SEA'
    approval      : optional {thread_id, approver, comment}. When given, an
                    APPROVED row is appended to approval_log in the same
                    transaction as the PO updates, so a switch is never
                    recorded without its approval (or the reverse).

    Returns
    -------
    dict with keys:
        updated_count   (int)   — number of POs switched (0 if coverage unreachable)
        updated_pos     (list)  — each switched PO: {po_number, eindt, menge,
                                   old_method, new_method, cumulative_after}
        remaining_pos   (list)  — POs not switched; includes both POs consumed by
                                   committed SOs (pre-break-even) and POs not needed
                                   after coverage is achieved
        coverage_qty    (float) — running total at the last switched PO
        requested_qty   (float) — the requested_qty passed in (echoed back)
        method          (str)   — the method that was applied
        message         (str)   — human-readable summary
    """
    logger.info(
        "[execute_po_logistic_change] %s / %s — switching POs to %s to cover qty=%.0f",
        matnr, werks, method, requested_qty,
    )

    plan = plan_po_logistic_change(matnr, werks, requested_qty, method)
    method = plan["method"]
    updated_pos = plan["switch_pos"]
    running = plan["coverage_qty"]

    if plan["covered"]:
        conn = _get_conn()
        try:
            with conn:  # one transaction: all POs switch (and are logged), or nothing is written
                conn.executemany(
                    "UPDATE purchase_orders SET logistic_method = ? WHERE po_number = ?",
                    [(method, p["po_number"]) for p in updated_pos],
                )
                if approval is not None:
                    _insert_approval(
                        conn, approval.get("thread_id"), matnr, werks, requested_qty, method,
                        [p["po_number"] for p in updated_pos], "APPROVED",
                        approval.get("approver"), approval.get("comment"),
                    )
        finally:
            conn.close()
        msg = (
            f"Switched {len(updated_pos)} PO(s) to {method}. "
            f"Cumulative available after switch: {running:.0f} units "
            f"(requested: {requested_qty:.0f})."
        )
    else:
        msg = (
            f"No POs switched — cumulative available ({running:.0f}) still does not "
            f"reach requested_qty ({requested_qty:.0f}). "
            f"Shortage cannot be fully resolved by this method change alone."
        )

    logger.info(
        "[execute_po_logistic_change] switched %d PO(s) to %s — coverage=%.0f / requested=%.0f",
        len(updated_pos), method, running, requested_qty,
    )
    for p in updated_pos:
        logger.info(
            "  PO %s (%s)  %s → %s  menge=%.0f  cumulative=%.0f",
            p["po_number"], p["eindt"], p["old_method"], p["new_method"],
            p["menge"], p["cumulative_after"],
        )
    return {
        "updated_count": len(updated_pos),
        "updated_pos":   updated_pos,
        "remaining_pos": plan["remaining_pos"],
        "coverage_qty":  running,
        "requested_qty": requested_qty,
        "method":        method,
        "message":       msg,
    }


# ─────────────────────────────────────────────────────────────────────────────
# log_approval
# ─────────────────────────────────────────────────────────────────────────────

def log_approval(
    thread_id: str | None,
    matnr: str,
    werks: str,
    requested_qty: float,
    method: str,
    po_numbers: list[str],
    decision: str,
    approver: str | None,
    comment: str | None,
) -> None:
    """
    Appends one planner decision to the approval_log table.

    The table is append-only: rows are inserted, never updated or deleted, so
    it serves as the fulfillment case record required by policy rule 4.

    decision is 'APPROVED', 'REJECTED' or 'STALE_PROPOSAL' (approved, but the
    SAP data changed after the proposal was shown, so nothing was executed).
    An executed approval is logged by execute_po_logistic_change(approval=...)
    instead, in the same transaction as the PO updates.
    """
    conn = _get_conn()
    try:
        with conn:
            _insert_approval(conn, thread_id, matnr, werks, requested_qty, method,
                             po_numbers, decision, approver, comment)
    finally:
        conn.close()


def _insert_approval(conn, thread_id, matnr, werks, requested_qty, method,
                     po_numbers, decision, approver, comment) -> None:
    conn.execute(
        """
        INSERT INTO approval_log
            (thread_id, matnr, werks, requested_qty, method, po_numbers,
             decision, approver, comment, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (thread_id, matnr, werks, requested_qty, method, ",".join(po_numbers),
         decision, approver, comment),
    )
    logger.info(
        "[approval_log] %s / %s — %s by %s for %s",
        matnr, werks, decision, approver, ",".join(po_numbers) or "(no POs)",
    )
