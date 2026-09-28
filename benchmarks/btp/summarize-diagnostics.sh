#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "Error: $*" >&2
  exit 1
}

archive="${1:-}"
[[ -n "$archive" ]] || fail "usage: $0 <metrics-archive.tsdb.tar.zst>"
[[ -f "$archive" && ! -L "$archive" ]] || fail "archive must be a regular non-symlink file"
command -v jq >/dev/null 2>&1 || fail "jq is required"
command -v tar >/dev/null 2>&1 || fail "tar is required"

has_member() {
  tar --zstd -tf "$archive" 2>/dev/null | grep -Fx "$1" >/dev/null
}

if ! has_member diagnostics/index.json; then
  echo "No diagnostics sidecar is present in the benchmark archive."
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n\n' '### Sanitized benchmark diagnostics' 'No diagnostics sidecar is present in the benchmark archive.' >>"$GITHUB_STEP_SUMMARY"
  fi
  exit 0
fi

emit_line() {
  printf '%s\n' "$1"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '%s\n' "$1" >>"$GITHUB_STEP_SUMMARY"
  fi
}

emit_line '### Sanitized benchmark diagnostics'
emit_line 'Raw log and Event messages, identifiers, warning text, and timestamps are omitted.'

coverage_summary="$(tar --zstd -xOf "$archive" diagnostics/index.json | jq -r '
  def safe_bool($value): if $value == true then "true" elif $value == false then "false" else "unknown" end;
  def safe_count($value):
    if ($value | type) == "number" and ($value | isfinite) and $value >= 0 and $value == ($value | floor)
    then ($value | tostring) else "unknown" end;
  "Coverage: sidecar_complete=\(safe_bool(.complete)), truncated=\(safe_bool(.truncated)), records=\(safe_count(.records)), dropped_records=\(safe_count(.dropped_records)); logs_complete=\(safe_bool(.logs.complete)), logs_records=\(safe_count(.logs.records)), logs_dropped=\(safe_count(.logs.dropped_records)); events_complete=\(safe_bool(.events.complete)), events_records=\(safe_count(.events.records)), events_dropped=\(safe_count(.events.dropped_records)); warnings=\(if (.warnings | type) == "array" then (.warnings | length | tostring) else "unknown" end)"
')"
emit_line "$coverage_summary"

warning_summary="$(tar --zstd -xOf "$archive" diagnostics/index.json | jq -sr '
  def category($warning):
    if ($warning | test("^diagnostic logs .*: forbidden$")) then "logs_forbidden"
    elif ($warning | test("^diagnostic logs .*: unauthorized$")) then "logs_unauthorized"
    elif ($warning | test("^diagnostic logs .*: not_found$")) then "logs_not_found"
    elif ($warning | test("^diagnostic logs .*: rate_limited$")) then "logs_rate_limited"
    elif ($warning | test("^diagnostic logs .*: transport_error$")) then "logs_transport_error"
    elif ($warning | test("^diagnostic logs .*: (reconnect_limit|replay_boundary_overflow)$")) then "logs_stream_limit"
    elif ($warning | test("^diagnostic logs .*: (overlong_line|interrupted_line)$")) then "logs_record_limit"
    elif ($warning | test("^diagnostic logs .*: (collector_error|sink_error)$")) then "logs_collector_error"
    elif ($warning | test("^diagnostic logs .*: inventory:unavailable$")) then "logs_inventory_unavailable"
    elif ($warning | test("^diagnostic Events .*: forbidden$")) then "events_forbidden"
    elif ($warning | test("^diagnostic Events .*: unauthorized$")) then "events_unauthorized"
    elif ($warning | test("^diagnostic Events .*: not_found$")) then "events_not_found"
    elif ($warning | test("^diagnostic Events .*: rate_limited$")) then "events_rate_limited"
    elif ($warning | test("^diagnostic Events .*: api_error$")) then "events_api_error"
    elif ($warning | test("^diagnostic Events .*: poll_limit$")) then "events_poll_limit"
    elif ($warning | test("^diagnostic Events .*: pagination_limit$")) then "events_pagination_limit"
    elif ($warning | test("^diagnostic Events .*: api_switched$")) then "events_api_switched"
    elif ($warning | test("^diagnostic Events .*: (time_unknown|before_start)$")) then "events_time_attribution_gap"
    elif ($warning | test("^diagnostic Events .*: seen_state_evicted$")) then "events_state_limit"
    elif ($warning | test("^diagnostic provider identity .* is unavailable$")) then "provider_identity_unavailable"
    else "other" end;
  [ .[] | .warnings[]? | if type == "string" then category(.) else "other" end ]
  | group_by(.)[]
  | "Diagnostics warning: category=\(.[0]) count=\(length)"
