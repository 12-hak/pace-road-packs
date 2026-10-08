"""School-zones manifest helpers (stdlib only, no osmium).

The weekly CI job rebuilds Queensland only. Before this module existed it then
wrote `school-zones-manifest.json` from that single build, which dropped the
NSW/VIC entries from the published manifest even though their packs were still
on the release. Pace clients look states up in the manifest, so NSW/VIC never
downloaded on fresh installs.

This module merges instead of overwriting:

* states rebuilt in this run replace their old entries;
* states not rebuilt are carried forward from the previously published packs
  (`--carry-forward-dir`, verified by sha256), the previously published
  manifest (`--base-manifest`), or local `<state>_school_meta.json` sidecars;
* the result is always ordered QLD, NSW, VIC (then anything else) and keeps the
  legacy top-level QLD fields.

Ordering matters: the Pace app parses the manifest with first-match regexes, so
its legacy top-level `sha256`/`packFile` reads pick up the first `states[]`
entry. QLD must stay first.

CLI (used by the workflow):

  python tools/school_manifest.py fingerprint MANIFEST
  python tools/school_manifest.py check MANIFEST --require QLD,NSW,VIC \
      [--dist DIR] [--release-assets ASSETS.json]
  python tools/school_manifest.py diff OLD NEW
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

LICENCE = "ODbL-1.0"
ATTRIBUTION = "Road data © OpenStreetMap contributors"
COVERAGE_NOTE = (
    "OSM school-zone tagging is incomplete; segment count reflects tagged ways only, "
    "not every signed school zone on the ground."
)

# Standard windows when OSM has no usable conditional times.
# QLD: 7–9 / 14–16. NSW/VIC commonly 8–9:30 / 14:30–16 (document in README).
STATES: dict[str, dict] = {
    "qld": {
        "code": "QLD",
        "region": "Queensland",
        "timezone": "Australia/Brisbane",
        "file": "qld_school_zones.csv.gz",
        "pbf": "queensland.osm.pbf",
        "url": "https://download.openstreetmap.fr/extracts/oceania/australia/queensland.osm.pbf",
        "windows": "0700-0900|1400-1600",
        "default_limit": 40,
    },
    "nsw": {
        "code": "NSW",
        "region": "New South Wales",
        "timezone": "Australia/Sydney",
        "file": "nsw_school_zones.csv.gz",
        "pbf": "new_south_wales.osm.pbf",
        "url": "https://download.openstreetmap.fr/extracts/oceania/australia/new_south_wales.osm.pbf",
        "windows": "0800-0930|1430-1600",
        "default_limit": 40,
    },
    "vic": {
        "code": "VIC",
        "region": "Victoria",
        "timezone": "Australia/Melbourne",
        "file": "vic_school_zones.csv.gz",
        "pbf": "victoria.osm.pbf",
        "url": "https://download.openstreetmap.fr/extracts/oceania/australia/victoria.osm.pbf",
        "windows": "0800-0930|1430-1600",
        "default_limit": 40,
    },
}

STATE_ORDER = [m["code"] for m in STATES.values()]  # QLD, NSW, VIC
BY_CODE = {m["code"]: (key, m) for key, m in STATES.items()}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_pack_header(gz_path: Path) -> dict[str, str]:
    """Return the `# key=value` header of a school pack (stops at first data row)."""
    header: dict[str, str] = {}
    with gzip.open(gz_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.startswith("#"):
                break
            body = line[1:].strip()
            if "=" in body:
                key, value = body.split("=", 1)
                header.setdefault(key.strip(), value.strip())
    return header


def entry_from_pack(gz_path: Path, state_key: str) -> dict:
    """Rebuild a manifest entry from a published pack file (sha/bytes from the bytes)."""
    meta = STATES[state_key]
    header = read_pack_header(gz_path)
    code = header.get("state", meta["code"]).upper()
    if code != meta["code"]:
        raise ValueError(f"{gz_path} header says state={code}, expected {meta['code']}")

    def as_int(name: str) -> int:
        try:
            return int(header.get(name, "0"))
        except ValueError:
            return 0

    return {
        "code": meta["code"],
        "region": header.get("region", meta["region"]),
        "timezone": meta["timezone"],
        "osmTimestamp": header.get("osm_date", ""),
        "built": header.get("built", ""),
        "sha256": sha256_file(gz_path),
        "bytes": gz_path.stat().st_size,
        "packFile": meta["file"],
        "source": header.get("source", meta["url"]),
        "licence": LICENCE,
        "attribution": ATTRIBUTION,
        "ways": as_int("ways"),
        "segments": as_int("segments"),
        "defaultWindows": header.get("default_windows", meta["windows"]),
        "coverageNote": COVERAGE_NOTE,
    }


def load_manifest(path: Path | None) -> dict | None:
    if path is None:
        return None
    if not path.exists() or path.stat().st_size == 0:
        print(f"Base manifest {path} not found; treating as empty")
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_states(manifest: dict | None) -> dict[str, dict]:
    """States by code. A schema v1 (QLD-only, top-level) manifest maps to QLD."""
    if not manifest:
        return {}
    out: dict[str, dict] = {}
    for entry in manifest.get("states") or []:
        code = str(entry.get("code", "")).upper()
        if code:
            out[code] = entry
    if not out and manifest.get("sha256"):
        out["QLD"] = {
            "code": "QLD",
            "region": manifest.get("region", "Queensland"),
            "timezone": "Australia/Brisbane",
            "osmTimestamp": manifest.get("osmTimestamp", ""),
            "built": manifest.get("built", ""),
            "sha256": manifest["sha256"],
            "bytes": manifest.get("bytes", 0),
            "packFile": manifest.get("packFile", STATES["qld"]["file"]),
            "source": manifest.get("source", STATES["qld"]["url"]),
        }
    return out


def order_entries(by_code: dict[str, dict]) -> list[dict]:
    known = [by_code[c] for c in STATE_ORDER if c in by_code]
    extra = [by_code[c] for c in sorted(by_code) if c not in STATE_ORDER]
    return known + extra


def assemble_entries(
    rebuilt: list[dict],
    base_manifest: dict | None = None,
    carry_forward_dir: Path | None = None,
    sidecar_dir: Path | None = None,
    log=print,
) -> list[dict]:
    """Merge rebuilt entries with everything already published.

    Priority for a state that was *not* rebuilt this run:
      1. its published pack in `carry_forward_dir` (truth: what is on the release).
         If the base manifest has an entry with the same sha256 it is reused
         verbatim, otherwise the entry is rebuilt from the pack header.
      2. its entry in `base_manifest` (the published manifest).
      3. a local `<key>_school_meta.json` sidecar in `sidecar_dir`.
    """
    base = manifest_states(base_manifest)
    merged: dict[str, dict] = {}
    rebuilt_codes = {e["code"].upper() for e in rebuilt}

    for code in list(STATE_ORDER) + sorted(c for c in base if c not in STATE_ORDER):
        if code in rebuilt_codes:
            continue
        key = BY_CODE.get(code, (None, None))[0]
        base_entry = base.get(code)

        gz = carry_forward_dir / STATES[key]["file"] if (carry_forward_dir and key) else None
        if gz is not None and gz.exists():
            from_pack = entry_from_pack(gz, key)
            if base_entry and base_entry.get("sha256") == from_pack["sha256"]:
                merged[code] = base_entry
                log(f"  {code}: kept published manifest entry (sha {from_pack['sha256'][:12]}… verified)")
            else:
                merged[code] = from_pack
                why = "missing from" if not base_entry else "sha differs from"
                log(f"  {code}: rebuilt entry from published pack ({why} base manifest)")
            continue
        if base_entry:
            merged[code] = base_entry
            log(f"  {code}: kept published manifest entry (pack not re-verified)")
            continue
        if sidecar_dir and key:
            sidecar = sidecar_dir / f"{key}_school_meta.json"
            if sidecar.exists():
                merged[code] = json.loads(sidecar.read_text(encoding="utf-8"))
                log(f"  {code}: from local sidecar {sidecar.name}")
                continue

    for entry in rebuilt:
        merged[entry["code"].upper()] = entry
        log(f"  {entry['code']}: rebuilt this run (sha {entry['sha256'][:12]}…)")

    return order_entries(merged)


def combined_manifest(entries: list[dict], built: str | None = None) -> dict:
    """Multi-state school-zones-manifest.json payload.

    Backward compatible: top-level region/sha256/bytes/packFile still describe
    Queensland so older Pace builds keep working. New clients read `states`.
    """
    entries = order_entries({e["code"].upper(): e for e in entries})
    by_code = {e["code"]: e for e in entries}
    qld = by_code.get("QLD") or (entries[0] if entries else None)
    payload: dict = {
        "schemaVersion": 2,
        "built": built or date.today().isoformat(),
        "licence": LICENCE,
        "attribution": ATTRIBUTION,
        "states": entries,
    }
    if qld is not None:
        payload.update(
            {
                "region": qld.get("region", ""),
                "osmTimestamp": qld.get("osmTimestamp", ""),
                "sha256": qld.get("sha256", ""),
                "bytes": qld.get("bytes", 0),
                "packFile": qld.get("packFile", ""),
                "source": qld.get("source", ""),
            }
        )
    return payload


def write_combined_manifest(entries: list[dict], out_path: Path, built: str | None = None) -> dict:
    payload = combined_manifest(entries, built)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    codes = ",".join(e["code"] for e in payload["states"])
    print(f"Wrote {out_path} ({len(payload['states'])} state(s): {codes})")
    return payload


def fingerprint(manifest: dict | None) -> str:
    """Stable identity of what clients download (ignores `built` dates)."""
    states = manifest_states(manifest)
    parts = [
        f"{code}:{states[code].get('sha256', '')}:{states[code].get('bytes', 0)}:{states[code].get('packFile', '')}"
        for code in sorted(states)
    ]
    return ";".join(parts)


def check(
    manifest: dict,
    required: list[str],
    dist: Path | None = None,
    release_assets: dict[str, int] | None = None,
) -> list[str]:
    """Return a list of problems (empty = OK).

    Every listed state must have a pack that will exist after publishing: either
    in `dist` (to be uploaded, sha and size must match) or already on the
    release with the same size.
    """
    problems: list[str] = []
    states = manifest_states(manifest)
    for code in required:
        if code.upper() not in states:
            problems.append(f"required state {code} missing from manifest")
    listed = [str(e.get("code", "")).upper() for e in manifest.get("states") or []]
    if "QLD" in listed and listed[0] != "QLD":
        problems.append(f"QLD must be the first states[] entry (got {listed})")
    if manifest.get("sha256") and states.get("QLD") and manifest["sha256"] != states["QLD"].get("sha256"):
        problems.append("legacy top-level sha256 does not match the QLD entry")
    for code, entry in states.items():
        pack = entry.get("packFile", "")
        if not pack or not entry.get("sha256"):
            problems.append(f"{code}: missing packFile or sha256")
            continue
        local = dist / pack if dist else None
        if local is not None and local.exists():
            if local.stat().st_size != entry.get("bytes"):
                problems.append(f"{code}: {pack} size {local.stat().st_size} != manifest {entry.get('bytes')}")
            if sha256_file(local) != entry["sha256"]:
                problems.append(f"{code}: {pack} sha256 does not match manifest")
        elif release_assets is not None:
            if pack not in release_assets:
                problems.append(f"{code}: {pack} is neither in dist nor on the release")
            elif release_assets[pack] != entry.get("bytes"):
                problems.append(
                    f"{code}: release asset {pack} is {release_assets[pack]} bytes, manifest says {entry.get('bytes')}"
                )
    return problems


def describe_diff(old: dict | None, new: dict | None) -> list[str]:
    a, b = manifest_states(old), manifest_states(new)
    lines: list[str] = []
    for code in order_entries({c: {"code": c} for c in set(a) | set(b)}):
        c = code["code"]
        if c not in a:
            e = b[c]
            lines.append(f"+ {c}: added   {e.get('packFile')} sha {e.get('sha256', '')[:12]}… {e.get('bytes')} B osm {e.get('osmTimestamp')}")
        elif c not in b:
            lines.append(f"- {c}: REMOVED (was {a[c].get('packFile')})")
        elif a[c].get("sha256") != b[c].get("sha256"):
            lines.append(
                f"~ {c}: updated {a[c].get('sha256', '')[:12]}… -> {b[c].get('sha256', '')[:12]}… osm {b[c].get('osmTimestamp')}"
            )
        else:
            lines.append(f"= {c}: unchanged sha {b[c].get('sha256', '')[:12]}…")
    return lines


def _load_assets(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assets = data.get("assets", data) if isinstance(data, dict) else data
    return {a["name"]: int(a["size"]) for a in assets}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd", required=True)
    fp = sub.add_parser("fingerprint")
    fp.add_argument("manifest")
    ck = sub.add_parser("check")
    ck.add_argument("manifest")
    ck.add_argument("--require", default=",".join(STATE_ORDER))
    ck.add_argument("--dist", default="")
    ck.add_argument("--release-assets", default="", help="JSON from `gh release view --json assets`")
    df = sub.add_parser("diff")
    df.add_argument("old")
    df.add_argument("new")
    args = parser.parse_args(argv)

    if args.cmd == "fingerprint":
        print(fingerprint(load_manifest(Path(args.manifest))))
        return 0
    if args.cmd == "diff":
        for line in describe_diff(load_manifest(Path(args.old)), load_manifest(Path(args.new))):
            print(line)
        return 0
    manifest = load_manifest(Path(args.manifest)) or {}
    required = [c.strip().upper() for c in args.require.split(",") if c.strip()]
    assets = _load_assets(Path(args.release_assets)) if args.release_assets else None
    problems = check(manifest, required, Path(args.dist) if args.dist else None, assets)
    listed = ",".join(e.get("code", "?") for e in manifest.get("states") or [])
    if problems:
        print(f"school manifest check FAILED (states: {listed}):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"school manifest check ok: states {listed} (required {','.join(required)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
