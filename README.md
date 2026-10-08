# Pace road packs

Prebuilt Australia OSM speed-limit packs for the [Pace](https://github.com/12-hak/pace) driving companion.

Pace apps poll the latest release manifest on start:

`https://github.com/12-hak/pace-road-packs/releases/latest/download/pack-manifest.json`

If the remote pack is newer (by `osmTimestamp` / `sha256`), the app downloads `road_pack.csv.gz` over unmetered networks and activates it.

## Licence / attribution

Road data © OpenStreetMap contributors.

Licensed under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/).

See also: [OpenStreetMap copyright](https://www.openstreetmap.org/copyright).

## How packs are built

1. Download the Geofabrik Australia extract (`australia-latest.osm.pbf`).
2. Keep drivable ways that have an explicit numeric `maxspeed` (km/h).
3. Split ways into short segments and write `road_pack.csv.gz`.
4. Emit `pack-manifest.json` with `region`, `osmTimestamp`, `built`, `sha256`, `bytes`, and ODbL fields.

The builder lives in this repo (`tools/build_road_pack.py`) so the data repository is self-contained. Large PBF/CSV artifacts are **not** committed to git; only GitHub Releases carry the pack files.

### Local build

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 tools/build_road_pack.py --out-dir dist
```

Outputs:

- `dist/road_pack.csv.gz`
- `dist/pack-manifest.json`

## Automatic publishing

GitHub Actions workflow [`.github/workflows/publish-australia-pack.yml`](.github/workflows/publish-australia-pack.yml):

| Trigger | When |
| --- | --- |
| `schedule` | Mondays at **06:00 UTC** (`0 6 * * 1`) |
| `workflow_dispatch` | Manual run from the Actions tab |

Each run:

1. Checks free disk (Australia PBF is ~1 GB; the job fails clearly if space is tight).
2. Reads Geofabrik `state.txt` and compares `osmTimestamp` to the current [latest release](https://github.com/12-hak/pace-road-packs/releases/latest) `pack-manifest.json`.
3. If unchanged, exits successfully with a **no update** message (no empty release).
4. If newer, builds the pack, then creates a dated release tag `YYYY-MM-DD` (or `YYYY-MM-DD-HHMM` on collision), titled like `Australia OSM pack YYYY-MM-DD`, uploads `pack-manifest.json` and `road_pack.csv.gz`, and marks it latest.

### Manual run (`workflow_dispatch`)

1. Open **Actions** → **Publish Australia OSM pack**.
2. Click **Run workflow** on `main`.
3. Wait for the job (download + build can take a long time).

Uses the default `GITHUB_TOKEN` with `contents: write` to publish assets on this same repository.

## School-zone overlays (QLD / NSW / VIC)

Separate OSM-derived school-zone packs live on the rolling [`packs`](https://github.com/12-hak/pace-road-packs/releases/tag/packs) release:

- Manifest: `https://github.com/12-hak/pace-road-packs/releases/download/packs/school-zones-manifest.json`
- Files: `qld_school_zones.csv.gz`, `nsw_school_zones.csv.gz`, `vic_school_zones.csv.gz`

The Pace app keeps the Queensland pack bundled for offline first-run, and downloads NSW/VIC on demand when GPS enters that state (prefer unmetered, same as the road pack).

### Builder

```bash
python3 tools/build_school_zones.py --state nsw --out-dir dist
python3 tools/build_school_zones.py --state vic --out-dir dist
python3 tools/build_school_zones.py --all --out-dir dist   # QLD+NSW+VIC
```

PBF sources (openstreetmap.fr extracts; underscores in filenames):

| State | URL |
| --- | --- |
| QLD | `…/oceania/australia/queensland.osm.pbf` |
| NSW | `…/oceania/australia/new_south_wales.osm.pbf` |
| VIC | `…/oceania/australia/victoria.osm.pbf` |

### Manifest schema (v2)

Top-level `region` / `sha256` / `bytes` / `packFile` still describe **Queensland** so older Pace builds keep working. Newer clients read the `states` array:

```json
{
  "schemaVersion": 2,
  "states": [
    { "code": "QLD", "region": "Queensland", "packFile": "qld_school_zones.csv.gz", "sha256": "…", "bytes": 0, "segments": 0 },
    { "code": "NSW", "…": "…" },
    { "code": "VIC", "…": "…" }
  ]
}
```

### Coverage honesty (OSM tagging)

School zones are extracted only where OSM tags indicate a school zone (`hazard=school_zone`, `maxspeed:conditional` with school hours, `source:maxspeed` / `traffic_sign` mentioning school, etc.). **This is not a complete ground-truth inventory of every signed school zone.**

Approximate segment counts from the current build (varies with OSM freshness):

| State | Ways (approx) | Segments (approx) | Notes |
| --- | --- | --- | --- |
| QLD | — | ~4.1k | Bundled in Pace APK; also on `packs` |
| NSW | ~5.9k | ~10.6k | Relatively well tagged |
| VIC | ~0.9k | ~1.7k | **Thin** — many VIC school zones lack OSM school tagging |

Default active windows when OSM has no usable times: QLD `0700-0900|1400-1600`; NSW/VIC `0800-0930|1430-1600` (local school days). Prefer OSM `maxspeed:conditional` when present.

### Manifest is merged, never overwritten

The weekly job rebuilds **QLD only**. It then merges the new QLD entry into the
published manifest instead of replacing it, so NSW/VIC stay listed:

1. CI downloads the current `school-zones-manifest.json` and every
   `*_school_zones.csv.gz` on the `packs` release.
2. `build_qld_school_zones.py --base-manifest … --carry-forward-dir …` keeps each
   state that was not rebuilt. If its published pack's sha256 matches the old entry,
   the entry is kept verbatim; otherwise the entry is rebuilt from the pack header.
3. `tools/school_manifest.py check` fails the job if QLD, NSW or VIC is missing, if
   QLD is not the first `states[]` entry (older app parsers read the first match), or if
   a listed pack is missing or its size/sha differs.
4. The manifest is republished when any state's sha/bytes/packFile changes (not just
   QLD's). Packs upload before manifests.

`workflow_dispatch` has a **dry_run** input: it builds and checks everything, uploads
the manifests as a workflow artifact, and skips publishing.

Tests: `python -m unittest discover -s tests -v` (stdlib only; also run on PRs).

### Publish to `packs` (manual)

Always merge with what is published so other states are not dropped:

```bash
mkdir -p prev && gh release download packs -R 12-hak/pace-road-packs \
  -p 'school-zones-manifest.json' -p '*_school_zones.csv.gz' -D prev --clobber
python3 tools/build_school_zones.py --state nsw --out-dir dist \
  --base-manifest prev/school-zones-manifest.json --carry-forward-dir prev
gh release view packs -R 12-hak/pace-road-packs --json assets > prev/assets.json
python3 tools/school_manifest.py check dist/school-zones-manifest.json \
  --dist dist --release-assets prev/assets.json
gh release upload packs dist/nsw_school_zones.csv.gz --clobber --repo 12-hak/pace-road-packs
gh release upload packs dist/school-zones-manifest.json --clobber --repo 12-hak/pace-road-packs
```

Do not commit large PBF/CSV artifacts; Releases only.
