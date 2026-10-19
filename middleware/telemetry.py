"""Versioned, player-only telemetry; missing data never becomes zero or a guess."""
import math
import re

VERSION = "1"
OEM = "millennium_dawn_oem"
BASE_FIELDS = {
    "divisions": ("num_divisions", "divisions"),
    "planes": ("num_deployed_planes", "deployed aircraft"),
    "ships": ("num_ships", "ships"),
    "army_personnel_k": ("deployed_army_manpower_k", "thousand personnel"),
    "air_personnel_k": ("deployed_airforce_manpower_k", "thousand personnel"),
    "navy_personnel_k": ("deployed_navy_manpower_k", "thousand personnel"),
    "fuel_k": ("fuel_k", "thousand fuel units"),
    "fuel_capacity_k": ("max_fuel_k", "thousand fuel units"),
}
ECONOMY = {
    "gdp": ("gdp_total", "billion USD, annual GDP"),
    "treasury": ("treasury", "billion USD"),
    "debt": ("debt", "billion USD"),
    "balance": ("treasury_rate", "billion USD/week"),
    "income": ("display_income", "billion USD/week"),
    "expense": ("display_expense", "billion USD/week"),
    "interest": ("interest_rate", "percent"),
    "growth": ("last_quarter_gdp_growth_rate", "quarter-on-quarter GDP growth ratio"),
    "growth_pc": ("last_quarter_gdpc_growth_rate", "quarter-on-quarter GDP per-capita growth ratio"),
}


