#!/usr/bin/env python3
"""Release consistency gate. Standard library only; verification never mutates GitHub."""
import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import urllib.parse
import urllib.request
import uuid
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parent))
from privacy_gate import scan_archive, require_review


class GateError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise GateError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(text):
    return text.replace("\r\n", "\n").strip()


def text_digest(text):
    return digest(canonical(text).encode("utf-8"))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def display_version(version):
    return "v" + re.sub(r"-preview\.(\d+)", r" Preview\1", version, flags=re.I)


def links(text):
    return re.findall(r"\[[^\]]+\]\(([^\s)]+)\)", text)


def urls(contract):
    base = "https://github.com/" + contract["repository"]
    release = base + "/releases/tag/" + contract["tag"]
    assets = {a["name"]: base + "/releases/download/" + contract["tag"] + "/" + a["name"]
              for a in contract["assets"]}
    return base, release, assets


def check_text(contract, documents, body):
    readme = documents["README.md"]
    notes = documents[contract["notes_file"]]
    require(canonical(body) == canonical(notes), "Release body differs from repository release notes")
    base, release_url, asset_urls = urls(contract)
    archive = next(a for a in contract["assets"] if a["name"] == contract["archive"])
    for label, text in (("README", readme), ("Release", body)):
        found = links(text)
        require(found and found[0] == asset_urls[contract["archive"]], label + ": first link must be current ZIP")
        for url in asset_urls.values():
            require(url in found, label + ": missing asset link " + url)
        require(archive["sha256"] in text, label + ": missing current SHA-256")
        require(f'{archive["size"]:,}' in text or str(archive["size"]) in text,
                label + ": missing exact byte count")
        require("Source code" in text and "不包含四项目运行文件" in text,
                label + ": missing source snapshot guidance")
        require(("预发布" if contract["prerelease"] else "稳定版") in text, label + ": release status missing")
        for p in contract["manifest"]["projects"]:
            version = display_version(p["version"])
            if label == "README":
                row = f'| {p["number"]:02d} · {p["name"]} | {version} |'
                matching = [line for line in text.splitlines() if row in line]
                require(len(matching) == 1, label + ": missing or duplicated project row " + p["name"])
                entry = contract["entrypoints"][str(p["number"])]
                require(p["directory"] + "/" + entry in matching[0], label + ": wrong entrypoint " + p["name"])
            else:
                matching = [line for line in text.splitlines() if re.match(rf"^- {p['number']:02d}\s", line)]
                require(len(matching) == 1 and version.removeprefix("v") in matching[0],
                        label + ": wrong project version " + p["name"])
    require(release_url in links(readme), "README missing release notes link")
    current = readme.split("## 历史稳定版下载", 1)[0]
    for link in links(current):
        if link.startswith(base + "/releases/tag/") or link.startswith(base + "/releases/download/"):
            require(link == release_url or link in asset_urls.values(), "Old release link outside history section: " + link)
    for name, terms in contract["required_terms"].items():
        require(name in documents, "Required document missing: " + name)
        for term in terms:
            require(term in documents[name], "Required content missing from " + name + ": " + term)


def check_archive(path, contract):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "ZIP contains duplicate paths")
        require(all(not n.startswith(("/", "\\")) and ".." not in PurePosixPath(n.replace("\\", "/")).parts
                    for n in names), "ZIP contains unsafe paths")
        require(archive.testzip() is None, "ZIP CRC failure")
        member = contract["manifest_member"]
        actual = json.loads(archive.read(member).decode("utf-8-sig"))
        require(actual == contract["manifest"], "Downloaded package manifest differs from release contract")
        prefix = str(PurePosixPath(member).parent) + "/"
        if prefix == "./":
            prefix = ""
        for p in actual["projects"]:
            entry = prefix + p["directory"] + "/" + contract["entrypoints"][str(p["number"])]
            require(entry in names and archive.getinfo(entry).file_size > 0, "Missing project entrypoint: " + entry)
        for entry in contract["required_paths"]:
            require(prefix + entry in names and archive.getinfo(prefix + entry).file_size > 0,
                    "Missing deliverable: " + entry)
        result = {"entries": len(names), "crc_passed": True, "manifest_matches": True,
                  "project_count": len(actual["projects"]), "required_paths_passed": True}
    require("privacy_review" in contract, "Missing current privacy review")
    result["privacy"] = require_review(scan_archive(path), contract["privacy_review"])
    return result


