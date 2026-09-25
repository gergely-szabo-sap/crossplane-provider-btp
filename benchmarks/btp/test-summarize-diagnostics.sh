#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "$tmp_dir"' EXIT
mkdir -p "$tmp_dir/diagnostics"

cat >"$tmp_dir/diagnostics/index.json" <<'JSON'
{"schema_version":"0.2.0","records":2,"logs":{"complete":true},"events":{"complete":true},"warnings":[]}
JSON
cat >"$tmp_dir/diagnostics/events.jsonl" <<'JSONL'
{"component":"managed-resource","type":"Warning","reason":"CannotCreateExternalResource","regarding":{"kind":"DirectoryEntitlement","name":"private-resource-name"},"content":{"message":"private event text and identifier"}}
JSONL
cat >"$tmp_dir/diagnostics/logs.jsonl" <<'JSONL'
{"source":{"role":"provider"},"content":{"message":"{\"level\":\"error\",\"controller\":\"managed/directory.account.btp.sap.crossplane.io\",\"msg\":\"Reconciler error\",\"error\":\"Directory private-resource-name: atProvider.directoryFeatures: Required value; private-identifier\"}"}}
JSONL

tar --zstd -cf "$tmp_dir/fixture.tsdb.tar.zst" -C "$tmp_dir" diagnostics
output="$("$script_dir/summarize-diagnostics.sh" "$tmp_dir/fixture.tsdb.tar.zst")"

[[ "$output" == *"logs_complete=true"* ]]
[[ "$output" == *"kind=DirectoryEntitlement type=Warning reason=CannotCreateExternalResource count=1"* ]]
[[ "$output" == *"category=directory_features_required"* ]]
for forbidden in 'private-resource-name' 'private event text' 'private-identifier'; do
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
