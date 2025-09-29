# Schema Contract – Custody / Bond Normalized (Harris Phase)

Version: 1.0-harris-initial  
Generated: 2025-09-25Z  
Scope: `simple_harris` collection (other counties to adopt after phased rollout)

---
## 1. Purpose
Defines canonical fields delivered by the normalization pipeline to downstream consumers (front end, analytics, APIs). Establishes authoritative meanings for booking and aging related fields, stability guarantees, and deprecation roadmap for legacy fields.

---
## 2. Collections Covered
| Collection | Role | Notes |
|-----------|------|-------|
| `harris_bond` (and related raw) | Raw ingest | Daily feed; provides `file_date` but not true booking timestamp |
| `simple_harris` | Normalized output | FE / analytics should read from here |

---
## 3. Canonical Identity Fields
| Field | Type | Description | Stability |
|-------|------|-------------|-----------|
| `_upsert_key.anchor` | string | Deterministic anchor (case_number digits prefix fallback to SPN) | Stable once set |
| `case_number` | string | Digits-only prefix derived from source case number | Stable unless upstream numbering changes |
| `county` | string | Lowercase county slug (`harris`) | Constant |
| `category` | string | Derived docket group (`Criminal` / `Civil`) | Stable |

---
## 4. Booking & Aging Fields (Current Canonical Set)
| Field | Type | Example | Source Precedence | Notes |
|-------|------|---------|-------------------|-------|
| `booking_datetime` | string (ISO8601 UTC) | `2025-08-22T00:00:00Z` | `first_seen_at` → `updated_at` → legacy `booking_date` | Canonical booking instant (day-level precision currently) |
| `booking_date_v2` | string (YYYY-MM-DD) | `2025-08-22` | Derived from `booking_datetime` | Used for date grouping; **DO NOT** reverse-derive datetime |
| `booking_derivation_source` | enum | `legacy_booking_date` | As above | Indicates which field populated `booking_datetime` |
| `time_bucket_v2` | enum | `30d_60d` | Derived from `booking_datetime` | New aging taxonomy (see §5) |

### Legacy (Deprecated – Retained for Transition)
| Field | Issue | Planned Action |
|-------|-------|---------------|
| `booking_date` | Feed date proxy (not true booking) | Replace with `booking_date_v2` once FE migrated |
| `time_bucket` | Inflated freshness (based on file date heuristic) | Freeze and remove after all counties on v2 |

Deprecation Flag (future): `WRITE_BACK_BOOKING_DATE=1` will overwrite legacy `booking_date` with `booking_date_v2` (not yet enabled).

---
## 5. `time_bucket_v2` Taxonomy
| Bucket | Range Definition | Ordering | Rationale |
|--------|------------------|----------|-----------|
| `0_24h` | < 24 hours | 1 | Fresh intakes |
| `24_48h` | ≥24h <48h | 2 | Early watch window |
| `48_72h` | ≥48h <72h | 3 | 72h operational SLA edge |
| `3d_7d` | ≥72h <7d | 4 | Short-term pipeline |
| `7d_30d` | ≥7d <30d | 5 | Active aging zone |
| `30d_60d` | ≥30d <60d | 6 | Pre-stale horizon |
| `60d_plus` | ≥60d | 7 | Collapsed long tail (per business: >60d not actionable unless bound to customer entity) |

> Note: No buckets beyond 60 days; all collapsed into `60d_plus` as requested.

Computation: `booking_datetime` age compared to `now (UTC)`; truncated in hours then grouped.

---
## 6. Bond Fields (Current State – Pending Consolidation)
| Field | Type | Notes |
|-------|------|-------|
| `bond_amount` | number or null | Parsed numeric (preferred for sorting) |
| `bond` | mixed (number / string) | Transitional; will be normalized away |
| `bond_label` | string | Textual classification (may be derived from note) |
| `bond_note` (debug) | string | Raw feed note (may be surfaced as `bond_label`) |
| `needs_bond_help` | bool | Derived heuristic (excludes denied / non-actionable) |
| `booking_priority` | int | Rank based on initial ingest categorization (legacy) |
| `booking_age_category` | legacy enum | Pre-v2 heuristic; do not build new UX on this |

Planned Consolidation (future phase):
- Canonical numeric: `bond_amount`
- Canonical textual: `bond_label` (enum or descriptive string)
- Remove `bond` once consumers shifted.

---
## 7. Operational / Metadata Fields
| Field | Type | Description |
|-------|------|-------------|
| `scraped_at` | ISO datetime | Ingest timestamp (best-effort UTC) |
| `normalized_at` | ISO datetime | Normalization run timestamp |
| `tags` | array[string] | Includes anomaly tags (`future_date_candidate`, etc.) |
| `history` (raw collections) | array | Not replicated in simple yet for Harris bond (future enrichment plan) |

