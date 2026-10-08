"""Extract state school-zone segments from an OSM extract (QLD / NSW / VIC).

Build-time only. The app never queries OSM while driving.

  python3 tools/build_school_zones.py --state nsw --out-dir dist
  python3 tools/build_school_zones.py --state vic --out-dir dist
  python3 tools/build_school_zones.py --state qld --out-dir dist
  python3 tools/build_school_zones.py --all --out-dir dist
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import osmium

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_road_pack import (
    DRIVABLE,
    csv_name,
    download,
    extract_timestamp,
    parse_maxspeed,
    sha256_file,
    simplify_way,
    write_gzip_deterministic,
)
from school_manifest import (  # noqa: E402  (stdlib-only, unit-tested)
    STATES,
    assemble_entries,
    entry_from_pack,
    load_manifest,
    write_combined_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "tools" / "cache"
DIST = ROOT / "dist"

TIME_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")
CONDITIONAL_LIMIT = re.compile(r"^\s*(\d{1,3})\s*(?:km/?h)?\s*@", re.IGNORECASE)


def looks_like_school_zone(tags: osmium.osm.TagList) -> bool:
    hazard = (tags.get("hazard") or "").lower()
    variable = (tags.get("maxspeed:variable") or "").lower()
    source = " ".join(
        filter(
            None,
            [
                tags.get("source:maxspeed"),
                tags.get("source:maxspeed:conditional"),
                tags.get("zone:maxspeed"),
            ],
        )
    ).lower()
    traffic = (tags.get("traffic_sign") or "").lower()
    if hazard == "school_zone":
        return True
    if "school" in variable:
        return True
    if "school" in source or "school" in traffic:
        return True
    conditional = tags.get("maxspeed:conditional") or ""
    if "school" in conditional.lower():
        return True
    if looks_like_school_hours(conditional):
        return True
    return False


def looks_like_school_hours(conditional: str) -> bool:
    """Heuristic for AU school-zone time patterns (Mo-Fr morning + afternoon)."""
    if not conditional:
        return False
    raw = conditional.lower()
    if "mo-fr" not in raw and "mo-fri" not in raw:
        return False
    ranges = TIME_RANGE.findall(conditional)
    if not ranges:
        return False
    morning = any(int(h1) in (7, 8) and int(h2) in (8, 9, 10) for h1, _, h2, _ in ranges)
    afternoon = any(int(h1) in (14, 15) and int(h2) in (15, 16, 17) for h1, _, h2, _ in ranges)
    allday = any(int(h1) in (7, 8) and int(h2) in (15, 16, 17) for h1, _, h2, _ in ranges)
    return (morning and afternoon) or allday


def windows_from_conditional(conditional: str | None, fallback: str) -> str:
    if not conditional:
        return fallback
    ranges = TIME_RANGE.findall(conditional)
    if not ranges:
        return fallback
    parts: list[str] = []
    for h1, m1, h2, m2 in ranges:
        start = int(h1) * 100 + int(m1)
        end = int(h2) * 100 + int(m2)
        if 0 <= start < end <= 2359:
            parts.append(f"{start:04d}-{end:04d}")
    return "|".join(parts) if parts else fallback


def school_limit(tags: osmium.osm.TagList, default_limit: int) -> int:
    conditional = tags.get("maxspeed:conditional")
    if conditional:
        match = CONDITIONAL_LIMIT.match(conditional)
        if match:
            limit = int(match.group(1))
            if 10 <= limit <= 80:
                return limit
    posted = parse_maxspeed(tags.get("maxspeed"))
    if posted is not None and posted >= 80:
        return 60
    return default_limit


def build_rows(pbf: Path, default_windows: str, default_limit: int) -> tuple[list[str], int]:
    rows: list[str] = []
    ways = 0
    processor = (
        osmium.FileProcessor(str(pbf))
        .with_locations()
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        .with_filter(osmium.filter.KeyFilter("highway"))
    )
    for obj in processor:
        if not obj.is_way():
            continue
        highway = obj.tags.get("highway")
        if highway not in DRIVABLE:
            continue
        if not looks_like_school_zone(obj.tags):
            continue
        name = csv_name(obj.tags.get("name") or obj.tags.get("ref") or "Unnamed road")
        limit = school_limit(obj.tags, default_limit)
        windows = windows_from_conditional(obj.tags.get("maxspeed:conditional"), default_windows)
        points = [
            (node.lat, node.lon)
            for node in obj.nodes
            if node.location.valid()
        ]
        if len(points) < 2:
            continue
        ways += 1
        for lat1, lon1, lat2, lon2 in simplify_way(points):
            rows.append(
                f"{name},{limit},{lat1:.6f},{lon1:.6f},{lat2:.6f},{lon2:.6f},{windows}"
            )
        if ways % 500 == 0:
            print(f"  kept {ways:,} school ways / {len(rows):,} segments", flush=True)
    print(f"Kept {ways:,} school ways / {len(rows):,} segments")
    return rows, ways


def write_overlay(
    rows: list[str],
    meta: dict,
    source: str,
    dest: Path,
    osm_ts: str,
    ways: int,
) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    built = date.today().isoformat()
    header = [
        f"# region={meta['region']}",
        f"# state={meta['code']}",
        f"# built={built}",
        f"# osm_date={osm_ts}",
        f"# source={source}",
        "# licence=ODbL 1.0 https://www.openstreetmap.org/copyright",
        "# schema=road_name,school_limit_kmh,lat1,lon1,lat2,lon2,windows",
        f"# times={meta['timezone']} school days; windows are local HHMM-HHMM",
        f"# default_windows={meta['windows']}",
        "# Road data © OpenStreetMap contributors",
        f"# ways={ways}",
        f"# segments={len(rows)}",
    ]
    body = "\n".join(header + rows) + "\n"
    write_gzip_deterministic(dest, body)
    digest = sha256_file(dest)
    entry = {
        "code": meta["code"],
        "region": meta["region"],
        "timezone": meta["timezone"],
        "osmTimestamp": osm_ts,
        "built": built,
        "sha256": digest,
        "bytes": dest.stat().st_size,
        "packFile": dest.name,
        "source": source,
        "licence": "ODbL-1.0",
        "attribution": "Road data © OpenStreetMap contributors",
        "ways": ways,
        "segments": len(rows),
        "defaultWindows": meta["windows"],
        "coverageNote": (
            "OSM school-zone tagging is incomplete; segment count reflects tagged ways only, "
            "not every signed school zone on the ground."
        ),
    }
    print(f"Wrote {dest} ({dest.stat().st_size:,} bytes, {len(rows):,} segments, {ways:,} ways)")
    print(f"osm_date={osm_ts} sha256={digest}")
    return entry


def build_state(key: str, out_dir: Path, pbf_override: Path | None = None) -> dict:
    meta = STATES[key]
    pbf = pbf_override or (CACHE / meta["pbf"])
    download(meta["url"], pbf)
    rows, ways = build_rows(pbf, meta["windows"], meta["default_limit"])
    if not rows:
        raise RuntimeError(f"No {meta['region']} school-zone segments found")
    dest = out_dir / meta["file"]
    return write_overlay(rows, meta, meta["url"], dest, extract_timestamp(pbf), ways)


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--state", choices=sorted(STATES), help="Build one state")
    parser.add_argument("--all", action="store_true", help="Build QLD+NSW+VIC")
    parser.add_argument("--pbf", default="", help="Override PBF path (single --state)")
    parser.add_argument("--out-dir", default=str(DIST))
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Rebuild school-zones-manifest.json without building any state",
    )
    parser.add_argument(
        "--base-manifest",
        default="",
        help=(
            "Previously published school-zones-manifest.json. States not rebuilt this run "
            "are carried forward from it instead of being dropped."
        ),
    )
    parser.add_argument(
        "--carry-forward-dir",
        default="",
        help=(
            "Directory holding the previously published *_school_zones.csv.gz packs. "
            "Used to verify (by sha256) or rebuild entries for states not rebuilt this run."
        ),
    )
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_manifest = load_manifest(Path(args.base_manifest)) if args.base_manifest else None
    carry_dir = Path(args.carry_forward_dir) if args.carry_forward_dir else None

    keys: list[str]
    if args.manifest_only:
        keys = []
    elif args.all:
        keys = ["qld", "nsw", "vic"]
    elif args.state:
        keys = [args.state]
    else:
        parser.error("Specify --state, --all or --manifest-only")
        return 2

    rebuilt: list[dict] = []
    for key in keys:
        pbf = Path(args.pbf) if args.pbf and len(keys) == 1 else None
        entry = build_state(key, out_dir, pbf)
        (out_dir / f"{key}_school_meta.json").write_text(
            json.dumps(entry, indent=2) + "\n", encoding="utf-8"
        )
        rebuilt.append(entry)

    if args.manifest_only:
        # Packs already in out_dir are what would be uploaded: treat them as rebuilt.
        for key, meta in STATES.items():
            gz = out_dir / meta["file"]
            if not gz.exists():
                continue
            sidecar = out_dir / f"{key}_school_meta.json"
            entry = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else None
            if entry is None or entry.get("sha256") != sha256_file(gz):
                entry = entry_from_pack(gz, key)
            rebuilt.append(entry)

    # Merge, never overwrite: keep every state that was not rebuilt this run.
    print("Merging school-zones manifest:")
    entries = assemble_entries(
        rebuilt,
        base_manifest=base_manifest,
        carry_forward_dir=carry_dir,
        sidecar_dir=out_dir,
    )
    write_combined_manifest(entries, out_dir / "school-zones-manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())