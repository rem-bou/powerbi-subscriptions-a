# Power BI Report Subscriptions via REST API

How to read, inspect, and **create** Power BI email subscriptions programmatically,
including paginated-report **parameter values**, which no documented API exposes.

Everything here was reverse-engineered from the app.powerbi.com portal and verified
against a live tenant. It is written so a person or an LLM session with
`az` CLI access can follow it.

> **Disclaimer:** the internal API described here is **undocumented and unsupported**.
> Microsoft can change it at any time. Test against a throwaway subscription first.

Ready-to-use scripts for everything below are in [`scripts/`](scripts/); see §9.

---

## 1. The two API surfaces

| Surface | Auth needed | Read | Write | Parameter values |
|---|---|---|---|---|
| **Documented Admin API** (`api.powerbi.com/v1.0/myorg/admin/...`) | Fabric/Power BI **tenant admin** | list subscriptions per user / per report, recipients, frequency, dates | none | **NO** |
| **Internal metadata API** (`{wabi-cluster}/metadata/emailsubscriptions...`) | Normal user, but only sees **subscriptions they own** | full objects incl. schedule + parameter values | create (POST), take-over (PUT), update (PUT), delete (DELETE) | **YES** |

Key consequence: to read or clone the parameters of a subscription, **you must own it**.
You can take ownership in the portal (report → Subscriptions → take over) or through
the internal API (`PUT .../emailsubscriptions/{numericId}` with an empty body).

The internal API is the one the app.powerbi.com portal itself calls. §7 explains how
the contract was extracted from the portal's JS bundles.

---

## 2. Prerequisites

```bash
# Sign in. Use your normal account for the internal API (it only shows YOUR subscriptions).
# Use an admin account only for the /admin/ endpoints.
az login --allow-no-subscriptions

# Token for every call below (both surfaces use the same resource):
TOKEN=$(az account get-access-token --resource "https://analysis.windows.net/powerbi/api" --query accessToken -o tsv)
```

`az login` replaces the cached account. If you switch between an admin account and a
normal account, you must log in again each time.

### Find your backend (WABI) cluster

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.powerbi.com/powerbi/globalservice/v201606/clusterdetails"
# → {"regionName":"<Region>","clusterUrl":"https://WABI-<REGION>-<X>-PRIMARY-redirect.analysis.windows.net"}
```

Store the `clusterUrl` as `$C`; every internal-API call below uses it:

```bash
C="https://WABI-<REGION>-<X>-PRIMARY-redirect.analysis.windows.net"
```

---

## 3. Reading subscriptions as tenant admin (documented API)

This works only with a Fabric admin account; a non-admin gets a plain `Unauthorized`.
It returns metadata and recipients but **never parameter values**.

### 3.1 Per user

```bash
# Resolve the user's Entra ID object id
az ad user list --filter "startswith(displayName,'<Display Name>')" \
  --query "[].{name:displayName, upn:userPrincipalName, id:id}" -o json

# Subscriptions the user OWNS or RECEIVES (paginated!)
az rest --method get \
  --url "https://api.powerbi.com/v1.0/myorg/admin/users/{userAadId}/subscriptions" \
  --resource "https://analysis.windows.net/powerbi/api"
```

**Pagination gotcha:** the first page can have an empty `SubscriptionEntities` together
with a `continuationUri`. Keep following `continuationUri` until it is absent; an empty
first page does not mean there are no results.

### 3.2 Per report (includes recipient list)

```bash
az rest --method get \
  --url "https://api.powerbi.com/v1.0/myorg/admin/reports/{reportObjectId}/subscriptions" \
  --resource "https://analysis.windows.net/powerbi/api"
```

Each entry has `title`, `owner`, `users` (recipients), `frequency`, `startDate`/`endDate`,
`isEnabled`, `linkToContent`, `previewImage`, `artifactType` (`PaginatedReport`) and `artifactId`.

### 3.3 Mapping a report to its workspace

```bash
az rest --method get \
  --url "https://api.powerbi.com/v1.0/myorg/admin/groups?\$filter=name eq '<Workspace Name>'&\$expand=reports&\$top=1" \
  --resource "https://analysis.windows.net/powerbi/api"
