#!/usr/bin/env python3
import os
import subprocess
import sys

SCRIPT = os.path.join(os.path.dirname(__file__), "fingerprint-env.py")
KEY = "91" * 32
TECH_USER = {
    "email": "local-secret-user@example.invalid",
    "username": "local-private-user",
    "password": "local-password-never-print",
}
CIS = {
    "grant_type": "client_credentials",
    "uaa": {
        "clientid": "private-client-id",
        "clientsecret": "private-client-secret",
        "url": "https://private-auth.example.invalid",
    },
    "endpoints": {
        "accounts_service_url": "https://private-accounts.example.invalid",
        "entitlements_service_url": "https://private-entitlements.example.invalid",
        "provisioning_service_url": "https://private-provisioning.example.invalid",
    },
}


def run(tech_json, cis_json, key=KEY):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "BTP_ENV_COMPARE_HMAC_KEY": key,
        "BTP_TECHNICAL_USER": tech_json,
        "CIS_CENTRAL_BINDING": cis_json,
        "GLOBAL_ACCOUNT": "private-global-account",
        "CLI_SERVER_URL": "https://private-cli.example.invalid",
        "TECHNICAL_USER_EMAIL": "local-secret-user@example.invalid",
        "SECOND_DIRECTORY_ADMIN_EMAIL": "other-private-user@example.invalid",
    }
    return subprocess.run([sys.executable, SCRIPT], env=env, text=True, capture_output=True)


tech_compact = __import__("json").dumps(TECH_USER, separators=(",", ":"))
tech_reordered = '{ "password": "local-password-never-print", "email": "local-secret-user@example.invalid", "username": "local-private-user" }'
cis_compact = __import__("json").dumps(CIS, separators=(",", ":"))
cis_pretty = __import__("json").dumps(CIS, indent=2, sort_keys=False)
first = run(tech_compact, cis_compact)
second = run(tech_reordered, cis_pretty)
assert first.returncode == 0, first.stderr
assert second.returncode == 0, second.stderr
assert first.stdout == second.stdout, "canonical JSON fingerprints should ignore whitespace and key order"

for sensitive in [*TECH_USER.values(), *["private-client-id", "private-client-secret", "private-global-account", KEY]]:
    assert sensitive not in first.stdout, f"fingerprint output leaked {sensitive}"
assert "BTP_TECHNICAL_USER.password: present; bytes=17-32; hmac_sha256_128=" in first.stdout
assert "CIS_CENTRAL_BINDING.uaa.clientsecret: present; bytes=17-32; hmac_sha256_128=" in first.stdout

changed_user = dict(TECH_USER, password="different-private-password")
changed = run(__import__("json").dumps(changed_user), cis_compact)
assert changed.returncode == 0, changed.stderr
assert changed.stdout != first.stdout, "changed credential should produce a different fingerprint"
assert "different-private-password" not in changed.stdout

invalid_key = run(tech_compact, cis_compact, key="too-short")
assert invalid_key.returncode == 2
assert KEY not in invalid_key.stderr
assert "64-character hex key" in invalid_key.stderr

print("Environment fingerprint fixtures passed.")
