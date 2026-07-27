FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 witness \
    && install -d --owner=witness --group=witness /var/lib/dyro-witness /var/lib/dyro-witness-records

USER witness
ENTRYPOINT ["dyro", "witness", "serve"]
