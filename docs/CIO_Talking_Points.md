# CIO Brief — ATP Fulfillment Agent
## Talking Points (2–3 min)

---

### Opening (20 sec)
"This is an AI agent system that automates ATP fulfillment checks in supply chain —
the question 'can we ship X units by date Y?' This process normally takes hours in SAP.
This does it in about a minute."

---

### The Problem (30 sec)
Planners manually query SAP stock, compute ATP by hand, consult logistics policy,
then submit a change order. Each step is slow, error-prone, and undocumented.
The agent collapses the entire chain:

- Manual SAP query → 15–30 min → replaced by a single API call
- Spreadsheet ATP calc → error-prone → deterministic algorithm
- Policy consult → hours → Policy Agent reads the rules file in real time
- SAP change order → 1–2 days → PO method updated after one-click planner approval
- No audit trail → full decision trace returned in every response

---

### Architecture (45 sec)
Three agents in a graph: a **Supervisor** that orchestrates, a **SAP Agent** that runs
inventory math, and a **Policy Agent** that reads the corporate air-freight rules file
and applies them.

The routing mirrors the real org structure — planner, then compliance, then back to execute.
One API call for the answer; when a PO would change, the run pauses and a planner
approves it with a second call. The paused run is saved, so it survives a restart.

**Decision matrix:**
| ATP Result           | Outcome                  |
|----------------------|--------------------------|
| Stock available now  | Order confirmed          |
| PO covers on time    | Confirmed via sea freight|
| PO late, air OK      | AIR proposed → planner approves → POs switched |
| PO late, air blocked | Late delivery advised    |
| Quantity shortage    | Cannot fulfill           |

---

### The AI Logic (30 sec)
Two non-obvious design decisions:

1. **Break-even PO algorithm** — when switching to air freight, only the *minimum*
   necessary POs are updated. POs already absorbed by committed sales orders are never
   touched. Cost impact is minimized algorithmically, not left to judgment.

2. **Policy-as-a-document** — air freight authorization rules live in a Markdown file,
   not in code. A logistics manager can update plant-level rules without a code deploy.
   The Policy Agent reads and interprets it at runtime.

Also: `temperature=0` on the LLM — routing should be as repeatable as possible.
It reduces variation but isn't a hard guarantee, which is why the numbers and the
approval gate never depend on the LLM. Deliberate product decision.

---

### If asked: "Why multi-agent? Isn't this just an if/else?" (20 sec)
"For this slice alone, yes — it's a decision table and could be a function. I built it
as a supervisor with sub-agents because it's the first slice of a bigger workflow:
credit hold, cross-plant transfer, work-order expedite, rework, shortage root cause.
There, requests arrive as free text and some steps are open-ended, so adding a lever
means adding an agent and a routing rule, not rewriting the control flow. Each agent
only has its own tools, and anything that must be correct — the ATP math, PO selection,
the approval gate — is enforced in code. The LLM can change the path, never the numbers,
and never without a planner's sign-off."

*Presenter note: it scales with capabilities, not request volume. More requests per
second don't need more agents, and sequential LLM calls are the bottleneck under load.*

---

### Assumptions & Known Scope (20 sec)
This is a working prototype with clear boundaries:

- SQLite mock database (not live SAP)
- No SAP RFC/BAPI connector yet
- Single plant per request (no multi-plant allocation)
- No authentication on the API endpoint
- No freight cost comparison — policy-gated only
- No rollback workflow; approver name not authenticated yet

Each is a known gap with a clear production path.

---

### Roadmap (15 sec)
- **Phase 1 (now):** Prototype — mock SAP, all 5 decision paths validated
- **Phase 2 (beta):** PostgreSQL + SAP connector + API auth + async calls
- **Phase 3 (production):** Multi-plant, approval matrix by role and order value, audit logging to a data warehouse, rollback

---

### Closing (15 sec)
"The design prioritizes explainability over cleverness — every decision comes back
with a full audit trace showing exactly which agent did what, which policy clause
authorized it, and what the stock position was at decision time.
In regulated supply chain, that's a product requirement, not a nice-to-have."

---

*Open [docs/CIO_Brief.html](https://hpchou2.github.io/askATP-agent/CIO_Brief.html) for the visual version of this presentation.*
