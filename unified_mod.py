"""Compile one owned Voices installation for the overhaul selected in Paradox."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent
NAME = "Voices of History - Unified ChatGPT"
ENTRY = "mod/HOI4_AI_Voices_Universal.mod"
FOLDER = "HOI4_AI_Voices_Universal"
OWNER = "voices-unified-2026-10"
BASES = {"mod/MD_LOCAL_STANDARD.mod": "millennium_dawn",
         "mod/ugc_2777392649.mod": "millennium_dawn",
         "mod/Cold_War_Iron_Curtain.mod": "cwic_dev",
         "mod/ugc_3350890356.mod": "the_fire_rises"}
CONFIGS = {"vanilla": "config-unified-vanilla.json",
           "millennium_dawn_oem": "config-unified-md-oem.json",
           "millennium_dawn": "config-unified-md.json",
           "cwic_dev": "config-unified-cwic-dev.json",
           "the_fire_rises": "config-unified-tfr.json"}
DEPENDENCIES = {"millennium_dawn": "Millennium Dawn: A Modern Day Mod",
                "millennium_dawn_oem": "Millennium Dawn OEM Local",
                "cwic_dev": "Cold War Iron Curtain Dev Build", "the_fire_rises": "The Fire Rises"}


def select_unified(enabled):
    if ENTRY not in enabled:
        return None
    if len(enabled) != len(set(enabled)):
        raise RuntimeError("The active playset contains duplicate mod entries.")
    rest = set(enabled) - {ENTRY}
    if not rest:
        return "vanilla"
    if len(rest) == 1 and next(iter(rest)) in BASES:
        if enabled[-1] != ENTRY:
            raise RuntimeError("Place Unified Voices after the overhaul in the active playset.")
        return BASES[next(iter(rest))]
    raise RuntimeError("Use Unified Voices alone for vanilla, or after exactly one supported overhaul. Remove older Voices test entries from this playset.")


def characterization_source(enabled, userdir, game_dir):
    """Bind references to the same selected source the game will load, never stale config."""
    profile = select_unified(enabled)
    if not profile:
        md_bases = [entry for entry in enabled if BASES.get(entry) == "millennium_dawn"]
        if "mod/HOI4_AI_Voices_MD.mod" in enabled and len(enabled) == len(set(enabled)) == 2 and len(md_bases) == 1:
            profile = "millennium_dawn"
        else:
            return None
    if profile == "vanilla":
        source = Path(game_dir).resolve()
    else:
        base = next(entry for entry in enabled if entry in BASES)
        descriptor = Path(userdir) / base
        text = descriptor.read_text(encoding="utf-8-sig")
        paths = re.findall(r'(?m)^\s*path\s*=\s*"([^"\r\n]+)"\s*(?:#.*)?$', text)
        if len(paths) != 1:
            raise RuntimeError("The selected overhaul descriptor needs exactly one source path: " + base)
        source = Path(paths[0])
        if not source.is_absolute():
            source = Path(userdir) / source
        source = source.resolve()
    if profile == "millennium_dawn":
        from middleware.md_compat import require_regular_md
        require_regular_md(descriptor)
        require_regular_md(source / "descriptor.mod")
    if profile == "millennium_dawn_oem":
        from middleware.md_compat import require_oem
        require_oem(descriptor)
        require_oem(source / "descriptor.mod")
    if not (source / "history/countries").is_dir():
        raise RuntimeError("The selected game's country source is missing: " + str(source))
    return source


def digest(data):
    return hashlib.sha256(data).hexdigest()


def descriptor_tokens(data):
    """Ignore Paradox's whitespace/BOM/comments while preserving every quoted value."""
    source = data.decode("utf-8-sig")
    pattern = re.compile(r'\s+|\#[^\r\n]*|"(?:\\.|[^"\\])*"|[{}=]|[^\s"{}=#]+')
    tokens, position = [], 0
    for match in pattern.finditer(source):
        if match.start() != position:
            raise ValueError("Unrecognized descriptor syntax.")
        token = match.group()
        if not token.isspace() and not token.startswith("#"):
            tokens.append(token)
        position = match.end()
    if position != len(source):
        raise ValueError("Unrecognized descriptor syntax.")
    return tokens


