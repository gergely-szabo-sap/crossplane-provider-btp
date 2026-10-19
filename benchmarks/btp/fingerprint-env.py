#!/usr/bin/env python3
"""Print keyed, allowlisted environment fingerprints without exposing values."""

import hashlib
import hmac
import json
import os
import re
import sys

KEY_ENV = "BTP_ENV_COMPARE_HMAC_KEY"
TEXT_FIELDS = (
    "GLOBAL_ACCOUNT",
    "CLI_SERVER_URL",
    "TECHNICAL_USER_EMAIL",
    "SECOND_DIRECTORY_ADMIN_EMAIL",
)
JSON_FIELDS = {
    "BTP_TECHNICAL_USER": ("email", "username", "password"),
    "CIS_CENTRAL_BINDING": (
        "grant_type",
        "uaa.clientid",
        "uaa.clientsecret",
        "uaa.url",
        "endpoints.accounts_service_url",
        "endpoints.entitlements_service_url",
        "endpoints.provisioning_service_url",
    ),
}


def length_bucket(value: bytes) -> str:
    size = len(value)
    if size == 0:
        return "empty"
    for limit in (16, 32, 64, 128, 256, 512, 1024):
        if size <= limit:
            return f"1-{limit}" if limit == 16 else f"{(limit // 2) + 1}-{limit}"
    return "over-1024"


def fingerprint(key: bytes, label: str, value: bytes) -> str:
    message = b"btp-env-fingerprint-v1\0" + label.encode("utf-8") + b"\0" + value
    return hmac.new(key, message, hashlib.sha256).digest()[:16].hex()


def report_value(key: bytes, label: str, value: str | None) -> None:
    if value is None:
        print(f"{label}: unset")
        return
    encoded = value.encode("utf-8")
    print(
        f"{label}: set; bytes={length_bucket(encoded)}; "
        f"hmac_sha256_128={fingerprint(key, label, encoded)}"
    )


def nested_value(document: object, path: str) -> object:
    current = document
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def main() -> int:
    raw_key = os.environ.get(KEY_ENV, "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", raw_key):
        print(
            f"{KEY_ENV} must be a separately generated 64-character hex key "
            "(for example, generate with: openssl rand -hex 32).",
            file=sys.stderr,
        )
        return 2
    key = bytes.fromhex(raw_key)

    print("BTP environment comparison (keyed fingerprints; values are never printed)")
    print("Length values are coarse UTF-8 byte buckets; JSON whitespace/key order is normalized.")
    for name in TEXT_FIELDS:
        report_value(key, name, os.environ.get(name))

    for name, fields in JSON_FIELDS.items():
        raw = os.environ.get(name)
        if raw is None:
            print(f"{name}: unset")
            continue
        raw_bytes = raw.encode("utf-8")
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            print(
                f"{name}: set; json=invalid; bytes={length_bucket(raw_bytes)}; "
                f"raw_hmac_sha256_128={fingerprint(key, name + '.raw', raw_bytes)}"
            )
            continue

        canonical = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        print(
            f"{name}: set; json=valid; canonical_bytes={length_bucket(canonical)}; "
            f"hmac_sha256_128={fingerprint(key, name + '.json', canonical)}"
        )
        for path in fields:
            value = nested_value(document, path)
            label = f"{name}.{path}"
            if value is None:
                print(f"  {label}: absent")
                continue
            if isinstance(value, str):
                field_bytes = value.encode("utf-8")
            else:
                field_bytes = json.dumps(
                    value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
            print(
                f"  {label}: present; bytes={length_bucket(field_bytes)}; "
                f"hmac_sha256_128={fingerprint(key, label, field_bytes)}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
