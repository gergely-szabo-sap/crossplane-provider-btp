#!/usr/bin/env python3
"""Resolve and validate one explicitly designated BTP benchmark artifact."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

EXECUTION_CLI = "v0.9.2"
WORKFLOW = ".github/workflows/run-btp-benchmark.yaml"
ARTIFACT_PREFIX = "btp-synthetic-benchmark-metrics-"
MAX_JSON = 1 << 20
MAX_ARCHIVE = 1 << 30
MAX_ZIP = 1 << 30
CONTRACT_FILES = (
    "benchmarks/btp/config.yaml",
    "benchmarks/btp/k6/subaccount-create-delete.js",
    "benchmarks/btp/k6/crossplane-helpers.js",
    "benchmarks/btp/perses/overview.yaml",
    "benchmarks/btp/report-presentation.yaml",
)
FIELDS = {"run_id", "run_attempt", "artifact_id", "head_sha", "archive_sha256", "contract_sha256", "execution_cli", "environment_revision"}
REASONS = {"not_configured", "expired", "not_found", "access_denied", "download_unavailable", "invalid_reference", "invalid_archive", "incompatible_contract", "environment_revision_mismatch", "self_comparison", "identity_unavailable", "unsuitable_evidence"}

class Unavailable(Exception):
    def __init__(self, reason: str):
        self.reason = reason if reason in REASONS else "download_unavailable"


def fail(message: str) -> None:
    raise ValueError(message)


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
    if not isinstance(value, dict) or set(value) != {"schema_version", "baseline"} or value["schema_version"] != "v1":
        raise Unavailable("invalid_reference")
    ref = value["baseline"]
    if ref is None:
        raise Unavailable("not_configured")
    if not isinstance(ref, dict) or set(ref) != FIELDS:
        raise Unavailable("invalid_reference")
    for key in ("run_id", "run_attempt", "artifact_id"):
        if type(ref[key]) is not int or ref[key] <= 0:
            raise Unavailable("invalid_reference")
    for key, pattern in (("head_sha", r"[0-9a-f]{40}"), ("archive_sha256", r"[0-9a-f]{64}"), ("contract_sha256", r"[0-9a-f]{64}")):
        if not isinstance(ref[key], str) or not re.fullmatch(pattern, ref[key]):
            raise Unavailable("invalid_reference")
    if ref["execution_cli"] != EXECUTION_CLI or not isinstance(ref["environment_revision"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", ref["environment_revision"]):
        raise Unavailable("invalid_reference")
    return ref


def contract_digest(contents: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name in (*CONTRACT_FILES, "execution-cli"):
        data = contents[name] if name != "execution-cli" else EXECUTION_CLI.encode()
        name_bytes = name.encode()
        h.update(len(name_bytes).to_bytes(4, "big")); h.update(name_bytes)
        h.update(len(data).to_bytes(8, "big")); h.update(data)
    return h.hexdigest()


class HTTPSNoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow HTTPS redirects without forwarding headers across origins."""
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        source = urllib.parse.urlsplit(request.full_url)
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https":
            raise Unavailable("download_unavailable")

        same_origin = (source.scheme.lower(), source.hostname, source.port) == (
            target.scheme.lower(), target.hostname, target.port)
        safe_headers = {}
        if same_origin:
            safe_headers = {key: value for key, value in request.header_items()
                            if key.lower() not in {"authorization", "proxy-authorization", "cookie"}}
        return urllib.request.Request(newurl, headers=safe_headers, method="GET")


