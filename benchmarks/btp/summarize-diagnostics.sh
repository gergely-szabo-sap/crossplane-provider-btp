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
  exit 0
fi

printf '%s\n' 'Sanitized benchmark diagnostic summary (raw log and Event messages omitted):'

tar --zstd -xOf "$archive" diagnostics/index.json | jq -r '
  "Coverage: logs_complete=\(.logs.complete // false), events_complete=\(.events.complete // false), records=\(.records // 0), warnings=\((.warnings // []) | length)"
'

if has_member diagnostics/events.jsonl; then
  event_summary="$(tar --zstd -xOf "$archive" diagnostics/events.jsonl | jq -sr '
    def safe_kind:
      .regarding.kind as $kind
      | if (["Directory", "DirectoryEntitlement", "Entitlement", "Subaccount", "SubaccountApiCredential"] | index($kind))
        then $kind else "other" end;
    def safe_reason:
      (.reason // "") as $reason
      | if (["CreatedExternalResource", "DeletedExternalResource", "CannotCreateExternalResource", "CannotObserveExternalResource", "CannotDeleteExternalResource", "CannotUpdateExternalResource", "ExternalNameRecovered", "RecoveryLookupFailed", "RecoveryRefusedBrownfield", "AutoAssignedPreserved"] | index($reason))
        then $reason
        elif ($reason | test("^Cannot"; "i")) then "other_cannot"
        elif ($reason | test("^Failed"; "i")) then "other_failed"
        elif ($reason | test("^Error"; "i")) then "other_error"
        elif ($reason | test("^Successfully"; "i")) then "other_success"
        else "other" end;
    [ .[] | select(.regarding.kind? != null) |
      {kind: safe_kind,
       type: (if .type == "Normal" or .type == "Warning" then .type else "other" end),
       reason: safe_reason} ]
    | group_by([.kind, .type, .reason])[]
    | "Event: kind=\(.[0].kind) type=\(.[0].type) reason=\(.[0].reason) count=\(length)"
  ' 2>/dev/null || true)"
  if [[ -n "$event_summary" ]]; then
    printf '%s\n' "$event_summary"
  else
    echo 'Events: no recognized managed-resource Events.'
  fi
fi

if has_member diagnostics/logs.jsonl; then
  log_summary="$(tar --zstd -xOf "$archive" diagnostics/logs.jsonl | jq -sr '
    def safe_controller($c):
      if ($c | test("kind=directoryentitlement"; "i")) then "DirectoryEntitlement"
      elif ($c | test("managed/directory\\."; "i")) then "Directory"
      elif ($c | test("kind=directory"; "i")) then "Directory"
      else "other" end;
    [ .[] | select(.source.role? == "provider") |
      (try (.content.message | fromjson) catch {}) as $m
      | select(($m.level // "") == "error" or ($m.level // "") == "warn" or ($m.level // "") == "warning")
      | ($m.error // "") as $error
      | ($m.msg // "") as $message
      | {controller: safe_controller($m.controller // ""),
         level: (if $m.level == "error" then "error" else "warning" end),
         category:
           (if ($error | test("atProvider\\.directoryFeatures: Required value"; "i"))
            then "directory_features_required"
            elif ($error | test("RBAC: clusterrole[^\\n]*not found"; "i"))
            then "provider_rbac_role_missing"
            elif ($message == "Failed to watch" or ($error | test("failed to list \\*"; "i")))
            then "provider_watch_error"
            elif ($message == "Reconciler error") then "provider_reconcile_error"
            else "other_provider_log" end)} ]
    | group_by([.controller, .level, .category])[]
    | "Provider log: controller=\(.[0].controller) level=\(.[0].level) category=\(.[0].category) count=\(length)"
  ' 2>/dev/null || true)"
  if [[ -n "$log_summary" ]]; then
    printf '%s\n' "$log_summary"
  else
    echo 'Provider logs: no matching error or warning records.'
  fi
fi
