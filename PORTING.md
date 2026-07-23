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

Pending later-phase ports (read v1 first): PostGIS/apiary models, per-stage `*_records`
models and canonical payload helpers, three-way verify logic.
