# Air Shipping Policy — Corporate Logistics Guidelines

## Purpose

This document defines which plants are authorized to switch purchase order (PO) logistics method from SEA (ocean freight) to AIR (air freight) when orders are identified as not on-time. Air shipping incurs significantly higher freight costs and is subject to regional compliance and budget constraints.

---

## Plant-Level Policy

### TW01 — Taiwan Site 1 (Taipei)
- **Air shipping: PERMITTED**
- Authorized for air freight escalation when ATP check confirms a shortage and
  the requested quantity cannot be fulfilled by the customer's required date via
  sea shipment.
- Approval threshold: any shortage quantity is eligible; no minimum order value required.

### TW02 — Taiwan Site 2 (Taichung)
- **Air shipping: NOT PERMITTED**
- This plant operates under a strict freight cost ceiling. Air shipping is
  prohibited regardless of shortage severity.
- Escalation path: contact regional supply chain manager for manual review.

### SG01 — Singapore Site 1
- **Air shipping: PERMITTED with conditions**
- Air freight is authorized only when the unfulfilled quantity exceeds 500 units.
- For shortages below 500 units, sea freight re-scheduling is the required approach.

### JP01 — Japan Site 1 (Tokyo)
- **Air shipping: PERMITTED**
- Authorized for air freight when customer delivery date falls within 14 days and
  sea shipment cannot arrive in time. No quantity minimum.

### MY01 — Malaysia Site 1 (Penang)
- **Air shipping: NOT PERMITTED**
- Under active cost-reduction program through Q4 2026. Air shipping suspended
  until further notice.

---

## General Rules

1. Air shipping authorization applies only to open POs whose `logistic_method`
   is currently set to `SEA`.
2. Once changed to `AIR`, a PO cannot be reverted automatically — manual SAP
   change order required.
3. The minimum number of POs needed to cover the shortage quantity should be
   switched (earliest delivery date first), not all open POs.
4. Any air shipping escalation must be logged in the fulfillment case record.
