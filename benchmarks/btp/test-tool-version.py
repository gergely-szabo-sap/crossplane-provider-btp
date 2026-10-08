#!/usr/bin/env python3
"""Reject superseded xp-diadromos release selections in benchmark integration text."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = "v0.9.4"
FIXED_FILES = (
    ".github/workflows/run-btp-benchmark.yaml",
    "docs/contribution-notes/btp-synthetic-benchmark-runbook.md",
)
TEXT_SUFFIXES = {".go", ".js", ".json", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"}
VERSION = re.compile(r"(?<![A-Za-z0-9])v?\d+\.\d+\.\d+(?![A-Za-z0-9])")
TOOL = re.compile(r"xp[-_]diadromos|XP_METRICS_CI_VERSION|(?:execution|report|baseline-preflight) CLI|k6 image", re.I)
SYNTHETIC_NEGATIVES = {"0.0.0", "1.2.3", "9.9.9"}


def main() -> None:
    errors = []
    benchmark_files = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "benchmarks/btp").rglob("*")
        if path.is_file() and path.suffix in TEXT_SUFFIXES and "__pycache__" not in path.parts
    )
    files = (*FIXED_FILES, *benchmark_files)
    for relative in files:
        text = (ROOT / relative).read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in VERSION.finditer(line):
                # Release mentions are checked on their own line to avoid treating
                # nearby Crossplane/action/dependency versions as CLI selections.
                # Generic test assertions are also audited, including escaped Go
                # string literals, while deliberately invalid synthetic values remain valid.
                version_assertion = (
                    "EXPECTED_CLI" in line or "EXPECTED_EXECUTION" in line or "EXPECTED_REPORT" in line
                    or "validated_cli" in line or "execution_version=" in line or "report_version=" in line
                    or (relative.endswith(("benchmark_config_test.go", "test-validate-metrics-archive.py"))
                        and ("==" in line or "version=" in line or "VERSION" in line))
                )
                allowed = {EXPECTED.lstrip("v"), *SYNTHETIC_NEGATIVES}
                if ((TOOL.search(line) or version_assertion)
                        and match.group().lstrip("v") not in allowed):
                    errors.append(
                        f"{relative}:{line_number}: unexpected xp-diadromos selection {match.group()}"
                    )
    workflow = (ROOT / FIXED_FILES[0]).read_text(encoding="utf-8")
    if workflow.count("version: v0.9.4") != 2 or "XP_METRICS_CI_VERSION: v0.9.4" not in workflow:
        errors.append("workflow must explicitly select v0.9.4 for execution, preflight, and reporting")
    config = (ROOT / "benchmarks/btp/config.yaml").read_text(encoding="utf-8")
    if "xp-diadromos-k6:v0.9.4" not in config:
        errors.append("benchmark config must select the v0.9.4 k6 image")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Benchmark tooling version audit passed.")


if __name__ == "__main__":
    main()
