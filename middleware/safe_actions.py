"""Typed unified actions, country authority, engine guards and a durable result journal."""
from contextlib import closing
from pathlib import Path
import json
import math
import re
import sqlite3
import time
import uuid

from profiles import MD, CWIC_DEV, TFR, MD_EQUIPMENT, CWIC_EQUIPMENT, TFR_EQUIPMENT


def text_only(text):
    return bool(re.search(r"\b(take no actions?|no (?:game )?(?:actions?|commands?|changes)|"
                          r"do not (?:act|execute|change|apply)|don't (?:act|execute|change|apply)|"
                          r"do nothing|do not take (?:any )?actions?|preview only|dry[- ]run|"
                          r"text[- ]only|hypothetical(?:ly)?|non eseguire|nessuna azione|no hagas nada)\b", text, re.I))


CATALOG = """Return ONE JSON object: {"message":"in-character reply", "decision":"inform|negotiate|refuse|accept", "actions":[]}.
Use only these typed actions, with numeric values and exact synchronized country tags:
- improve_relations / worsen_relations {country_a, country_b}: small 30-day opinion signal.
- non_aggression_pact / guarantee / military_access {country_a, country_b}: country_a acts.
  Military access means country_a obtains access TO country_b; it requires country_b's consent.
- send_equipment {sender,receiver,equipment_type,quantity}: 1-500 equipment, donor stockpile required.
- ai_priority {target,priority:hold|infantry|mobile}: AI country's own production role emphasis for 14 days.
- add_political_power {target,amount}: player's explicit Office order only, -5 to +5.
Actions need decision=accept and a concrete authorized request. Never act for third countries.
Your acceptance of a proposal is distinct from execution. Describe actions as proposed;
only a separate engine confirmation can establish success. Refusal/negotiation/information has
actions=[]. 'Take no actions', hypothetical and text-only requests always have actions=[].
No arbitrary commands, effects, wars, annexations, free factories, unearned resource grants,
elections, leader changes or custom-overhaul systems. Unsupported requests stay narrative.
"""


def equipment_set(profile):
    return {MD: MD_EQUIPMENT, "millennium_dawn_oem": MD_EQUIPMENT, CWIC_DEV: CWIC_EQUIPMENT, TFR: TFR_EQUIPMENT}.get(
        profile, TFR_EQUIPMENT)


def response_schema(profile, proposals=False):
    """Responses structured output; authorization still belongs to validate()."""
    def obj(properties):
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}

    def variant(names, arguments):
        return obj({"name": {"type": "string", "enum": names}, "args": obj(arguments)})

    tag = {"type": "string", "pattern": "^[A-Z][A-Z0-9]{2}$"}
    diplomacy = variant(["improve_relations", "worsen_relations", "non_aggression_pact",
                         "guarantee", "military_access"], {"country_a": tag, "country_b": tag})
    equipment = variant(["send_equipment"], {
        "sender": tag, "receiver": tag,
        "equipment_type": {"type": "string", "enum": sorted(equipment_set(profile))},
        "quantity": {"type": "integer", "minimum": 1, "maximum": 500}})
    proposal = variant(["propose_equipment"], {
        "sender": tag, "receiver": tag,
        "equipment_type": {"type": "string", "enum": sorted(equipment_set(profile))},
        "quantity": {"type": ["integer", "null"], "minimum": 1, "maximum": 500}})
    priority = variant(["ai_priority"], {"target": tag,
        "priority": {"type": "string", "enum": ["hold", "infantry", "mobile"]}})
    political_power = variant(["add_political_power"], {"target": tag,
        "amount": {"type": "integer", "minimum": -5, "maximum": 5}})
    variants = [diplomacy, equipment, priority, political_power]
    if proposals:
        variants.append(proposal)
    return obj({"message": {"type": "string"},
                "decision": {"type": "string", "enum": ["inform", "negotiate", "refuse", "accept"]},
                "actions": {"type": "array", "maxItems": 2,
                            "items": {"anyOf": variants}}})


def _tag(value, player):
    value = str(value or "").upper()
    return player if value == "ROOT" else value


