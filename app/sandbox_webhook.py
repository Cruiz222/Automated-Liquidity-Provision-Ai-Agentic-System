"""Play the payment provider against a LOCAL sandbox-live server (there is no real bank behind it):

    export DEX_WEBHOOK_SECRET=...            # the same secret the server was started with
    python3 -m app.sandbox_webhook deposit --reference D1234ABCD --amount 500000000
    python3 -m app.sandbox_webhook payout  --withdrawal-id "wd:..." --outcome completed

Builds a JSON event, signs it with HMAC-SHA256 exactly as a real provider would, and posts it to the server.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

from .live import sign_event


def build_event(args: argparse.Namespace) -> tuple[str, dict]:
    event_id = args.event_id or "sbx-" + secrets.token_hex(6)
    if args.command == "deposit":
        return "payments", {"id": event_id, "type": "deposit.confirmed", "reference": args.reference,
                            "amount_minor": args.amount, "currency": "NGN"}
    kind = "payout.completed" if args.outcome == "completed" else "payout.failed"
    return args.source, {"id": event_id, "type": kind, "withdrawal_id": args.withdrawal_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8010")
    parser.add_argument("--event-id", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    dep = sub.add_parser("deposit")
    dep.add_argument("--reference", required=True)
    dep.add_argument("--amount", type=int, required=True, help="kobo")
    pay = sub.add_parser("payout")
    pay.add_argument("--withdrawal-id", required=True)
    pay.add_argument("--outcome", choices=("completed", "failed"), required=True)
    pay.add_argument("--source", choices=("payments", "chain"), default="payments", help="payments for NGN, chain for USDT")
    args = parser.parse_args(argv)
    secret = os.environ.get("DEX_WEBHOOK_SECRET", "")
    if len(secret) < 32:
        print("set DEX_WEBHOOK_SECRET to the server's webhook secret (32+ characters)", file=sys.stderr)
        return 2
    source, event = build_event(args)
    body = json.dumps(event).encode()
    request = urllib.request.Request(f"{args.url}/api/webhooks/{source}", data=body, method="POST",
                                     headers={"Content-Type": "application/json", **sign_event(secret, body)})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(response.status, response.read().decode())
        return 0
    except urllib.error.HTTPError as error:
        print(error.code, error.read().decode())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
