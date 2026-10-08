#!/usr/bin/env python3
"""Resolve and preflight the reviewed, checked-in BTP benchmark baseline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile

EXPECTED_CLI = "v0.9.4"
MAX_JSON = 1 << 20
MAX_ARCHIVE = 1 << 30
MAX_LOCAL_ARCHIVE = 50 * 1024 * 1024
CONTRACT_FILES = (
    "benchmarks/btp/config.yaml",
    "benchmarks/btp/k6/subaccount-create-delete.js",
    "benchmarks/btp/k6/crossplane-helpers.js",
    "benchmarks/btp/perses/overview.yaml",
    "benchmarks/btp/report-presentation.yaml",
)
LOCAL_FIELDS = {
    "archive_path", "run_id", "run_attempt", "artifact_id", "artifact_expires_at",
    "head_sha", "archive_sha256", "contract_sha256", "validated_cli", "environment_revision",
}
REASONS = {
    "not_configured", "invalid_reference", "invalid_archive", "incompatible_contract",
    "environment_revision_mismatch", "self_comparison", "identity_unavailable", "unsuitable_evidence",
}


class Unavailable(Exception):
    def __init__(self, reason: str):
        self.reason = reason if reason in REASONS else "invalid_archive"


def strict_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
        raise Unavailable("invalid_reference")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise Unavailable("invalid_reference")
    if (not isinstance(value, dict) or set(value) != {"schema_version", "baseline"} or
            value["schema_version"] != "v3"):
        raise Unavailable("invalid_reference")
    ref = value["baseline"]
    if ref is None:
        raise Unavailable("not_configured")
    if not isinstance(ref, dict) or set(ref) != LOCAL_FIELDS:
        raise Unavailable("invalid_reference")
    for key in ("run_id", "run_attempt", "artifact_id"):
        if type(ref[key]) is not int or ref[key] <= 0:
            raise Unavailable("invalid_reference")
    for key, pattern in (("head_sha", r"[0-9a-f]{40}"),
                         ("archive_sha256", r"[0-9a-f]{64}"),
                         ("contract_sha256", r"[0-9a-f]{64}")):
        if not isinstance(ref[key], str) or not re.fullmatch(pattern, ref[key]):
            raise Unavailable("invalid_reference")
    if (ref["validated_cli"] != EXPECTED_CLI or
            not isinstance(ref["environment_revision"], str) or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", ref["environment_revision"])):
        raise Unavailable("invalid_reference")
    if (not isinstance(ref["artifact_expires_at"], str) or
            not re.fullmatch(r"[0-9TZ:.-]{1,40}", ref["artifact_expires_at"]) or
            not isinstance(ref["archive_path"], str) or "\\" in ref["archive_path"]):
        raise Unavailable("invalid_reference")
    archive_path = PurePosixPath(ref["archive_path"])
    if (archive_path.is_absolute() or not archive_path.parts or str(archive_path) != ref["archive_path"] or
            any(part in {"", ".", ".."} for part in archive_path.parts)):
        raise Unavailable("invalid_reference")
    return ref


def contract_digest(contents: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in (*CONTRACT_FILES, "execution-cli"):
        data = contents[name] if name != "execution-cli" else EXPECTED_CLI.encode()
        name_bytes = name.encode()
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def output(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                if "\n" in value or "\r" in value:
                    raise ValueError("invalid output")
                stream.write(f"{key}={value}\n")


def unavailable(reason: str) -> int:
    output({"baseline_path": "", "baseline_mode": "current-only", "baseline_reason": reason,
            "baseline_provenance": "", "baseline_run_id": "", "baseline_attempt": "",
            "baseline_artifact_id": "", "baseline_head_sha": ""})
    print(f"Baseline unavailable ({reason}); current-only reporting will be used.")
    return 0


def resolve_local(ref: dict, workspace: Path, current_run_id: str) -> int:
    if current_run_id and ref["run_id"] == int(current_run_id):
        raise Unavailable("self_comparison")
    env_revision = os.environ.get("BTP_BENCHMARK_ENV_REVISION", "")
    if not env_revision or env_revision != ref["environment_revision"]:
        raise Unavailable("environment_revision_mismatch")

    root = workspace.resolve(strict=True)
    candidate = root
    for part in PurePosixPath(ref["archive_path"]).parts:
        candidate = candidate / part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            raise Unavailable("invalid_archive")
        if stat.S_ISLNK(info.st_mode):
            raise Unavailable("invalid_archive")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not stat.S_ISREG(candidate.stat().st_mode):
        raise Unavailable("invalid_archive")
    size = candidate.stat().st_size
    if size <= 0 or size > MAX_LOCAL_ARCHIVE or file_digest(candidate) != ref["archive_sha256"]:
        raise Unavailable("invalid_archive")

    current = {name: (root / name).read_bytes() for name in CONTRACT_FILES}
    if contract_digest(current) != ref["contract_sha256"]:
        raise Unavailable("incompatible_contract")
    provenance = (f"run={ref['run_id']};attempt={ref['run_attempt']};sha={ref['head_sha']};"
                  f"artifact={ref['artifact_id']};expires={ref['artifact_expires_at']}")
    output({"baseline_path": str(candidate), "baseline_mode": "comparison", "baseline_reason": "",
            "baseline_provenance": provenance, "baseline_run_id": str(ref["run_id"]),
            "baseline_attempt": str(ref["run_attempt"]), "baseline_artifact_id": str(ref["artifact_id"]),
            "baseline_head_sha": ref["head_sha"]})
    print("Checked-in benchmark baseline resolved and verified.")
    return 0


def resolve(args) -> int:
    try:
        return resolve_local(strict_json(args.reference), args.workspace, args.current_run_id)
    except Unavailable as exc:
        return unavailable(exc.reason)


def setup_private(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="btp-baseline-", dir=parent))
    os.chmod(path, 0o700)
    return path


def cli_report(cli: Path, archive: Path, directory: Path) -> dict:
    json_path, markdown_path = directory / "report.json", directory / "report.md"
    try:
        result = subprocess.run(
            [str(cli), "metrics", "stats", "--ci", "--input", str(archive),
             "--output", str(json_path), "--summary-output", str(markdown_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except subprocess.TimeoutExpired:
        raise Unavailable("unsuitable_evidence")
    if result.returncode != 0 or not json_path.is_file() or json_path.stat().st_size > 16 * 1024 * 1024:
        raise Unavailable("unsuitable_evidence")
    try:
        report = json.loads(json_path.read_text())
    except (OSError, json.JSONDecodeError):
        raise Unavailable("unsuitable_evidence")
    archives = report.get("archives") if isinstance(report, dict) else None
    if (not isinstance(archives, list) or len(archives) != 1 or
            not isinstance(archives[0], dict) or archives[0].get("archive", {}).get("complete") is not True):
        raise Unavailable("unsuitable_evidence")
    return report


def validate(args) -> int:
    private = None
    baseline, current = Path(args.baseline), Path(args.current)
    try:
        if (not baseline.is_file() or baseline.is_symlink() or not current.is_file() or current.is_symlink() or
                baseline.stat().st_size == 0 or baseline.stat().st_size > MAX_ARCHIVE):
            raise Unavailable("invalid_archive")
        if baseline.samefile(current) or file_digest(baseline) == file_digest(current):
            raise Unavailable("self_comparison")
        version = subprocess.run([args.report_cli, "version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True, timeout=15)
        if version.returncode != 0 or not re.search(
                rf"(?<![0-9.])v?{re.escape(EXPECTED_CLI.lstrip('v'))}(?![0-9.])", version.stdout):
            raise Unavailable("unsuitable_evidence")
        private = setup_private(Path(args.temp_dir))
        base_report = cli_report(Path(args.report_cli), baseline, private)
        try:
            current_report = cli_report(Path(args.report_cli), current, private)
        except Unavailable as exc:
            raise ValueError(f"current archive preflight failed ({exc.reason}); reporting must still be attempted")
        identities = []
        for report in (base_report, current_report):
            archives = report.get("archives")
            identity = archives[0].get("archive", {}).get("run_id") if isinstance(archives, list) and len(archives) == 1 else None
            if identity is None or str(identity) == "":
                raise Unavailable("identity_unavailable")
            identities.append(str(identity))
        if identities[0] == identities[1]:
            raise Unavailable("self_comparison")
        verifier = Path(__file__).with_name("verify-report.sh")
        for report in (base_report, current_report):
            report_path = private / "candidate.json"
            report_path.write_text(json.dumps(report))
            result = subprocess.run(["bash", str(verifier), str(report_path)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if result.returncode:
                raise Unavailable("unsuitable_evidence")
        output({"baseline_path": str(baseline.resolve()), "baseline_mode": "comparison",
                "baseline_reason": "", "baseline_provenance": args.provenance})
        print("Checked-in baseline archive passed complete-report and lifecycle preflight.")
        return 0
    except Unavailable as exc:
        if private is not None:
            import shutil
            shutil.rmtree(private, ignore_errors=True)
        return unavailable(exc.reason)
    except (OSError, ValueError, subprocess.SubprocessError):
        if private is not None:
            import shutil
            shutil.rmtree(private, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    resolver = sub.add_parser("resolve")
    resolver.add_argument("--reference", type=Path, default=Path("benchmarks/btp/baseline-ref.json"))
    resolver.add_argument("--workspace", type=Path, default=Path("."))
    resolver.add_argument("--current-run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    resolver.set_defaults(func=resolve)
    validator = sub.add_parser("validate")
    validator.add_argument("--baseline", required=True)
    validator.add_argument("--current", required=True)
    validator.add_argument("--report-cli", required=True)
    validator.add_argument("--provenance", required=True)
    validator.add_argument("--temp-dir", default=os.environ.get("RUNNER_TEMP", "/tmp"))
    validator.set_defaults(func=validate)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"baseline helper failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1)