```

**App-copy gotcha:** subscriptions created from a published App live on the app copy of
the report. The app copy has its own GUID and is listed in the workspace as `[App] <name>`.
The workspace original has a different GUID; `originalReportObjectId` in the internal API
links the two.

---

## 4. Reading YOUR subscriptions with parameter values (internal API)

### 4.1 All my subscriptions, grouped by artifact

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$C/metadata/emailsubscriptions"
```

This returns a JSON **list** of groups: `{subscriptions: [...], reportInfo | rdlReportInfo | dashboardInfo}`.
It returns `null` if you own nothing. For paginated reports, each subscription has:

- `id` (numeric) and `objectId` (GUID, the same GUID the admin API shows)
- `rdlReports[0].runtimeParameters.reportParameters`: **the saved parameter values**, as
  `{name, dataType, prompt, visible, useDefaultValue, values: [{label, value}]}`
- `rdlReports[0].runtimeParameters.renderingExtension`: `EXCELOPENXML` for Excel, `PDF` for PDF
- `schedule`, which is **always null on this route**; use §4.2 to get the schedule.

`useDefaultValue: true` means the value is resolved fresh at send time (for example, a
Month parameter rolls forward each month). `false` means the value is pinned forever (a
pinned Month sends the same month until someone edits it).

### 4.2 All subscriptions on one paginated report: full objects incl. schedule

```bash
# {numericReportId} = rdlReportInfo.reportId from 4.1 (NOT the GUID)
curl -s -H "Authorization: Bearer $TOKEN" "$C/metadata/emailsubscriptions/paginatedReport/{numericReportId}"
```

This is the best read route. It gives the full schedule, recipients as
`users: [{userId, workspaceId}]` (numeric ids), and the current parameter state.
Sibling routes exist for other artifact types: `/report/{id}` (PBIX) and `/scorecard/{id}`.

Observed schedule shape (monthly, 08:30 local time):

```json
{"id": 123456, "type": 8, "interval": 32, "subdayType": 1,
 "subdayTimes": "[\"8:30:00\"]", "localTimeZoneId": "<Windows time zone id>",
 "startDate": "2026-01-01T00:00:00", "endDate": "2027-01-01T00:00:00"}
```

`subdayTimes` is a **JSON array serialised as a string**; keep it in exactly that form.
`localTimeZoneId` is a Windows time zone name (e.g. `UTC`, `Pacific Standard Time`).
The enum values are only partly decoded (see §5.8). When creating, clone them from an
existing subscription that already has the frequency you want.

### 4.3 Things that do NOT work

- `GET $C/metadata/emailsubscriptions/{id}` → 405 (the route accepts PUT/DELETE only)
- `GET $C/metadata/emailsubscriptions/{objectId GUID}` → 404 (numeric ids only)
- Query params such as `?artifactObjectId=` on the list route are ignored.
- Calling with an account that owns nothing returns `null`. There is **no admin
  variant**, and `?asAdmin=true` is ignored.

### 4.4 Take ownership of someone else's subscription

```bash
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Length: 0" \
  "$C/metadata/emailsubscriptions/{numericId}"
```

This is the call behind the portal's take-over button (`takeOverSubscription` in the
client JS). After a take-over, hidden user/security parameters **still contain the
previous owner's values**; see §6.

---

## 5. Creating a subscription (internal API)

### 5.1 Endpoint

```
POST {cluster}/metadata/emailsubscriptions
Content-Type: application/json
→ 201 Created, body = numeric subscription id
```

### 5.2 The reliable recipe: clone an existing subscription

Building the payload from scratch is fragile, so clone an existing one instead.

1. Call `GET $C/metadata/emailsubscriptions/paginatedReport/{numericReportId}` (§4.2)
   and pick a source subscription you own on the same report.
