"""Clone subscriptions you own from one paginated report onto another (or the same) report.

Subscriptions cannot be repointed at a different report, so moving them (e.g. from the
[App] copy to the workspace copy, README 3.3 / 5.8) is create-then-delete. Each clone keeps
the source's schedule, recipients, format and full parameter snapshot; an optional
parameter spec can add/overwrite parameters on the way.

Send times can be staggered so dozens of renders don't fire at once, and an enable pass
runs after creation because the create route ignores `enabled` (README 5.8).

Usage:
  python clone_subscriptions.py --source <GUID> --target <GUID>                # dry run
  python clone_subscriptions.py --source <GUID> --target <GUID> --ids 123 \
      --recipients me@contoso.com --title-suffix " (TEST)" --execute           # trial one
  python clone_subscriptions.py --source <GUID> --target <GUID> --execute
  python clone_subscriptions.py --source <GUID> --target <GUID> --execute --delete-source
  python clone_subscriptions.py --source <GUID> --target <GUID> --param-spec spec.json --execute
"""
import argparse
import time

from pbisubs import (Client, HTTPFailure, apply_param_spec, create_body, load_param_spec, log,
                     params_of, parse_ids, parse_list, stagger_time, update_body, write_backup)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="source paginated report GUID")
    ap.add_argument("--target", help="target report GUID (default: same as source)")
    ap.add_argument("--ids", help="comma-separated source subscription ids (default: all)")
    ap.add_argument("--recipients", help="comma-separated override (default: clone source)")
    ap.add_argument("--title-suffix", default="", help="appended to each cloned title")
    ap.add_argument("--param-spec", help="JSON parameter spec to apply to each clone")
    ap.add_argument("--minimal", action="store_true", help="trimmed param shape if you get 400s")
    ap.add_argument("--start-time", default=None,
                    help="stagger: first send time H:MM (default: keep each source's time)")
    ap.add_argument("--batch-size", type=int, default=5, help="stagger: subs per time slot")
    ap.add_argument("--step-min", type=int, default=15, help="stagger: minutes between slots")
    ap.add_argument("--no-enable", action="store_true", help="skip the post-create enable pass")
    ap.add_argument("--delete-source", action="store_true",
                    help="delete each source subscription after its clone is created")
    ap.add_argument("--backup-dir", default=".")
    ap.add_argument("--execute", action="store_true", help="actually create (default: dry run)")
    ap.add_argument("--cluster", help="WABI cluster URL (default: auto-discover)")
    args = ap.parse_args()
    api = Client(args.cluster)

    source = api.resolve_report(args.source)
    target = api.resolve_report(args.target) if args.target else source
    spec = load_param_spec(args.param_spec)
    recipients = parse_list(args.recipients)

    subs = api.report_subscriptions(source["numericId"])
    ids = parse_ids(args.ids)
    if ids:
        subs = [s for s in subs if s["id"] in ids]
    subs = sorted(subs, key=lambda s: s["id"])
    if not subs:
        log("no matching subscriptions visible to this account (owner-only API, README 4)")
        return
    existing = {s["title"] for s in api.report_subscriptions(target["numericId"])}
    log("%d sub(s) on '%s' -> '%s' (%s)" % (
        len(subs), source["displayName"], target["displayName"],
        "app copy" if target["contentProviderKey"] else "workspace copy"))

    todo = []
    for i, s in enumerate(subs):
        times = stagger_time(i, args.start_time, args.batch_size, args.step_min) \
            if args.start_time else None
        body = create_body(s, target["numericId"], target["objectId"],
                           title=s["title"] + args.title_suffix, recipients=recipients,
                           report_parameters=apply_param_spec(params_of(s), spec, args.minimal),
                           subday_times=times)
        skip = body["title"] in existing
        print("  %s %d -> %s | at %s | %s" % (
            "SKIP (title exists on target):" if skip else "PLAN:", s["id"], body["title"],
            body["schedule"]["subdayTimes"], ", ".join(body["subscribees"])))
        if not skip:
            todo.append((s, body))

    if not args.execute:
        print("\nSummary: 0 created (DRY RUN - rerun with --execute)")
        return

    log("backup of source subscriptions: %s" % write_backup(subs, args.backup_dir))
    created, failed = [], []
    for s, body in todo:
        try:
            new_id = api.create(body)
            created.append(new_id)
            log("CREATED %d: %s" % (new_id, body["title"]))
            if args.delete_source:
                api.delete(s["id"])
                log("DELETED source %d" % s["id"])
        except HTTPFailure as e:
            failed.append((body["title"], e))
            log("FAILED %s: %s" % (body["title"], e))
        time.sleep(3)
    log("Summary: %d created, %d failed" % (len(created), len(failed)))

    if created and not args.no_enable:
        log("enable pass: %d subscription(s)" % len(created))
        for s in api.report_subscriptions(target["numericId"]):
            if s["id"] in created and not s["enabled"]:
                try:
                    api.update(update_body(s, target["objectId"], params_of(s), enabled=True))
                    log("ENABLED %d %s" % (s["id"], s["title"]))
                except HTTPFailure as e:
                    log("ENABLE FAILED %d: %s" % (s["id"], e))
                time.sleep(3)


if __name__ == "__main__":
    main()
