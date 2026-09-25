#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "$tmp_dir"' EXIT
mkdir -p "$tmp_dir/diagnostics"

cat >"$tmp_dir/diagnostics/index.json" <<'JSON'
{"schema_version":"0.2.0","records":12,"logs":{"complete":true},"events":{"complete":true},"warnings":[]}
JSON
cat >"$tmp_dir/diagnostics/events.jsonl" <<'JSONL'
{"component":"managed-resource","type":"Warning","reason":"CannotCreateExternalResource","regarding":{"kind":"DirectoryEntitlement","name":"private-resource-name"},"content":{"message":"private event text and identifier"}}
{"component":"managed-resource","type":"Warning","reason":"CannotResolvePrivateBinding","regarding":{"kind":"DirectoryEntitlement","name":"private-resource-name"},"content":{"message":"private event text and identifier"}}
{"component":"managed-resource","type":"Warning","reason":"private-resource-name","regarding":{"kind":"DirectoryEntitlement","name":"private-resource-name"},"content":{"message":"private event text and identifier"}}
{"component":"managed-resource","type":"Warning","reason":"CannotResolveResourceReferences","regarding":{"kind":"DirectoryEntitlement","name":"private-resource-name"},"content":{"message":"cannot resolve reference private-directory-guid"}}
{"component":"managed-resource","type":"Warning","reason":"CannotCreateExternalResource","regarding":{"kind":"DirectoryEntitlement","name":"private-resource-name"},"content":{"message":"BTP HTTP status 403 for private-account-id"}}
JSONL
cat >"$tmp_dir/diagnostics/logs.jsonl" <<'JSONL'
{"source":{"role":"provider"},"host_received_at":"2026-09-25T16:00:00.000Z","content":{"message":"{\"level\":\"error\",\"controller\":\"managed/directory.account.btp.sap.crossplane.io\",\"msg\":\"Reconciler error\",\"error\":\"Directory private-resource-name: atProvider.directoryFeatures: Required value; private-identifier\"}"}}
{"source":{"role":"provider"},"host_received_at":"2026-09-25T16:00:30.000Z","content":{"message":"{\"level\":\"error\",\"controller\":\"private-controller-name\",\"msg\":\"Reconciler error\",\"error\":\"RBAC: clusterrole private-role not found\"}"}}
{"source":{"role":"provider"},"host_received_at":"2026-09-25T16:02:00.000Z","content":{"message":"{\"level\":\"error\",\"controller\":\"private-controller-name\",\"msg\":\"Reconciler error\",\"error\":\"RBAC: clusterrole private-role not found\"}"}}
{"source":{"role":"provider"},"host_received_at":"2026-09-25T16:03:00.000Z","content":{"message":"{\"level\":\"error\",\"controller\":\"managed/directoryentitlement.account.btp.sap.crossplane.io\",\"msg\":\"Reconciler error\",\"error\":\"failed to resolve reference private-directory-guid\"}"}}
{"source":{"role":"provider"},"host_received_at":"2026-09-25T16:04:00.000Z","content":{"message":"{\"level\":\"error\",\"controller\":\"private-controller-name\",\"msg\":\"Reconciler error\",\"error\":\"BTP HTTP status 403 for private-account-id\"}"}}
{"source":{"role":"provider"},"host_received_at":"2026-09-25T16:05:00.000Z","content":{"message":"{\"level\":\"error\",\"controller\":\"private-controller-name\",\"msg\":\"Reconciler error\",\"error\":\"context deadline exceeded for private-resource-name\"}"}}
{"source":{"role":"provider"},"host_received_at":"2026-09-25T16:06:00.000Z","content":{"message":"{\"level\":\"error\",\"controller\":\"private-controller-name\",\"msg\":\"Reconciler error\",\"error\":\"token private-token denied for account private-id\"}"}}
JSONL

tar --zstd -cf "$tmp_dir/fixture.tsdb.tar.zst" -C "$tmp_dir" diagnostics
output="$("$script_dir/summarize-diagnostics.sh" "$tmp_dir/fixture.tsdb.tar.zst")"

[[ "$output" == *"logs_complete=true"* ]]
[[ "$output" == *"kind=DirectoryEntitlement type=Warning reason=CannotCreateExternalResource detail=unclassified count=1"* ]]
[[ "$output" == *"kind=DirectoryEntitlement type=Warning reason=CannotCreateExternalResource detail=btp_http_403 count=1"* ]]
[[ "$output" == *"kind=DirectoryEntitlement type=Warning reason=CannotResolveResourceReferences detail=resource_reference_resolution count=1"* ]]
[[ "$output" == *"kind=DirectoryEntitlement type=Warning reason=other_cannot detail=unclassified count=1"* ]]
[[ "$output" == *"kind=DirectoryEntitlement type=Warning reason=other detail=unclassified count=1"* ]]
[[ "$output" == *"category=directory_features_required capture_window=capture_0_60s count=1"* ]]
[[ "$output" == *"category=provider_rbac_role_missing capture_window=capture_0_60s count=1"* ]]
[[ "$output" == *"category=provider_rbac_role_missing capture_window=capture_60s_plus count=1"* ]]
[[ "$output" == *"category=resource_reference_resolution capture_window=capture_60s_plus count=1"* ]]
[[ "$output" == *"category=btp_http_403 capture_window=capture_60s_plus count=1"* ]]
[[ "$output" == *"category=provider_request_timeout capture_window=capture_60s_plus count=1"* ]]
[[ "$output" == *"category=other_provider_log capture_window=capture_60s_plus count=1"* ]]
[[ "$output" == *"relative to the first retained diagnostic log timestamp, not benchmark start"* ]]
for forbidden in 'private-resource-name' 'private event text' 'private-identifier' 'private-role' 'private-controller-name' 'private-directory-guid' 'private-account-id' 'private-token' 'private-id'; do
  [[ "$output" != *"$forbidden"* ]] || {
    echo "diagnostic summary leaked a private value: $forbidden" >&2
    exit 1
  }
done

mkdir -p "$tmp_dir/empty"
tar --zstd -cf "$tmp_dir/no-diagnostics.tar.zst" -C "$tmp_dir" empty
output="$("$script_dir/summarize-diagnostics.sh" "$tmp_dir/no-diagnostics.tar.zst")"
[[ "$output" == *"No diagnostics sidecar is present"* ]]

echo 'Sanitized diagnostics summary fixtures passed.'
