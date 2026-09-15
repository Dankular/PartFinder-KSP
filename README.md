# CKAN ship-part resolver

`ckan_ship_parts.py` reads one or more KSP1 `.craft` files and reports the part IDs they use. With `--ksp`, it scans `GameData` and the CKAN instance registry, so installed parts are attributed to their installed GameData root. With `--archive`, it can inspect downloaded CKAN ZIPs and identify parts from mods that are not currently installed.

```powershell
python .\ckan_ship_parts.py .\MyShip.craft --ksp "C:\Games\KSP"
python .\ckan_ship_parts.py .\MyShip.craft --ksp "C:\Games\KSP" --archive "C:\Users\me\AppData\Local\CKAN\downloads\SomeMod.zip"
python .\ckan_ship_parts.py .\MyShip.craft --ksp "C:\Games\KSP" --archive-dir "C:\Users\me\AppData\Local\CKAN\downloads"
python .\ckan_ship_parts.py .\MyShip.craft --ksp "C:\Games\KSP" --json
python .\ckan_ship_parts.py .\MyShip.craft --part-registry .\part-registry.json
python .\ckan_ship_parts.py .\MyShip.craft --part-registry-url "https://raw.githubusercontent.com/YOUR-ORG/PartFinder-KSP/main/parts.json"
```

`MISSING` means the part is not present in the supplied local files. CKAN's metadata does not itself list every `PART` node, so a completely unknown part cannot be resolved from metadata alone without inspecting the mod archive. The next useful extension is to enumerate CKAN's download cache automatically and scan all ZIPs.

## Build a complete part registry

For the shared `PartFinder-KSP` GitHub repository, the VPS should run:

```powershell
python .\build_part_registry.py --download --dataset-dir .\dataset --cache .\mod-cache --workers 8

# VPS mode: archive is deleted after each package is indexed
python .\build_part_registry.py --download --ephemeral --dataset-dir .\dataset --workers 2 `
  --previous-state-url "https://raw.githubusercontent.com/Dankular/PartFinder-KSP/main/package-index.json"
```

The normal mode keeps `.mod-cache` and `dataset/package-index.json` between runs. VPS mode uses `--ephemeral`: each archive is temporary and deleted immediately after indexing. On refresh, `--previous-state-url` loads the prior published `package-index.json`; unchanged packages are reused in memory, while new or changed packages are downloaded and inspected. Publish `parts.json`, `packages.json`, `manifest.json`, and `package-index.json` to GitHub. Use `--limit 10` for a smoke test first. Do not retain `.mod-cache` on the VPS.

## VPS direct-to-GitHub mode

To keep no catalog-derived files on the VPS, use the direct publisher instead:

```bash
PARTFINDER_GITHUB_TOKEN=... python3 stream_catalog_to_github.py \
  --repo Dankular/PartFinder-KSP
```

It processes one package at a time, commits that package immediately, and deletes its temporary archive before moving on. It skips packages whose URL/hash is unchanged, so reruns refresh only new or changed mods.
