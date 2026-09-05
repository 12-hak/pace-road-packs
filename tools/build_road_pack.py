"""Build Pace's on-device OSM speed pack from a Geofabrik extract.

Downloads are build-time only. The Pace app never queries OSM while driving.

Artifacts are written to dist/ (pack-manifest.json + road_pack.csv.gz) for
GitHub Release publishing from this data repo.

  python3 tools/build_road_pack.py
  python3 tools/build_road_pack.py --out-dir dist

Road data © OpenStreetMap contributors (ODbL 1.0).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

import osmium

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "tools" / "cache"
DEFAULT_OUT = ROOT / "dist"
DEFAULT_URL = "https://download.geofabrik.de/australia-oceania/australia-latest.osm.pbf"
DEFAULT_REGION = "Australia"
STATE_URL = "https://download.geofabrik.de/australia-oceania/australia-updates/state.txt"

DRIVABLE = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "unclassified",
    "residential",
    "living_street",
    "busway",
    "road",
}

EXPLICIT_LIMIT = re.compile(r"^\s*(\d{1,3})\s*(?:km/?h)?\s*$", re.IGNORECASE)
SKIP_LIMIT = {"none", "signals", "walk", "variable", "unposted", "unlimited"}
MIN_SEGMENT_METRES = 8.0
MAX_SEGMENT_METRES = 90.0
HEADING_SPLIT_DEGREES = 22.0
METRES_PER_DEGREE_LAT = 111_320.0


def parse_maxspeed(value: str | None) -> int | None:
    if not value:
        return None
    raw = value.strip().lower()
    if not raw or raw in SKIP_LIMIT:
        return None
    if any(marker in raw for marker in (":", ";", "@", "mph", "knots")):
        return None
    match = EXPLICIT_LIMIT.match(value)
    if not match:
        return None
    limit = int(match.group(1))
    return limit if 10 <= limit <= 130 else None


def csv_name(name: str) -> str:
    cleaned = " ".join(name.replace(",", " ").replace("#", " ").split())
    return (cleaned[:80] if cleaned else "Unnamed road")


def heading(lat1: float, lon1: float, lat2: float, lon2: float) -> float | None:
    if lat1 == lat2 and lon1 == lon2:
        return None
    dy = lat2 - lat1
    dx = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.degrees(math.atan2(dx, dy)) % 360.0


def heading_delta(first: float | None, second: float | None) -> float:
    if first is None or second is None:
        return 0.0
    delta = abs(first - second) % 360.0
    return min(delta, 360.0 - delta)


def simplify_way(points: list[tuple[float, float]]) -> list[tuple[float, float, float, float]]:
    if len(points) < 2:
        return []
    segments: list[tuple[float, float, float, float]] = []
    start = points[0]
    prev = points[0]
    prev_heading: float | None = None
    accumulated = 0.0
    for point in points[1:]:
        piece = segment_metres(prev[0], prev[1], point[0], point[1])
        current_heading = heading(prev[0], prev[1], point[0], point[1])
        turn = heading_delta(prev_heading, current_heading)
        should_cut = accumulated > 0 and (
            accumulated + piece >= MAX_SEGMENT_METRES or turn >= HEADING_SPLIT_DEGREES
        )
        if should_cut:
            if accumulated >= MIN_SEGMENT_METRES:
                segments.append((start[0], start[1], prev[0], prev[1]))
            start = prev
            accumulated = 0.0
        accumulated += piece
        prev = point
        if current_heading is not None:
            prev_heading = current_heading
    if accumulated >= MIN_SEGMENT_METRES:
        segments.append((start[0], start[1], prev[0], prev[1]))
    return segments


def segment_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dx = (lon2 - lon1) * METRES_PER_DEGREE_LAT * math.cos(mean_lat)
    dy = (lat2 - lat1) * METRES_PER_DEGREE_LAT
    return math.hypot(dx, dy)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"Using cached extract: {dest} ({dest.stat().st_size:,} bytes)")
        return
    print(f"Downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)
    print(f"Saved {dest} ({dest.stat().st_size:,} bytes)")


def build_rows(pbf: Path) -> list[str]:
    rows: list[str] = []
    ways = 0
    skipped = 0
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
        limit = parse_maxspeed(obj.tags.get("maxspeed"))
        if limit is None:
            skipped += 1
            continue
        name = csv_name(obj.tags.get("name") or obj.tags.get("ref") or "Unnamed road")
        points = [
            (node.lat, node.lon)
            for node in obj.nodes
            if node.location.valid()
        ]
        if len(points) < 2:
            continue
        ways += 1
        for lat1, lon1, lat2, lon2 in simplify_way(points):
            rows.append(f"{name},{limit},{lat1:.6f},{lon1:.6f},{lat2:.6f},{lon2:.6f}")
        if ways % 25_000 == 0:
            print(f"  scanned {ways:,} tagged ways, {len(rows):,} segments", flush=True)
    print(f"Kept {ways:,} ways / {len(rows):,} segments; skipped {skipped:,} without an explicit km/h maxspeed")
    return rows


def extract_timestamp(pbf: Path | None = None) -> str:
    pbf = pbf or (CACHE / "australia-latest.osm.pbf")
    try:
        if pbf.exists():
            reader = osmium.io.Reader(str(pbf))
            try:
                header = reader.header()
                for key in ("osmosis_replication_timestamp", "timestamp"):
                    value = header.get(key)
                    if value:
                        return str(value).strip()
            finally:
                reader.close()
    except Exception as exc:
        print(f"PBF timestamp unavailable ({exc})")
    return geofabrik_timestamp()


def geofabrik_timestamp(state_url: str = STATE_URL) -> str:
    request = urllib.request.Request(state_url, headers={"User-Agent": "Pace-road-pack-build"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("timestamp="):
            # Geofabrik escapes colons as \:
            return line.split("=", 1)[1].strip().replace(r"\:", ":")
    return ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def rows_from_cache() -> tuple[list[str], str]:
    cache_csv = CACHE / "road_pack.csv"
    if not cache_csv.exists():
        raise FileNotFoundError(f"Missing cached pack: {cache_csv}")
    source = DEFAULT_URL
    rows: list[str] = []
    for line in cache_csv.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("# source="):
            source = stripped.split("=", 1)[1].strip()
        elif stripped and not stripped.startswith("#"):
            rows.append(stripped)
    return rows, source


def write_pack(rows: list[str], region: str, source: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    osm_ts = extract_timestamp()
    built = date.today().isoformat()
    header = [
        f"# region={region}",
        f"# built={built}",
        f"# osm_date={osm_ts}",
        f"# source={source}",
        "# licence=ODbL 1.0 https://www.openstreetmap.org/copyright",
        "# schema=road_name,speed_limit_kmh,lat1,lon1,lat2,lon2",
        "# Road data © OpenStreetMap contributors",
    ]
    body = "\n".join(header + rows) + "\n"
    cache_csv = CACHE / "road_pack.csv"
    gz_path = out_dir / "road_pack.csv.gz"
    cache_csv.write_text(body, encoding="utf-8")
    with gzip.open(gz_path, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    digest = sha256_file(gz_path)
    payload = {
        "region": region,
        "osmTimestamp": osm_ts,
        "built": built,
        "sha256": digest,
        "bytes": gz_path.stat().st_size,
        "packFile": "road_pack.csv.gz",
        "source": source,
        "licence": "ODbL-1.0",
        "attribution": "Road data © OpenStreetMap contributors",
    }
    write_manifest(CACHE / "pack-manifest.json", payload)
    write_manifest(out_dir / "pack-manifest.json", payload)
    print(f"Wrote {cache_csv} ({cache_csv.stat().st_size:,} bytes)")
    print(f"Wrote {gz_path} ({gz_path.stat().st_size:,} bytes)")
    print(f"Wrote {out_dir / 'pack-manifest.json'}")
    print(f"osm_date={osm_ts} sha256={digest}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Australia OSM road pack for Pace releases")
    parser.add_argument("--url", default=DEFAULT_URL, help="Geofabrik Australia PBF URL")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--pbf", default="", help="Local PBF path (default: tools/cache/australia-latest.osm.pbf)")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT), help="Output directory for pack artifacts")
    parser.add_argument("--from-cache", action="store_true", help="Rebuild gz/manifest from cached CSV only")
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    if args.from_cache:
        rows, source = rows_from_cache()
    else:
        pbf = Path(args.pbf) if args.pbf else CACHE / "australia-latest.osm.pbf"
        download(args.url, pbf)
        rows = build_rows(pbf)
        source = args.url
    if not rows:
        print("No explicit maxspeed segments found", file=sys.stderr)
        return 1
    write_pack(rows, args.region, source, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
