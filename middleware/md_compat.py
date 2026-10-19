"""Identity checks for the verified regular Millennium Dawn distribution."""
from pathlib import Path
import re

WORKSHOP_ID = "2777392649"
REGULAR_NAMES = {"Millennium Dawn: A Modern Day Mod", "Millennium Dawn: Developer Version"}


def quoted_field(text, key):
    values = re.findall(r'(?m)^\s*' + re.escape(key) + r'\s*=\s*"([^"\r\n]*)"\s*(?:#.*)?$', text)
    if len(values) != 1:
        raise ValueError("The descriptor needs exactly one quoted " + key + " field.")
    return values[0]


def is_regular_md(path):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        return quoted_field(text, "remote_file_id") == WORKSHOP_ID and quoted_field(text, "name") in REGULAR_NAMES
    except (OSError, UnicodeError, ValueError):
        return False


def require_regular_md(path):
    if not is_regular_md(path):
        raise RuntimeError("Expected regular Millennium Dawn descriptor metadata (Workshop " + WORKSHOP_ID + "): " + str(path))


def is_oem(path):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        # Local OEM descriptors deliberately have no Workshop id. Never infer OEM
        # from regular MD's shared Workshop id, folder name, or equipment names.
        return quoted_field(text, "name") in {"Millennium Dawn OEM Local", "Millennium Dawn: OEM Edition"}
    except (OSError, UnicodeError, ValueError):
        return False


def require_oem(path):
    if not is_oem(path):
        raise RuntimeError("Expected explicit Millennium Dawn OEM metadata: " + str(path))