---
## 8. Field Stability & Guarantees
Classification:
| Level | Meaning |
|-------|---------|
| **Stable** | Name, type, semantics will not change without version bump |
| **Additive** | Field may gain new enum values; handle unknown gracefully |
| **Transitional** | Field will be removed post-migration; rely on replacement |

Current Mapping:
| Field | Stability |
|-------|----------|
| `booking_datetime` | Stable |
| `booking_date_v2` | Stable |
| `time_bucket_v2` | Stable (additive enum) |
| `booking_derivation_source` | Additive (may add `arrest_datetime` later) |
| `booking_date` | Transitional |
| `time_bucket` | Transitional |
| `bond` | Transitional |
| `bond_amount` | Stable |
| `bond_label` | Stable (enum expansion allowed) |

---
## 9. Derivation Algorithm (Pseudo)
```
if ENABLE_BOOKING_DERIVE:
    if missing booking_datetime:
        for src in [first_seen_at, updated_at, booking_date]:
            dt = parse(src)
            if dt: chosen = src; break
        if chosen:
            booking_datetime = dt (UTC, zero microseconds)
            booking_date_v2 = date(dt)
            booking_derivation_source = chosen

if ENABLE_TIME_BUCKET_V2 and booking_datetime:
    age_hours = (now_utc - booking_datetime)/3600
    bucket = map_to_v2_bucket(age_hours)
    time_bucket_v2 = bucket
```

---
## 10. Front-End Consumption Pattern
```
const agingBucket = doc.time_bucket_v2 || doc.time_bucket; // fallback
const bookingDateDisplay = doc.booking_datetime || doc.booking_date_v2 || doc.booking_date;
const isV2 = Boolean(doc.time_bucket_v2);
```

Recommended ordering for charts: `["0_24h","24_48h","48_72h","3d_7d","7d_30d","30d_60d","60d_plus"]`.

---
## 11. Validation Metrics (Post Harris Backfill)
| Metric | Value |
|--------|-------|
| Docs | 4097 |
| booking_datetime coverage | 100% |
| time_bucket_v2 coverage | 100% |
| Derivation source breakdown | 100% `legacy_booking_date` (current feed granularity) |
| Future anomalies | 0 |

---
## 12. Migration / Rollout Plan Snapshot
Phase | Status | Notes
------|--------|------
Baseline snapshot | COMPLETE | Pre-change metrics locked
Derive + v2 (Harris) | COMPLETE | Feature flags enabled
Backfill residuals | COMPLETE | 39 docs updated
FE toggle adoption | PENDING | Implement `useTimeBucketV2`
Legacy overwrite | DEFERRED | Await FE validation
Extend to other counties | PENDING | Same flags + backfill

---
## 13. Known Gaps / Future Enhancements
1. True booking timestamp source (if an upstream feed provides it later) – will take precedence over legacy date.
2. Multi-county generalization – factor helpers into shared module.
3. Bond normalization – unify `bond`, `bond_amount`, `bond_label`.
4. History enrichment – replicate raw `history` into simple with controlled size.
5. SLA metrics – computed daily summary collection (future spec).

---
## 14. Backward Compatibility Checklist
Change | Break Risk | Mitigation
-------|------------|-----------
Add `booking_datetime` | None | Additive only
Add `time_bucket_v2` | None | Fallback to legacy bucket
Remove `time_bucket` (future) | High | Delay until FE migration confirmed
Overwrite `booking_date` | Medium | Flag gated + pre-announcement
Remove `bond` | Medium | FE to switch to `bond_amount` + `bond_label`

---
## 15. Versioning Rules
- Patch (x.y.z): Spelling / doc clarifications only.
- Minor (x.y): Added optional fields or enum expansions.
- Major (x): Removal or semantic change of Stable fields.

Current version: **1.0** (initial stable contract for new aging model).

---
## 16. Ownership & Contact
| Area | Owner |
|------|-------|
| Ingestion (Harris) | Data Pipeline Team |
| Normalization logic | Data Pipeline Team |
| Front-end adaptation | Application / UI Team |
| Contract maintenance | Shared (pipeline + FE leads) |

---
## 17. Quick Reference Enum Values
`time_bucket_v2`: `0_24h`, `24_48h`, `48_72h`, `3d_7d`, `7d_30d`, `30d_60d`, `60d_plus`  
`booking_derivation_source`: `first_seen_at`, `updated_at`, `legacy_booking_date` (current), future: `arrest_datetime` (TBD)

---
## 18. Consumer Action Items
1. Implement feature flag `useTimeBucketV2`.
2. Switch aging visuals to `time_bucket_v2` (with fallback) – verify counts.
3. Display canonical booking time via `booking_datetime`.
4. Prepare to drop legacy fields after confirmation window.

---
*End of Contract v1.0 – Harris Phase*