2. Build the create payload with **exactly** the fields below. The stored object also has
   server-computed fields (`owner`, `ownerId`, `personal`, `bypassRLS`, `provisionState`,
   `isContentUsable`). Some combinations of them are tolerated, but they appeared in
   failing payloads; the clean contract below is the one verified to work.
3. Change what you need (title, parameter values, schedule).
4. POST it and expect `201` plus a numeric id.
5. Verify with the same GET as step 1.

### 5.3 Verified working payload contract

```jsonc
{
  "enabled": true,
  "expirationTime": "2027-01-01T00:00:00",       // when the subscription stops
  "schedule": {
    "type": 8, "interval": 32, "subdayType": 1,  // clone from a source sub (§4.2)
    "recurrenceFactor": 0, "relativeInterval": 0,
    "localTimeZoneId": "<Windows time zone id>",
    "startDate": "2026-01-01T00:00:00",
    "endDate": "2027-01-01T00:00:00",
    "subdayTimes": "[\"8:30:00\"]",              // JSON-in-a-string, keep format
    "isSubscriptionSchedule": true
  },
  "users": [],                                    // leave empty; recipients go in subscribees
  "title": "My subscription name",
  "subject": null,                                // email subject (null = default)
  "message": null,                                // email body text
  "subscribees": ["recipient@contoso.com"],       // recipient emails (or Entra object ids)
  "subscribeesWithMetadata": [],
  "LinkToContent": true,                          // include link to the report
  "AccessToReport": false,                        // grant recipients access (sharing)
  "dataDriven": false, "dataDrivenMetaData": null,
  "subscriptionMetaData": null,
  "rdlReports": [{
    "rdlReportId": 1234567,                       // NUMERIC report id
    "reportObjectId": "00000000-0000-0000-0000-000000000000",  // report GUID, REQUIRED (see 5.4)
    "runtimeParameters": {
      "renderingExtension": "EXCELOPENXML",       // or "PDF"
      "includePreview": false,                    // preview image in email body
      "reportParameters": [
        {"name": "Month", "dataType": "DateTime", "prompt": "Select Month",
         "visible": true, "useDefaultValue": true, "valueType": "Static",
         "queryReference": null,
         "values": [{"label": "Jan-2026", "value": "1/31/2026 12:00:00 AM"}]}
        // ...one entry per report parameter, ALL of them, hidden ones included.
        // Clone the full list from the source subscription and edit only what changes.
      ]
    }
  }]
}
```

### 5.4 The 400 trap

A wrong payload returns **`400` with an empty body** (`Content-Type: application/octet-stream`,
header text "The request could not be processed due to missing or invalid information"),
with no error details at all. The fix that turned the 400s into 201s was adding
**`rdlReports[0].reportObjectId`** (the report GUID). If you get a 400:

1. Check that `reportObjectId` is present and is the GUID of the **same** report as `rdlReportId`.
2. Strip server-computed fields and match the §5.3 contract exactly.
3. Keep `subdayTimes` as a string, not an array.
4. Omit `id`, `objectId` and `schedule.id` on a create (the server assigns them).

### 5.5 Update / delete

**Edit is `PUT $C/metadata/emailsubscriptions`**: the COLLECTION route, with no id in the
URL. Send the §5.3 create contract **plus `id`, `objectId` and `schedule.id`**, which
select the subscription and its schedule row. Changes to `enabled`, `schedule.subdayTimes`
and the parameter list were all verified to apply (`updateSubscription` in web.min.js).
A wrong body returns `400 PowerBIEntityFrameworkOperationException`, for example if you
send the raw read-back object. Strip the server fields and use the §5.3 shape.

**Trap:** `PUT $C/metadata/emailsubscriptions/{numericId}` is take-over ONLY (§4.4).
Even with a body, it only takes over: it returns 200, silently ignores the whole body,
and may rewrite the schedule row id. If an edit "succeeds but changes nothing", you used
this route.