def descriptor_fields(data):
    """Compare unique top-level fields independently of the launcher's field order."""
    tokens = descriptor_tokens(data)
    fields, position = {}, 0
    while position < len(tokens):
        key = tokens[position]
        if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or key in fields or position + 2 >= len(tokens)
                or tokens[position + 1] != "="):
            raise ValueError("Invalid or duplicate descriptor field.")
        position += 2
        start = position
        if tokens[position] == "{":
            depth = 1
            position += 1
            while position < len(tokens) and depth:
                if tokens[position] == "{":
                    depth += 1
                elif tokens[position] == "}":
                    depth -= 1
                position += 1
            if depth:
                raise ValueError("Unclosed descriptor block.")
        elif tokens[position] in ("}", "="):
            raise ValueError("Invalid descriptor value.")
        else:
            position += 1
        # Keep every value and each block's internal order intact.
        fields[key] = tuple(tokens[start:position])
    return fields


def descriptor_matches(actual, expected):
    try:
        return descriptor_fields(actual) == descriptor_fields(expected)
    except (UnicodeError, ValueError):
        return False


def render(profile, unified=True, telemetry=False):
    import sys
    sys.path.insert(0, str(ROOT / "middleware"))
    from profiles import CWIC_GROUPS, TFR_GROUPS
    groups = {"vanilla": ("fascism", "communism", "democratic", "neutrality"),
              "millennium_dawn_oem": ("fascism", "communism", "democratic", "neutrality", "nationalist"),
              "millennium_dawn": ("fascism", "communism", "democratic", "neutrality", "nationalist"),
              "cwic_dev": tuple(k for k, _ in CWIC_GROUPS),
              "the_fire_rises": tuple(k for k, _ in TFR_GROUPS)}[profile]
    if not unified:
        files = {p.relative_to(ROOT / "mod/HOI4_AI_Diplomacy").as_posix(): p.read_bytes()
                 for p in (ROOT / "mod/HOI4_AI_Diplomacy").rglob("*") if p.is_file()}
        # All extensions share the same authored fixes; retain profile differences only.
        shared = ROOT / "native/shared"
        files.update({p.relative_to(shared).as_posix(): p.read_bytes()
                      for p in shared.rglob("*") if p.is_file()})
        overlay = ROOT / "native/legacy" / profile
        if not overlay.is_dir():
            raise ValueError("No legacy adapter for profile: " + profile)
        files.update({p.relative_to(overlay).as_posix(): p.read_bytes()
                      for p in overlay.rglob("*") if p.is_file()})
        if profile == "millennium_dawn":
            files["descriptor.mod"] += f'dependencies={{ "{DEPENDENCIES[profile]}" }}\n'.encode()
        if telemetry:
            from middleware.telemetry import attach
            attach(files, profile)
        return files
    template = ROOT / "native/shared"
    if not template.is_dir():
        raise RuntimeError("The unified native source is missing.")
    baseline = ROOT / "mod/HOI4_AI_Diplomacy"
    files = {p.relative_to(baseline).as_posix(): p.read_bytes()
             for p in baseline.rglob("*") if p.is_file()}
    files.update({p.relative_to(template).as_posix(): p.read_bytes()
                  for p in template.rglob("*") if p.is_file()})
    key = "common/scripted_effects/ai_diplo_export.txt"
    text = files[key].decode("utf-8-sig")
    for variable in ("aid_ideo", "n_ideo"):
        pattern = (r"set_temp_variable = \{ " + variable + r" = 0 \}\s*"
                   r"(?:if = \{ limit = \{ has_government = \w+ \} set_temp_variable = \{ "
                   + variable + r" = \d+ \} \}\s*)+")
        block = f"set_temp_variable = {{ {variable} = 0 }}\n"
        block += "".join(f"\tif = {{ limit = {{ has_government = {group} }} set_temp_variable = {{ {variable} = {i} }} }}\n"
                         for i, group in enumerate(groups, 1)) + "\n\t"
        text, count = re.subn(pattern, lambda _: block, text)
        if count != 1:
            raise RuntimeError("Political export template changed: " + variable)
    text = text.replace("profile=the_fire_rises", "profile=" + profile)
    files[key] = text.encode("utf-8")
    on_actions = "common/on_actions/zzz_ai_diplo_on_actions.txt"
    text = files[on_actions].decode("utf-8-sig").replace("\r\n", "\n")
    end_daily = "\t\t\t}\n\t\t}\n\t}\n\n\t# Quando"
    if text.count(end_daily) != 1:
        raise RuntimeError("Daily export template changed.")
    text = text.replace(end_daily, '\t\t\t\tlog = "VOICES_TICK|gid=[?aidiplo_gameid|0]|day=[?aidiplo_day|0]"\n' + end_daily)
    files[on_actions] = text.encode("utf-8")
    if profile != "the_fire_rises":
        gui = (ROOT / "native/variants/standard-diplomacy.gui").read_bytes()
        files["interface/ai_diplo.gui"] = gui
    files["descriptor.mod"] = (f'version="0.2.0-local"\nname="{NAME}"\n'
                               'tags={ "Gameplay" "Events" }\nsupported_version="1.19.*"\n').encode()
    if profile in DEPENDENCIES:
        # Overhauls replace common script directories; explicitly load this add-on after its base.
        files["descriptor.mod"] += f'dependencies={{ "{DEPENDENCIES[profile]}" }}\n'.encode()
    files["common/opinion_modifiers/voices_unified.txt"] = b'''opinion_modifiers = {
 voices_dialogue_goodwill = { value = 10 days = 30 }
 voices_dialogue_distrust = { value = -10 days = 30 }
}
'''
    files["common/ai_strategy/voices_priorities.txt"] = b'''# Bounded additions to normal AI role weights, activated by an expiring flag.
voices_infantry_priority = {
 enable = { is_ai = yes has_country_flag = voices_priority_infantry }
 abort_when_not_enabled = yes
 ai_strategy = { type = role_ratio id = infantry value = 10 }
}
voices_mobile_priority = {
 enable = { is_ai = yes has_country_flag = voices_priority_mobile }
 abort_when_not_enabled = yes
 ai_strategy = { type = role_ratio id = armor value = 5 }
 ai_strategy = { type = role_ratio id = motorized value = 5 }
}
'''
    if profile in ("millennium_dawn", "millennium_dawn_oem"):
        # MD's verified mobile template role is ifv_mechanized, not motorized.
        priority_file = "common/ai_strategy/voices_priorities.txt"
        files[priority_file] = files[priority_file].replace(b"id = motorized", b"id = ifv_mechanized")
    elif profile == "the_fire_rises":
        # TFR's motorized strategy exists upstream, but its actual mobile templates use mechanized.
        priority_file = "common/ai_strategy/voices_priorities.txt"
        files[priority_file] = files[priority_file].replace(b"id = motorized", b"id = mechanized")
    if telemetry:
        from middleware.telemetry import attach
        attach(files, profile)
    return files


