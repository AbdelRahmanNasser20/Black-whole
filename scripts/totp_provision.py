#!/usr/bin/env python3
"""Print the otpauth:// provisioning URI for the dashboard's optional TOTP
second factor ("auth once", BLACKWHOLE-14).

Usage:
    python3 scripts/totp_provision.py            # use TOTP_SECRET from env/.env
    python3 scripts/totp_provision.py --new      # generate a fresh secret + URI

Enrollment (one time, only if you want a second factor):
  1. Run with --new, copy the printed TOTP_SECRET into the web service's env
     (Render → black-whole-web → Environment).
  2. Scan the otpauth:// URI (or type the secret) into any authenticator app
     (Google Authenticator, 1Password, Authy, ...).
  3. Restart the service. The first login per device now also asks for the
     6-digit code; after that the device is trusted for a year.

If TOTP_SECRET is unset on the server, the login flow skips TOTP entirely —
password + the 365-day session cookie is all you get (which is the point:
one login per device per year).
"""
from __future__ import annotations

import argparse
import os
import sys

import pyotp

try:  # optional — mirror how the app loads .env
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

ISSUER = "Black Whole"
ACCOUNT = "operator"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--new", action="store_true",
        help="generate a fresh secret instead of reading TOTP_SECRET",
    )
    args = ap.parse_args()

    if args.new:
        secret = pyotp.random_base32()
        print(f"TOTP_SECRET={secret}")
    else:
        secret = (os.environ.get("TOTP_SECRET") or "").strip()
        if not secret:
            print("TOTP_SECRET is not set. Run with --new to generate one.",
                  file=sys.stderr)
            return 1

    uri = pyotp.TOTP(secret).provisioning_uri(name=ACCOUNT, issuer_name=ISSUER)
    print(uri)
    print(f"\nCurrent code (sanity check): {pyotp.TOTP(secret).now()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
