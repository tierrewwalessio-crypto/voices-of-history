from pathlib import Path
from unittest.mock import patch
import importlib.util
import io
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "middleware")]
from radio_mods import gameplay_mods, is_radio_mod
from unified_mod import ENTRY, select_unified


class RadioModTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.userdir = Path(self.temp.name)
        self.source = self.userdir / "station"
        self.entry = "mod/radio.mod"
        for folder in ("mod", "station/music", "station/interface", "station/gfx", "station/localisation/english"):
            (self.userdir / folder).mkdir(parents=True, exist_ok=True)
        metadata = 'name="Station"\ntags={ "Sound" }\n'
        self.external = self.userdir / self.entry
        self.external.write_text(metadata + 'path="station"\n')
        (self.source / "descriptor.mod").write_text(metadata)
        (self.source / "music/station.txt").write_text('music_station="station"\nmusic={ song="Track-1" }\n')
        (self.source / "music/station.asset").write_text('music={\n name="Track-1"\n file="track.ogg"\n}\n')
        (self.source / "music/track.ogg").write_bytes(b"audio fixture")
        (self.source / "interface/station.gui").write_text('guiTypes={ containerWindowType={ name="station_faceplate" } containerWindowType={ name="station_stations_entry" } }')
        (self.source / "interface/station.gfx").write_text('spriteTypes={ spriteType={ name="GFX_radio_station_cover_station" textureFile="gfx/radio_station_cover_station.dds" } }')
        (self.source / "gfx/radio_station_cover_station.dds").write_bytes(b"image fixture")
        (self.source / "localisation/english/music_station_station_l_english.yml").write_text('l_english:\n station_TITLE:0 "Station"\n track-1:0 "Track"\n')

    def test_radio_assets_are_ignored_for_selection_but_original_order_is_preserved(self):
        self.external.write_text(self.external.read_text().replace('"Sound"', '"Music"'))
        mods = [self.entry, "mod/ugc_2777392649.mod", ENTRY]
        before = list(mods)
        selected = gameplay_mods(self.userdir, mods)
        self.assertEqual(selected, ["mod/ugc_2777392649.mod", ENTRY])
        self.assertEqual(select_unified(selected), "millennium_dawn")
        self.assertEqual(mods, before)
        self.assertTrue(is_radio_mod(self.userdir, self.entry))

    def test_sound_tag_and_radio_name_do_not_hide_gameplay_files(self):
        for relative in ("common/on_actions/hook.txt", "events/event.txt", "history/countries/USA.txt", "interface/countrydiplomacyview.gui", "gfx/leader.dds"):
            with self.subTest(relative=relative):
                path = self.source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
                self.assertFalse(is_radio_mod(self.userdir, self.entry))
                self.assertIn(self.entry, gameplay_mods(self.userdir, [self.entry, ENTRY]))
                path.unlink()

    def test_station_filenames_cannot_hide_other_windows_sprites_or_localisation(self):
        cases = (("interface/station.gui", 'guiTypes={ containerWindowType={ name="countrydiplomacyview" } }'),
                 ("interface/station.gfx", 'spriteTypes={ spriteType={ name="GFX_ai_diplo" } }'),
                 ("localisation/english/music_station_station_l_english.yml", 'l_english:\n AI_DIPLO_TITLE:0 "Overridden"\n'))
        for relative, replacement in cases:
            with self.subTest(relative=relative):
                path = self.source / relative
                old = path.read_bytes()
                path.write_text(replacement)
                self.assertFalse(is_radio_mod(self.userdir, self.entry))
                path.write_bytes(old)

    def test_replace_paths_archives_and_dependencies_never_qualify(self):
        for path in (self.external, self.source / "descriptor.mod"):
            old = path.read_bytes()
            for extra in ('replace_path="events"\n', 'archive="mod.zip"\n', 'dependencies={ "An overhaul" }\n'):
                with self.subTest(path=path.name, extra=extra):
                    path.write_bytes(old + extra.encode())
                    self.assertFalse(is_radio_mod(self.userdir, self.entry))
            path.write_bytes(old)

    def test_unknown_missing_or_unlabelled_mods_remain_subject_to_validation(self):
        for entry in ("mod/missing.mod", "../radio.mod", "mod/sub/radio.mod"):
            self.assertFalse(is_radio_mod(self.userdir, entry))
        self.external.write_text(self.external.read_text().replace('tags={ "Sound" }', 'tags={ "Gameplay" }'))
        self.assertFalse(is_radio_mod(self.userdir, self.entry))
        with self.assertRaises(RuntimeError):
            select_unified(gameplay_mods(self.userdir, [self.entry, ENTRY]))

    def test_duplicates_mixed_overhauls_old_voices_and_load_order_still_fail(self):
        for mods in ([self.entry, self.entry.upper(), ENTRY],
                     [self.entry, "mod/ugc_2777392649.mod", "mod/ugc_3350890356.mod", ENTRY],
                     [self.entry, "mod/ugc_2777392649.mod", "mod/HOI4_AI_Voices_ChatGPT.mod", ENTRY],
                     [self.entry, "mod/ugc_2777392649.mod", "mod/ugc_3754450495.mod", ENTRY],
                     [self.entry, ENTRY, "mod/ugc_2777392649.mod"]):
            with self.subTest(mods=mods), self.assertRaises(RuntimeError):
                select_unified(gameplay_mods(self.userdir, mods))

    def test_bridge_check_filters_radios_without_launching_or_writing_playset(self):
        spec = importlib.util.spec_from_file_location("radio_bridge", ROOT / "Launcher-Voices.py")
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        enabled = [self.entry, ENTRY]
        playset = self.userdir / "dlc_load.json"
        playset.write_text(json.dumps({"enabled_mods": enabled}))
        before = playset.read_bytes()
        cfg = {"game_profile": "vanilla", "injection": {"dry_run": True}}
        middleware = self.userdir / "middleware"
        middleware.mkdir()
        (middleware / "config-unified-vanilla.json").write_text(json.dumps(cfg))
        (self.userdir / "hoi4.exe").write_bytes(b"never executed")
        (self.userdir / "history/countries").mkdir(parents=True)
        (self.userdir / "launcher-bridge.json").write_text(json.dumps({"game_directory": str(self.userdir), "exePath": "hoi4.exe"}))
        output = io.StringIO()
        with patch.object(bridge, "ROOT", self.userdir), patch.object(bridge, "USERDIR", self.userdir), \
             patch.object(bridge, "DATA", self.userdir / "data"), patch("sys.stdout", output), \
             patch.object(bridge.subprocess, "Popen", side_effect=AssertionError("No launch permitted")), \
             patch.object(bridge, "install_or_compile", side_effect=AssertionError("No installation permitted")):
            self.assertEqual(bridge.main(["--check"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["ignored_radio_mods"], [self.entry])
        self.assertFalse(report["game_launch_requested"])
        self.assertEqual(playset.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