class GitHub:
    def __init__(self, token: str, repository: str, api: str = "https://api.github.com"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise Unavailable("invalid_reference")
        parsed_api = urllib.parse.urlsplit(api)
        if parsed_api.scheme != "https" or not parsed_api.hostname or parsed_api.username or parsed_api.password or parsed_api.query or parsed_api.fragment:
            raise Unavailable("invalid_reference")
        self.repo, self.api, self.token = repository, api.rstrip("/"), token

    def get(self, path: str, *, binary=False, limit=MAX_JSON):
        url = self.api + path
        if not url.startswith(self.api + "/"):
            raise Unavailable("download_unavailable")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403): raise Unavailable("access_denied")
            if exc.code == 404: raise Unavailable("not_found")
            raise Unavailable("download_unavailable")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Unavailable("download_unavailable")
        if len(payload) > limit:
            raise Unavailable("download_unavailable")
        if binary:
            return payload
        try:
            return json.loads(payload)
        except (UnicodeError, json.JSONDecodeError):
            raise Unavailable("download_unavailable")

    def source_bytes(self, sha: str) -> dict[str, bytes]:
        result = {}
        for name in CONTRACT_FILES:
            path = urllib.parse.quote(name, safe="/")
            item = self.get(f"/repos/{self.repo}/contents/{path}?ref={sha}")
            if not isinstance(item, dict) or item.get("encoding") != "base64" or not isinstance(item.get("content"), str):
                raise Unavailable("incompatible_contract")
            # GitHub Contents responses may wrap Base64 transport text in CR/LF.
            # Normalize only those line breaks; retain strict alphabet/padding checks.
            encoded = item["content"].replace("\r", "").replace("\n", "")
            try: result[name] = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error): raise Unavailable("incompatible_contract")
        return result

    def run_and_artifact(self, ref: dict):
        run = self.get(f"/repos/{self.repo}/actions/runs/{ref['run_id']}?exclude_pull_requests=true")
        workflow_path = str(run.get("path", "")).split("@", 1)[0] if isinstance(run, dict) else ""
        if not isinstance(run, dict) or run.get("id") != ref["run_id"] or workflow_path != WORKFLOW:
            raise Unavailable("invalid_reference")
        repository = self.get(f"/repos/{self.repo}")
        default_branch = repository.get("default_branch") if isinstance(repository, dict) else None
        if run.get("event") != "workflow_dispatch" or not default_branch or run.get("head_branch") != default_branch:
            raise Unavailable("invalid_reference")
        if run.get("head_sha") != ref["head_sha"] or run.get("status") != "completed" or run.get("conclusion") != "success" or run.get("run_attempt") != ref["run_attempt"]:
            raise Unavailable("invalid_reference")
        a = self.get(f"/repos/{self.repo}/actions/artifacts/{ref['artifact_id']}")
        name = f"{ARTIFACT_PREFIX}{ref['run_id']}-{ref['run_attempt']}"
        if not isinstance(a, dict) or a.get("id") != ref["artifact_id"]:
            raise Unavailable("not_found")
        artifact_run = a.get("workflow_run") if isinstance(a, dict) else None
        if (not isinstance(artifact_run, dict) or artifact_run.get("id") != ref["run_id"] or
            artifact_run.get("head_sha") != ref["head_sha"]):
            raise Unavailable("invalid_reference")
        if a.get("name") != name or a.get("expired") is not False:
            raise Unavailable("expired" if a.get("expired") is True else "invalid_reference")
        if type(a.get("size_in_bytes")) is not int or not 0 < a["size_in_bytes"] <= MAX_ZIP:
            raise Unavailable("invalid_archive")
        return run, a

    def download_artifact(self, artifact_id: int, destination: Path) -> None:
        url = f"{self.api}/repos/{self.repo}/actions/artifacts/{artifact_id}/zip"
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer " + self.token, "X-GitHub-Api-Version": "2022-11-28"})
        # Explicitly drop credentials on every redirect, including same-origin redirects.
        opener = urllib.request.build_opener(HTTPSNoAuthRedirect())
        try:
            with opener.open(request, timeout=30) as response:
                final = urllib.parse.urlsplit(response.geturl())
                host = final.hostname or ""
                api_host = urllib.parse.urlsplit(self.api).hostname
                allowed_redirect = (host == "results-receiver.actions.githubusercontent.com" or
                                    host.endswith(".blob.core.windows.net") or host.endswith(".amazonaws.com") or
                                    host.endswith(".githubusercontent.com"))
                if final.scheme != "https" or (host != api_host and not allowed_redirect):
                    raise Unavailable("download_unavailable")
                with destination.open("wb") as out:
                    os.chmod(destination, 0o600)
                    total = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk: break
                        total += len(chunk)
                        if total > MAX_ZIP: raise Unavailable("invalid_archive")
                        out.write(chunk)
        except Unavailable: raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            raise Unavailable("download_unavailable")


