"""Freeze Mooroolbark evidence for Fieldwork; never edit the source datasets.

Run with scripts/ml/.venv/bin/python so the existing read-only inventory reader
can reproduce its spatial and address rules against the frozen copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

CHUNK_BYTES = 64 * 1024 * 1024
SUPPORT = (
    "data/suburbs/mooroolbark", "data/reviews", "data/references",
    "data/training", "data/cohorts", "data/experiments", "data/roof-review-loop",
    "data/scene-batches", "data/scene-self-reviews", "data/scene-parcels",
    "data/address-evidence", "data/sam3-review",
)
EXCLUDED_SUFFIXES = ("-wal", "-shm", ".pyc", ".pid", ".lock", ".partial")


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest_file(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded(value))


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def table_counts(db):
    return {name: db.execute(f"SELECT count(*) FROM {quote(name)}").fetchone()[0]
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def snapshot_db(source, target, geo=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    with (sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original,
          sqlite3.connect(target) as frozen):
        original.backup(frozen)
        before = table_counts(frozen)
        if geo:
            # Authentication is installation-specific, never annotation evidence.
            frozen.execute("PRAGMA secure_delete=ON")
            frozen.execute("DROP TABLE IF EXISTS crm_api_keys")
            frozen.execute("DROP TABLE IF EXISTS crm_users")
            for table in ("crm_address_meta", "crm_features"):
                frozen.execute(f"DELETE FROM {table} WHERE address_id NOT IN (SELECT id FROM addresses WHERE upper(suburb)='MOOROOLBARK')")
            frozen.execute("DELETE FROM crm_pois WHERE address_id IS NOT NULL AND address_id NOT IN (SELECT id FROM addresses WHERE upper(suburb)='MOOROOLBARK')")
            frozen.commit()
            frozen.execute("VACUUM")
        frozen.execute("PRAGMA journal_mode=DELETE")
        integrity = frozen.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity failed: {source}: {integrity}")
        return {"sourceTables": before, "snapshotTables": table_counts(frozen), "integrity": integrity,
                "method": "SQLite online backup including committed WAL; auth tables removed and VACUUM applied" if geo else "SQLite online backup including committed WAL"}


def copy_stable(source, target):
    before = source.stat()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"Source changed while copying; rerun the snapshot: {source}")
    return (after.st_size, after.st_mtime_ns)


def semantic_inventory(value):
    return {k: v for k, v in value.items() if k != "fetched_at"}


def derive(root, source_root, scene_ids):
    sys.path.insert(0, str(root))
    from scripts.ml.scene_processing import inventory, summary

    derived = source_root / "data/fieldwork-derived/mooroolbark"
    frozen = inventory(source_root)
    live = inventory(root)
    if encoded(semantic_inventory(frozen)) != encoded(semantic_inventory(live)):
        write_json(derived / "source-inventory.json", live)
        write_json(derived / "frozen-inventory.json", frozen)
        raise RuntimeError("Frozen inventory differs from current local inventory; inspect saved comparisons")
    write_json(derived / "processing.json", frozen)
    head = {k: v for k, v in frozen.items() if k != "features"}
    head["featureCount"] = len(frozen["features"])
    head["scanSummary"] = summary(source_root)
    head["parity"] = {"canonicalInventoryMatches": True, "method": "Exact canonical JSON equality excluding fetched_at"}
    write_json(derived / "processing-summary.json", head)
    keys = ("key", "scene_id", "object_id", "band", "band_label", "label", "review_status", "area_m2", "detector_score")
    compact = [{"type": "Feature", "geometry": f["geometry"],
                "properties": {k: f["properties"].get(k) for k in keys}} for f in frozen["features"]]
    write_json(derived / "processing-map.geojson", {"type": "FeatureCollection", "features": compact})

    eyes = source_root / "data/reviews/eyes/eyes.sqlite"
    by_scene = defaultdict(lambda: {"tables": {}})
    global_rows = {"tables": {}}
    if eyes.exists():
        with sqlite3.connect(eyes.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                cols = [row[1] for row in db.execute(f"PRAGMA table_info({quote(table)})")]
                for row in db.execute(f"SELECT * FROM {quote(table)}"):
                    value = dict(row)
                    if "payload" in cols and isinstance(value.get("payload"), str):
                        try:
                            value["payload"] = json.loads(value["payload"])
                        except ValueError:
                            pass
                    sid = value.get("scene_id")
                    target = by_scene[sid] if sid in scene_ids else global_rows
                    target["tables"].setdefault(table, []).append(value)
    for sid, value in by_scene.items():
        write_json(derived / "analysis" / f"{sid}.json", value)
    write_json(derived / "analysis-global.json", global_rows)
    with sqlite3.connect((source_root / "data/australia_geo.db").as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        crm = {table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
               for table in ("crm_address_meta", "crm_features", "crm_pois")}
        crm["addresses"] = [dict(row) for row in db.execute("SELECT * FROM addresses WHERE upper(suburb)='MOOROOLBARK'")]
        crm["places"] = [dict(row) for row in db.execute("SELECT * FROM places WHERE upper(suburb)='MOOROOLBARK'")]
    write_json(derived / "crm.json", crm)
    return {"inventoryRoofs": len(frozen["features"]), "bands": frozen["counts"],
            "scanObjects": frozen["objects"], "tiles": len(frozen["queue"]),
            "addressCounts": frozen.get("address_counts", {}), "canonicalInventoryMatches": True}


def add_blob(path, blobs):
    chunks, whole = [], hashlib.sha256()
    with path.open("rb") as file:
        while True:
            block = file.read(CHUNK_BYTES)
            if not block and chunks:
                break
            whole.update(block)
            digest = hashlib.sha256(block).hexdigest()
            destination = blobs / digest
            if not destination.exists():
                destination.write_bytes(block)
            elif destination.stat().st_size != len(block):
                raise RuntimeError(f"Corrupt existing blob: {digest}")
            chunks.append({"sha256": digest, "bytes": len(block)})
            if not block:
                break
    return {"sha256": whole.hexdigest(), "bytes": path.stat().st_size, "chunks": chunks,
            "mtimeNs": str(path.stat().st_mtime_ns),
            "contentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}


def build(args):
    root, output = Path(args.root).resolve(), Path(args.output).resolve()
    previous = json.loads((output / "manifest.json").read_text()) if (output / "manifest.json").exists() else None
    if previous and not args.refresh:
        raise RuntimeError("Snapshot already finalized; choose a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    source_root, blobs = output / "source", output / "blobs"
    source_root.mkdir(exist_ok=True)
    blobs.mkdir(exist_ok=True)
    inventory = json.loads(Path(args.inventory).read_text())
    scene_ids = sorted(s["id"] for s in inventory["scenes"] if s["scope"]["frame_intersects"])
    excluded_scenes = sorted(set(inventory.get("excludedCurrentSceneIds", [])) |
                             {s["id"] for s in inventory["scenes"] if not s["scope"]["frame_intersects"]})
    known_scenes = {s["id"] for s in inventory["scenes"]} | set(excluded_scenes)
    current_scenes = {p.parent.name for p in (root / "data/scenes").glob("*/latest.json")}
    if current_scenes - known_scenes:
        raise RuntimeError("New current scenes require a fresh spatial inventory before capture")
    paths, excluded = set(), Counter()
    roots = [*SUPPORT, *(f"data/scenes/{sid}" for sid in scene_ids)]
    for relative in roots:
        folder = root / relative
        if not folder.exists():
            continue
        for current, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git", ".venv", "node_modules"}]
            for name in files:
                p = Path(current) / name
                if name.endswith(EXCLUDED_SUFFIXES) or p.is_symlink():
                    excluded["ephemeral_or_symlink"] += 1
                else:
                    paths.add(p.relative_to(root).as_posix())
    for relative in ("data/australia_geo.db", "data/australia_tiles.mbtiles", "data/australia_tiles_dedup.mbtiles", "public/address-roofs.json"):
        if (root / relative).is_file():
            paths.add(relative)
    # Analysis documentation and the exact reader implementation are part of replay provenance.
    for folder in ("src", "internal", "cmd", "proto", "gen", "scripts", "docs"):
        for current, dirs, files in os.walk(root / folder):
            dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git", ".venv", "node_modules", "models", "annotation", "annotations", "annotation-server", "annotation-migrate", "annotate"}]
            for name in files:
                p = Path(current) / name
                if p.suffix in {".go", ".py", ".ts", ".tsx", ".css", ".proto", ".md", ".mjs"} and p.name != "annotation_snapshot.py":
                    paths.add(p.relative_to(root).as_posix())
    for relative in ("go.mod", "go.sum", "package.json", "pnpm-lock.yaml", "Taskfile.yml", "AGENTS.md", "scripts/ml/requirements.txt"):
        paths.add(relative)
    source_stamps, database_reports = {}, {}
    if previous:
        write_json(output / "history" / f"{previous['snapshotId']}.json", previous)
        for old in previous["files"]:
            relative = old["path"]
            if relative not in paths and not relative.startswith("data/fieldwork-derived/"):
                # Only the disposable reconstruction changes; the original manifest/blobs remain.
                (source_root / relative).unlink(missing_ok=True)
    for index, relative in enumerate(sorted(paths)):
        source, destination = root / relative, source_root / relative
        if source.suffix in {".sqlite", ".db", ".mbtiles"}:
            temporary = destination.with_name(destination.name + ".snapshot-tmp")
            temporary.unlink(missing_ok=True)
            database_reports[relative] = snapshot_db(source, temporary, relative == "data/australia_geo.db")
            os.replace(temporary, destination)
        else:
            stamp = (source.stat().st_size, source.stat().st_mtime_ns)
            if previous and destination.exists() and stamp == (destination.stat().st_size, destination.stat().st_mtime_ns):
                source_stamps[relative] = stamp
            else:
                source_stamps[relative] = copy_stable(source, destination)
        if index % 500 == 0:
            print(f"Snapshot copied {index}/{len(paths)} files", flush=True)
    summary = derive(root, source_root, set(scene_ids))
    replay = subprocess.run(
        [sys.executable, str(source_root / "scripts/verify_annotation_restore.py"), str(source_root)],
        check=True, capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    summary["restoredReaderReplay"] = json.loads(replay.stdout)
    for relative, stamp in source_stamps.items():
        actual = (root / relative).stat()
        if stamp != (actual.st_size, actual.st_mtime_ns):
            raise RuntimeError(f"Source changed during snapshot: {relative}")
    summary.update({"sceneCount": len(scene_ids), "databaseSnapshots": database_reports})
    files = []
    all_paths = sorted(p for p in source_root.rglob('*') if p.is_file() and not p.name.endswith(("-wal", "-shm")))
    for index, path in enumerate(all_paths):
        files.append({"path": path.relative_to(source_root).as_posix(), **add_blob(path, blobs)})
        if index % 500 == 0:
            print(f"Hashed {index}/{len(all_paths)} snapshot files", flush=True)
    exclusions = [
        {"path": "data/imagery", "reason": "Disposable tile download cache; all referenced scene and frozen run imagery is preserved"},
        {"path": "data/scenes", "reason": f"{len(excluded_scenes)} current scene frames do not intersect Mooroolbark; shared training/evaluation artifacts retain their original context", "sceneIds": excluded_scenes},
        {"path": "data/australia_geo.db", "reason": "Installation credentials excluded (crm_users, crm_api_keys); unrelated CRM annotations removed. Existing geo context retained read-only.", "details": database_reports["data/australia_geo.db"]},
        {"path": "runtime", "reason": "Locks, PIDs, partial writes, bytecode and WAL/SHM omitted; SQLite backups include committed WAL", "counts": dict(excluded)},
        {"path": "environment", "reason": "Dependency installations, downloaded inference weights, authentication configuration and machine processes are not migrated; frozen code and stored analysis are preserved"},
    ]
    stable = {"schemaVersion": 1, "sceneIds": scene_ids, "files": files}
    manifest = {**stable, "snapshotId": hashlib.sha256(encoded(stable)).hexdigest(),
                "createdAt": datetime.now(UTC).isoformat(), "summary": summary, "exclusions": exclusions}
    # Independent reconstruction hashes every ordered chunk against the whole-file checksum.
    for entry in files:
        check = hashlib.sha256()
        size = 0
        for chunk in entry["chunks"]:
            path = blobs / chunk["sha256"]
            if digest_file(path) != chunk["sha256"]:
                raise RuntimeError(f"Corrupt chunk: {path.name}")
            with path.open("rb") as file:
                for block in iter(lambda: file.read(1024 * 1024), b""):
                    check.update(block)
                    size += len(block)
        if check.hexdigest() != entry["sha256"] or size != entry["bytes"]:
            raise RuntimeError(f"Reconstruction mismatch: {entry['path']}")
    # Publish only once all checks pass, so a concurrent importer sees a complete generation.
    write_json(output / "manifest.json.pending", manifest)
    os.replace(output / "manifest.json.pending", output / "manifest.json")
    current_hashes = {chunk['sha256'] for entry in files for chunk in entry['chunks']}
    report = {"snapshotId": manifest["snapshotId"], "fileCount": len(files),
              "sourceBytes": sum(f["bytes"] for f in files), "uniqueBlobs": len(current_hashes),
              "uniqueBlobBytes": sum((blobs / digest).stat().st_size for digest in current_hashes), "allFileReconstructionsMatch": True,
              "summary": summary, "exclusions": exclusions}
    write_json(output / "local-verification.json", report)
    print(json.dumps({k: v for k, v in report.items() if k not in {"summary", "exclusions"}}, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--refresh", action="store_true", help="Capture changed source state as a new immutable generation; preserve the previous manifest and blobs")
    build(parser.parse_args())
