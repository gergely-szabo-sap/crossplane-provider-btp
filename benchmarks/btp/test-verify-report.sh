#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
"$root/verify-report.sh" "$root/tests/report-valid.json" >/dev/null
jq '.archives[0].k6_metrics += [
  {"source":"raw_k6","metric":"xp_measurement_phase","metric_type":"counter","sample_count":1,"sum":1,"tags":{"resource_kind":"SubaccountApiCredential","stage":"credential_readiness","field":"ready_false"}},
  {"source":"raw_k6","metric":"xp_measurement_phase","metric_type":"counter","sample_count":1,"sum":1,"tags":{"resource_kind":"SubaccountApiCredential","stage":"credential_readiness","field":"poll_last_forbidden"}},
  {"source":"raw_k6","metric":"xp_measurement_phase","metric_type":"counter","sample_count":1,"sum":1,"tags":{"resource_kind":"SubaccountApiCredential","stage":"credential_readiness","field":"private-condition-message"}},
  {"source":"raw_k6","metric":"xp_measurement_phase","metric_type":"counter","sample_count":1,"sum":1,"tags":{"resource_kind":"private-resource-name","stage":"credential_readiness","field":"ready_false"}},
  {"source":"raw_k6","metric":"xp_lifecycle_phase_duration","metric_type":"trend","sample_count":1,"finite_sample_count":1,"percentiles":{"p50":600000},"tags":{"resource_kind":"DirectoryEntitlement","stage":"readiness","outcome":"timeout","reason":"timeout"}},
  {"source":"raw_k6","metric":"xp_measurement_phase","metric_type":"counter","sample_count":1,"sum":1,"tags":{"resource_kind":"DirectoryEntitlement","stage":"create_request","field":"accepted"}},
  {"source":"raw_k6","metric":"xp_lifecycle_phase_duration","metric_type":"trend","sample_count":1,"finite_sample_count":1,"percentiles":{"p50":900000},"tags":{"resource_kind":"private-resource-name","stage":"readiness","outcome":"failure","reason":"private-error"}}
]' "$root/tests/report-valid.json" >"$tmp/phases.json"
GITHUB_STEP_SUMMARY="$tmp/phases-summary.md" "$root/verify-report.sh" "$tmp/phases.json" >"$tmp/phases.log"
grep -F '| DirectoryEntitlement | readiness | timeout | timeout | 1 | 600000 |' "$tmp/phases-summary.md" >/dev/null
grep -F '| DirectoryEntitlement | create_request | accepted | 1 |' "$tmp/phases-summary.md" >/dev/null
grep -F '| ready_false | 1 |' "$tmp/phases-summary.md" >/dev/null
grep -F '| poll_last_forbidden | 1 |' "$tmp/phases-summary.md" >/dev/null
if grep -F -e 'private-resource-name' -e 'private-error' -e 'private-condition-message' "$tmp/phases-summary.md" >/dev/null; then
  echo 'lifecycle phase summary leaked an unreviewed tag value' >&2
  exit 1
fi

reject() {
  local name=$1 expression=$2
  jq "$expression" "$root/tests/report-valid.json" >"$tmp/$name.json"
  if "$root/verify-report.sh" "$tmp/$name.json" >/dev/null 2>&1; then
    echo "unexpectedly accepted $name fixture" >&2
    exit 1
  fi
}
kinds=(Subaccount Directory Entitlement DirectoryEntitlement SubaccountApiCredential)
for kind in "${kinds[@]}"; do
  key=$(printf '%s' "$kind" | tr '[:upper:]' '[:lower:]')
  reject "${key}-missing-ready" ".archives[0].k6_metrics |= map(select(.metric != \"xp_time_to_ready\" or .tags.resource_kind != \"$kind\"))"
  reject "${key}-missing-delete" ".archives[0].k6_metrics |= map(select(.metric != \"xp_time_to_delete\" or .tags.resource_kind != \"$kind\"))"
  reject "${key}-censored" ".archives[0].k6_metrics |= map(if .metric == \"xp_time_to_ready\" and .tags.resource_kind == \"$kind\" then .censored = true else . end)"
  reject "${key}-nonfinite" ".archives[0].k6_metrics |= map(if .metric == \"xp_time_to_delete\" and .tags.resource_kind == \"$kind\" then .finite_sample_count = 0 | .non_finite_sample_count = 1 else . end)"
  reject "${key}-missing-create" ".archives[0].k6_metrics |= map(select(.metric != \"xp_operation_duration\" or .tags.resource_kind != \"$kind\" or .tags.operation != \"create\"))"
  reject "${key}-missing-delete-operation" ".archives[0].k6_metrics |= map(select(.metric != \"xp_operation_duration\" or .tags.resource_kind != \"$kind\" or .tags.operation != \"delete\"))"
done
reject multiple-archives '.archives += [.archives[0]]'
reject policy '.policy = {"filename":"policy.yaml"}'
reject checks '.checks = [{"status":"passed"}]'
reject wrong-status '.status = "passed"'

jq '.archives[0].k6_metrics |= map(select(.metric != "xp_time_to_ready" or .tags.resource_kind != "DirectoryEntitlement"))' \
  "$root/tests/report-valid.json" >"$tmp/directoryentitlement-missing-ready.json"
if GITHUB_STEP_SUMMARY="$tmp/summary.md" "$root/verify-report.sh" "$tmp/directoryentitlement-missing-ready.json" >"$tmp/failure.log" 2>&1; then
  echo 'unexpectedly accepted missing DirectoryEntitlement Ready evidence' >&2
  exit 1
fi
grep -F 'DirectoryEntitlement ready evidence: missing' "$tmp/failure.log" >/dev/null
grep -F '| DirectoryEntitlement | missing | ok | ok | ok |' "$tmp/summary.md" >/dev/null

echo 'Report verifier fixtures passed.'
