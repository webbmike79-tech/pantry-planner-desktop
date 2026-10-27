FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
    && update-ca-certificates --fresh \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir libsql-experimental libsql-client \
    && pip install --no-cache-dir --upgrade "certifi>=2026.10.23"

COPY --chown=user:user . /app
USER user

ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}"]
