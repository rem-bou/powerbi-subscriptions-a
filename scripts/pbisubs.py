"""Shared helpers for the Power BI internal subscription API (see ../README.md).

Standard library only. Authentication comes from the Azure CLI: run `az login` first,
as the account that owns (or will own) the subscriptions.
"""
import copy
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

PBI_RESOURCE = "https://analysis.windows.net/powerbi/api"
CLUSTER_DISCOVERY = "https://api.powerbi.com/powerbi/globalservice/v201606/clusterdetails"
SUBS = "/metadata/emailsubscriptions"
TOKEN_TTL_S = 30 * 60


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# --- az CLI ---------------------------------------------------------------------------

def az(args):
    exe = shutil.which("az")
    if not exe:
        raise SystemExit("Azure CLI (az) not found on PATH")
    out = subprocess.run([exe] + args, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit("az %s failed:\n%s" % (" ".join(args[:2]), out.stderr.strip()))
    return out.stdout.strip()


def signed_in_user():
    """Owner fields follow whoever holds the token, so read them rather than hardcode."""
    return json.loads(az(["ad", "signed-in-user", "show", "--query",
                          "{id:id,name:displayName,upn:userPrincipalName}", "-o", "json"]))


# --- HTTP client ----------------------------------------------------------------------

class Client:
    """Thin client for the internal API. Discovers the WABI cluster and refreshes the
    token every 30 minutes so long runs don't die with 403 TokenExpired."""

    def __init__(self, cluster=None):
        self._token, self._token_at = None, 0
        self.cluster = (cluster or os.environ.get("PBI_CLUSTER") or self._discover()).rstrip("/")

    def token(self, force=False):
        if force or not self._token or time.time() - self._token_at > TOKEN_TTL_S:
            self._token = az(["account", "get-access-token", "--resource", PBI_RESOURCE,
                              "--query", "accessToken", "-o", "tsv"])
            self._token_at = time.time()
        return self._token

    def _discover(self):
        req = urllib.request.Request(CLUSTER_DISCOVERY,
                                     headers={"Authorization": "Bearer " + self.token()})
        return json.load(urllib.request.urlopen(req, timeout=60))["clusterUrl"]

    def request(self, method, path, body=None, burst_retries=5):
        """Returns the decoded response text. Retries the create-route burst limit
        (429 "Retry in 1 seconds", README 5.7); other HTTP errors are raised."""
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(burst_retries + 1):
            headers = {"Authorization": "Bearer " + self.token()}
            if data is not None:
                headers["Content-Type"] = "application/json"
            elif method in ("PUT", "POST"):
                headers["Content-Length"] = "0"
            req = urllib.request.Request(self.cluster + path, data=data, method=method,
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    return resp.read().decode()
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < burst_retries:
                    detail = e.read().decode(errors="replace")
                    if "ActivityLimitReached" in detail:   # hourly quota, not a burst
                        raise HTTPFailure(e.code, detail)
                    time.sleep(2 + attempt)
                    continue
                if e.code == 403 and attempt < burst_retries:
                    detail = e.read().decode(errors="replace")
                    if "TokenExpired" in detail:
                        self.token(force=True)
                        continue
                    raise HTTPFailure(e.code, detail)
                raise HTTPFailure(e.code, e.read().decode(errors="replace"))

    def get_json(self, path):
        text = self.request("GET", path)
        return json.loads(text) if text else None

    # --- subscription routes ------------------------------------------------------

    def my_subscriptions(self):
        """README 4.1: everything you own, grouped by artifact. No schedules."""
        return self.get_json(SUBS) or []

    def report_subscriptions(self, numeric_report_id):
        """README 4.2: full objects (schedule, recipients, parameters) on one report."""
        return self.get_json("%s/paginatedReport/%d" % (SUBS, numeric_report_id)) or []

    def create(self, body):
        """README 5.1. Returns the new numeric subscription id."""
        return int(self.request("POST", SUBS, body).strip().strip('"'))

    def update(self, body):
        """README 5.5: edit = PUT on the COLLECTION route; body id/objectId select the sub.
        Never PUT /{id} for an edit - that is take-over and ignores the body."""
        return self.request("PUT", SUBS, body)

    def take_over(self, sub_id):
        """README 4.4."""
        return self.request("PUT", "%s/%d" % (SUBS, sub_id))

    def delete(self, sub_id):
        return self.request("DELETE", "%s/%d" % (SUBS, sub_id))

    def send_now(self, body):
        """README 5.7. Quota ~15/user/hour; 429 ActivityLimitReached raises HTTPFailure."""
        return self.request("POST", SUBS + "/instant", body, burst_retries=0)

    # --- report lookup ------------------------------------------------------------

    def resolve_report(self, report_guid):
        """GUID -> everything the scripts need about a paginated report.

        numericId comes from /metadata/report/{guid}. contentProviderKey and
        originalReportObjectId only exist on app copies and are only exposed on the
        list route, so they are read from there when you already own a subscription
        on the report (None otherwise - correct for workspace copies)."""
        rep = self.get_json("/metadata/report/%s" % report_guid)["report"]
        info = {"numericId": rep["id"], "objectId": rep["objectId"],
                "displayName": rep.get("displayName") or rep.get("name"),
                "contentProviderKey": None, "originalReportObjectId": None}
        for group in self.my_subscriptions():
            rdl = group.get("rdlReportInfo") or {}
            if rdl.get("reportId") == info["numericId"]:
                info["contentProviderKey"] = rdl.get("contentProviderKey")
                info["originalReportObjectId"] = rdl.get("originalReportObjectId")
                break
        return info


class HTTPFailure(Exception):
    def __init__(self, code, detail):
        super().__init__("%d %s" % (code, detail[:300]))
        self.code, self.detail = code, detail


# --- payload builders -----------------------------------------------------------------

def recipients_of(sub):
    """Recipient emails of a subscription read from the per-report route."""
    out = [u.get("emailAddress") or u.get("userPrincipalName")
           for u in (sub.get("subscribeesWithMetadata") or [])]
    return [r for r in out if r]


def params_of(sub):
    return sub["rdlReports"][0]["runtimeParameters"]["reportParameters"]


def create_body(sub, report_numeric_id, report_guid, title=None, recipients=None,
                report_parameters=None, subday_times=None):
    """README 5.3 create contract, cloned from a stored subscription.

    sub must come from Client.report_subscriptions (the only route with schedules)."""
    rt = sub["rdlReports"][0]["runtimeParameters"]
    schedule = {k: v for k, v in sub["schedule"].items() if k != "id"}  # server assigns
    schedule.setdefault("recurrenceFactor", 0)
    schedule.setdefault("relativeInterval", 0)
    schedule["isSubscriptionSchedule"] = True
    if subday_times:
        schedule["subdayTimes"] = subday_times          # JSON-in-a-string, README 4.2
    return {
        "enabled": sub["enabled"],                       # ignored by the server, README 5.8
        "expirationTime": sub["expirationTime"],
        "schedule": schedule,
        "users": [],
        "title": title if title is not None else sub["title"],
        "subject": sub.get("subject"),
        "message": sub.get("message"),
        "subscribees": list(recipients if recipients is not None else recipients_of(sub)),
        "subscribeesWithMetadata": [],
        "LinkToContent": sub.get("LinkToContent", True),
        "AccessToReport": sub.get("AccessToReport", False),
        "dataDriven": False,
        "dataDrivenMetaData": None,
        "subscriptionMetaData": None,
        "rdlReports": [{
            "rdlReportId": report_numeric_id,
            "reportObjectId": report_guid,               # REQUIRED, README 5.4
            "runtimeParameters": {
                "renderingExtension": rt["renderingExtension"],
                "includePreview": rt.get("includePreview", False),
                "reportParameters": copy.deepcopy(
                    report_parameters if report_parameters is not None else rt["reportParameters"]),
            },
        }],
    }


def update_body(sub, report_guid, report_parameters=None, enabled=None):
    """README 5.5 edit contract: the create contract plus id, objectId and schedule.id."""
    body = create_body(sub, sub["rdlReports"][0]["rdlReportId"], report_guid,
                       report_parameters=report_parameters)
    body["id"] = sub["id"]
    body["objectId"] = sub["objectId"]
    body["schedule"]["id"] = sub["schedule"]["id"]
    if enabled is not None:
        body["enabled"] = enabled
    return body


def instant_body(sub, report, owner, recipients=None):
    """README 5.7 send-now payload. report = Client.resolve_report(...)."""
    return {
        "message": sub.get("message"),
        "recipients": list(recipients if recipients is not None else recipients_of(sub)),
        "contentProviderKey": report["contentProviderKey"],
        "contentProviderCategory": None,
        "share": sub.get("LinkToContent", True),
        "subject": sub.get("subject"),
        "subscriptionTitle": sub["title"],
        "subscriptionOwnerFullName": owner["name"],
        "subscriptionOwnerEmailAddress": owner["upn"],
        "subscriptionObjectId": sub["objectId"],
        "rdlReportEmailInfo": {
            "reportName": report["displayName"],
            "reportObjectId": report["objectId"],
            "originalReportObjectId": report["originalReportObjectId"],
            "userObjectId": owner["id"],
            "workspaceId": 1,                            # literally hardcoded 1 in the portal JS
        },
        "subscriptionData": {"subscriptionId": sub["id"], "dataDriven": False},
    }


# --- parameter snapshot edits ---------------------------------------------------------

def load_param_spec(path):
    """Parameter spec file (see examples/param-spec.example.json):
        {"set":     [{"after": "<existing param>", "param": {<full parameter object>}}],
         "refresh": ["<param name>", ...]}
    `set` inserts the parameter after `after` (or overwrites it if already present);
    `refresh` flips existing parameters to useDefaultValue=true (README 5.8)."""
    if not path:
        return {"set": [], "refresh": []}
    with open(path, encoding="utf-8") as fh:
        spec = json.load(fh)
    spec.setdefault("set", [])
    spec.setdefault("refresh", [])
    return spec


def apply_param_spec(params, spec, minimal=False):
    """Return a new parameter list with the spec applied, keeping RDL declaration order.
    minimal trims inserted parameters to the core descriptor set, a fallback if the
    full shape is rejected with a 400."""
    set_names = {s["param"]["name"] for s in spec["set"]}
    refresh = set(spec["refresh"])
    out = []
    for p in params:
        if p["name"] in set_names:
            continue
        p = copy.deepcopy(p)
        if p["name"] in refresh:
            p["useDefaultValue"] = True      # stored values become a dead fallback
        out.append(p)
    for s in spec["set"]:
        p = copy.deepcopy(s["param"])
        if minimal:
            p = {k: v for k, v in p.items() if k in ("name", "dataType", "useDefaultValue", "values")}
        idx = next((i for i, e in enumerate(out) if e["name"] == s.get("after")), len(out) - 1)
        out.insert(idx + 1, p)
    return out


def describe_param(p):
    vals = ", ".join(str(v.get("value")) for v in (p.get("values") or []))
    if len(vals) > 60:
        vals = vals[:57] + "..."
    return "%s=%s%s" % (p["name"], "<default>" if p.get("useDefaultValue") else "",
                        "" if p.get("useDefaultValue") else "[%s]" % vals)


# --- misc -----------------------------------------------------------------------------

def parse_ids(text):
    return {int(x) for x in text.split(",") if x.strip()} if text else None


def parse_list(text):
    return [x.strip() for x in text.split(",") if x.strip()] if text else None


def write_backup(subs, directory="."):
    """Write the raw subscription objects so a run can be inspected or rolled back."""
    path = os.path.join(directory, "subs-backup-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(subs, fh, indent=1)
    return path


def stagger_time(index, start, batch_size, step_min):
    """subdayTimes for the index-th subscription: batches of batch_size, step_min apart,
    so dozens of renders don't fire at the same instant (README 5.8)."""
    h, m = (int(x) for x in start.split(":"))
    total = h * 60 + m + (index // batch_size) * step_min
    return '["%d:%02d:00"]' % (total // 60 % 24, total % 60)
