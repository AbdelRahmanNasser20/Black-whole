# Playwright base ships Chromium + every system lib it needs. We add only xvfb.
# Pin the tag to a version compatible with the repo's playwright>=1.40.
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HEADLESS=0

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install "playwright>=1.40" google-cloud-storage
RUN playwright install chromium

# Copy only the helper + probe files (no repo secrets, small image).
COPY auction_extractors/govdeals_playwright_helpers.py /app/govdeals_playwright_helpers.py
COPY probe/probe_govdeals.py /app/probe_govdeals.py
COPY probe/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