def _integer(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or int(value) != value:
        raise ValueError("An exact whole number is required.")
    if not low <= value <= high:
        raise ValueError(f"Amount must be between {low} and {high}.")
    return int(value)


def validate(action, state, world, channel, decision, user_text, profile, automatic=False):
    """Return normalized action. Authority is derived from the channel, never from model args."""
    if decision != "accept" or text_only(user_text):
        raise ValueError("This turn does not authorize actions.")
    if channel != "decree" and not channel.startswith(("leader:", "strategy:")):
        raise ValueError("This is a narrative channel, not an action channel.")
    player = state.get("tag", "")
    actor = channel.split(":", 1)[1] if channel.startswith(("leader:", "strategy:")) else player
    if (not re.fullmatch(r"[A-Z][A-Z0-9]{2}", actor) or not re.fullmatch(r"[A-Z][A-Z0-9]{2}", player)
            or not state.get("gid")):
        raise ValueError("Synchronize the campaign before acting.")
    if actor != player and actor not in world:
        raise ValueError("The acting country is not synchronized.")
    name, args = action.get("name"), action.get("args", {})
    if not isinstance(args, dict):
        raise ValueError("Action arguments must be an object.")
    if automatic and name != "ai_priority":
        raise ValueError("The strategic pilot can only adjust its own AI priority.")
    allowed = {player, actor}
    def country(key, default):
        result = _tag(args.get(key, default), player)
        if result not in allowed:
            raise ValueError("The representative cannot act for a third country.")
        return result
    out = {"name": name, "actor": actor, "player": player}
    if name == "ai_priority":
        if actor == player or country("target", actor) != actor:
            raise ValueError("AI priorities belong to the AI representative's own country.")
        if not automatic and not re.search(r"\b(priority|priorities|production|prepare|defen[cs]e|rearm|mobil|infantry|army)\b", user_text, re.I):
            raise ValueError("A production-priority request is required.")
        priority = args.get("priority")
        if priority not in ("hold", "infantry", "mobile"):
            raise ValueError("Unknown AI priority.")
        out["priority"] = priority
    elif name in ("improve_relations", "worsen_relations", "non_aggression_pact", "guarantee", "military_access"):
        patterns = {"improve_relations": r"relation|friend|goodwill|cooperat|ties|rapprochement",
                    "worsen_relations": r"relation|condemn|denounc|distrust|protest|ties",
                    "non_aggression_pact": r"non[- ]?aggression|pact",
                    "guarantee": r"guarantee|independence",
                    "military_access": r"military access|access to|transit|passage"}
        if not re.search(patterns[name], user_text, re.I):
            raise ValueError("No matching diplomatic request was made.")
        a, b = country("country_a", actor), country("country_b", player)
        if a == b:
            raise ValueError("Diplomacy requires two distinct countries.")
        # Access's scoped effect grants A access to B; B must be the represented AI.
        if name == "military_access":
            if b != actor or actor == player:
                raise ValueError("The country granting access must be the representative in this chat.")
        elif a != actor:
            raise ValueError("The acting country must be the representative in this chat.")
        out.update(a=a, b=b)
    elif name == "send_equipment":
        if not re.search(r"equipment|rifles?|artillery|suppl|weapons?|trucks?|convoys?|lend[- ]lease", user_text, re.I):
            raise ValueError("An explicit equipment request is required.")
        sender, receiver = country("sender", actor), country("receiver", player)
        if sender != actor or receiver == actor:
            raise ValueError("Only the represented donor can authorize its stockpile transfer.")
        equipment = args.get("equipment_type")
        if equipment not in equipment_set(profile):
            raise ValueError("That equipment type is not verified for this profile.")
        out.update(sender=sender, receiver=receiver, equipment=equipment,
                   quantity=_integer(args.get("quantity"), 1, 500))
    elif name == "add_political_power":
        if channel != "decree" or country("target", player) != player or not re.search(r"political power|\bpp\b", user_text, re.I):
            raise ValueError("Political power requires an explicit player Office order.")
        out["amount"] = _integer(args.get("amount"), -5, 5)
        if out["amount"] == 0:
            raise ValueError("No change requested.")
    else:
        raise ValueError("Unsupported unified action: " + str(name))
    return out


def describe(action):
    name, actor = action["name"], action["actor"]
    if name == "ai_priority":
        return f"{actor}: {action['priority']} production emphasis for 14 days"
    if name == "send_equipment":
        return f"{actor} offers {action['quantity']} {action['equipment'].replace('_', ' ')} to {action['receiver']} (requires donor stockpile)"
    if name == "add_political_power":
        return f"{actor}: political power {action['amount']:+d}"
    labels = {"improve_relations": "goodwill signal (+10 opinion for 30 days)",
              "worsen_relations": "distrust signal (-10 opinion for 30 days)",
              "non_aggression_pact": "non-aggression pact", "guarantee": "guarantee of independence",
              "military_access": "military access"}
    return f"{action['a']} → {action['b']}: {labels.get(name, name)}"


def command(action, transaction_id, gid, day=None):
    """One guarded engine transaction. No model-written script text is interpolated."""
    if not re.fullmatch(r"[a-f0-9]{32}", transaction_id):
        raise ValueError("Invalid transaction identity.")
    actor, player, name = action["actor"], action["player"], action["name"]
    flag = "voices_tx_" + transaction_id
    def marker(status):
        # The console/run tokenizer removes string quotes before effect parsing.
        # '=' inside the old quoted marker became extra effect tokens in HOI4.
        return f"log = VOICES_ACTION_id_{transaction_id}_gid_{int(gid)}_status_{status}"
    day_guard = f" check_variable = {{ aidiplo_day = {int(day)} }}" if day is not None else ""
    guard = f"exists = yes {player} = {{ is_ai = no check_variable = {{ aidiplo_gameid = {int(gid)} }}{day_guard} }}"
    if actor != player:
        guard += " is_ai = yes has_capitulated = no"
    post = "always = yes"
    if name == "ai_priority":
        guard += " is_ai = yes"
        effect = "clr_country_flag = voices_priority_infantry clr_country_flag = voices_priority_mobile"
        if action["priority"] != "hold":
            priority_flag = "voices_priority_" + action["priority"]
            effect += f" set_country_flag = {{ flag = {priority_flag} value = 1 days = 14 }}"
            post = f"has_country_flag = {priority_flag}"
        else:
            post = "NOT = { has_country_flag = voices_priority_infantry } NOT = { has_country_flag = voices_priority_mobile }"
    elif name in ("improve_relations", "worsen_relations"):
        b = action["b"]
        mod = "voices_dialogue_goodwill" if name == "improve_relations" else "voices_dialogue_distrust"
        guard += f" {b} = {{ exists = yes }} NOT = {{ has_country_flag = voices_signal_{b} }}"
        effect = (f"add_opinion_modifier = {{ target = {b} modifier = {mod} }} "
                  f"set_country_flag = {{ flag = voices_signal_{b} value = 1 days = 30 }}")
        # Native MD applied the exact +10 but this immediate combined check
        # returned false. Report the guarded branch as executed; the actual
        # pair value and expiry need a separate native/save comparison.
        return_status = "executed"
    elif name in ("non_aggression_pact", "guarantee", "military_access"):
        a, b = action["a"], action["b"]
        guard += f" {a} = {{ exists = yes NOT = {{ has_war_with = {b} }} }} {b} = {{ exists = yes }}"
        effect = f"{a} = {{ diplomatic_relation = {{ country = {b} relation = {name} active = yes }} }}"
        if name == "non_aggression_pact":
            post = f"{a} = {{ has_non_aggression_pact_with = {b} }}"
        elif name == "guarantee":
            post = f"{b} = {{ is_guaranteed_by = {a} }}"
        else:
            post = f"{a} = {{ has_military_access_to = {b} }}"
    elif name == "send_equipment":
        receiver, equipment, qty = action["receiver"], action["equipment"], action["quantity"]
        guard += f" {receiver} = {{ exists = yes }} has_equipment = {{ {equipment} > {qty - 1} }}"
        effect = (f"add_equipment_to_stockpile = {{ type = {equipment} amount = -{qty} }} "
                  f"{receiver} = {{ add_equipment_to_stockpile = {{ type = {equipment} amount = {qty} producer = {actor} }} }}")
        # This reports the guarded branch, not independently measured recipient totals.
        return_status = "executed"
    elif name == "add_political_power":
        amount = action["amount"]
        if amount < 0:
            guard += f" has_political_power > {abs(amount) - 1}"
        effect = f"add_political_power = {amount}"
        return_status = "executed"
    else:
        raise ValueError("Uncompiled action.")
    status = locals().get("return_status", "applied")
    return (f"eval_effect {actor} = {{ if = {{ limit = {{ has_country_flag = {flag} }} {marker('duplicate')} }} "
            f"else_if = {{ limit = {{ {guard} }} set_country_flag = {flag} {effect} "
            f"if = {{ limit = {{ {post} }} {marker(status)} }} else = {{ {marker('unverified')} }} }} "
            f"else = {{ {marker('rejected')} }} }}")


class ActionJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,profile TEXT,gid INTEGER,day INTEGER,"
                       "action TEXT,state TEXT,created REAL,result TEXT)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def create(self, action, state, profile, preview):
        tx = uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,NULL)",
                       (tx, profile, int(state["gid"]), int(state["day"]), json.dumps(action),
                        "preview" if preview else "pending", time.time()))
        return tx

    def set_status(self, tx, status):
        with closing(self.connect()) as db, db:
            db.execute("UPDATE actions SET state=? WHERE id=? AND state='pending'", (status, tx))

    def receive(self, line):
        match = (re.search(r"VOICES_ACTION_id_([a-f0-9]{32})_gid_(\d+)_status_(applied|executed|rejected|duplicate|unverified)\b", line)
                 or re.search(r"VOICES_ACTION\|id=([a-f0-9]{32})\|gid=(\d+)\|status=(applied|executed|rejected|duplicate|unverified)\b", line))
        if not match:
            return None
        tx, gid, status = match.groups()
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT action,profile,day FROM actions WHERE id=? AND gid=? AND state IN ('pending','uncertain')", (tx, int(gid))).fetchone()
            if not row:
                return None
            db.execute("UPDATE actions SET state=?,result=? WHERE id=?", (status, line.strip(), tx))
        return {"id": tx, "gid": int(gid), "status": status, "action": json.loads(row[0]), "profile": row[1], "day": row[2]}

    def recent(self, profile, gid, limit=12, day=None):
        with closing(self.connect()) as db:
            rows = db.execute("SELECT id,action,state FROM actions WHERE profile=? AND gid=? AND (? IS NULL OR day<=?) ORDER BY created DESC LIMIT ?", (profile, gid, day, day, limit)).fetchall()
        return [{"id": r[0], "action": json.loads(r[1]), "state": r[2]} for r in rows]