def install_or_compile(userdir, profile, initial=False, legacy=False, telemetry=False):
    """Only replace our unchanged generated files. Never modify a playset or base mod."""
    userdir = Path(userdir).resolve()
    mods = userdir / "mod"
    folder = ({"millennium_dawn": "HOI4_AI_Voices_MD",
               "cwic_dev": "HOI4_AI_Voices_CWIC_Dev",
               "the_fire_rises": "HOI4_AI_Voices_TFR"}[profile] if legacy else FOLDER)
    target = (mods / folder).resolve()
    if target.parent != mods.resolve():
        raise RuntimeError("Unexpected mod destination.")
    descriptor = mods / (folder + ".mod")
    manifest_path = target / ".voices-generated.json"
    if initial:
        if target.exists() or descriptor.exists():
            raise RuntimeError("The unified destination already exists; nothing was overwritten.")
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("owner") != OWNER:
            raise RuntimeError("The destination is not owned by this Voices installation.")
        old = manifest["files"]
        actual = {str(p.relative_to(target)).replace("\\", "/") for p in target.rglob("*")
                  if p.is_file() and p != manifest_path}
        if actual != set(old):
            raise RuntimeError("Unified mod files were added or removed. Preserve them before rebuilding.")
        for relative, expected in old.items():
            if digest((target / relative).read_bytes()) != expected:
                raise RuntimeError("A unified file was edited externally: " + relative)
        # Launcher discovery rewrites indentation and line endings in the external .mod.
        # The internal descriptor was hash-verified above, so it is the owned reference.
        expected_descriptor = (target / "descriptor.mod").read_bytes() + f'path="{target.as_posix()}"\n'.encode()
        if digest(expected_descriptor) != manifest["descriptor_sha256"]:
            raise RuntimeError("The unified descriptor ownership metadata is inconsistent.")
        if not descriptor_matches(descriptor.read_bytes(), expected_descriptor):
            raise RuntimeError("The unified descriptor was edited externally.")
    files = render(profile, unified=not legacy, telemetry=telemetry)
    # Back up the whole small owned installation before changing its generated profile.
    if target.exists():
        backup = ROOT / "launcher-backup/unified" / str(__import__("time").time_ns())
        shutil.copytree(target, backup / folder)
        shutil.copy2(descriptor, backup / descriptor.name)
    mods.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".voices-stage-", dir=mods))
    external = files["descriptor.mod"] + f'path="{target.as_posix()}"\n'.encode()
    manifest = {"owner": OWNER, "profile": profile,
                "files": {k: digest(v) for k, v in files.items()},
                "descriptor_sha256": digest(external)}
    try:
        for relative, data in files.items():
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (stage / manifest_path.name).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if not target.exists():
            os.replace(stage, target)
        else:
            # No recursive delete or move; replace only hash-verified owned files.
            for relative in files:
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage / relative, destination)
            # Optional telemetry can be disabled again. Remove only old owned
            # files whose hashes were verified above, never arbitrary contents.
            for relative in set(old) - set(files):
                (target / relative).unlink()
            os.replace(stage / manifest_path.name, manifest_path)
        # Preserve equivalent launcher formatting to avoid unnecessary registration churn.
        if not descriptor.exists() or not descriptor_matches(descriptor.read_bytes(), external):
            temporary_descriptor = mods / (folder + ".mod.tmp")
            temporary_descriptor.write_bytes(external)
            os.replace(temporary_descriptor, descriptor)
    finally:
        # Verify the resolved scratch target before removing only files we just staged.
        if stage.exists():
            if stage.resolve().parent != mods.resolve() or not stage.name.startswith(".voices-stage-"):
                raise RuntimeError("Unexpected staging directory.")
            for path in stage.rglob("*"):
                if path.is_file():
                    path.unlink()
            for directory in sorted((p for p in stage.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                directory.rmdir()
            stage.rmdir()
    return manifest



def configure_bases(userdir, enabled=None):
    """Resolve explicit aliases and regular MD metadata from the selected descriptors."""
    from middleware.md_compat import is_regular_md, require_regular_md, is_oem, require_oem, quoted_field
    userdir = Path(userdir)
    mapping_path = userdir / "voices-profiles.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8")) if mapping_path.exists() else {}
    if not isinstance(mapping, dict):
        raise ValueError("voices-profiles.json must map mod descriptors to profiles.")
    if enabled is None:
        playset = userdir / "dlc_load.json"
        enabled = json.loads(playset.read_text(encoding="utf-8")).get("enabled_mods", []) if playset.exists() else []
    for entry in enabled:
        candidate = Path(entry)
        if candidate.is_absolute() or candidate.parent.as_posix() != "mod" or candidate.suffix != ".mod":
            continue
        descriptor = userdir / candidate
        if descriptor.is_file() and is_regular_md(descriptor):
            if entry in mapping and mapping[entry] != "millennium_dawn":
                raise RuntimeError("The explicit profile conflicts with the selected descriptor.")
            mapping[entry] = "millennium_dawn"
        elif descriptor.is_file() and is_oem(descriptor):
            if entry in mapping and mapping[entry] != "millennium_dawn_oem":
                raise RuntimeError("OEM must use its separate profile, never regular MD.")
            mapping[entry] = "millennium_dawn_oem"
    selected_names = {}
    for entry, profile in mapping.items():
        candidate = Path(entry)
        if (not isinstance(profile, str) or profile not in DEPENDENCIES or
                candidate.is_absolute() or candidate.parent.as_posix() != "mod" or candidate.suffix != ".mod"):
            raise ValueError("Invalid explicit base descriptor mapping.")
        descriptor = userdir / candidate
        text = descriptor.read_text(encoding="utf-8-sig")
        name = quoted_field(text, "name")
        if profile == "millennium_dawn":
            require_regular_md(descriptor)
        if profile == "millennium_dawn_oem":
            require_oem(descriptor)
        BASES[entry] = profile
        if entry in enabled or not enabled:
            selected_names.setdefault(profile, set()).add(name)
    for profile, names in selected_names.items():
        if len(names) != 1:
            raise ValueError("More than one selected base descriptor maps to profile: " + profile)
        DEPENDENCIES[profile] = names.pop()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Install a separate opt-in Voices adapter; never changes playsets.")
    parser.add_argument("--userdir", required=True, type=Path)
    parser.add_argument("--profile", required=True, choices=tuple(CONFIGS))
    parser.add_argument("--initial", action="store_true", help="Create only; refuses an existing destination")
    parser.add_argument("--legacy", action="store_true", help="Install the broader legacy catalog for an overhaul")
    parser.add_argument("--telemetry", action="store_true", help="Opt in to verified player readiness/economy exports")
    args = parser.parse_args()
    configure_bases(args.userdir)
    manifest = install_or_compile(args.userdir, args.profile, args.initial, args.legacy, args.telemetry)
    print("Installed profile:", manifest["profile"], "files:", len(manifest["files"]))
