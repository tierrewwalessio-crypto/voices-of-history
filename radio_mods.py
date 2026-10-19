"""Recognize narrowly scoped, unpacked radio stations without trusting mod names."""
from pathlib import Path
import re


def _entries(tokens):
    """Read assignments at this block's level, retaining nested blocks as tokens."""
    position = 0
    while position < len(tokens):
        key = tokens[position]
        if position + 2 >= len(tokens) or tokens[position + 1] != "=":
            raise ValueError("Invalid radio asset syntax")
        start = position + 2
        position = start + 1
        if tokens[start] == "{":
            depth = 1
            while position < len(tokens) and depth:
                depth += (tokens[position] == "{") - (tokens[position] == "}")
                position += 1
            if depth:
                raise ValueError("Unclosed radio asset block")
        yield key, tokens[start:position]


def _named_blocks(path, root_key, kind, allowed_names):
    from unified_mod import descriptor_fields
    fields = descriptor_fields(path.read_bytes())
    if set(fields) != {root_key}:
        return False
    blocks = list(_entries(fields[root_key][1:-1]))
    if not blocks:
        return False
    for key, value in blocks:
        if key != kind or value[0] != "{":
            return False
        names = [v for k, v in _entries(value[1:-1]) if k == "name"]
        allowed = {('"' + name + '"',) for name in allowed_names.get(path.stem, ())}
        if len(names) != 1 or names[0] not in allowed:
            return False
    return True


def is_radio_mod(userdir, entry):
    """Fail closed on gameplay files, replace paths, missing sources or unknown layouts.

    Accept music plus station-specific MMCT faceplates, cover sprites and song
    localisation. Arbitrary interface/gfx/localisation mods are not exempted.
    """
    from unified_mod import descriptor_fields
    try:
        candidate = Path(entry)
        if candidate.is_absolute() or candidate.parent.as_posix() != "mod" or candidate.suffix.lower() != ".mod":
            return False
        external = descriptor_fields((Path(userdir) / candidate).read_bytes())
        paths = external.get("path", ())
        if len(paths) != 1 or not paths[0].startswith('"'):
            return False
        source = Path(paths[0][1:-1])
        if not source.is_absolute():
            source = Path(userdir) / source
        source = source.resolve()
        internal = descriptor_fields((source / "descriptor.mod").read_bytes())
        for fields in (external, internal):
            if "replace_path" in fields or "archive" in fields or "dependencies" in fields:
                return False
            if not {'"Sound"', '"Music"'}.intersection(fields.get("tags", ())):
                return False
        files, stations, songs = [], set(), set()
        for path in source.rglob("*"):
            if path.is_symlink() or not path.resolve().is_relative_to(source):
                return False
            if not path.is_file():
                continue
            files.append(path)
            if len(files) > 10000:
                return False
            relative = path.relative_to(source)
            if relative.parts[0].lower() == "music" and path.suffix.lower() in (".txt", ".asset"):
                if path.stat().st_size > 2_000_000:
                    return False
                text = path.read_text(encoding="utf-8-sig")
                if path.suffix.lower() == ".txt":
                    stations.update(re.findall(r'(?m)^\s*music_station\s*=\s*"([A-Za-z0-9_]+)"', text))
                else:
                    songs.update(re.findall(r'(?m)^\s*name\s*=\s*"([^"\r\n]+)"', text))
        if not stations or not songs:
            return False
        for path in files:
            relative = path.relative_to(source)
            parts, suffix = relative.parts, path.suffix.lower()
            if len(parts) == 1:
                if suffix == ".mod" or path.name.lower() in ("thumbnail.png", "thumbnail.jpg", "thumbnail.jpeg"):
                    continue
                return False
            top = parts[0].lower()
            if top == "music" and suffix in (".txt", ".asset", ".ogg", ".mp3", ".wav"):
                continue
            if top == "gfx" and len(parts) == 2 and path.stem.startswith("radio_station_") and suffix in (".dds", ".png"):
                continue
            if top == "interface" and len(parts) == 2 and path.stem in stations:
                if path.stat().st_size > 2_000_000:
                    return False
                if suffix == ".gui" and _named_blocks(path, "guiTypes", "containerWindowType", {s: {s + "_faceplate", s + "_stations_entry"} for s in stations}):
                    continue
                if suffix == ".gfx" and _named_blocks(path, "spriteTypes", "spriteType", {s: {"GFX_radio_station_cover_" + s} for s in stations}):
                    continue
            if top == "localisation" and suffix == ".yml" and any(path.name.startswith("music_station_" + s + "_l_") for s in stations):
                if path.stat().st_size > 2_000_000:
                    return False
                keys = set(re.findall(r'(?m)^\s*([^\s:#]+)\s*:\s*(?:\d+\s*)?"', path.read_text(encoding="utf-8-sig")))
                expected = songs | {s + "_TITLE" for s in stations}
                if keys and {key.casefold() for key in keys} <= {key.casefold() for key in expected}:
                    continue
            return False
        return True
    except (OSError, UnicodeError, ValueError, IndexError):
        return False


def gameplay_mods(userdir, enabled):
    """Filter verified radio assets only; never rewrite the actual playset."""
    normalized = [entry.replace("\\", "/").casefold() for entry in enabled]
    if len(normalized) != len(set(normalized)):
        raise RuntimeError("The active playset contains duplicate mod entries.")
    return [entry for entry in enabled if not is_radio_mod(userdir, entry)]