def safe_extract(zip_path: Path, destination: Path) -> Path:
    if zip_path.stat().st_size > MAX_ZIP: raise Unavailable("invalid_archive")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            if len(infos) != 1: raise Unavailable("invalid_archive")
            item = infos[0]
            name = item.filename
            mode = item.external_attr >> 16
            if (not name or "\\" in name or name.startswith("/") or PurePosixPath(name).name != name or ".." in PurePosixPath(name).parts or
                item.is_dir() or (stat.S_IFMT(mode) and not stat.S_ISREG(mode)) or item.file_size <= 0 or item.file_size > MAX_ARCHIVE or
                not name.endswith(".tsdb.tar.zst") or ".partial" in name):
                raise Unavailable("invalid_archive")
            target = destination / name
            total = 0
            with archive.open(item) as src, target.open("xb") as dst:
                os.chmod(target, 0o600)
                while True:
                    data = src.read(1024 * 1024)
                    if not data: break
                    total += len(data)
                    if total > MAX_ARCHIVE: raise Unavailable("invalid_archive")
                    dst.write(data)
            if total != item.file_size or total == 0: raise Unavailable("invalid_archive")
            return target
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError, zlib.error):
        raise Unavailable("invalid_archive")


def output(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                if "\n" in value or "\r" in value: fail("invalid output")
                stream.write(f"{key}={value}\n")


def unavailable(reason: str) -> int:
    output({"baseline_path": "", "baseline_mode": "current-only", "baseline_reason": reason, "baseline_provenance": "", "baseline_run_id": "", "baseline_attempt": "", "baseline_artifact_id": "", "baseline_head_sha": ""})
    print(f"Baseline unavailable ({reason}); current-only reporting will be used.")
    return 0


def setup_private(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="btp-baseline-", dir=parent))
    os.chmod(path, 0o700)
    return path


def resolve(args) -> int:
    private = None
    try:
        ref = strict_json(args.reference)
        if args.current_run_id and ref["run_id"] == int(args.current_run_id): raise Unavailable("self_comparison")
        env_revision = os.environ.get("BTP_BENCHMARK_ENV_REVISION", "")
        if not env_revision or env_revision != ref["environment_revision"]: raise Unavailable("environment_revision_mismatch")
        token = os.environ.get("GH_TOKEN", "")
        if not token: raise Unavailable("access_denied")
        gh = GitHub(token, args.repository, args.api_url)
        run, artifact = gh.run_and_artifact(ref)
        contents = gh.source_bytes(ref["head_sha"])
        if contract_digest(contents) != ref["contract_sha256"]: raise Unavailable("incompatible_contract")
        current = {name: (args.workspace / name).read_bytes() for name in CONTRACT_FILES}
        if contract_digest(current) != ref["contract_sha256"]: raise Unavailable("incompatible_contract")
        private = setup_private(Path(args.temp_dir))
        zip_path = private / "artifact.zip"
        gh.download_artifact(ref["artifact_id"], zip_path)
        archive = safe_extract(zip_path, private)
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
        if digest.hexdigest() != ref["archive_sha256"]: raise Unavailable("invalid_archive")
        staged = private / f"baseline-{ref['run_id']}-{ref['run_attempt']}.tsdb.tar.zst"
        archive.rename(staged)
        zip_path.unlink(missing_ok=True)
        expires = artifact.get("expires_at", "unknown")
        if not isinstance(expires, str) or not re.fullmatch(r"[0-9TZ:.-]{1,40}", expires):
            raise Unavailable("invalid_reference")
        provenance = f"run={ref['run_id']};attempt={ref['run_attempt']};sha={ref['head_sha']};artifact={ref['artifact_id']};expires={expires}"
        output({"baseline_path": str(staged), "baseline_mode": "comparison", "baseline_reason": "", "baseline_provenance": provenance,
                "baseline_run_id": str(ref["run_id"]), "baseline_attempt": str(ref["run_attempt"]),
                "baseline_artifact_id": str(ref["artifact_id"]), "baseline_head_sha": ref["head_sha"]})
        print("Designated benchmark artifact resolved and staged privately.")
        return 0
    except Unavailable as exc:
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)
        return unavailable(exc.reason)
    except (OSError, ValueError, subprocess.SubprocessError):
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)
        raise


