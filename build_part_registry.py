#!/usr/bin/env python3
"""Build the PartFinder-KSP reverse index from the CKAN catalog."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from ckan_ship_parts import cfg_parts

DEFAULT_REPO = "https://github.com/KSP-CKAN/CKAN-meta/archive/master.tar.gz"


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "PartFinder-KSP/1.0"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def fetch_json(url: str) -> dict:
    return json.loads(fetch(url).decode("utf-8-sig"))


def metadata_from_repo(blob: bytes) -> list[dict]:
    packages = []
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.lower().endswith(".ckan"):
                continue
            try:
                item = json.loads(archive.extractfile(member).read().decode("utf-8-sig"))
            except (AttributeError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(item, dict) and item.get("identifier"):
                packages.append(item)
    return packages


def current_packages(packages: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for package in packages:
        if package.get("kind") in {"metapackage", "dlc"} or not package.get("download"):
            continue
        identifier = package["identifier"]
        old = latest.get(identifier)
        if old is None or str(package.get("release_date", "")) > str(old.get("release_date", "")):
            latest[identifier] = package
    return sorted(latest.values(), key=lambda p: p["identifier"].casefold())


def archive_url(package: dict) -> str:
    value = package["download"]
    return value[0] if isinstance(value, list) else value


def package_record(package: dict) -> dict:
    return {key: package.get(key) for key in
            ("identifier", "name", "version", "release_date", "ksp_version", "abstract")}


def index_one(package: dict, cache: Path, number: int, total: int, ephemeral: bool = False) -> dict:
    url = archive_url(package)
    print(f"[{number}/{total}] downloading {package['identifier']}", flush=True)
    temporary_dir = tempfile.TemporaryDirectory(prefix="partfinder-") if ephemeral else None
    try:
        archive_path = (Path(temporary_dir.name) / "mod.zip" if temporary_dir else
                        cache / (hashlib.sha256(url.encode()).hexdigest() + ".zip"))
        if not archive_path.exists():
            temporary = archive_path.with_suffix(".tmp")
            temporary.write_bytes(fetch(url))
            temporary.replace(archive_path)
        parts: set[str] = set()
        with zipfile.ZipFile(archive_path) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".cfg"):
                    parts.update(cfg_parts(archive.read(name).decode("utf-8-sig", errors="replace")))
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    finally:
        if temporary_dir:
            temporary_dir.cleanup()
    return {"package": package_record(package), "url": url, "parts": sorted(parts),
            "archive_sha256": digest, "source_hash": package.get("download_hash")}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", default=DEFAULT_REPO)
    parser.add_argument("--dataset-dir", type=Path, help="write GitHub-ready dataset files here")
    parser.add_argument("--output", type=Path, default=Path("part-registry.json"), help="legacy single-file output")
    parser.add_argument("--cache", type=Path, default=Path(".mod-cache"))
    parser.add_argument("--download", action="store_true", help="download and inspect archives")
    parser.add_argument("--workers", type=int, default=8, help="parallel archive workers")
    parser.add_argument("--ephemeral", action="store_true", help="delete each archive immediately after indexing")
    parser.add_argument("--previous-state-url", help="published package-index.json from the previous run")
    parser.add_argument("--limit", type=int, help="inspect only the first N packages")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")

    dataset = args.dataset_dir
    state_path = dataset / "package-index.json" if dataset else None
    state: dict[str, dict] = {}
    if state_path and state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")).get("packages", {})
        except json.JSONDecodeError:
            print(f"warning: ignoring invalid checkpoint {state_path}")
    if args.previous_state_url:
        try:
            state = fetch_json(args.previous_state_url).get("packages", {})
            print(f"Loaded previous state: {len(state)} packages")
        except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError) as exc:
            print(f"warning: unable to load previous state: {exc}")

    print(f"Fetching CKAN metadata: {args.repo_url}")
    packages = current_packages(metadata_from_repo(fetch(args.repo_url)))
    selected = packages[:args.limit] if args.limit else packages
    failures: list[dict] = []
    results: dict[str, dict] = {}
    if args.download:
        if not args.ephemeral:
            args.cache.mkdir(parents=True, exist_ok=True)
        work = []
        for number, package in enumerate(selected, 1):
            identifier = package["identifier"]
            url = archive_url(package)
            old = state.get(identifier, {})
            current_hash = package.get("download_hash")
            hash_matches = not current_hash or old.get("source_hash") == current_hash
            if old.get("url") == url and hash_matches and isinstance(old.get("parts"), list):
                results[identifier] = old
            else:
                work.append((number, package))
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(index_one, package, args.cache, number, len(selected), args.ephemeral): package
                       for number, package in work}
            for future in as_completed(futures):
                package = futures[future]
                try:
                    result = future.result()
                    results[package["identifier"]] = result
                    if state_path:
                        write_json(state_path, {"schema_version": 1, "packages": results})
                except (OSError, urllib.error.URLError, zipfile.BadZipFile, KeyError) as exc:
                    failures.append({"identifier": package["identifier"],
                                     "url": archive_url(package), "error": str(exc)})

    part_index: dict[str, list[dict]] = defaultdict(list)
    package_output = []
    for package in selected:
        identifier = package["identifier"]
        result = results.get(identifier)
        if not result:
            continue
        record = dict(result["package"])
        record["url"] = result["url"]
        record["part_count"] = len(result["parts"])
        package_output.append(record)
        for part in result["parts"]:
            part_index[part].append({"identifier": identifier, "name": package.get("name"),
                                     "version": package.get("version"), "url": result["url"]})

    registry = {"schema_version": 1, "parts": {
        part: sorted(mods, key=lambda item: item["identifier"].casefold())
        for part, mods in sorted(part_index.items(), key=lambda item: item[0].casefold())}}
    if dataset:
        write_json(dataset / "parts.json", registry)
        write_json(dataset / "packages.json", {"schema_version": 1, "packages": package_output})
        write_json(dataset / "package-index.json", {"schema_version": 1, "packages": results})
        write_json(dataset / "manifest.json", {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "metadata_repository": args.repo_url,
            "package_count": len(packages),
            "packages_processed": len(package_output),
            "archives_with_parts": sum(1 for item in package_output if item["part_count"]),
            "part_count": len(part_index),
            "failures": failures,
        })
        print(f"Wrote dataset: {dataset}")
    else:
        registry["metadata_repository"] = args.repo_url
        registry["package_count"] = len(packages)
        registry["failures"] = failures
        write_json(args.output, registry)
        print(f"Wrote: {args.output}")
    print(f"Catalog packages: {len(packages)}")
    print(f"Part IDs indexed: {len(part_index)}")
    print(f"Failures: {len(failures)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
