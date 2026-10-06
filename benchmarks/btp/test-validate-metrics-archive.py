#!/usr/bin/env python3
"""Offline mechanics tests for validate-metrics-archive.py."""

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import time

ROOT = Path(__file__).resolve().parent
VALID_REPORT = ROOT / "tests/report-valid.json"
VALIDATOR = ROOT / "validate-metrics-archive.py"
CURRENT_RENDERER = ROOT / "tests/presentation-current-renderer.md"

spec = importlib.util.spec_from_file_location("validate_metrics_archive", VALIDATOR)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

STUB = r'''#!/usr/bin/env python3
import http.server, json, os, pathlib, sys, urllib.parse
cli_name = pathlib.Path(sys.argv[0]).name
version = os.environ.get("STUB_REPORT_VERSION" if cli_name == "report-cli" else "STUB_EXECUTION_VERSION", "0.9.2")
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args == ["version"]:
    print("xp-diadromos " + version)
    raise SystemExit(0)
if os.environ.get("STUB_FAIL") == "command":
    raise SystemExit(9)
if args[:2] == ["metrics", "serve"]:
    address = args[args.index("--listen-address") + 1]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if os.environ.get("STUB_MALFORMED"):
                body = b"not-json"
            elif self.path.startswith("/-/ready"):
                body = b"Prometheus is Ready\\n"
            elif os.environ.get("STUB_NAN"):
                body = json.dumps({"status":"success","data":{"resultType":"matrix","result":[{"metric":{"archive_filename":"fixture"},"values":[[1,"NaN"],[2,"4.25"]]}]}}).encode()
            elif os.environ.get("STUB_INFINITY"):
                body = json.dumps({"status":"success","data":{"resultType":"matrix","result":[{"metric":{"archive_filename":"fixture"},"values":[[1,"Infinity"],[2,"4.25"]]}]}}).encode()
            else:
                body = json.dumps({"status":"success","data":{"resultType":"vector","result":[{"metric":{"archive_filename":"fixture"},"value":[1,"4.25"]}]}}).encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args): pass
    host, port = address.rsplit(":", 1)
    pathlib.Path(os.environ["PORT_FILE"]).write_text(port)
    http.server.ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
if args[:2] == ["metrics", "stats"]:
    if "--ci" not in args:
        print(json.dumps({"archives":[{"archive":{"complete":True,"first_sample":"2026-10-02T14:49:27Z","last_sample":"2026-10-02T15:13:27Z"}}]}))
        raise SystemExit(0)
    report = json.loads(pathlib.Path(os.environ["VALID_REPORT"]).read_text())
    comparison = "--baseline" in args
    if comparison:
        report["archives"][0]["archive"]["run_id"] = "baseline-run"
        current = json.loads(json.dumps(report["archives"][0]))
        current["archive"]["run_id"] = "current-run"
        report["archives"].append(current)
    checks = os.environ.get("STUB_CHECKS", "array")
    if checks == "null":
        report["checks"] = None
    elif checks == "nonempty":
        report["checks"] = [{"status": "failed"}]
    current_fixture = pathlib.Path(os.environ["CURRENT_RENDERER_FIXTURE"]).read_text().splitlines()[2:]
    rows = [tuple(cell.strip() for cell in line.strip().strip("|").split("|")) for line in current_fixture if line.startswith("|")]
    if comparison:
        md = pathlib.Path(os.environ["COMPARISON_RENDERER_FIXTURE"]).read_text()
        if os.environ.get("STUB_UNAVAILABLE"):
            md = "| Measurement | Baseline | Current | Change (%) |\n| --- | ---: | ---: | ---: |\n" + "".join(
                f"| {label} | Unavailable — missing evidence | 2 ms | Unavailable — baseline: missing evidence |\n"
                for label, _, _, _ in [tuple(cell.strip() for cell in line.strip().strip("|").split("|")) for line in md.splitlines()[2:] if line.startswith("|")]
            )
    else:
        if os.environ.get("STUB_UNAVAILABLE"):
            rows = [(label, "Unavailable — missing evidence") for label, _ in rows]
        md = "| Measurement | Value |\n| --- | ---: |\n" + "".join(f"| {label} | {value} |\n" for label, value in rows)
    pathlib.Path(args[args.index("--output") + 1]).write_text(json.dumps(report))
    pathlib.Path(args[args.index("--summary-output") + 1]).write_text(md)
    raise SystemExit(0)
raise SystemExit(2)
'''


