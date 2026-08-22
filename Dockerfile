# Sponsor Job Agent -- production-oriented image (CLAUDE.md Phase 6 section
# 41). One image serves both roles via CMD/entrypoint override:
#   dashboard: uvicorn app.main:app ...  (this file's default CMD)
#   worker:    python -m app.workers.cli run ...
#
# Deliberately excludes (see .dockerignore): .env, candidate_data/, data/,
# output/ -- no candidate data or database files are ever baked into the
# image. Configuration (including DATABASE_URL) is supplied entirely via
# environment variables at `docker run`/compose time.

FROM python:3.12-slim AS base

# No compiler toolchain needed -- psycopg[binary] ships prebuilt wheels, and
# every other dependency is pure Python.
RUN useradd --create-home --uid 10001 sponsoragent

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY start.sh ./start.sh

# Runtime-only directories -- never populated from the build context (see
# .dockerignore); each deployment mounts/creates its own.
RUN mkdir -p /app/data /app/output /app/candidate_data \
    && chown -R sponsoragent:sponsoragent /app

USER sponsoragent

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
