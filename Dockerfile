# Cloud image for the black-whole.com deploy (discovery-only).
#
# The web service is stateless: it reads/writes Supabase Postgres and serves the
# public site + admin dashboard. The discovery scrape uses GovDeals' maestro
# JSON API (plain HTTP) — it NEVER launches Chromium on the default path — so we
# deliberately DO NOT run `playwright install`. That keeps the image small and
# avoids the browser's system-library footprint. The heavy publish pipeline
# (Playwright + FB/eBay) stays on the operator's Mac.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LISTING_WEB_HOST=0.0.0.0 \
    LISTING_WEB_RELOAD=0

WORKDIR /app

# Install deps first for layer caching. `openai` (from the extractors group) is
# pulled in explicitly because the discovery scraper's optional quantity-refine
# step can use the OpenAI API; everything else it needs is in base deps.
COPY pyproject.toml README.md ./
COPY automation/ ./automation/
COPY auction_extractors/ ./auction_extractors/
COPY deals/ ./deals/
COPY recorder/ ./recorder/
COPY scripts/ ./scripts/
COPY run.py ./

RUN pip install --upgrade pip && \
    pip install . openai

# Render injects $PORT; the app reads LISTING_WEB_PORT. Default for local runs.
ENV LISTING_WEB_PORT=8765
EXPOSE 8765

# Web service default. The cron job overrides this command (see render.yaml).
CMD ["sh", "-c", "LISTING_WEB_PORT=${PORT:-8765} python -m automation.web"]