`DELETE $C/metadata/emailsubscriptions/{numericId}` removes a subscription. It needs the
owner's token.

### 5.6 Creating as a different owner (service account)

The owner is **whoever's token makes the POST**. To create subscriptions owned by a
service account, `az login` as that account and run the same create.

The owner must be able to **see the report the payload points at**. A create against an
app copy returns `404 PowerBIEntityNotFound` (not 403) until the account has the
**published app installed**. Workspace Viewer alone is not enough, and Viewers can't even
list the `[App] ...` copy in the workspace. To fix it, add the account to the app
audience if needed, then open the app once in a browser signed in as that account.

### 5.7 Send now (instant run)

This is the portal's per-subscription "Send now" button:

```
POST {cluster}/metadata/emailsubscriptions/instant
→ 200, empty body; render + email happen async server-side
```

Verified working payload (from `getEmailInfo` in web.SubscriptionPane.min.js). All values
come from the §4.2 GET plus the `rdlReportInfo` block of the §4.1 list route
(`contentProviderKey`, `originalReportObjectId`):

```jsonc
{
  "message": null,                                // email body override
  "recipients": ["recipient@contoso.com"],        // plain email list
  "contentProviderKey": "<rdlReportInfo.contentProviderKey or null>",
  "contentProviderCategory": null,                // null is accepted
  "share": true,                                  // = LinkToContent
  "subject": null,
  "subscriptionTitle": "My subscription name",
  "subscriptionOwnerFullName": "<owner display name>",
  "subscriptionOwnerEmailAddress": "owner@contoso.com",
  "subscriptionObjectId": "<subscription GUID>",
  "rdlReportEmailInfo": {
    "reportName": "<report name>",
    "reportObjectId": "<report GUID>",
    "originalReportObjectId": "<rdlReportInfo.originalReportObjectId or null>",
    "userObjectId": "<caller Entra oid>",         // az ad signed-in-user show --query id
    "workspaceId": 1                              // literally hardcoded 1 in the portal JS
  },
  "subscriptionData": {"subscriptionId": 123456, "dataDriven": false}
}
```

There are two different throttling limits; don't confuse them:

- **Create route**: a burst limit. You get 429 "Retry in 1 seconds" after about 30 rapid
  calls. Pace calls about 3 s apart and retry immediately; it clears in seconds.
- **Instant route**: a hard quota of **about 15 sends per user per hour**, after which
  every call returns `429 ActivityLimitReached ("PostInstantEmail throttled for subscription ...")`
  until the window resets. Short backoffs don't help: wait about 10 minutes between
  rounds, and expect a batch of several dozen emails to take a few hours. Refresh the token
  between rounds too, or a run longer than an hour dies with `403 TokenExpired`.

### 5.8 Rewriting the stored parameter snapshot (report gained a parameter)

A subscription stores a **snapshot of the full parameter list** when it is created. If
the RDL later gains a parameter, existing subscriptions keep the old list and never pick
up the new one, so the snapshot has to be rewritten. There are two ways, both verified:

| | Route |
|---|---|
| Edit in place | `PUT /metadata/emailsubscriptions` (collection route, §5.5) |
| Recreate | POST (§5.3), then DELETE (§5.5) |

Subscriptions **cannot be repointed** at a different report. Moving between the app copy
and the workspace copy is therefore always a recreate, which makes recreate the better
option when you need to do both at once.

A clone verified this way round-tripped the schedule, several hundred security-parameter
values, the recipients and the rendering extension, and the new parameters landed in RDL
declaration order.

Gotchas:

- **`enabled` is ignored on create.** The clone came back `enabled: false` from an
  `enabled: true` source. Re-enable it afterwards with the §5.5 collection-route PUT, or
  the subscription will never fire on its schedule. Instant sends (§5.7) still work while
  a subscription is disabled, so a successful send-now is **not** evidence that the
  schedule is live.
