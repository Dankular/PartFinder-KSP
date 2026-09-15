#!/usr/bin/env python3
"""Index CKAN packages one at a time and commit each result to GitHub.

Only the current archive being inspected exists temporarily. The aggregate
registry is kept in memory and persisted directly to GitHub after every package.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ckan_ship_parts import cfg_parts

DEFAULT_CATALOG = "https://github.com/KSP-CKAN/CKAN-meta/archive/master.tar.gz"


def request(url: str, token: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "PartFinder-KSP/1.0",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "PartFinder-KSP/1.0"})
    with urllib.request.urlopen(req, timeout=180) as response:
        return response.read()


def current_packages(blob: bytes) -> list[dict]:
    latest: dict[str, dict] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.lower().endswith(".ckan"):
                continue
            try:
                package = json.loads(archive.extractfile(member).read().decode("utf-8-sig"))
            except (AttributeError, UnicodeError, json.JSONDecodeError):
                continue
            if package.get("kind") in {"metapackage", "dlc"} or not package.get("download"):
                continue
            old = latest.get(package["identifier"])
            if old is None or str(package.get("release_date", "")) > str(old.get("release_date", "")):
                latest[package["identifier"]] = package
    return sorted(latest.values(), key=lambda item: item["identifier"].casefold())


def archive_url(package: dict) -> str:
    value = package["download"]
    return value[0] if isinstance(value, list) else value


def inspect_package(package: dict) -> list[str]:
    url = archive_url(package)
    with tempfile.NamedTemporaryFile(prefix="partfinder-", suffix=".zip") as temporary:
        temporary.write(fetch_bytes(url))
        temporary.flush()
        with zipfile.ZipFile(temporary.name) as archive:
            parts: set[str] = set()
            for name in archive.namelist():
                if name.lower().endswith(".cfg"):
                    parts.update(cfg_parts(archive.read(name).decode("utf-8-sig", errors="replace")))
            return sorted(parts)


def contents(repo: str, path: str, token: str) -> dict:
    try:
        result = request(f"https://api.github.com/repos/{repo}/contents/{path}?ref=main", token)
        return json.loads(base64.b64decode(result["content"]).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise


def commit(repo: str, token: str, head: str, files: dict[str, str], headline: str) -> str:
    query = """
    mutation($input: CreateCommitOnBranchInput!) {
      createCommitOnBranch(input: $input) { commit { oid url } }
    }
    """
    payload = {"query": query, "variables": {"input": {
        "branch": {"repositoryNameWithOwner": repo, "branchName": "main"},
        "message": {"headline": headline},
        "expectedHeadOid": head,
        "fileChanges": {"additions": [
            {"path": path, "contents": base64.b64encode(value.encode()).decode()}
            for path, value in files.items()
        ]},
    }}}
    result = request("https://api.github.com/graphql", token, "POST", payload)
    if result.get("errors"):
        raise RuntimeError(result["errors"])
    return result["data"]["createCommitOnBranch"]["commit"]["oid"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub repository, e.g. Dankular/PartFinder-KSP")
    parser.add_argument("--token-env", default="PARTFINDER_GITHUB_TOKEN")
    parser.add_argument("--catalog-url", default=DEFAULT_CATALOG)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"missing GitHub token in ${args.token_env}")

    repo_api = f"https://api.github.com/repos/{args.repo}"
    head = request(f"{repo_api}/git/ref/heads/main", token)["object"]["sha"]
    parts = contents(args.repo, "parts.json", token).get("parts", {})
    packages = contents(args.repo, "packages.json", token).get("packages", {})
    if isinstance(packages, list):
        packages = {item["identifier"]: item for item in packages}
    catalog = current_packages(fetch_bytes(args.catalog_url))
    selected = catalog[:args.limit] if args.limit else catalog
    print(f"Catalog packages: {len(catalog)}", flush=True)

    for number, package in enumerate(selected, 1):
        identifier = package["identifier"]
        url = archive_url(package)
        old = packages.get(identifier, {})
        if old.get("url") == url and (not package.get("download_hash") or old.get("source_hash") == package.get("download_hash")):
            print(f"[{number}/{len(selected)}] unchanged {identifier}", flush=True)
            continue
        try:
            package_parts = inspect_package(package)
            old_parts = old.get("parts", [])
            for part in old_parts:
                parts[part] = [item for item in parts.get(part, []) if item.get("identifier") != identifier]
                if not parts[part]:
                    del parts[part]
            record = {"identifier": identifier, "name": package.get("name"),
                      "version": package.get("version"), "url": url,
                      "source_hash": package.get("download_hash"), "parts": package_parts}
            for part in package_parts:
                parts.setdefault(part, []).append({"identifier": identifier,
                    "name": package.get("name"), "version": package.get("version"), "url": url})
                parts[part].sort(key=lambda item: item["identifier"].casefold())
            packages[identifier] = record
            manifest = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                        "catalog_packages": len(catalog), "indexed_packages": len(packages),
                        "part_count": len(parts)}
            files = {"parts.json": json.dumps({"schema_version": 1, "parts": parts}, separators=(",", ":")),
                     "packages.json": json.dumps({"schema_version": 1, "packages": packages}, separators=(",", ":")),
                     "manifest.json": json.dumps(manifest, separators=(",", ":"))}
            head = commit(args.repo, token, head, files, f"Index {identifier}")
            print(f"[{number}/{len(selected)}] committed {identifier} ({len(package_parts)} parts)", flush=True)
        except (OSError, urllib.error.URLError, zipfile.BadZipFile, KeyError, RuntimeError) as exc:
            print(f"[{number}/{len(selected)}] failed {identifier}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
