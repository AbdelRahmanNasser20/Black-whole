import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

HOME = Path.home()

DOWNLOAD_ROOT = Path(os.getenv(
    "LISTING_DOWNLOAD_ROOT",
    HOME / "Desktop" / "Banquet chiars Pictures",
))

STATE_ROOT = HOME / ".listing_automation"
CHROME_PROFILE = STATE_ROOT / "chrome_profile"
LOG_DIR = STATE_ROOT / "logs"
SCRATCH_DIR = STATE_ROOT / "scratch"

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
MAX_API_CALLS_PER_RUN = _env_int("MAX_API_CALLS_PER_RUN", 5)
MAX_API_CALLS_PER_DAY = _env_int("MAX_API_CALLS_PER_DAY", 25)
# Ships ON. Set DEWATERMARK_OFFLINE=0 in .env to allow real API calls.
DEWATERMARK_OFFLINE = _env_bool("DEWATERMARK_OFFLINE", True)

DEFAULT_PRICE_PER_CHAIR = 20
FB_PACKAGE_WEIGHT_LB = 12
FB_PACKAGE_WEIGHT_OZ = 0
FB_SHIPPING_CARRIER = "USPS Ground Advantage ($17.76)"
FB_CATEGORY = "Dining Furniture Sets"
FB_CONDITION = "Used - Good"

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DEWATERMARK_API_KEY = os.getenv("DEWATERMARK_API_KEY")
DEWATERMARK_API_URL = "https://platform.dewatermark.ai/api/object_removal/v2/erase_watermark"

CAROUSEL_STABLE_CHECKS = 3
CAROUSEL_MAX_CLICKS = 40
PAGE_LOAD_WAIT_MS = 4000

for p in (DOWNLOAD_ROOT, STATE_ROOT, CHROME_PROFILE, LOG_DIR, SCRATCH_DIR, API_CACHE_DIR):
    p.mkdir(parents=True, exist_ok=True)
