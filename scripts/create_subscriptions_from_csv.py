"""Create many subscriptions on one paginated report from a CSV, cloning a template.

The template is a subscription you already own on the report. Each new subscription
copies its schedule, format and full parameter snapshot (hidden ones included), then
applies the overrides from its CSV row. Cloning is far more reliable than building
payloads from scratch (README 5.2).

CSV columns (see examples/subscriptions.example.csv):
  title         required, subscription name (rows whose title already exists are skipped)
  recipients    optional, ';'-separated emails (default: the template's recipients)
  send_time     optional, H:MM in the template's time zone (default: template's time)
  param:<Name>  optional, value for report parameter <Name>:
                  value                   single value, label = value
                  value|label             single value with a display label
                  v1|l1;v2|l2             multi-value
                  @default                useDefaultValue=true (resolved at each send)
                  (empty)                 keep the template's value

Usage:
  python create_subscriptions_from_csv.py --report <GUID> --template-id <subId> --csv subs.csv
  python create_subscriptions_from_csv.py --report <GUID> --template-id <subId> --csv subs.csv --execute
"""
import argparse
import copy
import csv
import time

from pbisubs import (Client, HTTPFailure, create_body, log, params_of, recipients_of,
                     update_body)


def parse_values(cell):
    out = []
    for item in cell.split(";"):
        value, _, label = item.strip().partition("|")
        out.append({"label": label or value, "value": value})
    return out


def apply_row(params, row):
    known = {p["name"] for p in params}
    for col, cell in row.items():
        if not col or not col.startswith("param:") or cell is None or not cell.strip():
            continue
        name = col[len("param:"):]
        if name not in known:
            raise SystemExit("CSV column %r: report has no parameter %r (have: %s)"
                             % (col, name, ", ".join(sorted(known))))
        for p in params:
            if p["name"] == name:
                if cell.strip() == "@default":
                    p["useDefaultValue"] = True
                else:
                    p["useDefaultValue"] = False
                    p["values"] = parse_values(cell)
    return params


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="paginated report GUID")
    ap.add_argument("--template-id", type=int, required=True,
                    help="numeric id of a subscription you own on this report")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--execute", action="store_true", help="actually create (default: dry run)")
    ap.add_argument("--cluster", help="WABI cluster URL (default: auto-discover)")
    args = ap.parse_args()
    api = Client(args.cluster)

    report = api.resolve_report(args.report)
    subs = api.report_subscriptions(report["numericId"])
    template = next((s for s in subs if s["id"] == args.template_id), None)
    if not template:
        raise SystemExit("template %d not found on report %s - you must own it (README 4)"
                         % (args.template_id, report["displayName"]))
    existing = {s["title"] for s in subs}

    with open(args.csv, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    log("%d row(s); template %d '%s' on %s" % (len(rows), template["id"], template["title"],
                                             report["displayName"]))

    todo = []
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title:
            raise SystemExit("every CSV row needs a title")
        recips = [r.strip() for r in (row.get("recipients") or "").split(";") if r.strip()] \
            or recipients_of(template)
        send_time = (row.get("send_time") or "").strip()
        times = '["%s:00"]' % send_time if send_time else None
        params = apply_row(copy.deepcopy(params_of(template)), row)
        body = create_body(template, report["numericId"], report["objectId"], title=title,
                           recipients=recips, report_parameters=params, subday_times=times)
        if title in existing:
            print("  SKIP (exists): %s" % title)
            continue
        overrides = ["%s=%s" % (c[6:], v) for c, v in row.items()
                     if c and c.startswith("param:") and v and v.strip()]
        print("  PLAN: %s | %s | %s -> %s" % (title, body["schedule"]["subdayTimes"],
                                             "; ".join(overrides) or "no overrides",
                                             ", ".join(recips)))
        todo.append(body)

    if not args.execute:
        print("\nSummary: 0 created (DRY RUN - rerun with --execute)")
        return

    created, failed = [], []
    for body in todo:
        try:
            new_id = api.create(body)
            created.append(new_id)
            log("CREATED %d: %s" % (new_id, body["title"]))
        except HTTPFailure as e:
            failed.append((body["title"], e))
            log("FAILED %s: %s" % (body["title"], e))
        time.sleep(3)
    log("Summary: %d created, %d failed" % (len(created), len(failed)))

    # The create route ignores `enabled` (README 5.8): new subs come back disabled.
    if created and template["enabled"]:
        log("enable pass: %d subscription(s)" % len(created))
        for s in api.report_subscriptions(report["numericId"]):
            if s["id"] in created and not s["enabled"]:
                try:
                    api.update(update_body(s, report["objectId"], params_of(s), enabled=True))
                    log("ENABLED %d %s" % (s["id"], s["title"]))
                except HTTPFailure as e:
                    log("ENABLE FAILED %d: %s" % (s["id"], e))
                time.sleep(3)


if __name__ == "__main__":
    main()
