from pathlib import Path
import importlib.util
import json
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "middleware")]
import functions
from md_compat import is_regular_md
from profiles import MD, MD_EQUIPMENT, MD_IDEOLOGY
from profile_lore import make_lore
from unified_mod import BASES, DEPENDENCIES, ENTRY, configure_bases, characterization_source, render, select_unified
from tools.verify_md_source import verify

spec = importlib.util.spec_from_file_location("regular_md_bridge", ROOT / "Launcher-Voices.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class RegularMDTests(unittest.TestCase):
    def setUp(self):
        bases, dependencies = dict(BASES), dict(DEPENDENCIES)
        def restore():
            BASES.clear()
            BASES.update(bases)
            DEPENDENCIES.clear()
            DEPENDENCIES.update(dependencies)
            functions.configure_profile("vanilla")
        self.addCleanup(restore)

    def descriptor(self, root, entry, source, name="Millennium Dawn: A Modern Day Mod"):
        (root / "mod").mkdir(exist_ok=True)
        metadata = f'name="{name}"\nremote_file_id="2777392649"\nsupported_version="1.19.*"\n'
        (root / entry).write_text(metadata + f'path="{source.as_posix()}"\nreplace_path="events"\nreplace_path="common/ideas"\n')
        return metadata

    def test_workshop_local_and_renamed_descriptors_select_regular_md(self):
        for entry, name in (("mod/ugc_2777392649.mod", "Millennium Dawn: A Modern Day Mod"),
                            ("mod/MD_LOCAL_STANDARD.mod", "Millennium Dawn: Developer Version"),
                            ("mod/custom-regular.mod", "Millennium Dawn: Developer Version")):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "source"
                (source / "history/countries").mkdir(parents=True)
                metadata = self.descriptor(root, entry, source, name)
                (source / "descriptor.mod").write_text(metadata)
                enabled = [entry, ENTRY]
                configure_bases(root, enabled)
                self.assertEqual(select_unified(enabled), MD)
                self.assertEqual(bridge.select_profile(enabled), "config-unified-md.json")
                self.assertEqual(characterization_source(enabled, root, root), source.resolve())
                self.assertEqual(bridge.select_profile([entry, "mod/HOI4_AI_Voices_MD.mod"]), "config-md.json")
                self.assertEqual(characterization_source([entry, "mod/HOI4_AI_Voices_MD.mod"], root, root), source.resolve())
                for unified in (False, True):
                    self.assertIn(f'dependencies={{ "{name}" }}'.encode(), render(MD, unified=unified)["descriptor.mod"])

    def test_oem_has_a_distinct_profile_even_if_it_reuses_regular_workshop_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            entry = "mod/MD_OEM_LOCAL.mod"
            self.descriptor(root, entry, source, "Millennium Dawn OEM Local")
            self.assertFalse(is_regular_md(root / entry))
            configure_bases(root, [entry, ENTRY])
            self.assertEqual(select_unified([entry, ENTRY]), "millennium_dawn_oem")
            (root / "voices-profiles.json").write_text(json.dumps({entry: MD}))
            with self.assertRaises(RuntimeError):
                configure_bases(root, [entry, ENTRY])

    def test_regular_descriptor_cannot_point_characterization_at_oem_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            (source / "history/countries").mkdir(parents=True)
            entry = "mod/ugc_2777392649.mod"
            self.descriptor(root, entry, source)
            (source / "descriptor.mod").write_text('name="Millennium Dawn: OEM Edition"\nremote_file_id="2777392649"\n')
            with self.assertRaises(RuntimeError):
                characterization_source([entry, ENTRY], root, root)
            with self.assertRaises(RuntimeError):
                make_lore({"game_profile": MD, "characterization": {"source": str(source)}})

    def test_regular_profile_has_its_own_config_memory_and_native_export(self):
        self.assertEqual(MD, "millennium_dawn")
        for name in ("config-md.json", "config-unified-md.json"):
            cfg = json.loads((ROOT / "middleware" / name).read_text())
            self.assertEqual(cfg["game_profile"], MD)
            self.assertIn("ai_diplo_md.sqlite3", cfg["paths"]["memory_db"])
            self.assertNotIn("oem", json.dumps(cfg).lower())
        for unified in (False, True):
            files = render(MD, unified=unified)
            export = files["common/scripted_effects/ai_diplo_export.txt"].decode("utf-8-sig")
            self.assertIn("profile=millennium_dawn|", export)
            self.assertNotIn("millennium_dawn_oem", export)
            self.assertEqual(len(re.findall(r"has_government = \w+ \}\s+set_temp_variable = \{ aid_ideo = \d+", export)), 5)
        functions.configure_profile("millennium_dawn_oem")

    def source_fixture(self, root):
        for folder in ("common/ideologies", "common/units/equipment", "common/ai_templates", "localisation/english"):
            (root / folder).mkdir(parents=True)
        (root / "descriptor.mod").write_text('name="Millennium Dawn: Developer Version"\nremote_file_id="2777392649"\nsupported_version="1.19.*"\n')
        groups = ("fascism", "communism", "democratic", "neutrality", "nationalist")
        (root / "common/ideologies/00_ideologies.txt").write_text("ideologies = {\n" + "\n".join(f"\t{group} = {{ }}" for group in groups) + "\n}")
        (root / "localisation/english/parties_l_english.yml").write_text("l_english:\n" + "\n".join(f' {group}: "{MD_IDEOLOGY[str(i)]}"' for i, group in enumerate(groups, 1)))
        (root / "common/units/equipment/synthetic.txt").write_text("equipments = {\n" + "\n".join(f"\t{identifier} = {{ is_archetype = yes }}" for identifier in sorted(MD_EQUIPMENT)) + "\n}")
        (root / "common/ai_templates/MD_generic.txt").write_text("\n".join(f"template_{role} = {{ role = {role} }}" for role in ("infantry", "armor", "ifv_mechanized")))

    def test_source_audit_verifies_definition_types_and_rejects_missing_identifiers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.source_fixture(root)
            report = verify(root)
            self.assertEqual(set(report["equipment"]), MD_EQUIPMENT)
            self.assertEqual(report["priority_roles"], ["armor", "ifv_mechanized", "infantry"])
            equipment = root / "common/units/equipment/synthetic.txt"
            equipment.write_text(equipment.read_text().replace("is_archetype = yes", "is_archetype = no", 1))
            with self.assertRaisesRegex(ValueError, "not an archetype"):
                verify(root)

    def test_committed_regular_source_evidence_matches_adapter_identifiers(self):
        evidence = json.loads((ROOT / "tests/fixtures/regular-md-identifiers.json").read_text())
        for key in ("regular_md_local", "regular_md_upstream"):
            record = evidence[key]
            self.assertEqual(record["repository"], "MillenniumDawn/Millennium-Dawn")
            self.assertEqual(record["workshop_id"], "2777392649")
            self.assertEqual(set(record["equipment"]), MD_EQUIPMENT)
            self.assertEqual(record["priority_roles"], ["armor", "ifv_mechanized", "infantry"])
            self.assertEqual(len(record["outlooks"]), 5)
            self.assertRegex(record["verified_commit"], r"^[a-f0-9]{40}$")

    def test_two_regular_md_installations_are_not_a_valid_combined_playset(self):
        with self.assertRaises(RuntimeError):
            select_unified(["mod/MD_LOCAL_STANDARD.mod", "mod/ugc_2777392649.mod", ENTRY])


if __name__ == "__main__":
    unittest.main()
