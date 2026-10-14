FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /srv
ENV UV_PROJECT_ENVIRONMENT=/srv/.venv
# So `python scripts/<name>.py` can import `app` (scripts sit beside it).
ENV PYTHONPATH=/srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
# The two operator scripts a deployment needs (13 D3): the first admin, and the
# root admin's recovery. Dev-only scripts (seeds, the live walk) stay out.
COPY scripts/create_admin.py scripts/reset_root.py ./scripts/
RUN useradd --create-home apichain && chown -R apichain /srv
USER apichain
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
