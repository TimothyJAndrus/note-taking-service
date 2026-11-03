# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY --from=build /install /usr/local
COPY app ./app
COPY migrations ./migrations
# Fixed non-root UID so the ECS task (and a read-only root filesystem) can rely on it.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
# One process per container; scale by adding tasks (keeps Prometheus metrics per-process and simple).
# --proxy-headers: trust X-Forwarded-* from the load balancer (the container is only reachable through it).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
