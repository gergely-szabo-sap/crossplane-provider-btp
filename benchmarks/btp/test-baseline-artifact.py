#!/usr/bin/env python3
"""Offline structural/security tests for the checked-in baseline helper."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from argparse import Namespace
from unittest import mock

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("baseline_artifact", ROOT / "baseline-artifact.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def descriptor(archive_path, archive, contract_hash):
    return {"schema_version": "v3", "baseline": {
        "archive_path": archive_path, "run_id": 123, "run_attempt": 2, "artifact_id": 456,
        "artifact_expires_at": "2026-10-13T00:00:00Z", "head_sha": "a" * 40,
        "archive_sha256": hashlib.sha256(archive).hexdigest(), "contract_sha256": contract_hash,
        "validated_cli": "v0.9.4", "environment_revision": "dedicated-account-3",
    }}


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        reference = root / "ref.json"
        output_file = root / "outputs"
        workspace = root / "workspace"
        for name in module.CONTRACT_FILES:
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        contract_hash = module.contract_digest({name: (workspace / name).read_bytes()
                                                for name in module.CONTRACT_FILES})
        archive = workspace / "benchmarks/btp/baseline/benchmark.tsdb.tar.zst"
        archive.parent.mkdir(parents=True)
        archive_bytes = b"reviewed synthetic baseline"
        archive.write_bytes(archive_bytes)
        good = descriptor("benchmarks/btp/baseline/benchmark.tsdb.tar.zst", archive_bytes, contract_hash)

        def invoke(ref, *, current="900", env_revision="dedicated-account-3"):
            reference.write_text(json.dumps(ref))
            output_file.write_text("")
            env = {"GITHUB_OUTPUT": str(output_file), "BTP_BENCHMARK_ENV_REVISION": env_revision}
            args = Namespace(reference=reference, workspace=workspace, current_run_id=current)
            with mock.patch.dict(os.environ, env, clear=True):
                assert module.resolve(args) == 0
            return dict(line.split("=", 1) for line in output_file.read_text().splitlines())

        values = invoke(good)
        assert values["baseline_path"] == str(archive)
        assert values["baseline_mode"] == "comparison" and values["baseline_run_id"] == "123"
        assert values["baseline_provenance"].endswith("expires=2026-10-13T00:00:00Z")
        assert not list(root.glob("btp-baseline-*"))

        # Original artifact expiry is historical provenance; it does not invalidate Git data.
        expired_provenance = json.loads(json.dumps(good))
        expired_provenance["baseline"]["artifact_expires_at"] = "2020-01-01T00:00:00Z"
        assert invoke(expired_provenance)["baseline_mode"] == "comparison"

        for malformed in (
            '{"schema_version":"v3","schema_version":"v3","baseline":null}',
            json.dumps({"schema_version": "v2", "baseline": good["baseline"]}),
            json.dumps({"schema_version": "v3", "baseline": {**good["baseline"], "extra": True}}),
        ):
            reference.write_text(malformed)
            try:
                module.strict_json(reference)
            except module.Unavailable as error:
                assert error.reason == "invalid_reference"
            else:
                raise AssertionError("invalid or legacy descriptor was accepted")

        null_ref = {"schema_version": "v3", "baseline": None}
        assert invoke(null_ref)["baseline_reason"] == "not_configured"
        assert invoke(good, current="123")["baseline_reason"] == "self_comparison"
        assert invoke(good, env_revision="wrong")["baseline_reason"] == "environment_revision_mismatch"

        for unsafe_path in (
            "../outside.tsdb.tar.zst", "/tmp/outside.tsdb.tar.zst", "benchmarks\\escape.tsdb.tar.zst",
            "./archive.tsdb.tar.zst", "benchmarks//archive.tsdb.tar.zst",
        ):
            bad = json.loads(json.dumps(good))
            bad["baseline"]["archive_path"] = unsafe_path
            reference.write_text(json.dumps(bad))
            try:
                module.strict_json(reference)
            except module.Unavailable as error:
                assert error.reason == "invalid_reference"
            else:
                raise AssertionError(f"unsafe archive path accepted: {unsafe_path}")

        changed_hash = json.loads(json.dumps(good))
        changed_hash["baseline"]["archive_sha256"] = "b" * 64
        assert invoke(changed_hash)["baseline_reason"] == "invalid_archive"
        changed_contract = json.loads(json.dumps(good))
        changed_contract["baseline"]["contract_sha256"] = "c" * 64
        assert invoke(changed_contract)["baseline_reason"] == "incompatible_contract"

        wrong_validation_release = json.loads(json.dumps(good))
        wrong_validation_release["baseline"]["validated_cli"] = "v9.9.9"
        reference.write_text(json.dumps(wrong_validation_release))
        try:
            module.strict_json(reference)
        except module.Unavailable as error:
            assert error.reason == "invalid_reference"
        else:
            raise AssertionError("baseline validated by an unselected CLI was accepted")

        legacy_shape = json.loads(json.dumps(good))
        legacy_shape["schema_version"] = "v2"
        legacy_shape["baseline"]["execution_cli"] = legacy_shape["baseline"].pop("validated_cli")
        reference.write_text(json.dumps(legacy_shape))
        try:
            module.strict_json(reference)
        except module.Unavailable as error:
            assert error.reason == "invalid_reference"
        else:
            raise AssertionError("legacy baseline shape was accepted")

        for name in module.CONTRACT_FILES:
            path = workspace / name
            original = path.read_bytes()
            path.write_bytes(original + b" changed")
            assert invoke(good)["baseline_reason"] == "incompatible_contract", name
            path.write_bytes(original)

        archive.unlink()
        assert invoke(good)["baseline_reason"] == "invalid_archive"
        archive.write_bytes(b"")
        assert invoke(good)["baseline_reason"] == "invalid_archive"
        archive.write_bytes(archive_bytes)
        with mock.patch.object(module, "MAX_LOCAL_ARCHIVE", len(archive_bytes) - 1):
            assert invoke(good)["baseline_reason"] == "invalid_archive"

        alias = archive.parent / "alias.tsdb.tar.zst"
        alias.symlink_to(archive)
        alias_ref = json.loads(json.dumps(good))
        alias_ref["baseline"]["archive_path"] = "benchmarks/btp/baseline/alias.tsdb.tar.zst"
        assert invoke(alias_ref)["baseline_reason"] == "invalid_archive"
        alias.unlink()

        link_parent = workspace / "linked"
        link_parent.symlink_to(archive.parent)
        parent_ref = json.loads(json.dumps(good))
        parent_ref["baseline"]["archive_path"] = "linked/benchmark.tsdb.tar.zst"
        assert invoke(parent_ref)["baseline_reason"] == "invalid_archive"

        # Unexpected checkout/filesystem errors are operational failures, not a successful fallback.
        args = Namespace(reference=reference, workspace=workspace, current_run_id="900")
        parent_ref["baseline"]["archive_path"] = "benchmarks/btp/baseline/benchmark.tsdb.tar.zst"
        reference.write_text(json.dumps(parent_ref))
        with mock.patch.dict(os.environ, {"BTP_BENCHMARK_ENV_REVISION": "dedicated-account-3"}, clear=True), \
                mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            try:
                module.resolve(args)
            except PermissionError:
                pass
            else:
                raise AssertionError("operational workspace read failure was converted to fallback")

    helper_source = (ROOT / "baseline-artifact.py").read_text()
    assert "def designate" not in helper_source
    assert "download_artifact" not in helper_source and "actions/artifacts" not in helper_source
    assert "GH_TOKEN" not in helper_source and "urllib" not in helper_source
    workflow = (ROOT.parent.parent / ".github/workflows/run-btp-benchmark.yaml").read_text()
    resolver = workflow[workflow.index("- name: Resolve checked-in benchmark baseline"): ]
    resolver = resolver[:resolver.index("- name:", 1)]
    assert "GH_TOKEN" not in resolver and "actions: read" not in workflow
    assert "baseline-artifact.py resolve" in resolver

    print("Checked-in baseline helper offline tests passed.")


if __name__ == "__main__":
    main()
