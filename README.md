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
