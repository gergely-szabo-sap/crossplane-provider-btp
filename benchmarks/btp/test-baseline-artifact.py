#!/usr/bin/env python3
"""Offline structural/security tests for the designated artifact helper."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile
from argparse import Namespace
from unittest import mock

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("baseline_artifact", ROOT / "baseline-artifact.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def descriptor():
    return {"schema_version": "v1", "baseline": {
        "run_id": 123, "run_attempt": 2, "artifact_id": 456,
        "head_sha": "a" * 40, "archive_sha256": "b" * 64,
        "contract_sha256": "c" * 64, "execution_cli": "v0.9.2",
        "environment_revision": "dedicated-account-3",
    }}


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ref_path = root / "ref.json"
        ref_path.write_text(json.dumps(descriptor()))
        assert module.strict_json(ref_path)["run_id"] == 123
        ref_path.write_text('{"schema_version":"v1","schema_version":"v1","baseline":null}')
        try: module.strict_json(ref_path)
        except module.Unavailable as error: assert error.reason == "invalid_reference"
        else: raise AssertionError("duplicate JSON key accepted")
        ref_path.write_text(json.dumps({"schema_version":"v1","baseline":None}))
        try: module.strict_json(ref_path)
        except module.Unavailable as error: assert error.reason == "not_configured"
        else: raise AssertionError("null designation was not recognized")
        output_path = root / "outputs"
        env = dict(__import__("os").environ, GITHUB_OUTPUT=str(output_path))
        result = __import__("subprocess").run(
            [__import__("sys").executable, str(ROOT / "baseline-artifact.py"), "resolve",
             "--reference", str(ref_path), "--temp-dir", str(root)], env=env,
            stdout=__import__("subprocess").PIPE, stderr=__import__("subprocess").PIPE, text=True)
        outputs = output_path.read_text()
        assert result.returncode == 0 and "baseline_mode=current-only" in outputs
        assert "baseline_path=" in outputs and "baseline_reason=not_configured" in outputs

        ref_path.write_text(json.dumps({"schema_version": "v1", "baseline": {**descriptor()["baseline"], "unexpected": True}}))
        try: module.strict_json(ref_path)
        except module.Unavailable as error: assert error.reason == "invalid_reference"
        else: raise AssertionError("unknown descriptor field accepted")

        content_a = {name: name.encode() for name in module.CONTRACT_FILES}
        content_b = dict(content_a)
        assert module.contract_digest(content_a) == module.contract_digest(content_b)
        content_b[module.CONTRACT_FILES[0]] += b"\n"
        assert module.contract_digest(content_a) != module.contract_digest(content_b)

        archive_path = root / "archive.zip"
        payload = b"synthetic finalized metrics archive"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.tsdb.tar.zst", payload)
        extracted = module.safe_extract(archive_path, root)
        assert extracted.read_bytes() == payload and extracted.stat().st_mode & 0o777 == 0o600

        for bad_name in ("../escape.tsdb.tar.zst", "/absolute.tsdb.tar.zst", "report.partial.tsdb.tar.zst", "directory/report.tsdb.tar.zst"):
            bad_zip = root / "bad.zip"
            with zipfile.ZipFile(bad_zip, "w") as archive:
                archive.writestr(bad_name, payload)
            try: module.safe_extract(bad_zip, root)
            except module.Unavailable as error: assert error.reason == "invalid_archive"
            else: raise AssertionError(f"unsafe ZIP member accepted: {bad_name}")
        with zipfile.ZipFile(root / "symlink.zip", "w") as archive:
            item = zipfile.ZipInfo("link.tsdb.tar.zst")
            item.create_system = 3
            item.external_attr = (0o120777 << 16)
            archive.writestr(item, payload)
        try: module.safe_extract(root / "symlink.zip", root)
        except module.Unavailable as error: assert error.reason == "invalid_archive"
        else: raise AssertionError("ZIP symlink accepted")
        with zipfile.ZipFile(root / "multiple.zip", "w") as archive:
            archive.writestr("one.tsdb.tar.zst", payload)
            archive.writestr("two.tsdb.tar.zst", payload)
        try: module.safe_extract(root / "multiple.zip", root)
        except module.Unavailable as error: assert error.reason == "invalid_archive"
        else: raise AssertionError("multiple ZIP members accepted")

    redirect = module.HTTPSNoAuthRedirect()
    authenticated = __import__("urllib.request", fromlist=["Request"]).Request(
        "https://api.github.com/artifact", headers={"Authorization": "Bearer secret", "Cookie": "private", "Accept": "application/zip"})
    redirected = redirect.redirect_request(authenticated, None, 302, "Found", {}, "https://artifact.example/signed")
    assert redirected.get_header("Authorization") is None and redirected.get_header("Cookie") is None
    assert redirected.get_header("Accept") == "application/zip"
    try: redirect.redirect_request(authenticated, None, 302, "Found", {}, "http://artifact.example/file")
    except module.Unavailable: pass
    else: raise AssertionError("insecure artifact redirect accepted")

    class FakeGitHub:
        def __init__(self, run_changes=None, artifact_changes=None):
            self.run = {"id": 123, "path": module.WORKFLOW + "@refs/heads/main", "event": "workflow_dispatch",
                        "head_branch": "main", "head_sha": "a" * 40, "status": "completed",
                        "conclusion": "success", "run_attempt": 2}
            self.run.update(run_changes or {})
            self.artifact = {"id": 456, "name": module.ARTIFACT_PREFIX + "123-2", "expired": False,
                             "size_in_bytes": 100, "expires_at": "2026-10-13T00:00:00Z",
                             # Documented artifact workflow_run shape has no event/attempt.
                             "workflow_run": {"id": 123, "head_sha": "a" * 40}}
            self.artifact.update(artifact_changes or {})
        def get(self, path):
            if "/actions/runs/" in path: return self.run
            if path.endswith("/repos/org/repo"): return {"default_branch": "main"}
            return self.artifact

    reference = descriptor()["baseline"]
    good = object.__new__(module.GitHub)
    good.repo = "org/repo"
    good.get = FakeGitHub().get
    assert good.run_and_artifact(reference)[1]["id"] == 456

    invalid_runs = [
        {"event": "pull_request"}, {"head_branch": "other"}, {"head_sha": "b" * 40},
        {"status": "in_progress"}, {"conclusion": "failure"}, {"run_attempt": 3},
        {"path": "other.yaml@refs/heads/main"}, {"id": 999},
    ]
    for change in invalid_runs:
        candidate = object.__new__(module.GitHub)
        candidate.repo = "org/repo"
        candidate.get = FakeGitHub(run_changes=change).get
        try: candidate.run_and_artifact(reference)
        except module.Unavailable as error: assert error.reason == "invalid_reference"
        else: raise AssertionError(f"invalid workflow run accepted: {change}")

    invalid_artifacts = [
        {"id": 999}, {"name": module.ARTIFACT_PREFIX + "123-1"}, {"expired": True},
        {"size_in_bytes": 0}, {"size_in_bytes": module.MAX_ZIP + 1},
        {"workflow_run": {"id": 999, "head_sha": "a" * 40}},
        {"workflow_run": {"id": 123, "head_sha": "b" * 40}},
        {"workflow_run": {"head_sha": "a" * 40}},
    ]
    for change in invalid_artifacts:
        candidate = object.__new__(module.GitHub)
        candidate.repo = "org/repo"
        candidate.get = FakeGitHub(artifact_changes=change).get
        try: candidate.run_and_artifact(reference)
        except module.Unavailable: pass
        else: raise AssertionError(f"invalid artifact metadata accepted: {change}")

    content_data = {name: (name + "\nsynthetic").encode() for name in module.CONTRACT_FILES}
    for wrapping, trailing in (("\n", ""), ("\r\n", ""), ("\n", "\r\n")):
        candidate = object.__new__(module.GitHub)
        candidate.repo = "org/repo"
        def content_response(path, wrapping=wrapping, trailing=trailing):
            name = __import__("urllib.parse", fromlist=["unquote"]).unquote(
                path.split("/contents/", 1)[1].split("?", 1)[0])
            encoded = __import__("base64").b64encode(content_data[name]).decode()
            return {"encoding": "base64", "content": encoded[:8] + wrapping + encoded[8:] + trailing}
        candidate.get = content_response
        decoded = candidate.source_bytes("a" * 40)
        assert decoded == content_data
        assert module.contract_digest(decoded) == module.contract_digest(content_data)
    for malformed in ("SGVsbG8= !", "SGVsbG8=\t", "SGVsbG8", "SGVsbG8=="):
        candidate = object.__new__(module.GitHub)
        candidate.repo = "org/repo"
        candidate.get = lambda _path, malformed=malformed: {"encoding": "base64", "content": malformed}
        try: candidate.source_bytes("a" * 40)
        except module.Unavailable as error: assert error.reason == "incompatible_contract"
        else: raise AssertionError(f"invalid Base64 accepted: {malformed!r}")
    for response in ({"encoding": "utf-8", "content": "SGVsbG8="},
                     {"encoding": "base64", "content": 123}):
        candidate = object.__new__(module.GitHub)
        candidate.repo = "org/repo"
        candidate.get = lambda _path, response=response: response
        try: candidate.source_bytes("a" * 40)
        except module.Unavailable as error: assert error.reason == "incompatible_contract"
        else: raise AssertionError(f"invalid Contents metadata accepted: {response!r}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        workspace = root / "workspace"
        for name in module.CONTRACT_FILES:
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        source = {name: (workspace / name).read_bytes() for name in module.CONTRACT_FILES}
        archive_bytes = b"private synthetic archive"
        archive_zip = root / "synthetic.zip"
        with zipfile.ZipFile(archive_zip, "w") as archive:
            archive.writestr("original.tsdb.tar.zst", archive_bytes)
        reference = descriptor()["baseline"]
        reference["archive_sha256"] = hashlib.sha256(archive_bytes).hexdigest()
        reference["contract_sha256"] = module.contract_digest(source)
        ref_path = root / "ref.json"
        ref_path.write_text(json.dumps({"schema_version": "v1", "baseline": reference}))

        class API:
            def run_and_artifact(self, ref):
                return ({"updated_at": "ignored"}, {"expires_at": "2026-10-13T00:00:00Z"})
            def source_bytes(self, sha): return source
            def download_artifact(self, artifact_id, destination): shutil.copyfile(archive_zip, destination)

        outputs = root / "outputs"
        env = {"GH_TOKEN": "synthetic-token", "BTP_BENCHMARK_ENV_REVISION": "dedicated-account-3",
               "GITHUB_OUTPUT": str(outputs)}
        args = Namespace(reference=ref_path, current_run_id="900", workspace=workspace,
                         temp_dir=root, repository="org/repo", api_url="https://api.github.com")
        with mock.patch.dict(os.environ, env), mock.patch.object(module, "GitHub", return_value=API()):
            assert module.resolve(args) == 0
        values = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
        staged = Path(values["baseline_path"])
        assert values["baseline_mode"] == "comparison" and staged.name == "baseline-123-2.tsdb.tar.zst"
        assert staged.read_bytes() == archive_bytes and values["baseline_artifact_id"] == "456"
        shutil.rmtree(staged.parent)

        reference["archive_sha256"] = "b" * 64
        ref_path.write_text(json.dumps({"schema_version": "v1", "baseline": reference}))
        outputs.write_text("")
        with mock.patch.dict(os.environ, env), mock.patch.object(module, "GitHub", return_value=API()):
            assert module.resolve(args) == 0
        assert "baseline_reason=invalid_archive" in outputs.read_text()

        outputs.write_text("")
        args.current_run_id = "123"
        with mock.patch.dict(os.environ, env):
            assert module.resolve(args) == 0
        assert "baseline_reason=self_comparison" in outputs.read_text()

    with tempfile.TemporaryDirectory(prefix="btp-designation-") as td:
        root = Path(td)
        archive_zip = root / "artifact.zip"
        archive_bytes = b"synthetic designation archive"
        with zipfile.ZipFile(archive_zip, "w") as archive:
            archive.writestr("candidate.tsdb.tar.zst", archive_bytes)
        cli = root / "xp-diadromos"
        cli.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if args == ["version"]:
    print(os.environ.get("STUB_VERSION", "xp-diadromos v0.9.2"))
    raise SystemExit(0)
report = json.loads(pathlib.Path(os.environ["STUB_REPORT"]).read_text())
metadata = report["archives"][0]["archive"]
metadata.update(complete=os.environ.get("STUB_COMPLETE", "true") == "true",
                run_id=json.loads(os.environ["STUB_IDENTITY"]))
if os.environ.get("STUB_IDENTITY_MISSING") == "1":
    metadata.pop("run_id", None)
if os.environ.get("STUB_LIFECYCLE") == "invalid":
    report["archives"][0]["k6_metrics"] = []
pathlib.Path(args[args.index("--output") + 1]).write_text(json.dumps(report))
pathlib.Path(args[args.index("--summary-output") + 1]).write_text("synthetic")
''')
        cli.chmod(0o700)
        report_template = json.loads((ROOT / "tests/report-valid.json").read_text())
        report_path = root / "report.json"
        report_path.write_text(json.dumps(report_template))
        source = {name: (Path.cwd() / name).read_bytes() for name in module.CONTRACT_FILES}

        class DesignationAPI:
            def run_and_artifact(self, ref):
                assert ref["run_id"] == 123 and ref["run_attempt"] == 2 and ref["artifact_id"] == 456
                return ({"id": 123}, {"expires_at": "2026-10-13T00:00:00Z"})
            def source_bytes(self, sha):
                assert sha == "a" * 40
                return source
            def download_artifact(self, artifact_id, destination):
                assert artifact_id == 456
                shutil.copyfile(archive_zip, destination)

        output_path = root / "designated.json"
        args = Namespace(run_id=123, run_attempt=2, artifact_id=456, head_sha="a" * 40,
                         environment_revision="dedicated-account-3", repository="org/repo",
                         api_url="https://api.github.com", temp_dir=root, report_cli=str(cli),
                         output=str(output_path))
        env = {"GH_TOKEN": "synthetic-token", "BTP_BENCHMARK_ENV_REVISION": "dedicated-account-3",
               "STUB_REPORT": str(report_path), "STUB_IDENTITY": json.dumps("run-1791288000123456789")}
        with mock.patch.dict(os.environ, env), mock.patch.object(module, "GitHub", return_value=DesignationAPI()):
            assert module.designate(args) == 0
        descriptor_written = json.loads(output_path.read_text())
        designated = descriptor_written["baseline"]
        assert set(descriptor_written) == {"schema_version", "baseline"}
        assert descriptor_written["schema_version"] == "v1"
        assert designated["run_id"] == 123 and designated["run_attempt"] == 2
        assert designated["artifact_id"] == 456 and designated["head_sha"] == "a" * 40
        assert designated["archive_sha256"] == hashlib.sha256(archive_bytes).hexdigest()
        assert designated["contract_sha256"] == module.contract_digest(source)
        assert designated["execution_cli"] == "v0.9.2"
        assert designated["environment_revision"] == "dedicated-account-3"
        assert output_path.stat().st_mode & 0o777 == 0o600
        assert module.strict_json(output_path)["run_id"] == 123

        original_descriptor = output_path.read_bytes()
        with mock.patch.dict(os.environ, env), mock.patch.object(module, "GitHub", return_value=DesignationAPI()):
            try: module.designate(args)
            except ValueError as error: assert "refusing overwrite" in str(error)
            else: raise AssertionError("designation overwrote an existing descriptor")
        assert output_path.read_bytes() == original_descriptor

        invalid_cases = [
            ({"STUB_IDENTITY_MISSING": "1"}, "identity_unavailable"),
            ({"STUB_IDENTITY": json.dumps(None)}, "identity_unavailable"),
            ({"STUB_IDENTITY": json.dumps("  \t")}, "identity_unavailable"),
            ({"STUB_IDENTITY": json.dumps(123)}, "identity_unavailable"),
            ({"STUB_VERSION": "xp-diadromos v0.9.1"}, "unsuitable_evidence"),
            ({"STUB_COMPLETE": "false"}, "unsuitable_evidence"),
            ({"STUB_LIFECYCLE": "invalid"}, "unsuitable_evidence"),
        ]
        for index, (overrides, reason) in enumerate(invalid_cases):
            args.output = str(root / f"rejected-{index}.json")
            with mock.patch.dict(os.environ, {**env, **overrides}), mock.patch.object(
                    module, "GitHub", return_value=DesignationAPI()):
                assert module.designate(args) == 1
            assert not Path(args.output).exists()

    print("Baseline artifact helper offline tests passed.")


if __name__ == "__main__":
    main()
