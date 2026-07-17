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
| `backend/app/database.py` | `app/database.py` | Adapted: SQLAlchemy 2 `DeclarativeBase`, psycopg 3 driver, settings-based URL | Phase 1 integration tests |

Pending Phase 1 ports (read v1 first): PostGIS models, per-stage `*_records`
models and canonical payload helpers, three-way verify logic, auth shape.
