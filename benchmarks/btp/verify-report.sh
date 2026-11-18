#!/usr/bin/env bash
set -euo pipefail

role=${BTP_VERIFY_ROLE:-Current}
if [[ "${1:-}" == --comparison ]]; then
  report=${2:?usage: verify-report.sh --comparison REPORT.json}
  if [[ ! -f "$report" || -L "$report" ]]; then
    echo 'benchmark report must be a regular, non-symlink JSON file' >&2
    exit 1
  fi
  if ! jq -e '
    (.archives | type == "array" and length == 2) and
    ([.archives[] | (.archive.run_id // .archive.runId // empty)] as $ids |
      ($ids | length == 2) and ($ids | unique | length == 2))
  ' "$report" >/dev/null 2>&1; then
    echo 'comparison report must contain baseline and current archives with distinct run identities' >&2
    exit 1
  fi
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  jq '.archives = [.archives[0]]' "$report" >"$tmp/baseline.json"
  jq '.archives = [.archives[1]]' "$report" >"$tmp/current.json"
  baseline_status=0
  current_status=0
  BTP_VERIFY_ROLE=Baseline "$0" "$tmp/baseline.json" || baseline_status=$?
  BTP_VERIFY_ROLE=Current "$0" "$tmp/current.json" || current_status=$?
  (( baseline_status == 0 && current_status == 0 ))
  exit $?
fi
if [[ "${1:-}" == --role ]]; then
  role=${2:?missing role}
  report=${3:?missing report}
else
  report=${1:?usage: verify-report.sh [--comparison] REPORT.json}
fi
if [[ ! -f "$report" || -L "$report" ]]; then
  echo 'benchmark report must be a regular, non-symlink JSON file' >&2
  exit 1
fi

if ! contract_data="$(jq -r '
  def metrics:
    (.archives[0].k6_metrics // []) | if type == "array" then . else [] end;
  def lifecycle($kind; $metric):
    [metrics[] | select(.metric == $metric and .tags.resource_kind == $kind)];
  def metric_status($samples; $metric_type; $check_censored):
    if ($samples | length) == 0 then "missing"
    elif ($samples | length) != 1 then "duplicate_groups"
    elif $samples[0].source != "raw_k6" then "source_mismatch"
    elif $samples[0].metric_type != $metric_type then "metric_type_mismatch"
    elif $samples[0].sample_count != 5 or
         $samples[0].finite_sample_count != 5 or
         (($samples[0].non_finite_sample_count // 0) != 0) then "sample_count_invalid"
    elif $check_censored and $samples[0].censored == true then "censored"
    elif (($samples[0].percentiles.p50 | type) != "number") then "finite_value_missing"
    elif (($samples[0].percentiles.p50 | isfinite) | not) then "finite_value_missing"
    else "ok" end;
  def trend_status($kind; $metric):
    metric_status(lifecycle($kind; $metric); "trend"; true);
  def operation_status($kind; $operation):
    [metrics[] | select(.metric == "xp_operation_duration" and
      .tags.resource_kind == $kind and .tags.operation == $operation and
      .tags.outcome == "success")] as $successes
    | [metrics[] | select(.metric == "xp_operation_duration" and
      .tags.resource_kind == $kind and .tags.operation == $operation and
      (.tags.outcome == "failure" or .tags.outcome == "timeout"))] as $failures
    | if ($failures | length) > 0 then "failed_operation"
      else metric_status($successes; "trend"; false) end;
  [
    (if .schema_version == "v1" then empty else "schema_version" end),
    (if .status == "not_evaluated" then empty else "status" end),
    (if .policy == null then empty else "policy" end),
    (if ((.checks // []) | length) == 0 then empty else "checks" end),
    (if ((.archives | type) == "array" and (.archives | length) == 1)
     then empty else "archive_count" end)
  ] as $base_errors
  | (if (.archives | type) == "array" and (.archives | length) == 1 then
       ["Subaccount", "Directory", "Entitlement", "DirectoryEntitlement", "SubaccountApiCredential"] as $kinds
       | [$kinds[] as $kind
          | [$kind,
             trend_status($kind; "xp_time_to_ready"),
             operation_status($kind; "create"),
             trend_status($kind; "xp_time_to_delete"),
             operation_status($kind; "delete")]
          | @tsv]
     else [] end) as $rows
  | [metrics[]
     | select(.source == "raw_k6" and .metric == "xp_lifecycle_phase_duration" and .metric_type == "trend")
     | (.tags.resource_kind // "") as $kind
     | (.tags.stage // "") as $phase
     | (.tags.outcome // "") as $outcome
     | (.tags.reason // "") as $reason
     | select((["create_request", "readiness", "delete_request", "kubernetes_absence_wait"] | index($phase)) != null)
     | select((["success", "failure", "timeout"] | index($outcome)) != null)
     | select((["none", "timeout", "reconcile_error", "api_error", "unknown"] | index($reason)) != null)
     | select(((["Subaccount", "Directory", "Entitlement", "DirectoryEntitlement", "SubaccountApiCredential"] | index($kind)) != null))
     | ["PHASE", $kind, $phase, $outcome, $reason,
        (if (.sample_count | type) == "number" and .sample_count >= 0 then (.sample_count | tostring) else "0" end),
        (if (.percentiles.p50 | type) == "number" and ((.percentiles.p50 | isfinite)) then (.percentiles.p50 | tostring) else "n/a" end)]
     | @tsv] as $phase_rows
  | [metrics[]
     | select(.source == "raw_k6" and .metric == "xp_measurement_phase" and .metric_type == "counter")
     | (.tags.resource_kind // "") as $kind
     | (.tags.stage // "") as $phase
     | (.tags.field // "") as $event
     | select((["create_request", "readiness", "delete_request", "kubernetes_absence_wait"] | index($phase)) != null)
     | select((["started", "requested", "accepted", "observed", "failed"] | index($event)) != null)
     | select((["Subaccount", "Directory", "Entitlement", "DirectoryEntitlement", "SubaccountApiCredential"] | index($kind)) != null)
     | ["MARKER", $kind, $phase, $event,
        (if (.sum | type) == "number" and (.sum | isfinite) then (.sum | tostring)
         elif (.sample_count | type) == "number" and .sample_count >= 0 then (.sample_count | tostring)
         else "0" end)]
     | @tsv] as $marker_rows
  | [metrics[]
     | select(.source == "raw_k6" and .metric == "xp_lifecycle_phase_duration" and .metric_type == "trend")
     | (.tags.resource_kind // "") as $kind
     | (.tags.outcome // "") as $outcome
     | select(((["Subaccount", "Directory", "Entitlement", "DirectoryEntitlement", "SubaccountApiCredential"] | index($kind)) != null))
     | select((["failure", "timeout"] | index($outcome)) != null)
     | ["FAILURE", $kind, "lifecycle_phase", $outcome]
     | @tsv] as $failure_rows
  | (["BASE\t\($base_errors | if length == 0 then "ok" else join(",") end)"] + $rows + $phase_rows + $marker_rows + $failure_rows)[]
' "$report" 2>/dev/null)"; then
  echo '::error title=Benchmark report contract::Report JSON could not be validated.'
  echo 'Benchmark report JSON could not be validated.' >&2
  exit 1
fi

base_status=""
summary=$'## BTP benchmark lifecycle evidence — '"$role"$' archive\n\n| Resource kind | Ready | Create operation | Deleted | Delete operation |\n| --- | --- | --- | --- | --- |\n'
failures=0
phase_heading_added=false
marker_heading_added=false
while IFS=$'\t' read -r row_type field1 field2 field3 field4 field5 field6 _; do
  if [[ "$row_type" == FAILURE ]]; then
    ((failures += 1))
    printf '::error title=Failed BTP lifecycle operation::%s archive: %s has recorded %s outcome evidence.\n' "$role" "$field1" "$field3"
    continue
  fi
  if [[ "$row_type" == PHASE ]]; then
    if [[ "$phase_heading_added" == false ]]; then
      summary+=$'\n### Workload phase durations (client-observed)\n\n| Resource kind | Phase | Outcome | Category | Samples | p50 (ms) |\n| --- | --- | --- | --- | ---: | ---: |\n'
      phase_heading_added=true
    fi
    summary+="| $field1 | $field2 | $field3 | $field4 | $field5 | $field6 |"$'\n'
    continue
  fi
  if [[ "$row_type" == MARKER ]]; then
    if [[ "$marker_heading_added" == false ]]; then
      summary+=$'\n### Workload phase markers\n\n| Resource kind | Phase | Marker | Count |\n| --- | --- | --- | ---: |\n'
      marker_heading_added=true
    fi
    summary+="| $field1 | $field2 | $field3 | $field4 |"$'\n'
    continue
  fi
  if [[ "$row_type" == BASE ]]; then
    base_status=$field1
    if [[ "$base_status" != ok ]]; then
      ((failures += 1))
      echo "::error title=Benchmark report contract::Invalid report fields: $base_status"
      summary+=$'\n**Invalid report fields:** '"$base_status"$'\n'
    fi
    continue
  fi
  kind=$row_type
  ready=$field1
  create=$field2
  deleted=$field3
  delete_op=$field4
  summary+="| $kind | $ready | $create | $deleted | $delete_op |"$'\n'
  for entry in "ready:$ready" "create operation:$create" "delete:$deleted" "delete operation:$delete_op"; do
    field=${entry%%:*}
    status=${entry#*:}
    if [[ "$status" != ok ]]; then
      ((failures += 1))
      printf '::error title=Missing BTP lifecycle evidence::%s archive: %s %s evidence: %s\n' "$role" "$kind" "$field" "$status"
    fi
  done
done <<<"$contract_data"


if (( failures == 0 )); then
  summary+=$'\nAll five resource kinds have exactly five instances of the required report evidence.\n'
  printf '%s' "$summary"
  printf '%s\n' 'Benchmark report contract verified.'
else
  summary+=$'\n**Result:** failed; see the step annotations for missing or invalid evidence.\n'
  printf '%s' "$summary"
  echo 'Benchmark report failed the five-kind, five-instance lifecycle contract; see per-resource evidence above.' >&2
fi

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  printf '%s' "$summary" >> "$GITHUB_STEP_SUMMARY"
fi

(( failures == 0 ))
