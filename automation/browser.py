import os
import signal
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
# Prefer Patchright (stealth fork) so we share the facebook-crm poller's engine
# and don't trip Facebook's bot checkpoint on its profile. Falls back to vanilla
# Playwright if patchright isn't installed — same pattern the poller uses.
try:
    from patchright.async_api import async_playwright, BrowserContext
    _ENGINE = "patchright"
except ImportError:  # pragma: no cover - fallback path
    from playwright.async_api import async_playwright, BrowserContext
    _ENGINE = "playwright"

from .config import CHROME_PROFILE, CHROME_PROFILE_SHARED

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SINGLETON_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")

# CDP endpoint of an already-running local browser — the facebook-crm poller,
# which now launches with --remote-debugging-port. When reachable we ATTACH to
# it and open our own tab in its logged-in, stealthed session: no second browser,
# no profile lock, no taking turns. Set LISTING_CDP_ENDPOINT="" to always launch
# our own browser instead.
CDP_ENDPOINT = os.getenv("LISTING_CDP_ENDPOINT", "http://localhost:9222")
CDP_CONNECT_TIMEOUT_MS = 3000


async def _try_connect_cdp(pw):
    """Return a Browser attached over CDP to a running local browser, or None.

    Fast-fails (short timeout) when nothing is listening so the launch fallback
    stays snappy. Never raises.
    """
    if not CDP_ENDPOINT:
        return None
    try:
        return await pw.chromium.connect_over_cdp(
            CDP_ENDPOINT, timeout=CDP_CONNECT_TIMEOUT_MS
        )
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OverflowError:
        return False


def _clear_stale_profile_lock() -> None:
    """Free this profile's SingletonLock so persistent_context can launch.

    Playwright refuses to launch if the profile has a SingletonLock from a prior
    run. The lock symlink target is "hostname-pid".

    Behavior depends on whether the profile is OURS or SHARED:
      • Own profile (default): a live owner is a prior crashed/backgrounded run
        of *this* pipeline — kill it (Gotcha #3) and clean up.
      • Shared profile (LISTING_CHROME_PROFILE set, e.g. the facebook-crm
        poller's): NEVER kill a live owner — that would take down the poller.
        Abort with a clear message and let the caller try again once the other
        process releases the profile. Only remove markers when the owner is dead.
    """
    lock = Path(CHROME_PROFILE) / "SingletonLock"
    owner_pid = None
    if lock.is_symlink():
        try:
            target = os.readlink(lock)
            if "-" in target:
                pid_str = target.rsplit("-", 1)[-1]
                if pid_str.isdigit():
                    pid = int(pid_str)
                    if 0 < pid < 2**31:
                        owner_pid = pid
        except OSError:
            pass

    if owner_pid is not None and _pid_alive(owner_pid):
        if CHROME_PROFILE_SHARED:
            raise RuntimeError(
                f"Chrome profile {CHROME_PROFILE} is in use by PID {owner_pid} "
                f"(likely the facebook-crm poller). A persistent profile allows "
                f"only one browser at a time — stop that process before running "
                f"the pipeline, or switch to a CDP-shared browser."
            )
        # Own profile: the live owner is our own stale run — reclaim it.
        try:
            os.kill(owner_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OverflowError):
            pass

    for name in SINGLETON_NAMES:
        p = Path(CHROME_PROFILE) / name
        try:
            p.unlink()
        except (FileNotFoundError, OSError):
            pass

    # Belt and suspenders: kill leftover Chrome for Testing — but ONLY for our
    # own profile. With a shared profile this could kill the poller's browser.
    if not CHROME_PROFILE_SHARED:
        try:
            subprocess.run(
                ["pkill", "-9", "-f", "chrome-mac-arm64/Google Chrome for Testing"],
                check=False, capture_output=True, timeout=5,
            )
        except Exception:
            pass


@asynccontextmanager
async def persistent_context(headless: bool = False) -> BrowserContext:
    async with async_playwright() as pw:
        # 1) Preferred: attach to a browser that's already running (the poller)
        #    and open our work in a NEW TAB of its logged-in session. No lock
        #    handling, no pausing, no second window.
        browser = await _try_connect_cdp(pw)
        if browser is not None:
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            pre_pages = set(ctx.pages)
            print(f"[browser] attached over CDP ({CDP_ENDPOINT}) — using the "
                  f"running browser's session in a new tab.")
            try:
                yield ctx
            finally:
                # Close ONLY the tabs we opened. Never close the shared context
                # or call browser.close() — that could kill the poller. Letting
                # async_playwright() exit just drops our CDP connection; the
                # remote browser keeps running.
                for pg in list(ctx.pages):
                    if pg not in pre_pages:
                        try:
                            await pg.close()
                        except Exception:
                            pass
            return

        # 2) Fallback: nothing to attach to — launch our own browser on our own
        #    profile (existing standalone behavior, with lock cleanup).
        _clear_stale_profile_lock()
        if _ENGINE == "patchright":
            # Patchright manages the automation fingerprint itself; overriding
            # user_agent or adding extra anti-detection args is a tell, so we
            # mirror the poller's minimal launch options for profile parity.
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE),
                headless=headless,
                viewport={"width": 1440, "height": 900},
                accept_downloads=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
        else:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE),
                headless=headless,
                viewport={"width": 1440, "height": 900},
                user_agent=USER_AGENT,
                accept_downloads=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        try:
            yield ctx
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