def prepare(args):
    root = Path(args.root).resolve()
    requirements = read_json(args.requirements)
    with zipfile.ZipFile(args.archive) as archive:
        candidates = [n for n in archive.namelist() if PurePosixPath(n).name == "版本与验证.json"]
        require(len(candidates) == 1, "Expected one authoritative package manifest")
        member = candidates[0]
        manifest = json.loads(archive.read(member).decode("utf-8-sig"))
    require({p["number"] for p in manifest["projects"]} == {1, 2, 3, 4}, "Expected all four projects")
    assets = [{"name": Path(path).name, "size": Path(path).stat().st_size, "sha256": file_digest(path)}
              for path in (args.archive, args.checksum)]
    archive_name = assets[0]["name"]
    require(Path(args.checksum).read_text(encoding="utf-8-sig").strip() == assets[0]["sha256"] + "  " + archive_name,
            "Checksum file does not match archive")
    review_file = getattr(args, "privacy_review", "release/privacy-review.json")
    review = read_json(root / review_file)
    documents = {name: (root / name).read_text(encoding="utf-8-sig")
                 for name in ["README.md", "PRIVACY.md", args.notes, review_file]}
    contract = {"schema": 2, "repository": args.repository, "branch": "main", "tag": args.tag,
                "prerelease": args.prerelease, "stable_latest_tag": args.stable_latest,
                "repository_description": requirements["repository_description"], "archive": archive_name,
                "checksum": assets[1]["name"], "assets": assets, "manifest_member": member,
                "manifest": manifest, "entrypoints": requirements["entrypoints"],
                "required_paths": requirements["required_paths"], "required_terms": requirements["required_terms"],
                "notes_file": args.notes, "privacy_review": review,
                "release_title": requirements["release_title"],
                "retired_releases": requirements.get("retired_releases", []),
                "documents": {n: text_digest(t) for n, t in documents.items()}}
    check_text(contract, documents, documents[args.notes])
    check_archive(args.archive, contract)
    write_json(args.contract, contract)
    print("PREPARED release contract for " + args.tag, flush=True)


def preflight(args):
    """Mandatory local check immediately before any draft upload/publication."""
    root = Path(args.root).resolve()
    contract = read_json(args.contract)
    require(contract.get("schema") == 2, "Privacy-enabled contract required")
    directory = Path(args.assets).resolve()
    for asset in contract["assets"]:
        target = directory / asset["name"]
        require(target.parent == directory, "Unsafe asset path")
        require(target.stat().st_size == asset["size"] and file_digest(target) == asset["sha256"],
                "Local asset changed: " + asset["name"])
    documents = {name: (root / name).read_text(encoding="utf-8-sig") for name in contract["documents"]}
    for name, expected in contract["documents"].items():
        require(text_digest(documents[name]) == expected, "Local document changed: " + name)
    check_text(contract, documents, documents[contract["notes_file"]])
    result = check_archive(directory / contract["archive"], contract)
    write_json(args.receipt, {"passed": True, "stage": "local_preflight_only", "tag": contract["tag"],
                            "archive": result, "archive_sha256": file_digest(directory / contract["archive"])})
    print("LOCAL PREFLIGHT PASSED; publication and fresh public verification still required", flush=True)


class PublicHTTP:
    """Anonymous requests, including downloads. Never consume an auth token or cached ZIP."""
    def open(self, url, method="GET"):
        request = urllib.request.Request(url, method=method, headers={
            "User-Agent": "AuctionPilot-release-consistency", "Accept": "*/*", "Cache-Control": "no-cache"})
        return urllib.request.urlopen(request, timeout=90)

    def json(self, url):
        with self.open(url) as response:
            return json.load(response)

    def text(self, url):
        with self.open(url) as response:
            return response.read().decode("utf-8-sig")

    def head(self, url):
        with self.open(url, "HEAD") as response:
            require(response.status == 200, "Broken link: " + url)

    def download(self, url, target):
        with self.open(url) as response, open(target, "wb") as output:
            require(response.status == 200, "Download failed: " + url)
            while chunk := response.read(1024 * 1024):
                output.write(chunk)


def git(root, *arguments):
    return subprocess.check_output(["git", "-C", str(root), *arguments], text=True, encoding="utf-8").strip()


