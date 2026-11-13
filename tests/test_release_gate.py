"""Failure-injection tests: no GitHub access and no large release download."""
import argparse
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SOURCE = Path(__file__).resolve().parents[1] / "scripts/release_gate.py"
spec = importlib.util.spec_from_file_location("release_gate", SOURCE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class FixtureHTTP:
    def __init__(self, case):
        self.contract = copy.deepcopy(case.contract)
        self.documents = dict(case.documents)
        self.state = copy.deepcopy(case.state)
        self.files = dict(case.files)
        self.downloads = []
        self.bad_link = False
        self.bad_page = False
        self.change_during_run = False
        self.snapshots = 0

    def json(self, url):
        if "/contents/" in url:
            name = gate.urllib.parse.unquote(url.split("/contents/", 1)[1].split("?", 1)[0])
            return {"content": base64.b64encode(self.documents[name].encode()).decode()}
        if "/git/ref/heads/" in url:
            return {"object": {"sha": self.state["commit"]}}
        if "/releases/tags/" in url:
            return dict(self.state["release"], assets=self.state["assets"])
        if url.endswith("/releases/latest"):
            return self.state["latest"]
        self.snapshots += 1
        about = self.state["about"]
        if self.change_during_run and self.snapshots > 1:
            about += " changed during download"
        return {"full_name": self.contract["repository"], "private": False,
                "description": about, "default_branch": self.state["default_branch"]}

    def head(self, url):
        if self.bad_link:
            raise gate.GateError("Broken public link")

    def text(self, url):
        return "stale page" if self.bad_page else self.documents["README.md"]

    def download(self, url, target):
        name = url.rsplit("/", 1)[1]
        self.downloads.append(name)
        target.write_bytes(self.files[name])


class ReleaseGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="release-gate-tests-")
        cls.root = Path(cls.directory.name)
        (cls.root / "scripts").mkdir()
        shutil.copyfile(SOURCE, cls.root / "scripts/release_gate.py")
        shutil.copyfile(SOURCE.with_name("privacy_gate.py"), cls.root / "scripts/privacy_gate.py")
        manifest = {"projects": [{"number": n, "name": f"Project {n}", "directory": f"0{n}-project", "version": f"{n}.0-preview.1"}
                                  for n in range(1, 5)]}
        archive = cls.root / "suite.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("suite/版本与验证.json", json.dumps(manifest))
            for p in manifest["projects"]:
                z.writestr("suite/" + p["directory"] + "/start.html", "entry")
            z.writestr("suite/report.txt", "public report")
        hash_value = gate.file_digest(archive)
        checksum = cls.root / "suite.zip.sha256.txt"
        checksum.write_text(hash_value + "  suite.zip\n")
        base = "https://github.com/test/project"
        download = base + "/releases/download/test-v1/"
        common = f"[ZIP]({download}suite.zip)\n[SHA256]({download}suite.zip.sha256.txt)\n"
        common += f"Source code 不包含四项目运行文件 预发布\n{archive.stat().st_size:,} 字节 {hash_value}\n"
        readme = common + f"[说明]({base}/releases/tag/test-v1)\n"
        body = common
        for p in manifest["projects"]:
            version = gate.display_version(p["version"])
            readme += f'| {p["number"]:02d} · {p["name"]} | {version} | {p["directory"]}/start.html |\n'
            body += f'- {p["number"]:02d} {p["name"]}: {version}\n'
        readme += "## 历史稳定版下载\n[old](https://github.com/test/project/releases/tag/stable)\n"
        for name, text in {"README.md": readme, "PRIVACY.md": "研究", "RELEASE_NOTES_test-v1.md": body}.items():
            (cls.root / name).write_text(text, encoding="utf-8")
        requirements = {"repository_description": "Four projects", "release_title": "Four projects", "entrypoints": {str(n): "start.html" for n in range(1, 5)},
                        "required_paths": ["report.txt"], "required_terms": {"PRIVACY.md": ["研究"]}}
        gate.write_json(cls.root / "requirements.json", requirements)
        review = {"payload_inventory_sha256": gate.scan_archive(archive)["payload_inventory_sha256"],
                  "reviewed": {topic: True for topic in ("public_text_and_data", "images_and_video_frames", "spoken_audio_and_subtitles", "binary_and_archive_metadata", "licenses_preserved")}}
        gate.write_json(cls.root / "release/privacy-review.json", review)
        args = argparse.Namespace(root=str(cls.root), requirements=str(cls.root / "requirements.json"),
                                  archive=str(archive), checksum=str(checksum), repository="test/project", tag="test-v1",
                                  stable_latest="stable", notes="RELEASE_NOTES_test-v1.md", prerelease=True,
                                  contract=str(cls.root / "release/contract.json"))
        gate.prepare(args)
        subprocess.run(["git", "init", "-q", str(cls.root)], check=True)
        subprocess.run(["git", "-C", str(cls.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(cls.root), "-c", "user.name=Gate Test", "-c", "user.email=gate@example.invalid",
                        "commit", "-qm", "fixture"], check=True)
        cls.contract = gate.read_json(cls.root / "release/contract.json")
        cls.documents = {n: (cls.root / n).read_text(encoding="utf-8") for n in
                         list(cls.contract["documents"]) + ["release/contract.json", "scripts/release_gate.py", "scripts/privacy_gate.py"]}
        cls.state = {"commit": gate.git(cls.root, "rev-parse", "HEAD"), "about": "Four projects", "default_branch": "main",
                     "release": {"id": 1, "tag_name": "test-v1", "target_commitish": "original-tag-commit",
                                 "body": body, "draft": False, "prerelease": True},
                     "assets": [{"id": i, "name": a["name"], "size": a["size"], "digest": "sha256:" + a["sha256"],
                                 "browser_download_url": download + a["name"]} for i, a in enumerate(cls.contract["assets"])],
                     "latest": {"id": 10, "tag_name": "stable"}}
        cls.files = {a["name"]: (cls.root / a["name"]).read_bytes() for a in cls.contract["assets"]}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.http = FixtureHTTP(self)
        self.receipt = self.root / "receipt.json"
        self.args = argparse.Namespace(root=str(self.root), contract=str(self.root / "release/contract.json"), receipt=str(self.receipt))
        self.environment = patch.dict(os.environ, {"RELEASE_EVENT_TAG": ""})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def reject(self, pattern):
        gate.write_json(self.receipt, {"passed": True, "run_id": "obsolete-success"})
        with self.assertRaisesRegex(gate.GateError, pattern):
            gate.verify(self.args, self.http)
        receipt = gate.read_json(self.receipt)
        self.assertFalse(receipt["passed"])
        self.assertNotEqual(receipt["run_id"], "obsolete-success")
        self.assertNotEqual(receipt["stage"], "complete")

    def test_success_downloads_every_asset_on_every_run(self):
        one = gate.verify(self.args, self.http)
        two = gate.verify(self.args, self.http)
        self.assertTrue(two["passed"])
        self.assertNotEqual(one["run_id"], two["run_id"])
        self.assertEqual(len(self.http.downloads), 4)
        self.assertEqual(two["archive"]["project_count"], 4)

    def test_stale_about(self):
        self.http.state["about"] = "Three projects"
        self.reject("About is stale")

    def test_stale_remote_readme(self):
        self.http.documents["README.md"] = "Old README"
        self.reject("Remote file differs")

    def test_release_notes_diverged(self):
        self.http.state["release"]["body"] += "extra old text"
        self.reject("Release body differs")

    def test_stale_project_version_rejected_even_when_preparing_new_contract(self):
        documents = dict(self.documents)
        documents["README.md"] = documents["README.md"].replace("v2.0 Preview1", "v0.1")
        with self.assertRaisesRegex(gate.GateError, "project row"):
            gate.check_text(self.contract, documents, documents[self.contract["notes_file"]])

    def test_old_first_download(self):
        documents = dict(self.documents)
        documents["README.md"] = "[old](https://example.org/old.zip)\n" + documents["README.md"]
        with self.assertRaisesRegex(gate.GateError, "first link"):
            gate.check_text(self.contract, documents, documents[self.contract["notes_file"]])

    def test_broken_link(self):
        self.http.bad_link = True
        self.reject("Broken public link")

    def test_stale_rendered_homepage(self):
        self.http.bad_page = True
        self.reject("Public page missing")

    def test_metadata_correct_but_download_wrong(self):
        data = self.http.files["suite.zip"]
        self.http.files["suite.zip"] = bytes([data[0] ^ 1]) + data[1:]
        self.reject("Fresh public download hash/size mismatch")

    def test_missing_project_entry(self):
        target = self.root / "missing-entry.zip"
        with zipfile.ZipFile(self.root / "suite.zip") as source, zipfile.ZipFile(target, "w") as output:
            for name in source.namelist():
                if "03-project/start.html" not in name:
                    output.writestr(name, source.read(name))
        with self.assertRaisesRegex(gate.GateError, "Missing project entrypoint"):
            gate.check_archive(target, self.contract)

    def test_manifest_versions_changed(self):
        contract = copy.deepcopy(self.contract)
        contract["manifest"]["projects"][2]["version"] = "99.0"
        with self.assertRaisesRegex(gate.GateError, "manifest differs"):
            gate.check_archive(self.root / "suite.zip", contract)

    def test_missing_video(self):
        contract = copy.deepcopy(self.contract)
        contract["required_paths"] = ["missing-video.mp4"]
        with self.assertRaisesRegex(gate.GateError, "Missing deliverable"):
            gate.check_archive(self.root / "suite.zip", contract)

    def test_nested_privacy_failure_blocks_otherwise_valid_package(self):
        import io
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as z:
            z.writestr("record.txt", "C:\\Users\\PrivatePerson\\notes")
        target = self.root / "leaking.zip"
        with zipfile.ZipFile(self.root / "suite.zip") as source, zipfile.ZipFile(target, "w") as output:
            for name in source.namelist():
                output.writestr(name, source.read(name))
            output.writestr("suite/data.zip", nested.getvalue())
        with self.assertRaisesRegex(RuntimeError, "Privacy scan failed"):
            gate.check_archive(target, self.contract)

    def test_retired_release_still_public_blocks_completion(self):
        contract = copy.deepcopy(self.contract)
        contract["retired_releases"] = [{"tag": "old-leak", "asset_urls": ["https://example.invalid/old.zip"]}]
        path = self.root / "release/retired-test.json"
        gate.write_json(path, contract)
        # Verify the retirement condition independently of the tracked-input guard.
        original_read = gate.read_json
        with patch.object(gate, "read_json", side_effect=lambda path: contract if Path(path) == Path(self.args.contract) else original_read(path)), patch.object(gate, "git", side_effect=lambda root,*a: self.state["commit"] if a == ("rev-parse", "HEAD") else ""):
            self.reject("still public")

    def test_unexpected_asset(self):
        self.http.state["assets"].append(dict(self.http.state["assets"][0]))
        self.reject("asset count")

    def test_wrong_stable_latest(self):
        self.http.state["latest"]["tag_name"] = "test-v1"
        self.reject("Stable latest")

    def test_unpublished_release(self):
        self.http.state["release"]["draft"] = True
        self.reject("Release status")

    def test_changed_during_download(self):
        self.http.change_during_run = True
        self.reject("changed during verification")

    def test_new_release_with_old_contract(self):
        with patch.dict(os.environ, {"RELEASE_EVENT_TAG": "test-v2"}):
            self.reject("event tag differs")

    def test_no_stale_success_on_bad_input(self):
        self.args.contract = str(self.root / "not-found.json")
        gate.write_json(self.receipt, {"passed": True})
        with self.assertRaises(FileNotFoundError):
            gate.verify(self.args, self.http)
        self.assertFalse(gate.read_json(self.receipt)["passed"])


if __name__ == "__main__":
    unittest.main()
