# v1-to-v2 porting ledger

One row per ported unit. "Verbatim" means byte-equivalent logic; "adapted"
names what changed and why. Parity evidence is the test that locks the port.
Never reinvent a keeper without reading its v1 original first.

| v1 source (ApiChain--Backend) | v2 destination | Verbatim/adapted | Parity evidence |
|---|---|---|---|
| `backend/app/services/blockchain.py` `compute_data_hash` | `app/services/canonical.py` `compute_data_hash` | Adapted: `Web3.keccak` swapped for `eth_hash.keccak` (same primitive, no chain dependency); serialization identical | `tests/test_hash_determinism.py::test_keccak_parity_with_v1_golden_vector` (golden hex captured from v1 venv 2026-07-17) |
| `backend/app/routers/batch.py` `_canonical_dt` | `app/services/canonical.py` `canonical_dt` | Adapted: renamed from router-private helper to shared service; logic identical | `tests/test_hash_determinism.py::test_canonical_dt_tz_aware_and_naive_agree` |
| `backend/tests/test_hash_determinism.py` | `tests/test_hash_determinism.py` | Adapted: retargeted imports; added golden-vector and canonical_dt tests | self |
| `backend/app/services/geocode.py` | `app/services/geocode.py` | Verbatim (docstring reformatted) | `tests/test_geocode.py` (ported verbatim) |
| `backend/tests/test_geocode.py` | `tests/test_geocode.py` | Verbatim | self |
| `backend/app/database.py` | `app/database.py` | Adapted: SQLAlchemy 2 `DeclarativeBase`, psycopg 3 driver, settings-based URL; added metadata naming convention | `tests/test_migrations.py` |
| `backend/app/models/user.py` | `app/models/identity.py` `User` | Adapted (P1-B): SQLAlchemy 2 typed `Mapped`; `password`→`password_hash`; added `is_root`; PII `info` tags; dropped `wallet_address`, `email`/`created_by` reshaped | `tests/test_migrations.py` |
| `backend/app/models/farmer.py` | `app/models/identity.py` `Farmer` | Adapted (P1-B): typed; dropped `wallet_address` + `location` (Geography deferred to Phase 3) + `is_verified`/`verification_status`; `onboarded_by`→`enrolled_by`, added `user_id` link (08 D8) | `tests/test_migrations.py` |
| `backend/app/models/batch.py` | `app/models/batch.py` `HoneyBatch` | Adapted (P1-B): typed; dropped six `*_tx_hash` + six lifecycle `*_at` + `blockchain_batch_id`; `current_state`(str)→`state`(enum) + `state_updated_at`; `blockchain_batch_id`→`batch_code` | `tests/test_migrations.py` |

New in v2 (no v1 origin): `audit_log`, `consent_records`, `refresh_tokens`, `idempotency_keys` (models in `app/models/audit.py`, `auth.py`), the four PG enum types, and the spine baseline migration `8547454dfd88`. Services new in v2: `audit_log` (P1-C hash-chain writer), `security`+`refresh_tokens` (P1-D; JWT/bcrypt/rotation — the v1 `require_roles` *shape* carried into `deps.py`, but v1's custodial-wallet/oracle auth is gone), `consent` (P1-E), `integrity` (P1-G), `idempotency` (P1-H). Endpoints `/v2/auth/*`, `/v2/farmers`, `/v2/batches`, `/v2/audit/health` are v2-native (v1's `/auth`, `/farmers`, `/batches` predate the audit-chain model).

**Phase 2 (anchoring) is entirely new in v2 — nothing was ported, and there was
nothing to port.** v1 anchored each state transition as its own Sepolia
transaction: there was no Merkle layer, no periodic anchor, no inclusion proof,
and no offline verification path. New modules: `app/services/merkle.py` (P2-B,
pure tree/proof/verify), `app/services/ots.py` (P2-D, the one external
boundary), `app/services/anchoring.py` (P2-E/F, stamp + upgrade jobs),
`app/services/anchor_proof.py` (P2-G, on-demand proof derivation),
`scripts/verify_anchor.py` (P2-H, standalone offline verifier), the
`merkle_anchor` model and migration `ee59964e0dcb`, the `anchor_target` enum,
and the endpoints `GET /v2/batches/{id}/anchor-proof` (moved to
`GET /v2/public/batches/{public_id}/anchor-proof` in P3-I) and
`GET /v2/audit/anchor-health`. One new external boundary: OpenTimestamps
calendar servers, isolated in `ots.py` with `tests/fakes.py::FakeCalendar`.

What Phase 2 *does* carry over from v1 is conceptual and worth stating: the
hash-anchoring discipline, canonical-payload determinism, and the consumer's
"check this against something public" trust story all survive. What changed is
that verification now checks a Merkle inclusion proof against a periodic public
anchor instead of reading six per-stage on-chain transactions (`01` §6).

