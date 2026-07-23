# apichain-backend

ApiChain v2 backend: FastAPI + PostgreSQL/PostGIS. Tamper-evidence via a
signed hash-chained `audit_log` with periodic Merkle-root anchoring to
Bitcoin through OpenTimestamps (no contracts, no keys). Part of the
Geo-AI and blockchain-enabled honey traceability system for Kenya's
smallholder honey value chain (JKUAT FYP, Ian Ndolo Mwau).

Design docs: `../apichain-v2/` (approved discovery documents 00-06 and the
Phase 0 plan). v1 reference: `../_v1-archive/ApiChain--Backend` (see
`PORTING.md` for lineage).

## Dev quickstart

```bash
cp .env.example .env
docker compose up --build
# API:            http://localhost:8000  (docs at /docs, health at /health)
# Postgres/PostGIS: localhost:5433, db apichain_v2 (apichain/apichain)
# MinIO console:   http://localhost:9001 (apichain / apichain-dev)
# Mailpit:         http://localhost:8025
```

No venv activation, no uvicorn zombie hunting: the container owns the
process tree. For a native inner loop instead: `uv run uvicorn app.main:app --reload`.

## Tests and gates

```bash
uv run pytest              # unit + (Phase 1+) integration tests
uv run ruff check .        # lint
uv run ruff format --check .
uv run mypy                # type-check app/
```

CI runs all of the above plus dependency audit, secret scan, and the
OpenAPI contract gates on every PR. Merges to main deploy to staging.

## Migrations

Alembic is the only way to change the schema. The v2 baseline revision is
authored in Phase 1; until then `alembic upgrade head` is a no-op.
