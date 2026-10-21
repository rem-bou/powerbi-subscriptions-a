"""Rewrite the stored parameter snapshot of existing subscriptions in place.

A subscription stores a full snapshot of the report's parameter list at create time.
When the RDL gains a parameter, or a parameter's valid values change, existing
subscriptions don't pick it up (README 5.8). This edits them with the collection-route
PUT (README 5.5), driven by a parameter spec file (examples/param-spec.example.json).

Every --execute run writes a backup first; --restore puts the old snapshots back.

Usage:
  python update_subscription_params.py --report <GUID> --param-spec spec.json            # dry run
  python update_subscription_params.py --report <GUID> --param-spec spec.json --ids 123 --execute
  python update_subscription_params.py --report <GUID> --param-spec spec.json --execute
  python update_subscription_params.py --report <GUID> --enable --execute                # just enable all
  python update_subscription_params.py --report <GUID> --restore subs-backup-....json --execute
"""
import argparse
import json
import time

from pbisubs import (Client, HTTPFailure, apply_param_spec, load_param_spec, log, params_of,
                     parse_ids, update_body, write_backup)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="paginated report GUID the subs are on")
    ap.add_argument("--param-spec", help="JSON parameter spec (set/refresh)")
    ap.add_argument("--ids", help="comma-separated subscription ids (default: all)")
    ap.add_argument("--enable", action="store_true", help="also set enabled=true")
    ap.add_argument("--disable", action="store_true", help="also set enabled=false")
    ap.add_argument("--minimal", action="store_true", help="trimmed param shape if you get 400s")
    ap.add_argument("--restore", help="backup file written by a previous --execute run")
    ap.add_argument("--backup-dir", default=".")
    ap.add_argument("--execute", action="store_true", help="actually update (default: dry run)")
    ap.add_argument("--cluster", help="WABI cluster URL (default: auto-discover)")
    args = ap.parse_args()
    if not (args.param_spec or args.enable or args.disable or args.restore):
        ap.error("nothing to do: pass --param-spec, --enable/--disable or --restore")
    api = Client(args.cluster)
    report = api.resolve_report(args.report)
    enabled = True if args.enable else False if args.disable else None

    if args.restore:
        with open(args.restore, encoding="utf-8") as fh:
            subs = json.load(fh)
        log("restoring %d subscription(s) from %s" % (len(subs), args.restore))
        for s in subs:
            if not args.execute:
                print("  PLAN restore %d %s (%d params)" % (s["id"], s["title"], len(params_of(s))))
                continue
            api.update(update_body(s, report["objectId"], params_of(s), enabled=s["enabled"]))
            log("RESTORED %d %s" % (s["id"], s["title"]))
            time.sleep(3)
        return

    spec = load_param_spec(args.param_spec)
    subs = api.report_subscriptions(report["numericId"])
    ids = parse_ids(args.ids)
    if ids:
        subs = [s for s in subs if s["id"] in ids]
    subs = sorted(subs, key=lambda s: s["id"])
    if not subs:
        log("no matching subscriptions visible to this account (owner-only API, README 4)")
        return
    log("%d subscription(s) targeted on '%s'" % (len(subs), report["displayName"]))

    set_names = {x["param"]["name"] for x in spec["set"]}
    for s in subs:
        before = {p["name"] for p in params_of(s)}
        notes = []
        if set_names - before:
            notes.append("add %s" % sorted(set_names - before))
        if set_names & before:
            notes.append("overwrite %s" % sorted(set_names & before))
        if set(spec["refresh"]) & before:
            notes.append("refresh %s" % sorted(set(spec["refresh"]) & before))
        if enabled is not None and s["enabled"] != enabled:
            notes.append("enabled -> %s" % enabled)
        print("  %d %s: %s" % (s["id"], s["title"], "; ".join(notes) or "no change"))

    if not args.execute:
        print("\nSummary: 0 updated (DRY RUN - rerun with --execute)")
        return

    path = write_backup(subs, args.backup_dir)
    log("backup written: %s  (roll back with --restore %s --execute)" % (path, path))
    ok, bad = 0, 0
    for s in subs:
        try:
            api.update(update_body(s, report["objectId"],
                                   apply_param_spec(params_of(s), spec, args.minimal), enabled))
            ok += 1
            log("UPDATED %d: %s" % (s["id"], s["title"]))
        except HTTPFailure as e:
            bad += 1
            log("FAILED %d %s: %s" % (s["id"], s["title"], e))
        time.sleep(3)
    log("Summary: %d updated, %d failed" % (ok, bad))


if __name__ == "__main__":
    main()
