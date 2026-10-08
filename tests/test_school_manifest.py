"""Regression tests for the school-zones manifest merge (stdlib only).

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import school_manifest as sm  # noqa: E402


def write_pack(path: Path, key: str, osm_date: str, built: str, salt: str = "") -> Path:
    """A tiny school pack with the same header layout as build_school_zones.write_overlay."""
    meta = sm.STATES[key]
    header = [
        f"# region={meta['region']}",
        f"# state={meta['code']}",
        f"# built={built}",
        f"# osm_date={osm_date}",
        f"# source={meta['url']}",
        "# licence=ODbL 1.0 https://www.openstreetmap.org/copyright",
        "# schema=road_name,school_limit_kmh,lat1,lon1,lat2,lon2,windows",
        f"# times={meta['timezone']} school days; windows are local HHMM-HHMM",
        f"# default_windows={meta['windows']}",
        "# Road data © OpenStreetMap contributors",
        "# ways=2",
        "# segments=3",
    ]
    rows = [f"Road {salt}{i},40,-27.0,153.0,-27.1,153.1,{meta['windows']}" for i in range(3)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename=path.name, mode="wb", fileobj=raw, mtime=0) as gz:
            gz.write(("\n".join(header + rows) + "\n").encode("utf-8"))
    return path


# --- Python port of the Pace app's parser (app/.../domain/SchoolZonesManifest.kt) ---
def app_parse(json_text: str) -> dict:
    def field(text: str, name: str) -> str:
        m = re.search(rf'"{name}"\s*:\s*"([^"]*)"', text)
        return m.group(1) if m else ""

    states = []
    m = re.search(r'"states"\s*:\s*\[(.*?)]\s*[,}]', json_text, re.DOTALL)
    if m:
        for obj in re.findall(r"\{[^{}]*}", m.group(1)):
            code, sha, pack = field(obj, "code"), field(obj, "sha256"), field(obj, "packFile")
            if code and (sha or pack):
                states.append({"code": code, "sha256": sha, "packFile": pack})
    return {"states": states, "sha256": field(json_text, "sha256"), "packFile": field(json_text, "packFile")}


class MergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.prev = self.tmp / "prev"
        self.dist = self.tmp / "dist"
        self.nsw = write_pack(self.prev / "nsw_school_zones.csv.gz", "nsw", "2026-09-15T00:24:38Z", "2026-09-15")
        self.vic = write_pack(self.prev / "vic_school_zones.csv.gz", "vic", "2026-09-15T00:24:38Z", "2026-09-15")
        old_qld = write_pack(self.prev / "qld_school_zones.csv.gz", "qld", "2026-10-05T00:21:21Z", "2026-10-05")
        self.old_qld_entry = sm.entry_from_pack(old_qld, "qld")
        new_qld = write_pack(self.dist / "qld_school_zones.csv.gz", "qld", "2026-10-12T00:20:00Z", "2026-10-12", salt="n")
        self.new_qld_entry = sm.entry_from_pack(new_qld, "qld")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _codes(self, entries):
        return [e["code"] for e in entries]

    def test_bug_repro_qld_only_base_is_repaired_from_published_packs(self):
        """The live 2026-10-05 manifest lists only QLD; NSW/VIC packs are still on the release."""
        base = sm.combined_manifest([self.old_qld_entry], built="2026-10-05")
        entries = sm.assemble_entries([self.new_qld_entry], base, self.prev, log=lambda *_: None)
        self.assertEqual(self._codes(entries), ["QLD", "NSW", "VIC"])
        by = {e["code"]: e for e in entries}
        self.assertEqual(by["QLD"]["sha256"], self.new_qld_entry["sha256"])
        self.assertEqual(by["NSW"]["sha256"], sm.sha256_file(self.nsw))
        self.assertEqual(by["NSW"]["bytes"], self.nsw.stat().st_size)
        self.assertEqual(by["NSW"]["osmTimestamp"], "2026-09-15T00:24:38Z")
        self.assertEqual(by["VIC"]["timezone"], "Australia/Melbourne")
        self.assertEqual(by["VIC"]["segments"], 3)

    def test_old_behaviour_would_have_dropped_nsw_vic(self):
        """Without base/carry-forward (what CI did before), only the rebuilt state survives."""
        entries = sm.assemble_entries([self.new_qld_entry], None, None, log=lambda *_: None)
        self.assertEqual(self._codes(entries), ["QLD"])

    def test_existing_entries_kept_verbatim_when_pack_unchanged(self):
        nsw_entry = sm.entry_from_pack(self.nsw, "nsw") | {"ways": 5901, "segments": 10637}
        vic_entry = sm.entry_from_pack(self.vic, "vic") | {"ways": 928, "segments": 1714}
        base = sm.combined_manifest([self.old_qld_entry, nsw_entry, vic_entry], built="2026-09-15")
        entries = sm.assemble_entries([self.new_qld_entry], base, self.prev, log=lambda *_: None)
        by = {e["code"]: e for e in entries}
        self.assertEqual(by["NSW"], nsw_entry)
        self.assertEqual(by["VIC"], vic_entry)
        self.assertEqual(by["QLD"], self.new_qld_entry)

    def test_base_entry_kept_when_pack_not_downloaded(self):
        nsw_entry = sm.entry_from_pack(self.nsw, "nsw")
        base = sm.combined_manifest([self.old_qld_entry, nsw_entry])
        entries = sm.assemble_entries([self.new_qld_entry], base, self.tmp / "empty", log=lambda *_: None)
        self.assertEqual(self._codes(entries), ["QLD", "NSW"])

    def test_stale_base_entry_replaced_when_published_pack_differs(self):
        stale = sm.entry_from_pack(self.nsw, "nsw") | {"sha256": "0" * 64}
        base = sm.combined_manifest([self.old_qld_entry, stale])
        entries = sm.assemble_entries([self.new_qld_entry], base, self.prev, log=lambda *_: None)
        self.assertEqual({e["code"]: e for e in entries}["NSW"]["sha256"], sm.sha256_file(self.nsw))

    def test_rebuilding_nsw_only_keeps_qld_first(self):
        base = sm.combined_manifest([self.old_qld_entry])
        new_nsw = sm.entry_from_pack(write_pack(self.dist / "nsw_school_zones.csv.gz", "nsw", "x", "y", "z"), "nsw")
        entries = sm.assemble_entries([new_nsw], base, self.prev, log=lambda *_: None)
        self.assertEqual(self._codes(entries), ["QLD", "NSW", "VIC"])
        self.assertEqual(entries[1]["sha256"], new_nsw["sha256"])

    def test_schema_v1_base_manifest_maps_to_qld(self):
        v1 = {"region": "Queensland", "sha256": "ab" * 32, "bytes": 1, "packFile": "qld_school_zones.csv.gz"}
        self.assertEqual(list(sm.manifest_states(v1)), ["QLD"])

    def test_payload_parses_in_app_and_legacy_fields_are_qld(self):
        entries = sm.assemble_entries([self.new_qld_entry], sm.combined_manifest([self.old_qld_entry]), self.prev,
                                      log=lambda *_: None)
        payload = sm.combined_manifest(entries)
        text = json.dumps(payload, indent=2) + "\n"
        parsed = app_parse(text)
        self.assertEqual([s["code"] for s in parsed["states"]], ["QLD", "NSW", "VIC"])
        self.assertEqual(parsed["sha256"], self.new_qld_entry["sha256"])  # first-match == QLD
        self.assertEqual(parsed["packFile"], "qld_school_zones.csv.gz")
        self.assertEqual(payload["sha256"], self.new_qld_entry["sha256"])

    def test_check_and_fingerprint(self):
        entries = sm.assemble_entries([self.new_qld_entry], None, self.prev, log=lambda *_: None)
        payload = sm.combined_manifest(entries)
        assets = {"nsw_school_zones.csv.gz": self.nsw.stat().st_size, "vic_school_zones.csv.gz": self.vic.stat().st_size}
        self.assertEqual(sm.check(payload, ["QLD", "NSW", "VIC"], self.dist, assets), [])
        qld_only = sm.combined_manifest([self.new_qld_entry])
        problems = sm.check(qld_only, ["QLD", "NSW", "VIC"], self.dist, assets)
        self.assertIn("required state NSW missing from manifest", problems)
        self.assertTrue(any("VIC" in p for p in problems))
        # Wrong size on release is caught.
        bad = dict(assets, **{"vic_school_zones.csv.gz": 1})
        self.assertTrue(any("release asset vic" in p for p in sm.check(payload, ["VIC"], self.dist, bad)))
        # Fingerprint ignores `built` but sees state changes.
        self.assertEqual(sm.fingerprint(payload), sm.fingerprint(sm.combined_manifest(entries, built="1999-01-01")))
        self.assertNotEqual(sm.fingerprint(payload), sm.fingerprint(qld_only))

    def test_diff_lines(self):
        old = sm.combined_manifest([self.old_qld_entry])
        new = sm.combined_manifest(sm.assemble_entries([self.new_qld_entry], old, self.prev, log=lambda *_: None))
        lines = sm.describe_diff(old, new)
        self.assertTrue(lines[0].startswith("~ QLD"))
        self.assertTrue(lines[1].startswith("+ NSW"))
        self.assertTrue(lines[2].startswith("+ VIC"))


if __name__ == "__main__":
    unittest.main()
