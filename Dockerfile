FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  UV_COMPILE_BYTECODE=1 \
  UV_LINK_MODE=copy \
  UV_PROJECT_ENVIRONMENT=/app/.venv \
  PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
  && apt-get install --no-install-recommends -y ca-certificates curl \
  && curl --fail --silent --show-error --location \
    https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    --output /etc/ssl/certs/rds-global-bundle.pem \
  && python -m pip install --no-cache-dir uv==0.12.19 \
  && apt-get purge -y --auto-remove curl \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts

RUN addgroup --system --gid 10001 rutas \
  && adduser --system --uid 10001 --ingroup rutas --home /app rutas \
  && chown -R rutas:rutas /app

USER rutas
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log", "--no-server-header"]
