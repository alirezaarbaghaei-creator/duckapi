# DuckAI2API - Railway-ready container
#
# Drives a REAL Google Chrome (not Playwright's bundled Chromium): Duck.ai
# fingerprints the bundled browser and hard-bans it with ERR_BN_LIMIT on the
# first request. See duckai.py's module docstring for the root-cause analysis.
#
# Railway injects PORT at runtime; main.py reads it via uvicorn --port $PORT.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Runtime deps for google-chrome-stable + the fonts/dicts headless Chrome needs.
# libnss3/libatk/etc. come from Chrome's own package list; the rest keep CJK and
# RTL pages from rendering as tofu boxes in the headless tab.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg fonts-liberation fonts-noto-color-emoji \
        fonts-noto-cjk fonts-noto-core \
        libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
        libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 libx11-6 libxcb1 \
        libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
        libxrandr2 xdg-utils wget \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome stable (real Chrome, the whole point of this image).
RUN install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # playwright install handles kernel-level deps for its own driver wiring;
    # the browser itself is the system Chrome above, NOT a download.
    && python -m playwright install-deps chromium || true

COPY main.py duckai.py dashboard.py dashboard.html tools.py toolrouter.py ./

# The container runs as root (Railway's default), so duckai.py auto-adds
# --no-sandbox; the env var makes it explicit and works for non-root runs too.
# PREWARM=1 + STANDBY=1 (default): boot warms a tab and a hot spare stays ready,
# so even the first request and every fresh-prompt send skip the challenge wait.
ENV DUCKAI_CHROME_NO_SANDBOX=1 \
    DUCKAI_PREWARM=1 \
    DUCKAI_STANDBY=1 \
    DUCKAI_STANDBY_COUNT=1 \
    DUCKAI_MAX_BROWSERS=2 \
    PORT=8080

# run.py is the local convenience entry (venv + dashboard open); in a container
# uvicorn IS the process, bound to Railway's dynamic $PORT.
EXPOSE 8080
CMD ["sh", "-c", "exec python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