- **Schedule decoding (partially confirmed):** for `type 8` (monthly), `interval` is a
  day-of-month bitmask. `interval 32` (2^5) fires on day 6, at the `subdayTimes` clock time
  in `localTimeZoneId`. If you have many subscriptions on one report, stagger them by
  varying `subdayTimes` (e.g. batches of 5 per 15-minute slot). Dozens of simultaneous
  renders have produced slow deliveries with mangled parameters.
- **The workspace copy has no content provider.** In its `rdlReportInfo`, both
  `contentProviderKey` and `originalReportObjectId` are `null`, and the §5.7 instant
  payload must pass them as `null`. Only the app copy carries the app's `contentProviderKey`.
- **No app-install prerequisite on the workspace copy.** The §5.6 404 trap applies only to
  app copies; a plain workspace create returns 201.
- **Pinned values are validated against the CURRENT report at render time.** If a new RDL
  version changes a parameter's valid values (e.g. from names to codes), pinned values in
  the snapshot become invalid and every send produces an `rsParametersNotSpecified` error
  mail instead of the report. Fix it in the subscription, not the report: set the
  dataset-driven hidden parameters to `useDefaultValue: true` so they re-resolve from the
  report's own datasets at each send. Keep any user-identity parameter pinned so the
  security context stays stable.
- **Recipients get a workspace link** when the subscription is on the workspace copy, not
  an app link, so they need at least Viewer on the workspace for `LinkToContent` to resolve.
- To get a report's numeric id from its GUID, call
  `GET $C/metadata/report/{guid}` and read `.report.id` (`/metadata/artifacts/{guid}` is 404).

---

## 6. Semantics & gotchas