## Phase 3a (lifecycle + Codex scorer)

v1 sources are under `ApiChain--Backend/backend/app/`. **No stage payload is
marked verbatim.** Fields were renamed and numerics changed type, so a v1
golden hash cannot be reproduced against v2 columns. Parity evidence is the v2
test that locks each canonical shape (exact field set and rendering, after a
real DB round-trip), not a cross-version hash.

| v1 source | v2 destination | Verbatim/adapted | Parity evidence |
|---|---|---|---|
| `models/apiary.py` `ApiaryLocation` | `app/models/stages.py` `ApiaryLocation` | Adapted: `Float`→`Numeric(9,6)`; **no `Geography` column** (D5, returns with the geo-mapping UI); lat/lon tagged `pii: sensitive` | `tests/test_stage_models.py` |
| `models/apiary_record.py` | `app/models/stages.py` `ApiaryRecord` | Adapted: typed, `Numeric`; keeps v1 Sprint 6's snapshot design (columns copied, not joined, so editing the apiary cannot break an anchored hash) | `tests/test_stage_models.py` |
| `models/batch_metadata.py` | `app/models/stages.py` `BatchMetadata` | Adapted: typed, `Numeric`, `UtcDateTime`; `honey_type`/`apiary_management_method` stay Pydantic-validated strings, not PG enums (v1 Sprint 8) | `tests/test_stage_models.py` |
| `models/harvest_record.py` | `app/models/stages.py` `HarvestRecord` | Adapted: typed, `Numeric`, `UtcDateTime`; `gps_lat/gps_lon` tagged `pii: sensitive` | `tests/test_stage_models.py` |
| `models/process_record.py` | `app/models/stages.py` `ProcessRecord` | Adapted: typed, `Numeric` | `tests/test_stage_models.py` |
| `models/lab_result.py` | `app/models/stages.py` `LabResult` | **Adapted, not verbatim (D4).** `sucrose_level`→`sucrose_g_100g`: v1's field held total sugars (~75–80%) under a name meaning the ≤5 g/100g Codex parameter (`02` R9), and Sprint 14 routed total sugars through it on purpose. Every measurement now names its quantity and unit: `moisture_content`→`moisture_pct`, `hmf_level`→`hmf_mg_kg`; added `fructose_glucose_g_100g`, `diastase_schade`, `free_acidity_meq_kg` for the scorer. `Float`→`Numeric`. `analyst_name` tagged `pii: identity` | `tests/test_stage_models.py`, `tests/test_stage_payloads.py::test_lab_payload_carries_no_retired_v1_field` |
| `models/lab_result.py` GeoAI columns (`predicted_moisture`, `predicted_sugar`, `predicted_hmf`, `authenticity_score`, `validation_status`, `explanation`) | — | **Dropped, not ported.** `02` retires the ML model as decision-maker; `codex_conformance` replaces the decision | `tests/test_stage_payloads.py::test_lab_payload_carries_no_retired_v1_field` |
| `models/packaging_record.py` | `app/models/stages.py` `PackagingRecord` | Adapted: typed; `qr_codes` stays gone (v1 Sprint 13, one QR per batch) | `tests/test_stage_models.py` |
| `models/distribution_record.py` | `app/models/stages.py` `DistributionRecord` | Adapted: typed | `tests/test_stage_models.py` |
| every stage model's `*_proof_hash` | — | **Dropped (D6).** v1 kept the hash of a per-stage Sepolia tx; v2's audit row in the same transaction already carries `payload_hash`, and Phase 2 anchors it | `tests/test_verify_endpoint.py` (the audit row is found by action) |
| `routers/batch.py` `_apiary_record_canonical_payload` | `app/services/stage_payloads.py` `apiary_record` | Adapted: fixed-precision strings (6 dp coords, 2 dp elsewhere) instead of native floats | `tests/test_stage_payloads.py::test_apiary_payload_matches_the_v1_field_set` |
| `routers/batch.py` `_metadata_record_canonical_payload` | `stage_payloads.batch_metadata` | Adapted: rendering as above; **`notes` still excluded** (v1 Sprint 8); `recorded_at` still hashed, the one bookkeeping timestamp kept, for parity | `test_stage_payloads.py::test_metadata_payload_lowercases_enums_and_fixes_numeric_precision`, `::test_metadata_notes_are_stored_but_never_hashed` |
| `routers/batch.py` `_harvest_record_canonical_payload` | `stage_payloads.harvest_record` | Adapted: rendering; datetimes via `canonical_dt`, stored through `UtcDateTime` | `test_stage_payloads.py::test_harvest_payload_matches_the_v1_field_set`, `::test_a_tz_aware_and_a_naive_datetime_hash_identically` |
| `routers/batch.py` `_process_record_canonical_payload` | `stage_payloads.process_record` | Adapted: rendering | `test_stage_payloads.py::test_process_payload_matches_the_v1_field_set` |
| `routers/batch.py` `_lab_result_canonical_payload` | `stage_payloads.lab_result` (+ the verdict, merged by the S3 handler) | **Adapted:** renamed measurements as above; GeoAI fields gone; **hashes `tested_at`**, which v1 stored but left out of the payload, so a certificate date could change without breaking verification; v1's measured floats were hashed native, v2's render as 2 dp strings; the anchored payload also carries the full Codex verdict and `rule_set_version` | `test_stage_payloads.py::test_lab_payload_names_every_measurement_with_its_unit`, `tests/test_lab_verify.py` |
| `routers/batch.py` `_packaging_record_canonical_payload` | `stage_payloads.packaging_record` | Adapted: rendering | `test_stage_payloads.py::test_packaging_payload_matches_the_v1_field_set` |
| `routers/batch.py` `_distribution_record_canonical_payload` | `stage_payloads.distribution_record` | Adapted: rendering | `test_stage_payloads.py::test_distribution_payload_matches_the_v1_field_set` |
| `routers/batch.py` `verify_batch` (`GET /batches/{id}/verify`) | `app/services/verification.py` + `app/routers/public.py` (`GET /v2/public/batches/{public_id}/verify`) | **Adapted:** the three ways are re-based on the audit log (recomputed / `payload_hash` on the audit row / Merkle inclusion) instead of DB / Sepolia tx / recomputed, since v2 has no per-stage transaction. See the notes below | `tests/test_verify_endpoint.py`, `tests/test_phase3a_acceptance.py` |
| `ApiBlockchain` `TraceabilityRegistry.sol` sequential state checks | `app/services/transitions.py` | Adapted: v1 enforced "no skipping" on chain, one function per transition; v2 enforces it in one `LEGAL` map before the stage insert, with a stable 409 `invalid_transition` | `tests/test_transitions.py` (all 36 state pairs), `tests/test_package_distribute.py::test_distributed_is_terminal_at_every_endpoint` |

