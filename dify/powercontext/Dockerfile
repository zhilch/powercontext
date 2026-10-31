FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir "powercontext[cli,server]==1.2.0"

# Local demo defaults: SQLite storage, no auth, bound to all interfaces so Dify
# containers on the same compose network can call the HTTP API.
ENV POWERCONTEXT_SERVER_HTTP_HOST=0.0.0.0 \
    POWERCONTEXT_SERVER_HTTP_PORT=8000 \
    POWERCONTEXT_SERVER_DATABASE_KIND=sqlite \
    POWERCONTEXT_SERVER_DATABASE_URL=sqlite+aiosqlite:////data/powercontext.db \
    POWERCONTEXT_SERVER_ACCESS_MODE=disabled \
    POWERCONTEXT_SERVER_ALLOW_UNAUTHENTICATED_NON_LOOPBACK=true \
    POWERCONTEXT_SERVER_DASHBOARD_ENABLED=false

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=12 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"

CMD ["powercontext", "server", "run"]
