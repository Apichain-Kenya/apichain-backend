# ApiChain v2 — Backend (`apichain-backend`)

**Student:** Ian Ndolo Mwau (SCT211-0034/2022, JKUAT) | **Supervisor:** Dr. Agnes Mindila
**Scope:** FastAPI + PostgreSQL/PostGIS REST API, hash-chained `audit_log`, Merkle anchoring via OpenTimestamps, rule-based Codex/KEBS scorer.
**Repository:** `github.com/Apichain-Kenya/apichain-backend`

For the workspace-wide map (v1-to-v2 relationship, both repos, archive), see `../.claude/CLAUDE.md`. Design authority: the approved docs in `../apichain-v2/` (00-06 + Phase 0 plan). Do not re-litigate settled decisions; if one looks wrong, surface it.

## Stack (settled, `05`)

- Python 3.12 (uv-managed, `uv.lock` committed; `uv run <cmd>` for everything, no manual venv).
- FastAPI, SQLAlchemy 2, psycopg 3, GeoAlchemy2, Alembic.
- keccak256 canonical hashing via `eth-hash` (NO web3, NO Solidity anywhere in v2).
- Dev stack: `docker compose up` (API + Postgres/PostGIS :5433 + MinIO + Mailpit). This replaces the v1 venv/taskkill routine.

## Key commands

```bash
docker compose up --build      # full dev stack
uv run pytest                  # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run alembic upgrade head    # migrations (baseline lands in Phase 1)
python scripts/export_openapi.py   # via uv run; regenerates openapi.json (CI gates freshness)
```

## Design principles (carried from v1 + v2 discovery, do not deviate)

1. **Hash determinism is non-negotiable.** Canonical payloads use `json.dumps(..., sort_keys=True, default=str)`; every DateTime entering a payload goes through `app/services/canonical.canonical_dt`. Locked by `tests/test_hash_determinism.py` including a v1 golden parity vector: if that test fails, the port broke, do not update the golden value.
2. **Never `Union[TypedModel, dict]` in Pydantic schemas** (v1 Sprint 9 lesson: smart-union silently routes typed payloads to dict).
3. **Alembic is the only path to schema change.** Downgrades required.
4. **PostGIS `Geography(POINT, 4326)`, point order `(longitude, latitude)`, never reversed.** GiST indexes on location columns.
5. **The server assigns audit position and row_hash** (Phase 1): clients never hash-chain.
6. **Idempotency-Key on every mutating endpoint** (Phase 1 middleware).
7. **Porting discipline:** read the v1 original before porting or adapting anything; record every port in `PORTING.md` with parity evidence; commit trailer `V1-Origin: <path>@<sha>`.
8. **openapi.json is generated, never hand-edited.** CI fails if it is stale.

## Phase status

Phase 0 (foundations) in progress: scaffold, canonical hashing port, containers, CI/CD, contract guardrails. Phase 1 adds: v2 schema baseline (`04` §5.2), 4-role auth, `audit_log` chain, consent records.
