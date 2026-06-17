# Production image for the Streamlit Scanner App (DEPLOY-001).
# Beginner note: a Dockerfile is a recipe Docker replays top-to-bottom to build a
# self-contained image. Each instruction adds a cached layer, so the *order* below
# is deliberate (rarely-changing steps first) to keep rebuilds fast. The
# docs/architecture/components/deployment-runtime.md LLD explains the design.

# Slim Debian + Python 3.11 — 3.11 is the CI/deploy target; "slim" drops build
# tooling we don't need at runtime, keeping the image small.
FROM python:3.11-slim-bookworm

# Image-wide environment. The first three are Python/pip hygiene (no .pyc files,
# unbuffered stdout so logs stream immediately, no pip download cache). The rest
# are the app's *fail-closed* production defaults: a deployed container demands
# real auth + config, writes all runtime data under /data, and binds Streamlit to
# every interface on 8501 in headless mode. A local smoke test overrides
# APP_ENV/AUTH_REQUIRED at `docker run` time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    AUTH_REQUIRED=true \
    DATA_DIR=/data \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# All subsequent paths are relative to /app (so app.py lives at /app/app.py).
WORKDIR /app

# Create a non-root user to run the app (least privilege). We pre-create /data and
# the user's home and hand them to appuser so the running process can write its
# volume and Streamlit's cache without root. Render exposes Docker secret files
# through group 1000, so appuser joins that group while still running non-root.
RUN groupadd --system appuser \
    && useradd --system --gid appuser --create-home --home-dir /home/appuser appuser \
    && (getent group 1000 || groupadd --gid 1000 render-secrets) \
    && usermod -a -G 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /home/appuser

# Install runtime dependencies BEFORE copying the source. Dependencies change far
# less often than code, so this layer stays cached across edits to app.py. Only
# the pinned runtime set is installed (constraints.txt locks exact versions);
# dev tools and the optional TA-Lib/pandas_ta accelerators are left out.
COPY requirements.txt constraints.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt -c constraints.txt

# Now copy the application source (respecting .dockerignore, which excludes
# secrets and generated state). --chown lands the files owned by appuser.
COPY --chown=appuser:appuser . .

# Drop root for everything that follows, including the CMD process.
USER appuser

# Document the port the container serves on (also lets `docker run -P` map it).
EXPOSE 8501

# Let Docker mark the container healthy/unhealthy by polling Streamlit's built-in
# health endpoint. urllib raises on a non-200 / no response, so the `python -c`
# exits non-zero and Docker counts a failed probe (3 retries before "unhealthy").
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD python -c "import sys, urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).read(); sys.exit(0)"

# Start the web server directly. The repo's plain-Python launcher is for local
# use (it prefetches data, then opens a browser) — wrong for a container, which
# should just become a server. `streamlit run` does exactly that.
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