def number(value, integer=False):
    """Native exporters use fixed three decimals or zero decimals explicitly."""
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid telemetry number")
    if integer:
        if not re.fullmatch(r"\d+|\d{1,3}(?:[,.]\d{3})+", value):
            raise ValueError("Invalid telemetry count")
        return int(re.sub(r"[,.]", "", value))
    if "," in value and "." in value:
        decimal = "," if value.rfind(",") > value.rfind(".") else "."
        value = value.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif "," in value:
        value = value.replace(",", ".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        raise ValueError("Invalid telemetry number")
    result = float(value)
    if not math.isfinite(result) or abs(result) > 1e12:
        raise ValueError("Out-of-range telemetry number")
    return result


def parse(line):
    marker = "AIDIPLO_TELEMETRY|"
    if marker not in line:
        return None
    payload = line.split(marker, 1)[1].strip()
    if len(payload) > 16000:
        return None
    packet = {}
    for part in payload.split("|"):
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        if key in packet:
            return None
        packet[key] = value
    if (packet.get("v") != VERSION or not re.fullmatch(r"[A-Z][A-Z0-9]{2}", packet.get("tag", ""))
            or not re.fullmatch(r"[a-z_]+", packet.get("profile", ""))):
        return None
    try:
        if number(packet["gid"], True) <= 0 or number(packet["day"], True) < 0:
            return None
        for key, value in packet.items():
            if key in ("v", "profile", "tag", "date", "gid", "day"):
                continue
            count = key in ("low_strength", "low_org", "sampled_divisions", "divisions", "planes", "ships") or key.startswith(("stock.", "army.", "need.", "has_"))
            parsed = number(value, count)
            if key.startswith("has_") and parsed not in (0, 1):
                return None
            if not key.startswith("econ.") and parsed < 0:
                return None
        if packet.get("profile") != OEM and any(k.startswith(("econ.", "has_")) for k in packet):
            return None
    except (KeyError, ValueError):
        return None
    return packet


def matches(packet, state, max_age=1):
    if not isinstance(packet, dict) or packet.get("v") != VERSION:
        return False
    try:
        return (all(packet.get(k) == str(state.get(k, "")) for k in ("profile", "tag"))
                and number(packet['gid'], True) == number(str(state['gid']), True)
                and 0 <= number(str(state["day"]), True) - number(packet["day"], True) <= max_age)
    except (KeyError, ValueError):
        return False


def context(state, max_age=1):
    packet = state.get("telemetry")
    if not matches(packet, state, max_age):
        return "Verified readiness/economy telemetry unavailable or stale. Missing values are unknown, never zero."
    lines = ["=== VERIFIED PLAYER TELEMETRY v1 ===",
             "Snapshot: " + packet.get("date", "unknown") + "; campaign day " + packet["day"] +
             "; age " + str(number(str(state["day"]), True) - number(packet["day"], True)) + " game days."]
    for key, (_, unit) in BASE_FIELDS.items():
        if key in packet:
            lines.append(f"{key}: {packet[key]} {unit}")
    for key, label in (("low_strength", "Divisions below 80% strength"), ("low_org", "Divisions below 50% organization")):
        if key in packet:
            lines.append(f"{label}: {packet[key]}; denominator {packet.get('sampled_divisions', 'unknown')} sampled country divisions (counts, not weighted averages).")
    for key in sorted(k[6:] for k in packet if k.startswith("stock.")):
        stock = number(packet['stock.' + key], True)
        deployed = number(packet.get('army.' + key, '0'), True) if 'army.' + key in packet else None
        need = number(packet.get('need.' + key, '0'), True) if 'need.' + key in packet else None
        row = f"Equipment {key}: reserve stock {stock} units"
        if deployed is not None and need is not None:
            row += f"; in armies {deployed}; armies require {need}; fielded-army deficit {max(0, need-deployed)}; net deficit after reserves {max(0, need-deployed-stock)}"
        lines.append(row + ". Future production/deployment/garrison orders are not included in this requirement.")
    if packet.get("profile") == OEM:
        for key, (_, unit) in ECONOMY.items():
            if packet.get("has_" + key) == "1" and 'econ.' + key in packet:
                value = number(packet['econ.' + key])
                if key.startswith("growth"):
                    value *= 100
                    unit = "percent, previous completed quarter; " + ("GDP per capita" if key == "growth_pc" else "total GDP")
                lines.append(f"OEM {key}: {value:g} {unit}")
            else:
                lines.append(f"OEM {key}: unavailable (country variable not initialized/exported)")
    lines.append("Not measured: division training, detailed supply, unit positions/front assignments, air/naval organization, design-specific aircraft inventory or production queues. Do not infer combat readiness from division/ship/plane totals alone.")
    return "\n".join(lines)


def native(profile):
    """Generate only documented engine queries and explicit OEM country variables."""
    from profiles import MD_EQUIPMENT, CWIC_EQUIPMENT, TFR_EQUIPMENT
    equipment = {"millennium_dawn": MD_EQUIPMENT, OEM: MD_EQUIPMENT,
                 "cwic_dev": CWIC_EQUIPMENT, "the_fire_rises": TFR_EQUIPMENT}.get(profile, {"infantry_equipment", "artillery_equipment", "convoy"})
    lines = ["# Opt-in telemetry v1; see docs/TELEMETRY.md for sources/units/limits.",
             "voices_export_telemetry = {", " save_event_target_as = voices_tlm_player",
             " set_temp_variable = { voices_tlm_low_strength = 0 }", " set_temp_variable = { voices_tlm_low_org = 0 }",
             " set_temp_variable = { voices_tlm_sampled_divisions = 0 }",
             " every_country_division = {",
             "  event_target:voices_tlm_player = { add_to_temp_variable = { voices_tlm_sampled_divisions = 1 } }",
             "  if = { limit = { unit_strength < 0.8 } event_target:voices_tlm_player = { add_to_temp_variable = { voices_tlm_low_strength = 1 } } }",
             "  if = { limit = { unit_organization < 0.5 } event_target:voices_tlm_player = { add_to_temp_variable = { voices_tlm_low_org = 1 } } }", " }"]
    fields = ["v=1", "profile=" + profile, "tag=[This.GetTag]", "gid=[?aidiplo_gameid|0]", "day=[?aidiplo_day|0]", "date=[GetDateText]",
              "low_strength=[?voices_tlm_low_strength|0]", "low_org=[?voices_tlm_low_org|0]", "sampled_divisions=[?voices_tlm_sampled_divisions|0]"]
    for key, (query, _) in BASE_FIELDS.items():
        lines.append(f" set_temp_variable = {{ voices_tlm_{key} = {query} }}")
        fields.append(f"{key}=[?voices_tlm_{key}|{'0' if key in ('divisions', 'planes', 'ships') else '3'}]")
    for index, equipment_type in enumerate(sorted(equipment)):
        for key, query in (("stock", "num_equipment"), ("army", "num_equipment_in_armies"), ("need", "num_target_equipment_in_armies")):
            var = f"voices_tlm_{key}_{index}"
            lines.append(f" set_temp_variable = {{ {var} = {query}@{equipment_type} }}")
            fields.append(f"{key}.{equipment_type}=[?{var}|0]")
    if profile == OEM:
        for key, (variable, _) in ECONOMY.items():
            lines += [f" set_temp_variable = {{ voices_tlm_{key} = 0 }}", f" set_temp_variable = {{ voices_tlm_has_{key} = 0 }}",
                      f" if = {{ limit = {{ has_variable = {variable} }} set_temp_variable = {{ voices_tlm_has_{key} = 1 }} set_temp_variable = {{ voices_tlm_{key} = {variable} }} }}"]
            fields += [f"has_{key}=[?voices_tlm_has_{key}|0]", f"econ.{key}=[?voices_tlm_{key}|3]"]
    lines += [' log = "AIDIPLO_TELEMETRY|' + '|'.join(fields) + '"', "}"]
    return ('\n'.join(lines) + '\n').encode()


def attach(files, profile):
    key = "common/scripted_effects/ai_diplo_export.txt"
    text = files[key].decode("utf-8-sig").replace('\r\n', '\n')
    pattern = r'(?m)^([ \t]*log = "AIDIPLO_STATE\|[^\r\n]+)$'
    text, count = re.subn(pattern, r'\1\n\tvoices_export_telemetry = yes', text)
    if count != 1:
        raise ValueError("Expected one player export hook")
    files[key] = text.encode()
    files["common/scripted_effects/voices_telemetry.txt"] = native(profile)
    return files
