#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "$tmp_dir"' EXIT
mkdir -p "$tmp_dir/diagnostics"

cat >"$tmp_dir/diagnostics/index.json" <<'JSON'
{"schema_version":"0.2.0","records":4,"logs":{"complete":true},"events":{"complete":true},"warnings":[]}
JSON
cat >"$tmp_dir/diagnostics/events.jsonl" <<'JSONL'
{"component":"managed-resource","type":"Warning","reason":"CannotCreateExternalResource","regarding":{"kind":"DirectoryEntitlement","name":"private-resource-name"},"content":{"message":"private event text and identifier"}}
{"component":"managed-resource","type":"Warning","reason":"CannotResolvePrivateBinding","regarding":{"kind":"DirectoryEntitlement","name":"private-resource-name"},"content":{"message":"private event text and identifier"}}
{"component":"managed-resource","type":"Warning","reason":"private-resource-name","regarding":{"kind":"DirectoryEntitlement","name":"private-resource-name"},"content":{"message":"private event text and identifier"}}
JSONL
cat >"$tmp_dir/diagnostics/logs.jsonl" <<'JSONL'
{"source":{"role":"provider"},"host_received_at":"2026-09-25T16:00:00.000Z","content":{"message":"{\"level\":\"error\",\"controller\":\"managed/directory.account.btp.sap.crossplane.io\",\"msg\":\"Reconciler error\",\"error\":\"Directory private-resource-name: atProvider.directoryFeatures: Required value; private-identifier\"}"}}
{"source":{"role":"provider"},"host_received_at":"2026-09-25T16:00:30.000Z","content":{"message":"{\"level\":\"error\",\"controller\":\"private-controller-name\",\"msg\":\"Reconciler error\",\"error\":\"RBAC: clusterrole private-role not found\"}"}}
{"source":{"role":"provider"},"host_received_at":"2026-09-25T16:02:00.000Z","content":{"message":"{\"level\":\"error\",\"controller\":\"private-controller-name\",\"msg\":\"Reconciler error\",\"error\":\"RBAC: clusterrole private-role not found\"}"}}
JSONL

tar --zstd -cf "$tmp_dir/fixture.tsdb.tar.zst" -C "$tmp_dir" diagnostics
output="$("$script_dir/summarize-diagnostics.sh" "$tmp_dir/fixture.tsdb.tar.zst")"

[[ "$output" == *"logs_complete=true"* ]]
[[ "$output" == *"kind=DirectoryEntitlement type=Warning reason=CannotCreateExternalResource count=1"* ]]
[[ "$output" == *"kind=DirectoryEntitlement type=Warning reason=other_cannot count=1"* ]]
[[ "$output" == *"kind=DirectoryEntitlement type=Warning reason=other count=1"* ]]
[[ "$output" == *"category=directory_features_required capture_window=capture_0_60s count=1"* ]]
[[ "$output" == *"category=provider_rbac_role_missing capture_window=capture_0_60s count=1"* ]]
[[ "$output" == *"category=provider_rbac_role_missing capture_window=capture_60s_plus count=1"* ]]
[[ "$output" == *"relative to the first retained diagnostic log timestamp, not benchmark start"* ]]
for forbidden in 'private-resource-name' 'private event text' 'private-identifier' 'private-role' 'private-controller-name'; do
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
