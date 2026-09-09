"""Extract Queensland school-zone segments from a state OSM extract.

Build-time only. The app never queries OSM while driving.

  python3 tools/build_qld_school_zones.py --pbf tools/cache/queensland.osm.pbf
"""

from __future__ import annotations

import argparse
import gzip
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
    write_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "app" / "src" / "main" / "assets"
DEFAULT_PBF = ROOT / "tools" / "cache" / "queensland.osm.pbf"
DEFAULT_URL = "https://download.openstreetmap.fr/extracts/oceania/australia/queensland.osm.pbf"

TIME_RANGE = re.compile(
    r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})"
)
CONDITIONAL_LIMIT = re.compile(
    r"^\s*(\d{1,3})\s*(?:km/?h)?\s*@",
    re.IGNORECASE,
)

STANDARD_WINDOWS = "0700-0900|1400-1600"


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
    if looks_like_qld_school_hours(conditional):
        return True
    return False


def looks_like_qld_school_hours(conditional: str) -> bool:
    if not conditional:
        return False
    raw = conditional.lower()
    if "mo-fr" not in raw and "mo-fri" not in raw:
        return False
    ranges = TIME_RANGE.findall(conditional)
    if not ranges:
        return False
    morning = any(int(h1) in (7, 8) and int(h2) in (8, 9) for h1, _, h2, _ in ranges)
    afternoon = any(int(h1) in (14, 15) and int(h2) in (15, 16) for h1, _, h2, _ in ranges)
    allday = any(int(h1) in (7, 8) and int(h2) in (15, 16) for h1, _, h2, _ in ranges)
    return (morning and afternoon) or allday


def windows_from_conditional(conditional: str | None) -> str:
    if not conditional:
        return STANDARD_WINDOWS
    ranges = TIME_RANGE.findall(conditional)
    if not ranges:
        return STANDARD_WINDOWS
    parts: list[str] = []
    for h1, m1, h2, m2 in ranges:
        start = int(h1) * 100 + int(m1)
        end = int(h2) * 100 + int(m2)
        if 0 <= start < end <= 2359:
            parts.append(f"{start:04d}-{end:04d}")
    return "|".join(parts) if parts else STANDARD_WINDOWS


def school_limit(tags: osmium.osm.TagList) -> int:
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
    return 40


def build_rows(pbf: Path) -> list[str]:
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
        limit = school_limit(obj.tags)
        windows = windows_from_conditional(obj.tags.get("maxspeed:conditional"))
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
    return rows


def write_overlay(rows: list[str], source: str, dest: Path, osm_ts: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    built = date.today().isoformat()
    header = [
        "# region=Queensland",
        f"# built={built}",
        f"# osm_date={osm_ts}",
        f"# source={source}",
        "# licence=ODbL 1.0 https://www.openstreetmap.org/copyright",
        "# schema=road_name,school_limit_kmh,lat1,lon1,lat2,lon2,windows",
        "# times=Australia/Brisbane school days; windows are local HHMM-HHMM",
        "# Road data © OpenStreetMap contributors",
    ]
    body = "\n".join(header + rows) + "\n"
    write_gzip_deterministic(dest, body)
    digest = sha256_file(dest)
    payload = {
        "region": "Queensland",
        "osmTimestamp": osm_ts,
        "built": built,
        "sha256": digest,
        "bytes": dest.stat().st_size,
        "packFile": dest.name,
        "source": source,
        "licence": "ODbL-1.0",
        "attribution": "Road data © OpenStreetMap contributors",
    }
    write_manifest(dest.with_name("school-zones-manifest.json"), payload)
    print(f"Wrote {dest} ({dest.stat().st_size:,} bytes, {len(rows):,} segments)")
    print(f"osm_date={osm_ts} sha256={digest}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pbf", default=str(DEFAULT_PBF))
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--out",
        default=str(ASSETS / "qld_school_zones.csv.gz"),
    )
    args = parser.parse_args()
    pbf = Path(args.pbf)
    download(args.url, pbf)
    rows = build_rows(pbf)
    if not rows:
        print("No Queensland school-zone segments found", file=sys.stderr)
        return 1
    write_overlay(rows, args.url, Path(args.out), extract_timestamp(pbf))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