def invoke(*, report_version="0.9.2", execution_version="0.9.2", fail=False, malformed=False, unavailable=False, nan=False, infinity=False, checks="array", baseline=False, expected=0):
    with tempfile.TemporaryDirectory(prefix="btp-replay-test-") as directory:
        root = Path(directory)
        report_cli = root / "report-cli"
        execution_cli = root / "execution-cli"
        for cli in (report_cli, execution_cli):
            cli.write_text(textwrap.dedent(STUB))
            cli.chmod(0o700)
        archive = root / "fixture.tsdb.tar.zst"
        archive.write_bytes(b"synthetic-not-an-archive")
        presentation = root / "presentation.yaml"
        presentation.write_text("schema_version: v1\nmeasurements: []\n")
        dashboard = root / "dashboard.yaml"
        panel_specs = [
            ("0_0", "Iteration", 'xp_diadromos_k6_iteration_duration_mean{scenario="create_delete"}'),
            ("0_1", "Iterations", 'xp_diadromos_k6_iterations_total{scenario="create_delete"}'),
            ("2_0", "Ready", 'xp_diadromos_k6_xp_time_to_ready_mean{scenario="create_delete"}'),
            ("2_2", "Absent", 'xp_diadromos_k6_xp_time_to_delete_mean{scenario="create_delete"}'),
            ("4_0", "Reconcile duration", 'sum by (controller, archive_filename) (rate(controller_runtime_reconcile_time_seconds_sum{job="provider"}[5m])) / sum by (controller, archive_filename) (rate(controller_runtime_reconcile_time_seconds_count{job="provider"}[5m]))'),
            ("4_1", "Queue depth", 'sum by (controller, archive_filename) (workqueue_depth{job="provider"})'),
            ("4_2", "Queue wait", 'sum by (controller, archive_filename) (rate(workqueue_queue_duration_seconds_sum{job="provider"}[5m])) / sum by (controller, archive_filename) (rate(workqueue_queue_duration_seconds_count{job="provider"}[5m]))'),
            ("5_0", "Request phases", 'xp_diadromos_k6_xp_lifecycle_phase_duration_mean{scenario="create_delete",stage=~"create_request|delete_request"}'),
            ("5_1", "Wait phases", 'xp_diadromos_k6_xp_lifecycle_phase_duration_mean{scenario="create_delete",stage=~"readiness|kubernetes_absence_wait"}'),
            ("5_2", "External operations", 'sum by (operation, archive_filename) (rate(upjet_resource_ext_api_duration_sum{job="provider"}[5m])) / sum by (operation, archive_filename) (rate(upjet_resource_ext_api_duration_count{job="provider"}[5m]))'),
        ]
        panels = []
        for panel_id, name, query in panel_specs:
            panels.append(f'''        "{panel_id}":
            kind: Panel
            spec:
                display:
                    name: {name}
                queries:
                    - kind: TimeSeriesQuery
                      spec:
                        plugin:
                            kind: PrometheusTimeSeriesQuery
                            spec:
                                query: {query}
''')
        dashboard.write_text("kind: Dashboard\nspec:\n    panels:\n" + "".join(panels))
        out = root / "private-output"
        out.mkdir(mode=0o700)
        command_log = root / "commands.jsonl"
        port_file = root / "port"
        env = dict(os.environ, STUB_REPORT_VERSION=report_version,
                   STUB_EXECUTION_VERSION=execution_version, VALID_REPORT=str(VALID_REPORT),
                   CURRENT_RENDERER_FIXTURE=str(CURRENT_RENDERER),
                   COMPARISON_RENDERER_FIXTURE=str(ROOT / "tests/presentation-comparison-renderer.md"),
                   COMMAND_LOG=str(command_log), PORT_FILE=str(port_file))
        if fail: env["STUB_FAIL"] = "command"
        if malformed: env["STUB_MALFORMED"] = "1"
        if unavailable: env["STUB_UNAVAILABLE"] = "1"
        if nan: env["STUB_NAN"] = "1"
        if infinity: env["STUB_INFINITY"] = "1"
        env["STUB_CHECKS"] = checks
        baseline_archive = root / "baseline.tsdb.tar.zst"
        baseline_archive.write_bytes(b"synthetic-baseline-archive")
        command = [sys.executable, str(VALIDATOR), "--report-cli", str(report_cli),
                   "--archive", str(archive), "--presentation", str(presentation),
                   *( ["--baseline", str(baseline_archive)] if baseline else []),
                   "--dashboard", str(dashboard), "--output-dir", str(out),
                   "--execution-cli", str(execution_cli)]
        result = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if (result.returncode == 0) != (expected == 0):
            raise AssertionError(f"unexpected result {result.returncode}: {result.stderr}")
        if expected == 0:
            summary = json.loads((out / "replay-summary.json").read_text())
            assert summary["finite_query_samples"] == 10
            assert summary["presentation_rows"] == 28
            commands = [json.loads(line) for line in command_log.read_text().splitlines()]
            ci = [command for command in commands if command[:3] == ["metrics", "stats", "--ci"]]
            assert len(ci) == 2 and ci[0][ci[0].index("--dashboard") + 1] == ci[1][ci[1].index("--dashboard") + 1]
            if baseline and not unavailable:
                markdown = (out / "report-with-presentation.md").read_text()
                assert "Unavailable — percentage change requires a strictly positive baseline" in markdown
                assert "| Subaccount observed reconciliation errors | 0 count | 1 count | Unavailable — percentage change requires a strictly positive baseline |" in markdown
        if port_file.exists():
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", int(port_file.read_text()))) != 0


