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

HOME = Path.home()

DOWNLOAD_ROOT = Path(os.getenv(
    "LISTING_DOWNLOAD_ROOT",
    HOME / "Desktop" / "Banquet chiars Pictures",
))

STATE_ROOT = HOME / ".listing_automation"
CHROME_PROFILE = STATE_ROOT / "chrome_profile"
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

DEWATERMARK_API_KEY = os.getenv("DEWATERMARK_API_KEY")
DEWATERMARK_API_URL = "https://platform.dewatermark.ai/api/object_removal/v2/erase_watermark"

CAROUSEL_STABLE_CHECKS = 3
CAROUSEL_MAX_CLICKS = 40
PAGE_LOAD_WAIT_MS = 4000

for p in (DOWNLOAD_ROOT, STATE_ROOT, CHROME_PROFILE, LOG_DIR, SCRATCH_DIR, API_CACHE_DIR, ATTACHMENTS_ROOT):
    p.mkdir(parents=True, exist_ok=True)
