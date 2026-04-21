import os
import signal
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from playwright.async_api import async_playwright, BrowserContext

from .config import CHROME_PROFILE

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SINGLETON_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _clear_stale_profile_lock() -> None:
    """Kill any Chromium holding this profile and remove the Singleton* files.

    Playwright's persistent_context refuses to launch if the profile directory
    has a SingletonLock from a prior crashed or backgrounded run. We detect a
    stale lock by reading its symlink target (hostname-PID) and killing that
    PID if it's still alive. Then we unlink all Singleton* markers.
    """
    lock = Path(CHROME_PROFILE) / "SingletonLock"
    if lock.is_symlink():
        try:
            target = os.readlink(lock)
            # SingletonLock format is "hostname-pid" — only try to kill if it matches
            if "-" in target:
                pid_str = target.rsplit("-", 1)[-1]
                if pid_str.isdigit():
                    pid = int(pid_str)
                    if 0 < pid < 2**31:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OverflowError):
                            pass
        except OSError:
            pass
    for name in SINGLETON_NAMES:
        p = Path(CHROME_PROFILE) / name
        try:
            p.unlink()
        except (FileNotFoundError, OSError):
            pass
    # Belt and suspenders: kill any Chrome for Testing still running
    try:
        subprocess.run(
            ["pkill", "-9", "-f", "chrome-mac-arm64/Google Chrome for Testing"],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass


@asynccontextmanager
async def persistent_context(headless: bool = False) -> BrowserContext:
    _clear_stale_profile_lock()
    async with async_playwright() as pw:
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
