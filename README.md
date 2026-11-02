# ATP Fulfillment Agent — SAP Logic Prototype

A FastAPI + LangGraph multi-agent system that mirrors SAP-style Available-to-Promise (ATP) checks and automatically escalates to air shipping when sea freight cannot meet the customer's requested delivery date.

---

## What is ATP?

ATP (Available-to-Promise) answers one question:  
**"Given what we have in stock and what's coming from vendors/other plants/manufacture, when is the earliest date we can ship X units to this customer?"**

The logic:
1. Start with current unrestricted stock
2. Subtract stock already committed to existing customer orders
3. If the net available covers the request → confirm today
4. If not → walk incoming purchase orders (POs) chronologically; after skipping POs whose units are fully consumed by committed SO demand, find the earliest PO that fills the gap and report its arrival date
5. If that PO arrives by the requested date → confirm via that PO
6. If that PO arrives too late → check company policy for AIR shipping authorization
7. If AIR is authorized → switch the relevant POs to AIR; otherwise report the constraint
8. If no combination of stock and POs covers the request → tell the customer the maximum available quantity and when

---

## Agent Workflow

```
START
  │
  ▼
Supervisor ──► SAP Agent: sap_extract + calculate_atp
  │
  ├── fulfillable=True,  on_time=True  ──────► FINISH  "fulfillable by <date> via SEA"
  │
  ├── fulfillable=False  ────────────────────► FINISH  "cannot fulfill, max available = X"
  │
  └── fulfillable=True,  on_time=False
        │   (quantity is available but SEA arrives too late)
        ▼
      Supervisor ──► Policy Agent: read_air_shipping_policy
        │
        ├── AIR NOT PERMITTED ───────────────► FINISH  "shortage, air blocked — reason: ..."
        │
        └── AIR PERMITTED
              │
              ▼
            Supervisor ──► SAP Agent: po_logistic_method_change → AIR
              │
              ▼
            FINISH  "switched PO(s) to AIR — see updated delivery dates"
```

### PO selection logic in `po_logistic_method_change`

The function walks POs in earliest-delivery-date order accumulating:

```
running = stock − committed_SOs + PO₁ + PO₂ + … + POᵢ
```

**Phase 1 — skip pre-break-even POs:** While `running < 0`, each PO's units are entirely absorbed by existing SO commitments. These POs are left untouched.

**Phase 2 — switch break-even through decisive:** The first PO that brings `running ≥ 0` is the break-even PO — SO commitments are now fully covered. Switch this PO and every subsequent PO until `running ≥ requested_qty` (the decisive PO).

If `stock − committed_SOs ≥ 0` from the start (no SO deficit), Phase 1 is skipped and switching begins with the first PO.

---

## Prerequisites

- Python 3.8+
- An Anthropic API key — get one at https://console.anthropic.com

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy the environment template and fill in your API key
cp .env.example .env
# Edit .env:  ANTHROPIC_API_KEY=sk-ant-...

# 3. Create and seed the mock SAP database  (run once; re-run to reset)
python setup_db.py

