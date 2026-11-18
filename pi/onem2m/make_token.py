"""Create a bearer token for the CSE, and the line to put in ACME's token file.

ACME does not store tokens themselves: each line of its token file is the token's
HMAC-SHA256, keyed with the CSE's secret ([basic.config] secret in acme/acme.ini).

    python3 -m onem2m.make_token --secret <cse secret>            # new random token
    python3 -m onem2m.make_token --secret <cse secret> <token>    # hash an existing one

Give the token to the client (ONEM2M_TOKEN in the Pi's .env, or the app's settings) and append
the hash line to acme/certs/http_token_auth.txt, then restart acme-cse.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import secrets


def token_hash(token: str, secret: str) -> str:
    """What ACME compares against (acmecse.etc.Utils.hashString)."""
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--secret", required=True, help="the CSE secret from acme.ini")
    parser.add_argument("token", nargs="?", help="token to hash (default: generate a new one)")
    args = parser.parse_args()

    token = args.token or secrets.token_urlsafe(32)
    print(f"token (give to the client):     {token}")
    print(f"hash  (add to the token file):  {token_hash(token, args.secret)}")


if __name__ == "__main__":
    main()
