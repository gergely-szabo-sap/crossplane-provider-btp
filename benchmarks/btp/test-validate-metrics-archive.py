#!/usr/bin/env python3
"""Offline mechanics tests for validate-metrics-archive.py."""

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

STUB = r'''#!/usr/bin/env python3
import http.server, json, os, pathlib, sys, urllib.parse
version = os.environ.get("STUB_VERSION", "0.8.1")
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
    if os.environ.get("STUB_UNAVAILABLE"):
        md = "| Measurement | Value |\n| --- | ---: |\n| A measurement | Unavailable — missing evidence |\n"
    else:
        md = "| Measurement | Value |\n| --- | ---: |\n| A measurement | 4 ms |\n"
    pathlib.Path(args[args.index("--output") + 1]).write_text(json.dumps(report))
    pathlib.Path(args[args.index("--summary-output") + 1]).write_text(md)
    raise SystemExit(0)
raise SystemExit(2)
'''


def invoke(*, version="0.8.1", fail=False, malformed=False, unavailable=False, expected=0):
    with tempfile.TemporaryDirectory(prefix="btp-replay-test-") as directory:
        root = Path(directory)
        cli = root / "xp-diadromos"
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
        env = dict(os.environ, STUB_VERSION=version, VALID_REPORT=str(VALID_REPORT),
                   COMMAND_LOG=str(command_log), PORT_FILE=str(port_file))
        if fail: env["STUB_FAIL"] = "command"
        if malformed: env["STUB_MALFORMED"] = "1"
        if unavailable: env["STUB_UNAVAILABLE"] = "1"
        command = [sys.executable, str(VALIDATOR), "--report-cli", str(cli),
                   "--archive", str(archive), "--presentation", str(presentation),
                   "--dashboard", str(dashboard), "--output-dir", str(out)]
        result = subprocess.run(command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if (result.returncode == 0) != (expected == 0):
            raise AssertionError(f"unexpected result {result.returncode}: {result.stderr}")
        if expected == 0:
            summary = json.loads((out / "replay-summary.json").read_text())
            assert summary["finite_query_samples"] == 4
            commands = [json.loads(line) for line in command_log.read_text().splitlines()]
            ci = [command for command in commands if command[:3] == ["metrics", "stats", "--ci"]]
            assert len(ci) == 2 and ci[0][ci[0].index("--dashboard") + 1] == ci[1][ci[1].index("--dashboard") + 1]
        if port_file.exists():
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", int(port_file.read_text()))) != 0


def main():
    invoke()
    invoke(version="0.0.0-dev", expected=1)
    invoke(fail=True, expected=1)
    invoke(malformed=True, expected=1)
    invoke(unavailable=True, expected=1)
    print("Archive replay validator synthetic tests passed.")


if __name__ == "__main__":
    main()