# 4. Start the API server
uvicorn api:app --reload
```

The server starts at **http://127.0.0.1:8000**.
Interactive Swagger docs: **http://127.0.0.1:8000/docs**

---

## API Reference

### `GET /health`

Liveness check.

```json
{ "status": "ok" }
```

### `POST /atp-check`

Run the full multi-agent ATP fulfillment workflow.

**Request body:**

```json
{
  "matnr":          "MAT001",
  "werks":          "TW01",
  "requested_qty":  100,
  "requested_date": "2026-06-10"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `matnr` | string | ✓ | Material number |
| `werks` | string | ✓ | Plant code (default: `TW01`) |
| `requested_qty` | float | ✓ | Quantity the customer needs (must be > 0) |
| `requested_date` | string | ✓ | Customer's required delivery date — `YYYY-MM-DD` |

**Response:**

```json
{
  "answer": "Order for MAT001 at TW01 cannot be fulfilled by 2026-06-10 via sea ...",
  "trace": [
    { "role": "human", "name": null,        "content": "Please check ATP ..." },
    { "role": "ai",    "name": "supervisor", "content": "Route to sap_agent ..." },
    { "role": "ai",    "name": "sap_agent",  "content": "calculate_atp result ..." },
    { "role": "human", "name": "supervisor", "content": "Check policy for TW01 ..." }
  ]
}
```

---

## Test Cases

### Test 1 — Fulfillable from current stock (on time)

```json
{ "matnr": "MAT001", "werks": "TW01", "requested_qty": 20, "requested_date": "2026-06-10" }
```

- `available_now = 150 − 120 = 30 ≥ 20` → fulfillable today, on time
- **Agent flow:** supervisor → sap_agent → supervisor FINISH
- **Expected answer:** Order confirmed from available stock

---

### Test 2 — Fulfillable via incoming PO, on time

```json
{ "matnr": "MAT002", "werks": "TW01", "requested_qty": 100, "requested_date": "2026-07-15" }
```

- `available_now = 300 − 250 = 50` (not enough on its own)
- PO1002 arrives 2026-07-15 (+200 units) → cumulative = 250 ≥ 100 → `earliest_date = 2026-07-15`
- `on_time = True` (2026-07-15 ≤ 2026-07-15)
- **Agent flow:** supervisor → sap_agent → supervisor FINISH
- **Expected answer:** Order fulfillable by 2026-07-15

---

### Test 3 — Fulfillable via PO but late → full AIR escalation

```json
{ "matnr": "MAT001", "werks": "TW01", "requested_qty": 100, "requested_date": "2026-06-10" }
```

- `available_now = 30` (not enough). PO1001 arrives 2026-07-10 → `on_time = False` (30 days late)
- TW01 policy: AIR **PERMITTED** with no minimum quantity
- PO selection: `available_now = 30 > 0`, no SO-consumed POs to skip. PO1001 is the decisive PO (30 + 100 = 130 ≥ 100) and is switched to AIR.
- **Agent flow:** supervisor → sap_agent (sap_extract + calculate_atp) → supervisor → policy_agent → supervisor → sap_agent (po_logistic_method_change) → supervisor FINISH
- **Expected answer:** Switched PO1001 to AIR

---

### Test 4 — Cannot fulfill (quantity shortage)

```json
{ "matnr": "MAT003", "werks": "TW01", "requested_qty": 200, "requested_date": "2026-07-20" }
```

- `available_now = 80 − 80 = 0`. PO1003 arrives 2026-07-20 (+150) → max available = 150 < 200 → `fulfillable = False`
- AIR shipping does not create new stock; no escalation is attempted
- **Agent flow:** supervisor → sap_agent → supervisor FINISH
- **Expected answer:** Cannot fulfill — maximum available is 150 pcs on 2026-07-20

---

### Test 5 — Break-even PO selection (SO-committed POs skipped)

```json
{ "matnr": "MAT004", "werks": "TW01", "requested_qty": 50, "requested_date": "2026-06-10" }
```

- `available_now = 50 − 200 = −150` — committed SOs exceed current stock; the first POs are entirely absorbed by SO demand
- PO walk:
  - PO1004 arrives 2026-07-05 (+100): running = −50 → still in SO-deficit territory, **skip**
  - PO1005 arrives 2026-07-15 (+120): running = +70 → break-even (≥ 0) and 70 ≥ 50 → decisive → **switch**
  - PO1006 arrives 2026-07-25 (+100): running = +170 → coverage already reached, leave untouched
- `on_time = False` (35 days late). TW01 policy: AIR **PERMITTED**
- `po_logistic_method_change`: PO1004 left on SEA (serves committed SOs); PO1005 switched to AIR; PO1006 untouched
- **Agent flow:** supervisor → sap_agent (sap_extract + calculate_atp) → supervisor → policy_agent → supervisor → sap_agent (po_logistic_method_change) → supervisor FINISH
- **Expected answer:** Only PO1005 switched to AIR; PO1004 and PO1006 left on SEA

---

## Seed Data Reference

| Material | Description       | Stock | Committed (SO)  | Net Available | POs                                                                     |
|----------|-------------------|------:|----------------:|--------------:|-------------------------------------------------------------------------|
| MAT001   | Server CPU Module |   150 |             120 |            30 | PO1001: 2026-07-10 +100                                                 |
| MAT002   | Memory 32GB       |   300 |             250 |            50 | PO1002: 2026-07-15 +200                                                 |
| MAT003   | NVMe SSD 1TB      |    80 |              80 |             0 | PO1003: 2026-07-20 +150                                                 |
| MAT004   | Graphics Card 8GB |    50 |     200 (×2 SO) |          −150 | PO1004: 2026-07-05 +100 · PO1005: 2026-07-15 +120 · PO1006: 2026-07-25 +100 |

Plant: **TW01** (Taiwan Site 1) | Storage location: **WH01** | All POs default to **SEA**.

Re-run `python setup_db.py` at any time to reset the database to this baseline.

---

## File Structure

```
askATP_simple/
├── api.py                        — FastAPI app, lifespan, /health and /atp-check endpoints
├── setup_db.py                   — creates and seeds sap_mock.db
├── requirements.txt
├── .env.example                  — copy to .env and fill in ANTHROPIC_API_KEY
│
├── agents/
│   ├── supervisor.py             — LangGraph StateGraph + supervisor routing logic
│   ├── sap_agent.py              — ReAct agent wrapping the three SAP tools
│   └── policy_agent.py           — ReAct agent wrapping the policy file reader
│
├── tools/
│   ├── sap_tools.py              — sap_extract, calculate_atp, po_logistic_method_change
│   └── policy_tools.py           — read_air_shipping_policy
│
├── policies/
│   └── air_shipping_policy.md    — per-plant AIR shipping authorization rules
│
└── sap_mock.db                   — generated by setup_db.py (do not edit by hand)
```

---

## SAP Field Name Glossary

| SAP Field | Meaning |
|-----------|---------|
| `matnr`   | Material number — unique product ID |
| `maktx`   | Material description — product name |
| `meins`   | Base unit of measure (`PC` = pieces) |
| `mtart`   | Material type (`FERT` = finished manufactured good) |
| `werks`   | Plant — the facility / site code |
| `lgort`   | Storage location within a plant |
| `labst`   | Unrestricted-use stock — quantity freely available to sell or ship |
| `menge`   | Quantity (on a PO line) |
| `eindt`   | Expected goods receipt date (PO delivery date) |
| `kwmeng`  | Confirmed quantity on a sales order |
| `edatu`   | Customer's requested delivery date on a sales order |