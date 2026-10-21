"""List the subscriptions you own, with schedules and saved parameter values.

The internal API only returns subscriptions owned by the signed-in account (README 4).

Usage:
  python list_subscriptions.py                         # every report you own subs on
  python list_subscriptions.py --report <reportGUID>   # full detail for one paginated report
  python list_subscriptions.py --report <reportGUID> --params
  python list_subscriptions.py --report <reportGUID> --json > subs.json
"""
import argparse
import json

from pbisubs import Client, describe_param, params_of, recipients_of


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", help="paginated report GUID (app or workspace copy)")
    ap.add_argument("--params", action="store_true", help="show saved parameter values")
    ap.add_argument("--json", action="store_true", help="dump raw API objects")
    ap.add_argument("--cluster", help="WABI cluster URL (default: auto-discover)")
    args = ap.parse_args()
    api = Client(args.cluster)

    if not args.report:
        groups = api.my_subscriptions()
        if args.json:
            print(json.dumps(groups, indent=1))
            return
        if not groups:
            print("You own no subscriptions.")
            return
        print("%-38s %-9s %-5s %s" % ("report GUID", "numeric", "subs", "name"))
        for g in groups:
            info = g.get("rdlReportInfo") or g.get("reportInfo") or g.get("dashboardInfo") or {}
            kind = "rdl" if g.get("rdlReportInfo") else "pbix" if g.get("reportInfo") else "dash"
            print("%-38s %-9s %-5d %s (%s)%s" % (
                info.get("reportObjectId") or info.get("objectId") or "-",
                info.get("reportId") or info.get("id") or "-",
                len(g["subscriptions"]), info.get("displayName") or "?", kind,
                "  [app copy]" if info.get("contentProviderKey") else ""))
        print("\nUse --report <GUID> on a paginated (rdl) report for schedules and parameters.")
        return

    report = api.resolve_report(args.report)
    subs = sorted(api.report_subscriptions(report["numericId"]), key=lambda s: s["id"])
    if args.json:
        print(json.dumps(subs, indent=1))
        return
    print("%s  (numeric %d, %s)  %d subscription(s)\n" % (
        report["displayName"], report["numericId"],
        "app copy" if report["contentProviderKey"] else "workspace copy", len(subs)))
    for s in subs:
        sch = s.get("schedule") or {}
        fmt = s["rdlReports"][0]["runtimeParameters"]["renderingExtension"]
        print("%d  %s  %s" % (s["id"], "ON " if s["enabled"] else "OFF", s["title"]))
        print("      %s %s | type %s interval %s | until %s | %s" % (
            sch.get("subdayTimes"), sch.get("localTimeZoneId"), sch.get("type"),
            sch.get("interval"), (s.get("expirationTime") or "")[:10], fmt))
        print("      to: %s" % (", ".join(recipients_of(s)) or "-"))
        if args.params:
            for p in params_of(s):
                print("        %s" % describe_param(p))


if __name__ == "__main__":
    main()
