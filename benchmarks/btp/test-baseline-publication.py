#!/usr/bin/env python3
"""Offline end-to-end tests for baseline preflight and report publication contracts."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parent
HELPER = ROOT / "baseline-artifact.py"
VALID = json.loads((ROOT / "tests/report-valid.json").read_text())

CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if args == ["version"]:
    print("xp-diadromos v0.9.4")
    raise SystemExit(0)
archive = pathlib.Path(args[args.index("--input") + 1])
if os.environ.get("FAIL_ARCHIVE") == archive.name:
    raise SystemExit(19)
report = json.loads(pathlib.Path(os.environ["FIXTURE_REPORT"]).read_text())
run_id = "same-internal-run" if os.environ.get("SAME_RUN") == "1" else archive.name
report["archives"][0]["archive"].update(
    complete=not (os.environ.get("BAD_CURRENT") == "1" and archive.name.startswith("current")),
    run_id=run_id)
if ((os.environ.get("BAD_BASELINE") == "1" and archive.name.startswith("baseline-")) or
        (os.environ.get("BAD_CURRENT") == "1" and archive.name.startswith("current"))):
    report["archives"][0]["k6_metrics"] = []
pathlib.Path(args[args.index("--output") + 1]).write_text(json.dumps(report))
pathlib.Path(args[args.index("--summary-output") + 1]).write_text("private synthetic report")
'''


def invoke(directory: Path, *, fail_archive="", bad_baseline=False, bad_current=False,
           same_run=False, same_file=False, same_bytes=False):
    cli = directory / "xp-diadromos"
    cli.write_text(textwrap.dedent(CLI))
    cli.chmod(0o700)
    baseline = directory / "baseline-123-2.tsdb.tar.zst"
    current = directory / "current.tsdb.tar.zst"
    baseline.write_bytes(b"baseline bytes")
    current.write_bytes(b"baseline bytes" if same_bytes else b"current bytes")
    if same_file:
        current = baseline
    output = directory / "github-output"
    env = dict(os.environ, GITHUB_OUTPUT=str(output), FIXTURE_REPORT=str(directory / "report.json"),
               FAIL_ARCHIVE=fail_archive, BAD_BASELINE="1" if bad_baseline else "0",
               BAD_CURRENT="1" if bad_current else "0", SAME_RUN="1" if same_run else "0")
    (directory / "report.json").write_text(json.dumps(VALID))
    command = ["python3", str(HELPER), "validate", "--baseline", str(baseline),
               "--current", str(current), "--report-cli", str(cli),
               "--provenance", "run=123;attempt=2", "--temp-dir", str(directory)]
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=20), output


def main():
    with tempfile.TemporaryDirectory(prefix="btp-baseline-publication-") as td:
        root = Path(td)
        result, output = invoke(root)
        assert result.returncode == 0, result.stderr
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        assert values["baseline_mode"] == "comparison"
        assert values["baseline_path"].endswith("baseline-123-2.tsdb.tar.zst")
        assert values["baseline_provenance"] == "run=123;attempt=2"
        comparison = json.loads(json.dumps(VALID))
        baseline_report = json.loads(json.dumps(VALID))
        baseline_report["archives"][0]["archive"].update(complete=True, run_id="baseline-run")
        current_report = json.loads(json.dumps(VALID))
        current_report["archives"][0]["archive"].update(complete=True, run_id="current-run")
        comparison["archives"] = [baseline_report["archives"][0], current_report["archives"][0]]
        report_path = root / "comparison.json"
        report_path.write_text(json.dumps(comparison))
        verified = subprocess.run(["bash", str(ROOT / "verify-report.sh"), "--comparison", str(report_path)],
                                  capture_output=True, text=True, timeout=20)
        assert verified.returncode == 0, verified.stderr
        assert "Baseline archive" in verified.stdout and "Current archive" in verified.stdout

    with tempfile.TemporaryDirectory(prefix="btp-baseline-fallback-") as td:
        result, output = invoke(Path(td), bad_baseline=True)
        assert result.returncode == 0, result.stderr
        assert "baseline_mode=current-only" in output.read_text()
        assert "baseline_reason=unsuitable_evidence" in output.read_text()
        assert "baseline_path=" in output.read_text()

    with tempfile.TemporaryDirectory(prefix="btp-current-report-failure-") as td:
        result, output = invoke(Path(td), fail_archive="current.tsdb.tar.zst")
        assert result.returncode != 0
        assert not output.exists() or "baseline_mode=current-only" not in output.read_text()
        assert result.returncode != 0 and "baseline helper failed" in result.stderr

    for label, options, reason in (
        ("same-id", {"same_run": True}, "self_comparison"),
        ("same-bytes", {"same_bytes": True}, "self_comparison"),
        ("same-file", {"same_file": True}, "self_comparison"),
        ("invalid-current", {"bad_current": True}, "current archive preflight failed"),
    ):
        with tempfile.TemporaryDirectory(prefix=f"btp-{label}-") as td:
            result, output = invoke(Path(td), **options)
            if label == "invalid-current":
                assert result.returncode != 0 and "baseline helper failed: ValueError" in result.stderr
                assert not output.exists() or "baseline_mode=current-only" not in output.read_text()
            else:
                assert result.returncode == 0, result.stderr
                assert f"baseline_reason={reason}" in output.read_text()

    workflow = (ROOT.parent.parent / ".github/workflows/run-btp-benchmark.yaml").read_text()
    resolve = workflow.index("- name: Resolve checked-in benchmark baseline")
    install_cli = workflow.index("- name: Install CLI for baseline preflight")
    validate = workflow.index("- name: Validate baseline and current report evidence")
    report = workflow.index("- name: Publish available benchmark report")
    verify = workflow.index("- name: Verify published report contract")
    upload = workflow.index("- name: Upload metrics archive")
    failure = workflow.index("- name: Fail if no archive was produced")
    assert resolve < install_cli < validate < report < verify < upload < failure
    resolver_step = workflow[resolve:install_cli]
    assert "BTP_BENCHMARK_ENV_REVISION" in resolver_step
    assert "GH_TOKEN" not in resolver_step and "actions: read" not in workflow
    assert "actions/artifacts" not in resolver_step and "expired)" not in workflow
    assert "baseline_artifact_link" not in workflow
    assert "if: ${{ !cancelled() && steps.test.outputs.archive != '' }}" in workflow[report:verify]
    assert "path: ${{ steps.test.outputs.archive }}" in workflow[upload:failure]
    assert "path: ${{ steps.test.outputs.archive }}/*" not in workflow[upload:failure]
    assert "if: ${{ !cancelled() && steps.report.outputs.report-json != '' }}" in workflow[verify:upload]
    assert "continue-on-error:" not in workflow[report:failure]
    comment = workflow[workflow.index("  publish-archive-comment:"):]
    assert "if: ${{ always() && !cancelled()" in comment
    assert "actions/checkout" not in comment
    assert "REPORT_MODE: ${{ needs.benchmark.outputs.report_mode }}" in comment
    print("Baseline preflight/publication state-machine tests passed.")


if __name__ == "__main__":
    main()