**Notes that do not fit a row:**

- **The `notes` asymmetry is an inconsistency carried for parity.** v1 left
  `notes` out of `batch_metadata`'s hash on purpose, so fixing a typo cannot
  invalidate anchored history. It then hashed `notes`, `handling_notes` and
  `handover_notes` in the other payloads. The rationale was applied to one table
  in six. The plan said to port field-for-field, so the asymmetry was kept and
  documented rather than silently fixed. Hashing notes nowhere would be a
  design change for a later phase, not a port.
- **Server bookkeeping timestamps (`recorded_at`, `evaluated_at`) are excluded
  from every payload.** The audit row's own `created_at` already records and
  anchors them. `batch_metadata.recorded_at` is the single exception, for
  parity.
- **Numerics are `Numeric`, not v1's `Float`**, so exact decimals render to a
  fixed-precision string with no repr surprises.
- **Public lookup key (P3-I).** v1's jar QR carried a 64-hex on-chain id, but
  v1's `/verify` *also* accepted the plain integer id (`batch_id.isdigit()`), so
  v1 could be enumerated too. v2 accepts only a random 128-bit `public_id`
  (Alembic `c41ded8b7533`), and the integer-keyed public routes are gone,
  Phase 2's `anchor-proof` included.
- **Public field policy (P3-I, Ian 2026-10-07).** v1's `/verify` returned stage
  payloads to anonymous scanners as stored. v2 classifies every field in
  `verification.FIELD_POLICY`, fail-closed:
  - hive coordinates go to 2 dp and altitude to 100 m;
  - free text, `analyst_name`, `transport_reference` and `apiary_id` are
    withheld;
  - a block whose public form differs from its exact pre-image publishes no
    hashes, because a hash over exact coordinates is brute-forceable from the
    2 dp cell.

**New in v2 (no v1 origin):**
- `app/services/codex_scoring.py`: the frozen, versioned rule set
  `codex-kenya-v1` with a golden vector. It replaces v1's ML authenticity
  decision.
- the `codex_conformance` table and `conformance_verdict` enum.
- `app/services/stage_writer.py`: one handler order for all five transitions.
- `app/services/ownership.py`: the farmer-scope check whose absence was v1's
  `farm-details` IDOR.
- `app/models/types.UtcDateTime`.
- migrations `cd6efa0b7cd3`, `4f58f6313266` and `c41ded8b7533`.

Pending later-phase ports (read v1 first): `models/document.py` (Phase 3b media
pipeline); `environmental_data` and `geo_ai` (only with `origin_verification`,
gated on the real-data partnership); PostGIS `Geography` (with the geo-mapping
UI).
