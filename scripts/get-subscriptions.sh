#!/usr/bin/env bash
# Read Power BI subscriptions with plain curl (no Python needed). See ../README.md.
# Usage:
#   ./get-subscriptions.sh mine                      # my subscriptions with parameter values
#   ./get-subscriptions.sh report <numericReportId>  # all my subs on a paginated report (incl. schedule)
#   ./get-subscriptions.sh admin-user <aadUserId>    # admin API: subs a user owns/receives (no params)
set -euo pipefail

TOKEN=$(az account get-access-token --resource "https://analysis.windows.net/powerbi/api" --query accessToken -o tsv)
C="${PBI_CLUSTER:-$(curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.powerbi.com/powerbi/globalservice/v201606/clusterdetails" \
  | python -c "import json,sys; print(json.load(sys.stdin)['clusterUrl'])")}"

case "${1:-}" in
  mine)
    curl -s -H "Authorization: Bearer $TOKEN" "$C/metadata/emailsubscriptions" ;;
  report)
    curl -s -H "Authorization: Bearer $TOKEN" "$C/metadata/emailsubscriptions/paginatedReport/$2" ;;
  admin-user)
    # Requires Fabric admin. Follows continuation pages (the first page can be empty).
    URL="https://api.powerbi.com/v1.0/myorg/admin/users/$2/subscriptions"
    while [ -n "$URL" ]; do
      RESP=$(curl -s -H "Authorization: Bearer $TOKEN" "$URL")
      echo "$RESP" | python -c "import json,sys; d=json.load(sys.stdin); [print(json.dumps(e)) for e in d.get('SubscriptionEntities',[])]"
      URL=$(echo "$RESP" | python -c "import json,sys; print(json.load(sys.stdin).get('continuationUri') or '')")
    done ;;
  *)
    grep '^#   ' "$0"; exit 1 ;;
esac