- **`useDefaultValue`**: `true` means the value is resolved at each send (a Month rolls
  forward, a list parameter follows the report's default list). `false` means it is frozen
  at whatever `values` holds. A subscription with Month pinned sends that month **forever**.
- **Hidden parameters travel too.** Hidden parameters (for example, a user-email parameter
  used for row-level filtering, or security lists derived from it) are stored per
  subscription like any other parameter. They are a **snapshot of the creator's security
  context**, so two users' subscriptions on the same report can carry very different
  values. Taking ownership does NOT refresh them.
- **`bypassRLS: true`** on stored subscriptions means the email renders with the stored
  parameter snapshot, not the new owner's live security. Re-check the content after a take-over.
- **Recipients**: the internal API stores them as numeric `users` pairs. On create, pass
  emails in `subscribees` instead and leave `users` empty. The owner is not implicitly a
  recipient; include yourself in `subscribees` if you want the email.
- **Admin `users/{id}/subscriptions`** returns subscriptions the user owns **and** ones
  they only receive. Check `owner` to tell them apart.
- **Two-accounts problem**: use an admin account for `/admin/` reads and a normal account
  for anything parameter-related. The internal API has no admin mode.

---

## 7. Where the contract came from (for future reverse-engineering)

The portal front end is public JS, and its API client code is readable:

```bash
# 1. Bundle list
curl -sL "https://app.powerbi.com/home" | grep -oE 'https?://[^"]+\.js'
# main bundle: .../resource/powerbiwfe/scripts/web.min.<hash>.js  (~10 MB)

# 2. Lazy chunks (the subscription pane is lazy-loaded)
curl -s "https://app.powerbi.com/<version>/scripts/hash-manifest.js"
# → "scripts/web.SubscriptionPane.min.<hash>.js"

# 3. Grep for the client class
#    web.min.js        → endpoint="metadata/emailsubscriptions", createSubscription (POST),
#                        takeOverSubscription (PUT null), getRdlReportSubscription,
#                        generic payload builder (schedule/users/subscribees/title/...)
#    web.SubscriptionPane.min.js → RDL builder: rdlReports=[{rdlReportId,
#                        parametersBlobInfoId, runtimeParameters, reportObjectId}]
```

Grep targets that paid off: `emailsubscriptions`, `subscribeesWithMetadata`, `rdlReports`.
Use Python string slicing rather than `grep -oE '.{1500}...'`; the bundles are huge
single-line files and grep times out on them.

---

## 8. Quick reference

| Thing | Where to get it |
|---|---|
| WABI cluster URL | `GET https://api.powerbi.com/powerbi/globalservice/v201606/clusterdetails` (§2) |
| Token resource (both surfaces) | `https://analysis.windows.net/powerbi/api` |
| Report numeric id | `rdlReportInfo.reportId` (§4.1) or `GET $C/metadata/report/{guid}` → `.report.id` |
| App `contentProviderKey` / `originalReportObjectId` | `rdlReportInfo` on the §4.1 list route (null on workspace copies) |
| Your Entra object id | `az ad signed-in-user show --query id -o tsv` |

---

## 9. Scripts

Everything in [`scripts/`](scripts/) uses the Python standard library only (Python 3.8+)
and authenticates through the Azure CLI. Run `az login --allow-no-subscriptions` first, as
the account that owns (or will own) the subscriptions. The WABI cluster is discovered
automatically; set `PBI_CLUSTER` or pass `--cluster` to override it.

Reports are always identified by their **GUID** (from the report URL, or from
`list_subscriptions.py`). The scripts look up the numeric id, app-copy
`contentProviderKey` and other details themselves.

**Every script that writes is a dry run by default.** It prints the plan; add `--execute`
to make the changes. Scripts that change existing subscriptions write a
`subs-backup-<timestamp>.json` file first.

| Script | What it does | README |
|---|---|---|
| `list_subscriptions.py` | Lists the reports you own subscriptions on, or one report's subscriptions with schedule, recipients and (`--params`) saved parameter values. `--json` dumps the raw objects. | §4 |
| `get-subscriptions.sh` | The same reads with plain `curl`, plus the admin per-user listing with pagination. | §3, §4 |
| `create_subscriptions_from_csv.py` | Creates one subscription per CSV row by cloning a template subscription you own, overriding the title, recipients, send time and any parameter values. | §5.2–5.3 |
| `clone_subscriptions.py` | Clones subscriptions onto another report (e.g. app copy → workspace copy) or the same report, with optional parameter changes, staggered send times, an enable pass and `--delete-source`. | §5.8 |
| `update_subscription_params.py` | Rewrites stored parameter snapshots in place from a spec file, and/or enables or disables subscriptions. `--restore` rolls back from a backup. | §5.5, §5.8 |
| `send_subscriptions_now.py` | "Send now" for one or many subscriptions, working in rounds around the hourly quota. | §5.7 |
| `pbisubs.py` | Shared client and payload builders used by all of the above. | |

Typical workflow:

```bash
cd scripts

# 1. Find the report GUID and a subscription to use as a template
python list_subscriptions.py
python list_subscriptions.py --report <reportGUID> --params

# 2. Plan, then create, a batch of subscriptions from a CSV
python create_subscriptions_from_csv.py --report <reportGUID> --template-id <subId> \
    --csv examples/subscriptions.example.csv
python create_subscriptions_from_csv.py --report <reportGUID> --template-id <subId> \
    --csv my-subs.csv --execute

# 3. Test-send one of them to yourself
python send_subscriptions_now.py --report <reportGUID> --ids <newSubId> \
    --recipients me@contoso.com --execute
```

**CSV format** (`create_subscriptions_from_csv.py`): a `title` column is required.
`recipients` (`;`-separated) and `send_time` (`H:MM`, in the template's time zone) are
optional. Add one `param:<Name>` column per parameter to override. A cell can hold
`value`, `value|label`, several values separated by `;`, or `@default` (resolve at each
send). An empty cell keeps the template's value. See
[`examples/subscriptions.example.csv`](scripts/examples/subscriptions.example.csv).

**Parameter spec** (`clone_subscriptions.py --param-spec`, `update_subscription_params.py`):
`set` inserts each parameter after the named `after` parameter, or overwrites it if it
already exists. `refresh` flips the listed parameters to `useDefaultValue: true`. See
[`examples/param-spec.example.json`](scripts/examples/param-spec.example.json).
