import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _root = Path(__file__).parent.parent
    load_dotenv(_root / ".env")
    # Pick up Telegram + Ollama keys from the auction_extractors .env so we
    # don't have to dupe them across two files.
    load_dotenv(_root / "auction_extractors" / ".env", override=False)
except ImportError:
    pass


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Supabase Postgres connection string for the shared `blackwhole` DB. Consumed
# by automation/db.py. URL is loaded from .env above.
BLACKWHOLE_DB_URL = os.getenv("BLACKWHOLE_DB_URL")

HOME = Path.home()

DOWNLOAD_ROOT = Path(os.getenv(
    "LISTING_DOWNLOAD_ROOT",
    HOME / "Desktop" / "Banquet chiars Pictures",
))

STATE_ROOT = HOME / ".listing_automation"
# Chrome profile holding the FB + eBay sessions. Overridable via
# LISTING_CHROME_PROFILE so the pipeline can reuse another project's profile
# (e.g. the facebook-crm poller's chrome_profile/). When pointed at a shared
# profile, the lock handling in browser.py refuses to kill a live owner so a
# running poller is never taken down.
CHROME_PROFILE = Path(os.getenv("LISTING_CHROME_PROFILE") or (STATE_ROOT / "chrome_profile"))
# True when the profile is borrowed from elsewhere (not our own state dir).
CHROME_PROFILE_SHARED = bool(os.getenv("LISTING_CHROME_PROFILE"))
LOG_DIR = STATE_ROOT / "logs"
SCRATCH_DIR = STATE_ROOT / "scratch"
ATTACHMENTS_ROOT = STATE_ROOT / "attachments"

# Dewatermark idempotency + accounting
API_CACHE_DIR = STATE_ROOT / "api_cache"
USAGE_LOG_PATH = LOG_DIR / "dewatermark_usage.jsonl"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Hard ceilings on dewatermark.ai API spend. Override via .env.
MAX_API_CALLS_PER_RUN = _env_int("MAX_API_CALLS_PER_RUN", 50)
MAX_API_CALLS_PER_DAY = _env_int("MAX_API_CALLS_PER_DAY", 250)
# Ships OFF. Set DEWATERMARK_OFFLINE=1 in .env to suppress real API calls during tests.
DEWATERMARK_OFFLINE = _env_bool("DEWATERMARK_OFFLINE", False)

# ── new-inventory alert blast (BLACKWHOLE-10) ───────────────────────────────
# SEND IS OFF BY DEFAULT. The blast job runs dry-run (log/preview only) until
# BOTH of these are set: ALERTS_SEND_ENABLED=1 and a registered provider name.
# No provider is picked yet, so leaving these unset is the safe production
# default — nothing is emailed.
ALERTS_SEND_ENABLED = _env_bool("ALERTS_SEND_ENABLED", False)
ALERTS_EMAIL_PROVIDER = os.getenv("ALERTS_EMAIL_PROVIDER", "")  # "" => dry-run
# Default match radius for a subscriber with no explicit radius_miles (the
# shipped `subscribers` table has no radius column yet — see migration 002).
# 0 anywhere / interest-only.
ALERTS_DEFAULT_RADIUS_MILES = _env_int("ALERTS_DEFAULT_RADIUS_MILES", 100)
# Daily send ceiling (same spirit as the CRM's MAX_SENDS_PER_DAY). Overflow is
# left un-sent for the next run.
MAX_ALERTS_PER_DAY = _env_int("MAX_ALERTS_PER_DAY", 100)
# CAN-SPAM footer + envelope. From/reply-to default to a black-whole.com address
# but are overridable; postal address is required copy for a real send.
ALERTS_FROM_EMAIL = os.getenv("ALERTS_FROM_EMAIL", "alerts@black-whole.com")
ALERTS_REPLY_TO = os.getenv("ALERTS_REPLY_TO", "")  # operator Gmail (PRD §8)
ALERTS_POSTAL_ADDRESS = os.getenv("ALERTS_POSTAL_ADDRESS", "")

DEFAULT_PRICE_PER_CHAIR = 20
FB_PACKAGE_WEIGHT_LB = 12
FB_PACKAGE_WEIGHT_OZ = 0
FB_SHIPPING_CARRIER = "USPS Ground Advantage ($17.76)"
FB_CATEGORY = "Dining Chairs"
FB_CONDITION = "Used (Good)"

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

FACEBOOK_BUSINESS_URL = os.getenv("FACEBOOK_BUSINESS_URL", "")

# Canonical public origin for the customer-facing site — canonical <link>
# tags, absolute og:image URLs, sitemap.xml entries. No trailing slash.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://black-whole.com").rstrip("/")

# Google Search Console ownership token. When set, public pages render
# <meta name="google-site-verification" content="...">. See docs/seo.md.
GOOGLE_SITE_VERIFICATION = os.getenv("GOOGLE_SITE_VERIFICATION", "")

DEWATERMARK_API_KEY = os.getenv("DEWATERMARK_API_KEY")
DEWATERMARK_API_URL = "https://platform.dewatermark.ai/api/object_removal/v2/erase_watermark"

# Supabase Storage — durable listing images (BLACKWHOLE-6). Shared `listing-images`
# bucket in the LIVE `blackwhole` project, one source of truth with the CRM.
# NOTE: deliberately separate from SUPABASE_URL/ANON above, which may point at a
# stale project — uploads MUST use these vars. Unset => uploads silently skipped
# and the pipeline falls back to local-disk serving (dev-friendly).
SUPABASE_STORAGE_URL = os.getenv("SUPABASE_STORAGE_URL")
SUPABASE_STORAGE_KEY = os.getenv("SUPABASE_STORAGE_KEY")
LISTING_IMAGES_BUCKET = os.getenv("LISTING_IMAGES_BUCKET", "listing-images")

CAROUSEL_STABLE_CHECKS = 3
CAROUSEL_MAX_CLICKS = 40
PAGE_LOAD_WAIT_MS = 4000

for p in (DOWNLOAD_ROOT, STATE_ROOT, CHROME_PROFILE, LOG_DIR, SCRATCH_DIR, API_CACHE_DIR, ATTACHMENTS_ROOT):
    p.mkdir(parents=True, exist_ok=True)
