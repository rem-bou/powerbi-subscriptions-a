"""Trigger the portal's "Send now" for subscriptions you own on a paginated report (README 5.7).

The instant route allows roughly 15 sends per user per hour, then returns
429 ActivityLimitReached until the window resets. So this works in rounds: try every
pending subscription, wait (default 10 min), repeat. The token is refreshed as needed.

Instant sends also work on disabled subscriptions, so a successful send-now does not
prove the schedule is live (README 5.8).

Usage:
  python send_subscriptions_now.py --report <GUID>                     # dry run
  python send_subscriptions_now.py --report <GUID> --ids 123,124 --execute
  python send_subscriptions_now.py --report <GUID> --recipients me@contoso.com --execute
"""
import argparse
import time

from pbisubs import (Client, HTTPFailure, instant_body, log, parse_ids, parse_list,
                     recipients_of, signed_in_user)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="paginated report GUID")
    ap.add_argument("--ids", help="comma-separated subscription ids (default: all)")
    ap.add_argument("--skip-ids", help="comma-separated subscription ids to exclude")
    ap.add_argument("--limit", type=int, help="send at most N")
    ap.add_argument("--recipients", help="comma-separated override (default: each sub's own)")
    ap.add_argument("--round-wait-min", type=int, default=10)
    ap.add_argument("--max-rounds", type=int, default=24)
    ap.add_argument("--execute", action="store_true", help="actually send (default: dry run)")
    ap.add_argument("--cluster", help="WABI cluster URL (default: auto-discover)")
    args = ap.parse_args()
    api = Client(args.cluster)

    report = api.resolve_report(args.report)
    owner = signed_in_user()
    override = parse_list(args.recipients)
    log("owner: %s <%s> | report: %s (%s)" % (
        owner["name"], owner["upn"], report["displayName"],
        "app copy" if report["contentProviderKey"] else "workspace copy"))

    subs = api.report_subscriptions(report["numericId"])
    want, skip = parse_ids(args.ids), parse_ids(args.skip_ids) or set()
    pending = sorted([s for s in subs if (not want or s["id"] in want) and s["id"] not in skip],
                     key=lambda s: s["id"])[:args.limit]
    log("%d subscription(s) to send" % len(pending))

    if not args.execute:
        for s in pending:
            print("  PLAN: send %d %s -> %s" % (s["id"], s["title"],
                                                ", ".join(override or recipients_of(s))))
        print("\nSummary: 0 sent (DRY RUN - rerun with --execute)")
        return

    sent, dead = [], []
    for rnd in range(1, args.max_rounds + 1):
        if not pending:
            break
        log("round %d: %d pending" % (rnd, len(pending)))
        still = []
        for s in pending:
            try:
                api.send_now(instant_body(s, report, owner, override))
                sent.append(s["id"])
                log("SENT %d: %s" % (s["id"], s["title"]))
            except HTTPFailure as e:
                if e.code == 429:
                    still.append(s)          # hourly quota - retry next round
                else:
                    dead.append((s, e))
                    log("FAILED %d %s: %s" % (s["id"], s["title"], e))
            time.sleep(3)
        pending = still
        if pending and rnd < args.max_rounds:
            log("quota hit, %d left - waiting %d min" % (len(pending), args.round_wait_min))
            time.sleep(args.round_wait_min * 60)

    log("Summary: %d sent, %d failed, %d unsent (quota)" % (len(sent), len(dead), len(pending)))
    for s in pending:
        print("  UNSENT %d %s" % (s["id"], s["title"]))


if __name__ == "__main__":
    main()