def test_report_rows_renderer_fixture():
    markdown = CURRENT_RENDERER.read_text()
    rows = validator.report_rows(markdown, comparison=False)
    assert len(rows) == 28
    assert rows[5][0] == "Mean time until Subaccount is Ready (client-observed)"
    assert rows[7][0] == "Mean time until Subaccount Kubernetes object is absent (client-observed)"
    for escaped in (r"Unsupported \q label", r"Unsupported \x28 label", "<b>unsafe</b>", "[link](https://example.com)"):
        malformed = markdown.replace("Provider container mean CPU usage", escaped, 1)
        try:
            validator.report_rows(malformed, comparison=False)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"unsafe measurement label accepted: {escaped!r}")

    comparison = (ROOT / "tests/presentation-comparison-renderer.md").read_text()
    comparison_rows = validator.report_rows(comparison, comparison=True)
    assert len(comparison_rows) == 28
    assert comparison_rows[23][1:4] == ["0 count", "1 count", "Unavailable — percentage change requires a strictly positive baseline"]
    for reason in validator.UNAVAILABLE_CHANGE_REASONS:
        assert f"Unavailable — {reason}" in comparison
        malformed = comparison.replace(f"Unavailable — {reason}", f"Unavailable — {reason}!")
        try:
            validator.report_rows(malformed, comparison=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"hostile unavailable reason suffix accepted: {reason!r}")
    for hostile in ("Unavailable — made-up reason", "Unavailable — baseline: ok [link](https://example.com)", "Unavailable — metric type differs between runs <b>x</b>"):
        malformed = comparison.replace("+4.0%", hostile, 1)
        try:
            validator.report_rows(malformed, comparison=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"unsafe comparison reason accepted: {hostile!r}")


def main():
    test_report_rows_renderer_fixture()
    invoke()
    invoke(report_version="0.0.0-dev", expected=1)
    invoke(execution_version="0.8.1", expected=1)
    invoke(fail=True, expected=1)
    invoke(malformed=True, expected=1)
    invoke(nan=True)
    invoke(infinity=True, expected=1)
    invoke(unavailable=True, expected=1)
    invoke(checks="null")
    invoke(checks="nonempty", expected=1)
    invoke(baseline=True)
    invoke(baseline=True, unavailable=True)
    print("Archive replay validator synthetic tests passed.")


if __name__ == "__main__":
    main()
