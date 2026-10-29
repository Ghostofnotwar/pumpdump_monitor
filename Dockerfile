FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    DATABASE_URL=sqlite:////data/bot.db \
    SNAPSHOT_DIR=/data/snapshots \
    PORT=8000

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gosu ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt && pip check

COPY --chmod=755 entrypoint.sh ./
COPY *.py ./
COPY static/ ./static/
COPY templates/ ./templates/
RUN mkdir -p /data/snapshots && chown -R app:app /data

# The entrypoint fixes mounted-volume ownership, then drops root via gosu.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]