def cli_report(cli: Path, archive: Path, directory: Path) -> dict:
    j, m = directory / "report.json", directory / "report.md"
    try:
        result = subprocess.run([str(cli), "metrics", "stats", "--ci", "--input", str(archive), "--output", str(j), "--summary-output", str(m)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    except subprocess.TimeoutExpired:
        raise Unavailable("unsuitable_evidence")
    if result.returncode != 0 or not j.is_file() or j.stat().st_size > 16 * 1024 * 1024:
        raise Unavailable("unsuitable_evidence")
    try: report = json.loads(j.read_text())
    except (OSError, json.JSONDecodeError): raise Unavailable("unsuitable_evidence")
    archives = report.get("archives") if isinstance(report, dict) else None
    if not isinstance(archives, list) or len(archives) != 1 or archives[0].get("archive", {}).get("complete") is not True:
        raise Unavailable("unsuitable_evidence")
    return report


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()


def validate(args) -> int:
    private = None
    baseline = Path(args.baseline)
    current = Path(args.current)
    try:
        if not baseline.is_file() or baseline.is_symlink() or not current.is_file() or current.is_symlink(): raise Unavailable("invalid_archive")
        if baseline.stat().st_size == 0 or baseline.stat().st_size > MAX_ARCHIVE: raise Unavailable("invalid_archive")
        if baseline.samefile(current) or file_digest(baseline) == file_digest(current): raise Unavailable("self_comparison")
        version = subprocess.run([args.report_cli, "version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15)
        if version.returncode != 0 or not re.search(r"(?<![0-9.])v?0\.9\.2(?![0-9.])", version.stdout):
            raise Unavailable("unsuitable_evidence")
        private = setup_private(Path(args.temp_dir))
        base_report = cli_report(Path(args.report_cli), baseline, private)
        try:
            curr_report = cli_report(Path(args.report_cli), current, private)
        except Unavailable as exc:
            fail(f"current archive preflight failed ({exc.reason}); reporting must still be attempted")
        ids = []
        for report in (base_report, curr_report):
            archives = report.get("archives")
            identity = archives[0].get("archive", {}).get("run_id") if isinstance(archives, list) and len(archives) == 1 else None
            if identity is None or str(identity) == "": raise Unavailable("identity_unavailable")
            ids.append(str(identity))
        if ids[0] == ids[1]: raise Unavailable("self_comparison")
        verifier = Path(__file__).with_name("verify-report.sh")
        for report in (base_report, curr_report):
            # Run independently to retain verifier's functional evidence contract.
            path = private / "candidate.json"
            path.write_text(json.dumps(report))
            result = subprocess.run(["bash", str(verifier), str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if result.returncode: raise Unavailable("unsuitable_evidence")
        output({"baseline_path": str(baseline.resolve()), "baseline_mode": "comparison", "baseline_reason": "", "baseline_provenance": args.provenance})
        print("Designated baseline archive passed complete-report and lifecycle preflight.")
        return 0
    except Unavailable as exc:
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)
        return unavailable(exc.reason)
    except (OSError, ValueError, subprocess.SubprocessError):
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)
        raise


def designate(args) -> int:
    # Designation deliberately writes only a metadata descriptor; it never dispatches or uploads.
    try:
        token = os.environ.get("GH_TOKEN", "")
        if not token: raise Unavailable("access_denied")
        if os.environ.get("BTP_BENCHMARK_ENV_REVISION") != args.environment_revision: raise Unavailable("environment_revision_mismatch")
        gh = GitHub(token, args.repository, args.api_url)
        ref = {"run_id": args.run_id, "run_attempt": args.run_attempt, "artifact_id": args.artifact_id,
               "head_sha": args.head_sha, "archive_sha256": "0" * 64, "contract_sha256": "0" * 64,
               "execution_cli": EXECUTION_CLI, "environment_revision": args.environment_revision}
        _, _ = gh.run_and_artifact(ref)
        source = gh.source_bytes(args.head_sha)
        private = setup_private(Path(args.temp_dir)); zip_path = private / "artifact.zip"
        gh.download_artifact(args.artifact_id, zip_path)
        archive = safe_extract(zip_path, private)
        ref["archive_sha256"] = file_digest(archive)
        ref["contract_sha256"] = contract_digest(source)
        if contract_digest({name: (Path.cwd() / name).read_bytes() for name in CONTRACT_FILES}) != ref["contract_sha256"]:
            raise Unavailable("incompatible_contract")
        report_dir = setup_private(Path(args.temp_dir))
        report = cli_report(Path(args.report_cli), archive, report_dir)
        version = subprocess.run([args.report_cli, "version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15)
        if version.returncode != 0 or not re.search(r"(?<![0-9.])v?0\.9\.2(?![0-9.])", version.stdout): raise Unavailable("unsuitable_evidence")
        identity = report.get("archives", [{}])[0].get("archive", {}).get("run_id")
        if not isinstance(identity, str) or not identity.strip(): raise Unavailable("identity_unavailable")
        report_file = report_dir / "candidate.json"
        report_file.write_text(json.dumps(report))
        verified = subprocess.run(["bash", str(Path(__file__).with_name("verify-report.sh")), str(report_file)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if verified.returncode: raise Unavailable("unsuitable_evidence")
        target = Path(args.output)
        if target.exists() or target.is_symlink(): fail("designation output already exists; refusing overwrite")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"schema_version": "v1", "baseline": ref}, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(target, 0o600)
        print("Wrote non-secret baseline descriptor; review before replacing the repository reference.")
        return 0
    except Unavailable as exc:
        print(f"Designation failed ({exc.reason}).", file=sys.stderr); return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    common.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    common.add_argument("--temp-dir", default=os.environ.get("RUNNER_TEMP", "/tmp"))
    r = sub.add_parser("resolve", parents=[common]); r.add_argument("--reference", type=Path, default=Path("benchmarks/btp/baseline-ref.json")); r.add_argument("--workspace", type=Path, default=Path(".")); r.add_argument("--current-run-id", default=os.environ.get("GITHUB_RUN_ID", "")); r.set_defaults(func=resolve)
    v = sub.add_parser("validate"); v.add_argument("--baseline", required=True); v.add_argument("--current", required=True); v.add_argument("--report-cli", required=True); v.add_argument("--provenance", required=True); v.add_argument("--temp-dir", default=os.environ.get("RUNNER_TEMP", "/tmp")); v.set_defaults(func=validate)
    d = sub.add_parser("designate", parents=[common]); d.add_argument("--run-id", required=True, type=int); d.add_argument("--run-attempt", required=True, type=int); d.add_argument("--artifact-id", required=True, type=int); d.add_argument("--head-sha", required=True); d.add_argument("--environment-revision", required=True); d.add_argument("--report-cli", required=True); d.add_argument("--output", required=True); d.set_defaults(func=designate)
    args = parser.parse_args()
    if args.command == "designate" and (args.run_id <= 0 or args.run_attempt <= 0 or args.artifact_id <= 0 or not re.fullmatch(r"[0-9a-f]{40}", args.head_sha) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.environment_revision)):
        parser.error("invalid designation identifiers or environment revision")
    return args.func(args)

if __name__ == "__main__":
    try: raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"baseline helper failed: {type(exc).__name__}", file=sys.stderr); raise SystemExit(1)
