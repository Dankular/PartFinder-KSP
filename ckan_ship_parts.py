#!/usr/bin/env python3
"""Find the mods needed by KSP1 .craft files.

This intentionally uses local CKAN data and mod archives. CKAN's public metadata
describes packages/install rules, but does not publish a part-name reverse index.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path


PART_LINE = re.compile(r"^\s*part\s*=\s*(\S+)", re.IGNORECASE | re.MULTILINE)
NAME_LINE = re.compile(r"^\s*name\s*=\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def craft_parts(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return {m.group(1).strip() for m in PART_LINE.finditer(text)}


def cfg_parts(text: str) -> set[str]:
    """Return names from PART nodes without requiring a full KSP ConfigNode parser."""
    found: set[str] = set()
    stack: list[tuple[str, int]] = []
    node_start = re.compile(r"^\s*([A-Za-z0-9_:+.-]+)\s*\{", re.MULTILINE)
    events = sorted(
        [(m.start(), "open", m) for m in node_start.finditer(text)]
        + [(m.start(), "close", None) for m in re.finditer(r"\}", text)]
    )
    for pos, kind, match in events:
        if kind == "open":
            stack.append((match.group(1), pos))
        elif stack:
            node, start = stack.pop()
            if node.upper() == "PART":
                body = text[start:pos]
                found.update(m.group(1) for m in NAME_LINE.finditer(body))
    return found


def scan_gamedata(gamedata: Path) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = defaultdict(set)
    if not gamedata.is_dir():
        return owners
    for cfg in gamedata.rglob("*.cfg"):
        try:
            parts = cfg_parts(cfg.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
        relative = cfg.relative_to(gamedata)
        owner = relative.parts[0] if relative.parts else "GameData"
        for part in parts:
            owners[part].add(owner)
    return owners


def registry_roots(ksp: Path) -> dict[str, str]:
    """Map top-level GameData folders to CKAN identifiers."""
    registry = ksp / "CKAN" / "registry.json"
    if not registry.is_file():
        return {}
    try:
        data = json.loads(registry.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: unable to read {registry}: {exc}", file=sys.stderr)
        return {}
    result: dict[str, str] = {}
    installed = data.get("installed_modules", {})
    for identifier, module in installed.items():
        for filename in module.get("files", []):
            normalized = filename.replace("\\", "/").strip("/")
            pieces = normalized.split("/")
            if pieces and pieces[0].lower() == "gamedata":
                pieces = pieces[1:]
            if pieces:
                result.setdefault(pieces[0].lower(), identifier)
    return result


def archive_parts(archives: list[Path]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = defaultdict(set)
    for archive in archives:
        if not archive.is_file() or archive.suffix.lower() != ".zip":
            continue
        try:
            with zipfile.ZipFile(archive) as zf:
                for name in zf.namelist():
                    if not name.lower().endswith(".cfg"):
                        continue
                    try:
                        text = zf.read(name).decode("utf-8-sig", errors="replace")
                    except (KeyError, UnicodeError):
                        continue
                    for part in cfg_parts(text):
                        owners[part].add(archive.name)
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"warning: unable to inspect {archive}: {exc}", file=sys.stderr)
    return owners


def load_part_registry(path: Path) -> dict[str, list[dict]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        parts = data.get("parts", {})
        return parts if isinstance(parts, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: unable to read part registry {path}: {exc}", file=sys.stderr)
        return {}


def load_part_registry_url(url: str) -> dict[str, list[dict]]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "PartFinder-KSP/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8-sig"))
        parts = data.get("parts", {})
        return parts if isinstance(parts, dict) else {}
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"warning: unable to read part registry URL {url}: {exc}", file=sys.stderr)
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("craft", type=Path, nargs="+", help="KSP .craft files")
    parser.add_argument("--ksp", type=Path, help="KSP install directory (for GameData and CKAN registry)")
    parser.add_argument("--archive", type=Path, action="append", default=[], help="ZIP mod archive to inspect (repeatable)")
    parser.add_argument("--archive-dir", type=Path, help="scan every ZIP in a CKAN download/cache directory")
    parser.add_argument("--part-registry", type=Path, help="generated part-registry.json to use for catalog lookup")
    parser.add_argument("--part-registry-url", help="published parts.json URL to use for catalog lookup")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    installed: dict[str, set[str]] = {}
    ckan_roots: dict[str, str] = {}
    if args.ksp:
        installed = scan_gamedata(args.ksp / "GameData")
        ckan_roots = registry_roots(args.ksp)
    archives = list(args.archive)
    if args.archive_dir and args.archive_dir.is_dir():
        archives.extend(args.archive_dir.glob("*.zip"))
    cached = archive_parts(archives)
    catalog = (load_part_registry(args.part_registry) if args.part_registry else
               load_part_registry_url(args.part_registry_url) if args.part_registry_url else {})

    rows = []
    for craft in args.craft:
        if not craft.is_file():
            parser.error(f"craft file not found: {craft}")
        parts = craft_parts(craft)
        for part in sorted(parts, key=str.casefold):
            local = sorted(installed.get(part, set()))
            ckan = sorted({ckan_roots.get(root.lower(), root) for root in local})
            cached_mods = sorted(cached.get(part, set()))
            catalog_mods = catalog.get(part, [])
            status = ("installed" if local else
                      "found-in-archive" if cached_mods else
                      "catalog-match" if catalog_mods else "unknown")
            rows.append({"craft": str(craft), "part": part, "status": status,
                         "installed_roots": local, "ckan_modules": ckan,
                         "archive_matches": cached_mods, "catalog_matches": catalog_mods})

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    for row in rows:
        if row["status"] == "installed":
            owner = ", ".join(row["ckan_modules"])
            print(f"OK       {row['part']}  ({owner})")
        elif row["status"] == "found-in-archive":
            print(f"ARCHIVE  {row['part']}  ({', '.join(row['archive_matches'])})")
        elif row["status"] == "catalog-match":
            matches = ", ".join(
                f"{item.get('identifier', '?')} {item.get('version', '')}".strip()
                for item in row["catalog_matches"]
            )
            print(f"CATALOG  {row['part']}  ({matches})")
        else:
            print(f"MISSING  {row['part']}  (no local part definition or scanned archive)")
    if ckan_roots:
        print(f"\nRead CKAN registry: {args.ksp / 'CKAN' / 'registry.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
