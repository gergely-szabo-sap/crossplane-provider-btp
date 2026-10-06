#!/usr/bin/env python3
"""Opt-in, private replay of an approved xp-diadromos metrics archive."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

EXPECTED_EXECUTION = "0.9.2"
EXPECTED_REPORT = "0.9.2"
MAX_RESULT_BYTES = 16 * 1024 * 1024


def fail(message: str) -> None:
    raise RuntimeError(message)


def regular_file(path: Path, description: str, executable: bool = False) -> Path:
    if path.is_symlink() or not path.is_file():
        fail(f"{description} must be a regular, non-symlink file")
    if executable and not os.access(path, os.X_OK):
        fail(f"{description} is not executable")
    return path.resolve()


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(args: list[str], *, output: Path | None = None) -> str:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        # Do not relay CLI output: it may contain archive labels or private context.
        fail(f"command failed (exit {result.returncode}): {Path(args[0]).name} {args[1]}")
    if output is not None:
        output.write_bytes(result.stdout)
    return result.stdout.decode("utf-8", errors="strict")


def check_version(cli: Path, expected: str, role: str) -> None:
    text = run([str(cli), "version"])
    if not re.search(rf"(?<![0-9.])v?{re.escape(expected)}(?![0-9.])", text):
        fail(f"{role} CLI version mismatch; expected {expected}")


def dashboard_queries(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    # Deliberately accepts the repository's simple literal query fields only.
    panel_re = re.compile(r'^\s{8}(["\w-]+):\s*\n\s{12}kind: Panel\s*\n\s{12}spec:\s*\n\s{16}display:\s*\n\s{20}name: (.+?)\s*$', re.M)
    starts = list(panel_re.finditer(text))
    if not starts:
        fail("dashboard contains no recognizable panel definitions")
    result: list[tuple[str, str]] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else text.find("    layouts:", match.end())
        if end < 0:
            end = len(text)
        section = text[match.end():end]
        query_match = re.search(r'^\s{32}query: (.+?)\s*$', section, re.M)
        if query_match:
            query = query_match.group(1).strip()
            if query.startswith("'") and query.endswith("'"):
                query = query[1:-1]
            result.append((match.group(1).strip('"'), query))
    if not result:
        fail("dashboard contains no supported Prometheus query fields")
    return result


def api_json(url: str, params: dict[str, str], timeout: float = 10) -> dict:
    request = urllib.request.Request(url + "?" + urllib.parse.urlencode(params))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESULT_BYTES + 1)
    if len(raw) > MAX_RESULT_BYTES:
        fail("loopback query response exceeded its size limit")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("loopback query returned malformed JSON")
    if not isinstance(payload, dict) or payload.get("status") != "success":
        fail("loopback query returned an unsuccessful response")
    return payload


def finite_values(result: object) -> tuple[int, list[dict[str, str]]]:
    if not isinstance(result, dict):
        fail("loopback query result has an invalid shape")
    kind, series = result.get("resultType"), result.get("result")
    if kind not in ("vector", "matrix") or not isinstance(series, list):
        fail("loopback query result has an unsupported shape")
    count = 0
    identities: list[dict[str, str]] = []
    for item in series:
        labels = item.get("metric")
        if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
            fail("loopback query has malformed source labels")
        identities.append(labels)
        field = "value" if kind == "vector" else "values"
        values = [item.get(field)] if kind == "vector" else item.get(field)
        if not isinstance(values, list):
            fail("loopback query has malformed sample values")
        for sample in values:
            if not isinstance(sample, list) or len(sample) != 2:
                fail("loopback query has malformed sample pair")
            try:
                number = float(sample[1])
            except (TypeError, ValueError):
                fail("loopback query has a non-numeric value")
            if math.isnan(number):
                # Prometheus uses NaN for undefined rate-of-sum / rate-of-count
                # windows (for example, an idle histogram window). It is not a
                # measurement and must not be treated as zero or as a failure.
                continue
            if not math.isfinite(number):
                fail("loopback query has a non-finite value")
            count += 1
    return count, identities


def replay_queries(cli: Path, archive: Path, output: Path, queries: list[tuple[str, str]],
                   start: str, end: str) -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    address = f"127.0.0.1:{port}"
    proc = subprocess.Popen(
        [str(cli), "metrics", "serve", "--input", str(archive), "--listen-address", address],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://{address}"
        ready = False
        for _ in range(100):
            if proc.poll() is not None:
                fail("loopback metrics server exited during startup")
            try:
                with urllib.request.urlopen(base + "/-/ready", timeout=0.25) as response:
                    ready = response.status == 200
                    break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.05)
        if not ready:
            fail("loopback metrics server did not become ready")
        saved = []
        for panel, query in queries:
            payload = api_json(base + "/api/v1/query_range", {
                "query": query, "start": start, "end": end, "step": "1s",
            })
            count, identities = finite_values(payload.get("data"))
            saved.append({"panel": panel, "query": query, "finite_samples": count, "source_identities": identities})
        (output / "dashboard-query-results.json").write_text(
            json.dumps(saved, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
        )
        core_panels = {"0_0", "0_1", "2_0", "2_2"}
        counts = {item["panel"]: item["finite_samples"] for item in saved}
        if not core_panels.issubset(counts) or any(counts[panel] == 0 for panel in core_panels):
            fail(f"iteration and lifecycle duration queries must each return finite observations (panels={sorted(counts)}, counts={counts})")
        if any(panel in {"2_1"} for panel in counts):
            fail("obsolete update panel is present in dashboard query replay")
        return sum(item["finite_samples"] for item in saved)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def report_rows(markdown: str, comparison: bool) -> list[list[str]]:
    lines = markdown.splitlines()
    header = "| Measurement | Baseline | Current | Change (%) |" if comparison else "| Measurement | Value |"
    separator = "| --- | ---: | ---: | ---: |" if comparison else "| --- | ---: |"
    starts = [i for i, line in enumerate(lines) if line.strip() in (
        "| Measurement | Value |", "| Measurement | Baseline | Current | Change (%) |",
    )]
    if len(starts) != 1 or lines[starts[0]].strip() != header or starts[0] + 1 >= len(lines) or lines[starts[0] + 1].strip() != separator:
        fail("CI Markdown table does not match the requested report mode")
    rows = []
    for line in lines[starts[0] + 2:]:
        if not line.strip().startswith("|"):
            break
        cells = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(cells) != (4 if comparison else 2):
            fail("CI Markdown contains a malformed measurement row")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 ()-]{0,119}", cells[0]):
            fail("CI Markdown contains an unexpected measurement label")
        value_pattern = re.compile(r"(?:-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:[.][0-9]+)?(?:[eE][+-]?[0-9]+)?(?: (?:ms|count|cores|bytes|millicores|MiB))?|n/a|unavailable|Unavailable — [A-Za-z0-9 ,.;:-]{1,200})\Z", re.IGNORECASE)
        if comparison:
            change_pattern = re.compile(r"(?:[+-]?[0-9]+[.][0-9]%|n/a|unavailable|Unavailable — (?:baseline|current): [A-Za-z0-9 ,.;:_-]{1,180})\Z", re.IGNORECASE)
            valid = all(value_pattern.fullmatch(value) for value in cells[1:3]) and change_pattern.fullmatch(cells[3])
        else:
            valid = value_pattern.fullmatch(cells[1]) is not None
        if not valid:
            fail("CI Markdown contains an unsafe measurement value")
        rows.append(cells)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-cli", required=True, type=Path)
    parser.add_argument("--execution-cli", type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--baseline", type=Path, help="optional approved baseline archive for private comparison replay")
    parser.add_argument("--presentation", type=Path, default=Path("benchmarks/btp/report-presentation.yaml"))
    parser.add_argument("--dashboard", type=Path, default=Path("benchmarks/btp/perses/overview.yaml"))
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        report_cli = regular_file(args.report_cli, "report CLI", executable=True)
        execution_cli = regular_file(args.execution_cli or args.report_cli, "execution CLI", executable=True)
        archive = regular_file(args.archive, "archive")
        baseline = regular_file(args.baseline, "baseline archive") if args.baseline else None
        if baseline is not None and (baseline == archive or sha256_file(baseline) == sha256_file(archive)):
            fail("baseline and current archives must be distinct files and bytes")
        presentation = regular_file(args.presentation, "presentation")
        dashboard = regular_file(args.dashboard, "dashboard")
        out = args.output_dir
        if out.is_symlink() or not out.is_dir() or out.resolve() == Path("/"):
            fail("output directory must be an existing, non-symlink directory")
        if out.stat().st_mode & 0o077:
            fail("output directory must not be accessible by group or other users")
        out = out.resolve()
        check_version(report_cli, EXPECTED_REPORT, "report")
        if args.execution_cli is not None:
            check_version(execution_cli, EXPECTED_EXECUTION, "execution")
        queries = dashboard_queries(dashboard)
        required_panels = {"4_0", "4_1", "4_2", "5_0", "5_1", "5_2"}
        found_panels = {panel for panel, _ in queries}
        if not required_panels.issubset(found_panels):
            fail(f"dashboard is missing diagnostic panels: {sorted(required_panels - found_panels)}")

        stats_path = out / "dashboard-stats.json"
        run([str(execution_cli), "metrics", "stats", "--input", str(archive), "--dashboard", str(dashboard)], output=stats_path)
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        archives = stats.get("archives") if isinstance(stats, dict) else None
        if not isinstance(archives, list) or len(archives) != 1:
            fail("dashboard statistics did not contain exactly one archive")
        archived = archives[0].get("archive", {})
        if archived.get("complete") is not True:
            fail("archive is partial or truncated")
        start, end = archived.get("first_sample"), archived.get("last_sample")
        if not start or not end:
            fail("archive statistics did not include a complete sample range")
        query_count = replay_queries(execution_cli, archive, out, queries, start, end)
        if query_count == 0:
            fail("dashboard queries returned no finite samples")
        if any(panel == "2_1" for panel, _ in queries):
            fail("obsolete update panel is present in dashboard")
        if any("rate(xp_diadromos_k6_" in query for _, query in queries):
            fail("sparse k6 completion-rate query remains in dashboard")

        json_with, md_with = out / "report-with-presentation.json", out / "report-with-presentation.md"
        json_without, md_without = out / "report-without-presentation.json", out / "report-without-presentation.md"
        base = [str(report_cli), "metrics", "stats", "--ci", "--input", str(archive), "--dashboard", str(dashboard)]
        if baseline is not None:
            base += ["--baseline", str(baseline)]
        run(base + ["--presentation", str(presentation), "--output", str(json_with), "--summary-output", str(md_with)])
        run(base + ["--output", str(json_without), "--summary-output", str(md_without)])
        if json_with.read_bytes() != json_without.read_bytes():
            fail("presentation selection changed CI JSON under the same dashboard override")
        report = json.loads(json_with.read_text(encoding="utf-8"))
        checks = report.get("checks")
        if report.get("status") != "not_evaluated" or report.get("policy") is not None or checks not in (None, []):
            fail("report-only status, policy, or checks contract is invalid")
        rows = report_rows(md_with.read_text(encoding="utf-8"), baseline is not None)
        if len(rows) != 28:
            fail(f"presentation has {len(rows)} rows; expected exactly 28")
        if baseline is None and any(value.startswith("Unavailable") or value.lower() in ("n/a", "unavailable") for _, value in rows):
            fail("one or more presentation rows are unavailable")
        verifier = Path(__file__).with_name("verify-report.sh")
        run(["bash", str(verifier), "--comparison", str(json_with)] if baseline is not None else ["bash", str(verifier), str(json_with)])
        (out / "replay-summary.json").write_text(json.dumps({
            "execution_cli": EXPECTED_EXECUTION,
            "report_cli": EXPECTED_REPORT,
            "dashboard_panels": len(queries),
            "finite_query_samples": query_count,
            "presentation_rows": len(rows),
            "report_status": "not_evaluated",
        }, indent=2) + "\n", encoding="utf-8")
        print(f"Archive replay passed: {len(queries)} dashboard queries, {len(rows)} presentation rows; private outputs: {out}")
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Archive replay failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
