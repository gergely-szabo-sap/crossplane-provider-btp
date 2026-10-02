#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")" && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
"$root/verify-report.sh" "$root/tests/report-valid.json" >"$tmp/valid.log"
grep -F 'exactly five instances' "$tmp/valid.log" >/dev/null

jq '.archives[0].k6_metrics += [
  {"source":"raw_k6","metric":"xp_lifecycle_phase_duration","metric_type":"trend","sample_count":1,"finite_sample_count":1,"percentiles":{"p50":600000},"tags":{"resource_kind":"DirectoryEntitlement","stage":"readiness","outcome":"timeout","reason":"timeout"}},
  {"source":"raw_k6","metric":"xp_measurement_phase","metric_type":"counter","sample_count":1,"sum":1,"tags":{"resource_kind":"DirectoryEntitlement","stage":"create_request","field":"accepted"}},
  {"source":"raw_k6","metric":"xp_lifecycle_phase_duration","metric_type":"trend","sample_count":1,"finite_sample_count":1,"percentiles":{"p50":900000},"tags":{"resource_kind":"private-resource-name","stage":"readiness","outcome":"failure","reason":"private-error"}}
]' "$root/tests/report-valid.json" >"$tmp/phases.json"
if GITHUB_STEP_SUMMARY="$tmp/phases-summary.md" "$root/verify-report.sh" "$tmp/phases.json" >"$tmp/phases.log" 2>&1; then
  echo 'unexpectedly accepted recorded timeout evidence' >&2
  exit 1
fi
grep -F 'DirectoryEntitlement has recorded timeout outcome evidence' "$tmp/phases.log" >/dev/null
grep -F '| DirectoryEntitlement | readiness | timeout | timeout | 1 | 600000 |' "$tmp/phases-summary.md" >/dev/null
grep -F '| DirectoryEntitlement | create_request | accepted | 1 |' "$tmp/phases-summary.md" >/dev/null
if grep -F -e 'private-resource-name' -e 'private-error' -e 'private-condition-message' "$tmp/phases-summary.md" >/dev/null; then
  echo 'lifecycle phase summary leaked an unreviewed tag value' >&2
  exit 1
fi

reject_file() {
  local name=$1 file=$2
  if "$root/verify-report.sh" "$file" >"$tmp/$name.log" 2>&1; then
    echo "unexpectedly accepted $name fixture" >&2
    exit 1
  fi
}
reject() {
  local name=$1 expression=$2
  jq "$expression" "$root/tests/report-valid.json" >"$tmp/$name.json"
  reject_file "$name" "$tmp/$name.json"
}

kinds=(Subaccount Directory Entitlement DirectoryEntitlement SubaccountApiCredential)
for kind in "${kinds[@]}"; do
  key=$(printf '%s' "$kind" | tr '[:upper:]' '[:lower:]')
  for spec in 'xp_time_to_ready:ready' 'xp_time_to_delete:deleted' 'xp_operation_duration:create' 'xp_operation_duration:delete'; do
    metric=${spec%%:*}; operation=${spec#*:}
    selector=".metric == \"$metric\" and .tags.resource_kind == \"$kind\""
    [[ "$metric" != xp_operation_duration ]] || selector+=" and .tags.operation == \"$operation\" and .tags.outcome == \"success\""
    for count in 0 1 4 6; do
      reject "${key}-${operation}-count-${count}" ".archives[0].k6_metrics |= map(if $selector then .sample_count = $count | .finite_sample_count = $count else . end)"
    done
    reject "${key}-${operation}-fractional" ".archives[0].k6_metrics |= map(if $selector then .sample_count = 4.5 | .finite_sample_count = 4.5 else . end)"
    reject "${key}-${operation}-finite-mismatch" ".archives[0].k6_metrics |= map(if $selector then .finite_sample_count = 4 else . end)"
    reject "${key}-${operation}-missing-finite" ".archives[0].k6_metrics |= map(if $selector then del(.finite_sample_count) else . end)"
    reject "${key}-${operation}-wrong-source" ".archives[0].k6_metrics |= map(if $selector then .source = \"tsdb\" else . end)"
    reject "${key}-${operation}-wrong-type" ".archives[0].k6_metrics |= map(if $selector then .metric_type = \"counter\" else . end)"
    reject "${key}-${operation}-missing-summary" ".archives[0].k6_metrics |= map(if $selector then .percentiles.p50 = null else . end)"
    reject "${key}-${operation}-nonfinite" ".archives[0].k6_metrics |= map(if $selector then .finite_sample_count = 0 | .non_finite_sample_count = 5 else . end)"
    reject "${key}-${operation}-duplicate-group" ".archives[0].k6_metrics += [.archives[0].k6_metrics[] | select($selector)]"
  done
  reject "${key}-ready-censored" ".archives[0].k6_metrics |= map(if .metric == \"xp_time_to_ready\" and .tags.resource_kind == \"$kind\" then .censored = true else . end)"
  reject "${key}-missing-ready" ".archives[0].k6_metrics |= map(select(.metric != \"xp_time_to_ready\" or .tags.resource_kind != \"$kind\"))"
  reject "${key}-missing-delete" ".archives[0].k6_metrics |= map(select(.metric != \"xp_time_to_delete\" or .tags.resource_kind != \"$kind\"))"
  reject "${key}-failed-operation-with-success" ".archives[0].k6_metrics += [{\"source\":\"raw_k6\",\"metric\":\"xp_operation_duration\",\"metric_type\":\"trend\",\"sample_count\":1,\"finite_sample_count\":1,\"percentiles\":{\"p50\":10},\"tags\":{\"resource_kind\":\"$kind\",\"operation\":\"create\",\"outcome\":\"failure\",\"reason\":\"api_error\"}}]"
  reject "${key}-failed-phase" ".archives[0].k6_metrics += [{\"source\":\"raw_k6\",\"metric\":\"xp_lifecycle_phase_duration\",\"metric_type\":\"trend\",\"sample_count\":1,\"finite_sample_count\":1,\"percentiles\":{\"p50\":10},\"tags\":{\"resource_kind\":\"$kind\",\"stage\":\"readiness\",\"outcome\":\"timeout\",\"reason\":\"timeout\"}}]"
done
reject multiple-archives '.archives += [.archives[0]]'
reject policy '.policy = {"filename":"policy.yaml"}'
reject checks '.checks = [{"status":"passed"}]'
reject wrong-status '.status = "passed"'

jq '.archives[0].k6_metrics |= map(select(.metric != "xp_time_to_ready" or .tags.resource_kind != "DirectoryEntitlement"))' \
  "$root/tests/report-valid.json" >"$tmp/directoryentitlement-missing-ready.json"
reject_file directoryentitlement-missing-ready "$tmp/directoryentitlement-missing-ready.json"
grep -F 'DirectoryEntitlement ready evidence: missing' "$tmp/directoryentitlement-missing-ready.log" >/dev/null

printf '%s\n' 'Five-instance report verifier fixtures passed.'