' 2>/dev/null || true)"
if [[ -n "$warning_summary" ]]; then
  while IFS= read -r line; do emit_line "$line"; done <<<"$warning_summary"
else
  emit_line 'Diagnostics warnings: none.'
fi

if has_member diagnostics/events.jsonl; then
  event_summary="$(tar --zstd -xOf "$archive" diagnostics/events.jsonl | jq -sr '
    def safe_kind:
      .regarding.kind as $kind
      | if ($kind | type) == "string" and (["Directory", "DirectoryEntitlement", "Entitlement", "Subaccount", "SubaccountApiCredential"] | index($kind))
        then $kind else "other" end;
    def safe_reason:
      (.reason // "") as $raw_reason
      | ($raw_reason | if type == "string" then . else "" end) as $reason
      | if (["CreatedExternalResource", "DeletedExternalResource", "CannotCreateExternalResource", "CannotObserveExternalResource", "CannotDeleteExternalResource", "CannotUpdateExternalResource", "CannotResolveResourceReferences", "ExternalNameRecovered", "RecoveryLookupFailed", "RecoveryRefusedBrownfield", "AutoAssignedPreserved"] | index($reason))
        then $reason
        elif ($reason | test("^Cannot"; "i")) then "other_cannot"
        elif ($reason | test("^Failed"; "i")) then "other_failed"
        elif ($reason | test("^Error"; "i")) then "other_error"
        elif ($reason | test("^Successfully"; "i")) then "other_success"
        else "other" end;
    def safe_error_category($input):
      ($input | if type == "string" then . else "" end) as $text
      | if ($text | test("cannot read client_secret from source|client_secret[^\\n]{0,40}(missing|not found)|missing[^\\n]{0,40}client_secret"; "i")) then "api_credential_client_secret_missing"
      elif ($text | test("cannot reconstruct external-name: (subaccount_id|name) missing from tfstate"; "i")) then "api_credential_state_missing"
      elif ($text | test("atProvider\\.directoryFeatures: Required value"; "i")) then "directory_features_required"
      elif ($text | test("RBAC: clusterrole[^\\n]*not found"; "i")) then "provider_rbac_role_missing"
      elif ($text | test("(cannot|failed to|unable to|could not) resolve[^\\n]{0,80}reference|reference[^\\n]{0,80}(not found|unresolved)"; "i")) then "resource_reference_resolution"
      elif ($text | test("(http|status|response)[^0-9]{0,32}401([^0-9]|$)|401 unauthorized"; "i")) then "btp_http_401"
      elif ($text | test("(http|status|response)[^0-9]{0,32}403([^0-9]|$)|403 forbidden"; "i")) then "btp_http_403"
      elif ($text | test("(http|status|response)[^0-9]{0,32}404([^0-9]|$)|404 not found"; "i")) then "btp_http_404"
      elif ($text | test("(http|status|response)[^0-9]{0,32}409([^0-9]|$)|409 conflict"; "i")) then "btp_http_409"
      elif ($text | test("(http|status|response)[^0-9]{0,32}429([^0-9]|$)|429 too many requests"; "i")) then "btp_http_429"
      elif ($text | test("(http|status|response)[^0-9]{0,32}4[0-9]{2}([^0-9]|$)"; "i")) then "btp_http_4xx_other"
      elif ($text | test("(http|status|response)[^0-9]{0,32}5[0-9]{2}([^0-9]|$)"; "i")) then "btp_http_5xx"
      elif ($text | test("context deadline exceeded|i/o timeout|request timed out|request timeout|timed out"; "i")) then "provider_request_timeout"
      elif ($text | test("connection refused|connection reset|no such host|tls handshake timeout|network is unreachable"; "i")) then "provider_transport_error"
      else "unclassified" end;
    def safe_event_detail:
      if .reason == "CannotResolveResourceReferences" then "resource_reference_resolution"
      elif .type == "Warning" then safe_error_category(.content.message // "")
      else "not_applicable" end;
    [ .[] | select(.regarding.kind? != null) |
      {kind: safe_kind,
       type: (if .type == "Normal" or .type == "Warning" then .type else "other" end),
       reason: safe_reason,
       detail: safe_event_detail} ]
    | group_by([.kind, .type, .reason, .detail])[]
    | "Event: kind=\(.[0].kind) type=\(.[0].type) reason=\(.[0].reason) detail=\(.[0].detail) count=\(length)"
  ' 2>/dev/null || true)"
  if [[ -n "$event_summary" ]]; then
    while IFS= read -r line; do emit_line "$line"; done <<<"$event_summary"
  else
    emit_line 'Events: no recognized managed-resource Events.'
  fi
fi

if has_member diagnostics/logs.jsonl; then
  log_summary="$(tar --zstd -xOf "$archive" diagnostics/logs.jsonl | jq -sr '
    def safe_controller($input):
      ($input | if type == "string" then . else "" end) as $c
      | if ($c | test("kind=directoryentitlement"; "i")) then "DirectoryEntitlement"
      elif ($c | test("subaccount[_-]?api[_-]?credential"; "i")) then "SubaccountApiCredential"
      elif ($c | test("managed/directory\\."; "i")) then "Directory"
      elif ($c | test("kind=directory"; "i")) then "Directory"
      else "other" end;
    def host_epoch:
      (.host_received_at // "") as $timestamp
      | if ($timestamp | type) != "string" then null
        else try ($timestamp | sub("\\.[0-9]+"; "") | fromdateiso8601) catch null end;
    def safe_error_category($input):
      ($input | if type == "string" then . else "" end) as $text
      | if ($text | test("cannot read client_secret from source|client_secret[^\\n]{0,40}(missing|not found)|missing[^\\n]{0,40}client_secret"; "i")) then "api_credential_client_secret_missing"
      elif ($text | test("cannot reconstruct external-name: (subaccount_id|name) missing from tfstate"; "i")) then "api_credential_state_missing"
      elif ($text | test("atProvider\\.directoryFeatures: Required value"; "i")) then "directory_features_required"
      elif ($text | test("RBAC: clusterrole[^\\n]*not found"; "i")) then "provider_rbac_role_missing"
      elif ($text | test("(cannot|failed to|unable to|could not) resolve[^\\n]{0,80}reference|reference[^\\n]{0,80}(not found|unresolved)"; "i")) then "resource_reference_resolution"
      elif ($text | test("(http|status|response)[^0-9]{0,32}401([^0-9]|$)|401 unauthorized"; "i")) then "btp_http_401"
      elif ($text | test("(http|status|response)[^0-9]{0,32}403([^0-9]|$)|403 forbidden"; "i")) then "btp_http_403"
      elif ($text | test("(http|status|response)[^0-9]{0,32}404([^0-9]|$)|404 not found"; "i")) then "btp_http_404"
      elif ($text | test("(http|status|response)[^0-9]{0,32}409([^0-9]|$)|409 conflict"; "i")) then "btp_http_409"
      elif ($text | test("(http|status|response)[^0-9]{0,32}429([^0-9]|$)|429 too many requests"; "i")) then "btp_http_429"
      elif ($text | test("(http|status|response)[^0-9]{0,32}4[0-9]{2}([^0-9]|$)"; "i")) then "btp_http_4xx_other"
      elif ($text | test("(http|status|response)[^0-9]{0,32}5[0-9]{2}([^0-9]|$)"; "i")) then "btp_http_5xx"
      elif ($text | test("context deadline exceeded|i/o timeout|request timed out|request timeout|timed out"; "i")) then "provider_request_timeout"
      elif ($text | test("connection refused|connection reset|no such host|tls handshake timeout|network is unreachable"; "i")) then "provider_transport_error"
      else "other_provider_log" end;
    [ .[] | host_epoch as $time | {record: ., timestamp: $time} ] as $records
    | ([$records[].timestamp | select(type == "number")] | if length > 0 then min else null end) as $capture_start
    | [ $records[] | .record as $record | select($record.source.role? == "provider")
      | (try ($record.content.message | fromjson) catch {}) as $m
      | select(($m.level // "") == "error" or ($m.level // "") == "warn" or ($m.level // "") == "warning")
      | ($m.error // "") as $error
      | ($m.msg // "") as $message
      | {controller: safe_controller($m.controller // ""),
         level: (if $m.level == "error" then "error" else "warning" end),
         category:
           (if $message == "Failed to watch" or ($error | test("failed to list \\*"; "i"))
            then "provider_watch_error"
            else safe_error_category($error) end),
         capture_window:
           (if $record.host_received_at == null or $capture_start == null then "time_unknown"
            elif (($record | host_epoch) - $capture_start) < 60 then "capture_0_60s"
            else "capture_60s_plus" end)} ]
    | group_by([.controller, .level, .category, .capture_window])[]
    | "Provider log: controller=\(.[0].controller) level=\(.[0].level) category=\(.[0].category) capture_window=\(.[0].capture_window) count=\(length)"
  ' 2>/dev/null || true)"
  if [[ -n "$log_summary" ]]; then
    emit_line 'Provider log time buckets are relative to the first retained diagnostic log timestamp, not benchmark start.'
    while IFS= read -r line; do emit_line "$line"; done <<<"$log_summary"
  else
    emit_line 'Provider logs: no matching error or warning records.'
  fi
fi