def snapshot(http, api, contract):
    repo = http.json(api)
    head = http.json(api + "/git/ref/heads/" + contract["branch"])["object"]["sha"]
    release = http.json(api + "/releases/tags/" + contract["tag"])
    latest = http.json(api + "/releases/latest")
    # Ignore counters such as download_count; bind all release content and asset identities.
    identity = {"commit": head, "about": repo["description"], "default_branch": repo["default_branch"],
                "release": {k: release[k] for k in ("id", "tag_name", "target_commitish", "body", "draft", "prerelease")},
                "assets": sorted([{k: a[k] for k in ("id", "name", "size", "digest", "browser_download_url")}
                                  for a in release["assets"]], key=lambda a: a["name"]),
                "latest": {"id": latest["id"], "tag_name": latest["tag_name"]}}
    require(repo["full_name"] == contract["repository"] and not repo["private"], "Expected public repository")
    return identity


def verify(args, http=None):
    # Invalidate prior success BEFORE loading inputs or doing any network operation.
    receipt_path = Path(args.receipt)
    receipt = {"passed": False, "run_id": str(uuid.uuid4()),
               "started_at_utc": datetime.now(timezone.utc).isoformat(), "stage": "inputs"}
    write_json(receipt_path, receipt)
    try:
        root = Path(args.root).resolve()
        contract_path = Path(args.contract).resolve()
        contract = read_json(contract_path)
        require(contract["schema"] == 2, "Unsupported contract schema; privacy review is mandatory")
        require(len(contract["manifest"]["projects"]) == 4, "Expected four projects")
        event_tag = os.environ.get("RELEASE_EVENT_TAG")
        retired_tags = {r["tag"] for r in contract.get("retired_releases", [])}
        require(not event_tag or event_tag == contract["tag"] or event_tag in retired_tags, "Release event tag differs from current contract")
        commit = git(root, "rev-parse", "HEAD")
        tracked = list(contract["documents"]) + [contract_path.relative_to(root).as_posix(), "scripts/release_gate.py", "scripts/privacy_gate.py"]
        require(not git(root, "status", "--porcelain", "--", *tracked), "Commit release inputs before verification")
        for name in tracked:
            git(root, "ls-files", "--error-unmatch", "--", name)
        receipt.update(contract_sha256=file_digest(contract_path), verifier_sha256=file_digest(__file__),
                       tag=contract["tag"], repository=contract["repository"], commit=commit)
        http = http or PublicHTTP()
        api = "https://api.github.com/repos/" + contract["repository"]
        base, release_url, asset_urls = urls(contract)
        receipt["stage"] = "remote_documents"
        before = snapshot(http, api, contract)
        retired_checked = []
        for old in contract.get("retired_releases", []):
            # A superseded leaking asset must not remain anonymously downloadable.
            for url in [api + "/releases/tags/" + old["tag"]] + old["asset_urls"]:
                try:
                    http.head(url)
                except urllib.error.HTTPError as error:
                    require(error.code in (404, 410), "Cannot confirm retired release removal")
                else:
                    raise GateError("Retired privacy-failing release is still public: " + old["tag"])
                retired_checked.append(url)
        require(before["commit"] == commit, "Reviewed commit is not current remote main")
        require(before["default_branch"] == contract["branch"], "Homepage uses a different branch")
        require(before["about"] == contract["repository_description"], "GitHub About is stale")
        require(before["latest"]["tag_name"] == contract["stable_latest_tag"], "Stable latest marker differs")
        release = before["release"]
        require(not release["draft"] and release["prerelease"] == contract["prerelease"], "Release status differs")
        documents = {}
        # The published contract must be the same input as this run, not an unpushed plan.
        for name in tracked:
            remote = http.json(api + "/contents/" + urllib.parse.quote(name) + "?ref=" + commit)
            content = base64.b64decode(remote["content"]).decode("utf-8-sig")
            local = (root / name).read_text(encoding="utf-8-sig")
            require(canonical(content) == canonical(local), "Remote file differs: " + name)
            if name in contract["documents"]:
                require(text_digest(content) == contract["documents"][name], "Document changed without new contract: " + name)
                documents[name] = content
        check_text(contract, documents, release["body"])
        require(len(before["assets"]) == len(contract["assets"]), "Unexpected public asset count")
        for expected in contract["assets"]:
            matches = [a for a in before["assets"] if a["name"] == expected["name"]]
            require(len(matches) == 1, "Missing public asset: " + expected["name"])
            actual = matches[0]
            require(actual["size"] == expected["size"] and actual["digest"] == "sha256:" + expected["sha256"]
                    and actual["browser_download_url"] == asset_urls[expected["name"]], "Public asset metadata differs")
        receipt["stage"] = "public_pages_and_links"
        all_links = set(links(documents["README.md"]) + links(release["body"]))
        checked_links = []
        for link in sorted(all_links):
            if link.startswith("#"):
                continue
            url = link if link.startswith("https://") else base + "/blob/" + contract["branch"] + "/" + link
            http.head(url)
            checked_links.append(url)
        for url in (base, release_url):
            page = http.text(url + "?release_gate=" + receipt["run_id"])
            visible = html.unescape(re.sub(r"<[^>]*>", " ", page))
            for p in contract["manifest"]["projects"]:
                require(display_version(p["version"]).removeprefix("v") in visible, "Public page missing project version: " + url)
            require(asset_urls[contract["archive"]] in html.unescape(page) and "Source code" in visible,
                    "Public page missing current download or source guidance: " + url)
        receipt["stage"] = "fresh_public_download"
        print("Downloading and checking public release assets (no cached archive).", flush=True)
        downloads = []
        with tempfile.TemporaryDirectory(prefix="auctionpilot-release-gate-") as directory:
            for expected in contract["assets"]:
                target = Path(directory) / expected["name"]
                require(target.parent == Path(directory), "Unsafe asset filename")
                http.download(asset_urls[expected["name"]], target)
                actual_hash = file_digest(target)
                require(target.stat().st_size == expected["size"] and actual_hash == expected["sha256"],
                        "Fresh public download hash/size mismatch: " + expected["name"])
                downloads.append({"name": expected["name"], "bytes": target.stat().st_size, "sha256": actual_hash})
            archive = next(a for a in contract["assets"] if a["name"] == contract["archive"])
            checksum = (Path(directory) / contract["checksum"]).read_text(encoding="utf-8-sig").strip()
            require(checksum == archive["sha256"] + "  " + archive["name"], "Downloaded checksum file is inconsistent")
            zip_result = check_archive(Path(directory) / contract["archive"], contract)
        receipt["stage"] = "final_snapshot"
        require(before == snapshot(http, api, contract), "GitHub changed during verification; rerun against current state")
        receipt.update(passed=True, stage="complete", completed_at_utc=datetime.now(timezone.utc).isoformat(),
                       homepage_verified=True, about_verified=True, release_notes_verified=True,
                       documents=list(documents), public_pages_checked=2, links=checked_links,
                       fresh_downloads=downloads, archive=zip_result, remote=before)
        receipt["privacy_verified"] = zip_result["privacy"]["passed"] and zip_result["privacy"]["human_review_matches"]
        receipt["retired_downloads_checked"] = retired_checked
        write_json(receipt_path, receipt)
        print("RELEASE COMPLETE: " + contract["tag"] + " — verified receipt: " + str(receipt_path), flush=True)
        return receipt
    except Exception as error:
        receipt.update(passed=False, error=str(error), failed_at_utc=datetime.now(timezone.utc).isoformat())
        write_json(receipt_path, receipt)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="Create contract from final ZIP and reviewed docs; does not publish")
    for name in ("archive", "checksum", "requirements", "repository", "tag", "stable-latest", "notes"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--prerelease", action="store_true")
    p.add_argument("--privacy-review", default="release/privacy-review.json")
    v = commands.add_parser("verify", help="Verify live GitHub and fresh anonymous download; fail closed")
    v.add_argument("--receipt", default="release-verification.json")
    l = commands.add_parser("preflight", help="Inspect exact local assets before upload or publication")
    l.add_argument("--assets", required=True)
    l.add_argument("--receipt", default="local-preflight.json")
    for sub in (p, v, l):
        sub.add_argument("--root", default=".")
        sub.add_argument("--contract", default="release/contract.json")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            write_json(args.receipt, {"passed": False, "stage": "local_preflight_started"})
            preflight(args)
        elif args.command == "prepare":
            prepare(args)
        else:
            verify(args)
    except Exception as error:
        parser.exit(1, "RELEASE INCOMPLETE: " + str(error) + "\n")


if __name__ == "__main__":
    main()
