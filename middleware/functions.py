"""
functions.py
DIALOGUE-TO-GAMEPLAY: traduce le "actions" decise dall'LLM in comandi console
di HoI4 (eseguiti da injector.py).

DUE TIPI DI COMANDO:
- DIRETTI (pp, st, ws, manpower, ae, civilwar, puppet, annex, wp, add_opinion):
  funzionano SENZA -debug. Usati quando il bersaglio e' il giocatore (ROOT) o
  quando il comando diretto accetta i tag.
- eval_effect (blocchi di effetti): potenti ma richiedono -debug. Usati per tutto
  il resto (regime, leggi, fazioni, patti, dichiarazioni di guerra, ecc.).
  Le stringhe che iniziano con "effect " vengono tradotte in "eval_effect " da
  injector.py.

Ogni azione ritorna:
  { "commands": [str...], "desc": str, "memory": str|None, "special": dict|None }
"""
import re
from profiles import (MD_ACTIONS, MD_CATALOG, MD_EQUIPMENT, MD_EQUIPMENT_ALIASES, MD, MD_OEM,
                      CWIC_DEV, CWIC_CATALOG, CWIC_EQUIPMENT, CWIC_EQUIPMENT_ALIASES,
                      TFR, TFR_CATALOG, TFR_EQUIPMENT, TFR_EQUIPMENT_ALIASES,
                      RESTRICTED_PROFILES)

GAME_PROFILE = "vanilla"


def configure_profile(profile):
    global GAME_PROFILE
    if profile not in ("vanilla", *RESTRICTED_PROFILES):
        raise ValueError(f"Unknown game profile: {profile}")
    GAME_PROFILE = profile

# Mappa COMPLETA: qualsiasi modo (EN/IT, singolare/plurale, sinonimi) -> archetipo HoI4 reale
# accettato da add_equipment_to_stockpile. Le chiavi sono gia' normalizzate (minuscolo, niente
# spazi/underscore doppi); _resolve_equip() normalizza l'input prima del lookup.
EQUIPMENT = {
    # --- fanteria / supporto ---
    "rifles": "infantry_equipment", "rifle": "infantry_equipment", "infantry": "infantry_equipment",
    "infantryequipment": "infantry_equipment", "smallarms": "infantry_equipment", "guns": "infantry_equipment",
    "fucili": "infantry_equipment", "fanteria": "infantry_equipment", "armileggere": "infantry_equipment",
    "support": "support_equipment", "supportequipment": "support_equipment", "supportcompany": "support_equipment",
    "supporto": "support_equipment", "equipaggiamentodisupporto": "support_equipment", "supportcompanies": "support_equipment",
    # --- artiglieria / contraerea / anticarro ---
    "artillery": "artillery_equipment", "artilleryequipment": "artillery_equipment", "howitzer": "artillery_equipment",
    "artiglieria": "artillery_equipment", "cannoni": "artillery_equipment", "obici": "artillery_equipment",
    "rocketartillery": "rocket_artillery_equipment", "rockets": "rocket_artillery_equipment", "razzi": "rocket_artillery_equipment",
    "antitank": "anti_tank_equipment", "at": "anti_tank_equipment", "atg": "anti_tank_equipment",
    "anticarro": "anti_tank_equipment", "controcarro": "anti_tank_equipment",
    "antiair": "anti_air_equipment", "aa": "anti_air_equipment", "flak": "anti_air_equipment", "aaa": "anti_air_equipment",
    "contraerea": "anti_air_equipment", "antiaerea": "anti_air_equipment",
    # --- motorizzato / meccanizzato ---
    "trucks": "motorized_equipment", "truck": "motorized_equipment", "motorized": "motorized_equipment",
    "motorised": "motorized_equipment", "camion": "motorized_equipment", "motorizzato": "motorized_equipment",
    "mechanized": "mechanized_equipment", "mechanised": "mechanized_equipment", "halftrack": "mechanized_equipment",
    "halftracks": "mechanized_equipment", "ifv": "mechanized_equipment", "meccanizzato": "mechanized_equipment",
    "apc": "mechanized_equipment",
    # --- corazzato (chassis, stile NSB) ---
    "lighttanks": "light_tank_chassis", "lighttank": "light_tank_chassis", "carrileggeri": "light_tank_chassis",
    "tanks": "medium_tank_chassis", "tank": "medium_tank_chassis", "mediumtanks": "medium_tank_chassis",
    "mediumtank": "medium_tank_chassis", "carri": "medium_tank_chassis", "carriarmati": "medium_tank_chassis",
    "carrimedi": "medium_tank_chassis", "armor": "medium_tank_chassis", "armour": "medium_tank_chassis",
    "heavytanks": "heavy_tank_chassis", "heavytank": "heavy_tank_chassis", "carripesanti": "heavy_tank_chassis",
    "superheavytanks": "super_heavy_tank_chassis", "superheavytank": "super_heavy_tank_chassis",
    "moderntanks": "modern_tank_chassis", "moderntank": "modern_tank_chassis",
    # --- aerei ---
    "fighters": "fighter_equipment", "fighter": "fighter_equipment", "caccia": "fighter_equipment",
    "heavyfighters": "heavy_fighter_equipment", "heavyfighter": "heavy_fighter_equipment", "cacciapesanti": "heavy_fighter_equipment",
    "cas": "cas_equipment", "closeairsupport": "cas_equipment", "divebomber": "cas_equipment", "stuka": "cas_equipment",
    "tacticalbombers": "tac_bomber_equipment", "tacticalbomber": "tac_bomber_equipment", "tacbomber": "tac_bomber_equipment",
    "bombers": "tac_bomber_equipment", "bomber": "tac_bomber_equipment", "bombardieri": "tac_bomber_equipment",
    "strategicbombers": "strat_bomber_equipment", "strategicbomber": "strat_bomber_equipment", "stratbomber": "strat_bomber_equipment",
    "bombardieristrategici": "strat_bomber_equipment",
    "navalbombers": "nav_bomber_equipment", "navalbomber": "nav_bomber_equipment", "navbomber": "nav_bomber_equipment",
    "bombardierinavali": "nav_bomber_equipment",
    "scoutplane": "scout_plane_equipment", "scout": "scout_plane_equipment", "ricognitore": "scout_plane_equipment",
    "transportplane": "transport_plane_equipment", "transportplanes": "transport_plane_equipment",
    "paratrooper": "transport_plane_equipment", "aereidatrasporto": "transport_plane_equipment",
    # --- navale / logistica ---
    "convoys": "convoy", "convoy": "convoy", "convogli": "convoy", "convoglio": "convoy",
}

# Insieme degli archetipi VALIDI (per accettare un tipo gia' corretto passato dall'IA).
EQUIP_ARCHETYPES = set(EQUIPMENT.values()) | {
    "infantry_equipment", "support_equipment", "artillery_equipment", "rocket_artillery_equipment",
    "anti_tank_equipment", "anti_air_equipment", "motorized_equipment", "mechanized_equipment",
    "light_tank_chassis", "medium_tank_chassis", "heavy_tank_chassis", "super_heavy_tank_chassis",
    "modern_tank_chassis", "fighter_equipment", "heavy_fighter_equipment", "cas_equipment",
    "tac_bomber_equipment", "strat_bomber_equipment", "nav_bomber_equipment", "scout_plane_equipment",
    "transport_plane_equipment", "convoy",
}

def _resolve_equip(s):
    """Mappa QUALSIASI descrizione (EN/IT, plurali, sinonimi, gia'-archetipo) all'archetipo
    HoI4 corretto per add_equipment_to_stockpile. Default prudente: infantry_equipment SOLO se
    proprio non si capisce."""
    if GAME_PROFILE in RESTRICTED_PROFILES:
        raw = str(s or "").strip()
        valid, aliases = {
            MD: (MD_EQUIPMENT, MD_EQUIPMENT_ALIASES),
            MD_OEM: (MD_EQUIPMENT, MD_EQUIPMENT_ALIASES),
            CWIC_DEV: (CWIC_EQUIPMENT, CWIC_EQUIPMENT_ALIASES),
            TFR: (TFR_EQUIPMENT, TFR_EQUIPMENT_ALIASES),
        }[GAME_PROFILE]
        if raw in valid:
            return raw
        return aliases.get(raw.lower())
    raw = str(s or "").strip().lower()
    if not raw:
        return "infantry_equipment"
    # se l'IA ha gia' passato un archetipo valido, usalo cosi' com'e'
    if raw in EQUIP_ARCHETYPES:
        return raw
    # normalizza: togli spazi/underscore/trattini, e suffissi/parole rumore
    norm = re.sub(r"[\s_\-]+", "", raw)
    if norm in EQUIPMENT:
        return EQUIPMENT[norm]
    # prova a togliere parole rumore comuni ("equipment", "equipaggiamento", "di", "da")
    cleaned = norm.replace("equipment", "").replace("equipaggiamento", "").replace("equip", "")
    if cleaned in EQUIPMENT:
        return EQUIPMENT[cleaned]
    # match parziale: la chiave piu' lunga contenuta nella stringa (es. "support company gear")
    best = None
    for k, v in EQUIPMENT.items():
        if len(k) >= 3 and k in norm and (best is None or len(k) > len(best[0])):
            best = (k, v)
    if best:
        return best[1]
    # ultimo: l'IA ha scritto qualcosa tipo "xxx_equipment" valido ma non in mappa
    if raw.endswith("_equipment") or raw.endswith("_chassis"):
        return raw
    return "infantry_equipment"

# Pattern (ordine: dal PIU' specifico al piu' generico) per dedurre il tipo dal testo libero
# del player/IA. Word-boundary friendly: evitano falsi positivi (es. 'at' dentro 'stato').
_EQUIP_HINTS = [
    (r"support|supporto|equipaggiamento di supporto", "support_equipment"),
    (r"rocket|razzi|katyusha|nebelwerfer", "rocket_artillery_equipment"),
    (r"anti[\s_-]?tank|anti[\s_-]?carro|anticarro|controcarro|\batg\b|\bat guns?\b", "anti_tank_equipment"),
    (r"anti[\s_-]?air|anti[\s_-]?aere|antiaere|contraere|\bflak\b|\baa\b|\baaa\b", "anti_air_equipment"),
    (r"artiglier|artillery|howitzer|obic|cannoni", "artillery_equipment"),
    (r"mechaniz|mechanis|meccanizz|half[\s_-]?track|\bapc\b|\bifv\b", "mechanized_equipment"),
    (r"motoriz|motoris|truck|camion", "motorized_equipment"),
    (r"super[\s_-]?heavy[\s_-]?tank|carri.{0,6}super", "super_heavy_tank_chassis"),
    (r"modern[\s_-]?tank|carri.{0,6}modern", "modern_tank_chassis"),
    (r"heavy[\s_-]?tank|carri.{0,6}pesant", "heavy_tank_chassis"),
    (r"light[\s_-]?tank|carri.{0,6}legger", "light_tank_chassis"),
    (r"\btank|carri arm|\bcarri\b|panzer|corazzat|armou?r", "medium_tank_chassis"),
    (r"heavy[\s_-]?fighter|caccia.{0,6}pesant", "heavy_fighter_equipment"),
    (r"\bcas\b|close[\s_-]?air|picchiatell|stuka|dive[\s_-]?bomb", "cas_equipment"),
    (r"naval[\s_-]?bomb|bombardier.{0,6}naval|nav[\s_-]?bomb", "nav_bomber_equipment"),
    (r"strateg.{0,6}bomb|bombardier.{0,6}strateg|strat[\s_-]?bomb", "strat_bomber_equipment"),
    (r"tactic.{0,6}bomb|tac[\s_-]?bomb|bombardier|\bbomber|bombardament", "tac_bomber_equipment"),
    (r"scout|ricognit", "scout_plane_equipment"),
    (r"transport[\s_-]?plane|aerei.{0,6}trasport|paracadut|paratroop", "transport_plane_equipment"),
    (r"fighter|caccia", "fighter_equipment"),
    (r"convoy|convogli", "convoy"),
    (r"rifle|fucil|infantry|fanteria|small[\s_-]?arms", "infantry_equipment"),
]

def infer_equipment_type(text):
    """Deduce l'archetipo HoI4 piu' specifico citato in un testo libero (richiesta del player o
    narrazione IA), o None. Rete per quando l'IA dimentica/sbaglia equipment_type."""
    blob = str(text or "").lower()
    for pat, arch in _EQUIP_HINTS:
        if re.search(pat, blob):
            return _resolve_equip(arch) if GAME_PROFILE in RESTRICTED_PROFILES else arch
    return None

LAWS = {
    "disarmed_nation", "volunteer_only", "limited_conscription", "extensive_conscription",
    "service_by_requirement", "all_adults_serve", "scraping_the_barrel",
    "undisturbed_isolation", "isolation", "civilian_economy", "early_mobilisation",
    "partial_economic_mobilisation", "war_economy", "tot_economic_mobilisation",
    "free_trade", "export_focus", "limited_exports", "closed_economy",
}

IDEOLOGIES = {"fascism", "communism", "democratic", "neutrality"}
FACTORY_TYPE = {"civilian": "industrial_complex", "military": "arms_factory",
                "naval": "dockyard", "civili": "industrial_complex",
                "militari": "arms_factory", "navali": "dockyard"}

# Risorse strategiche di HoI4 (le UNICHE valide per add_resource)
RESOURCES = {"oil", "steel", "aluminium", "tungsten", "chromium", "rubber"}
RESOURCE_ALIAS = {
    # EN
    "aluminum": "aluminium", "bauxite": "aluminium",
    "coal": "steel", "iron": "steel", "iron_ore": "steel", "metal": "steel", "ore": "steel",
    "petroleum": "oil", "fuel": "oil", "petrol": "oil", "gasoline": "oil", "crude": "oil",
    "wolfram": "tungsten", "wolframite": "tungsten", "chrome": "chromium", "chromite": "chromium",
    # IT
    "acciaio": "steel", "ferro": "steel", "carbone": "steel", "metallo": "steel", "minerale": "steel",
    "petrolio": "oil", "carburante": "oil", "benzina": "oil", "gomma": "rubber", "caucciu": "rubber",
    "alluminio": "aluminium", "bauxite_it": "aluminium",
    "tungsteno": "tungsten", "wolframio": "tungsten", "cromo": "chromium", "cromite": "chromium",
}

# Edifici di stato costruibili con add_building_construction (livello-stato)
BUILDINGS = {"arms_factory", "industrial_complex", "dockyard", "synthetic_refinery",
             "fuel_silo", "infrastructure", "anti_air_building", "radar_station",
             "rocket_site", "nuclear_reactor", "air_base"}
BUILDING_ALIAS = {
    "military_factory": "arms_factory", "mil_factory": "arms_factory",
    "arms": "arms_factory", "weapons_factory": "arms_factory",
    "civilian_factory": "industrial_complex", "civ_factory": "industrial_complex",
    "factory": "industrial_complex", "industry": "industrial_complex",
    "naval_dockyard": "dockyard", "shipyard": "dockyard",
    "refinery": "synthetic_refinery", "oil_refinery": "synthetic_refinery",
    "synthetic": "synthetic_refinery",
    "fuel": "fuel_silo", "fuel_storage": "fuel_silo",
    "roads": "infrastructure", "rail": "infrastructure",
    "aa": "anti_air_building", "anti_air": "anti_air_building", "flak": "anti_air_building",
    "radar": "radar_station", "rockets": "rocket_site", "rocket": "rocket_site",
    "reactor": "nuclear_reactor", "nuclear": "nuclear_reactor",
    "airfield": "air_base", "airbase": "air_base", "air_field": "air_base",
}


# Tabella nome->tag DINAMICA, popolata a runtime dal gioco (anche mod/nuove
# nazioni) tramite le righe AIDIPLO_TAG esportate da HoI4. Ha PRIORITA'.
LIVE_TAGS = {}


def register_tags(mapping):
    for name, tag in (mapping or {}).items():
        if name and tag:
            LIVE_TAGS[str(name).strip().lower()] = str(tag).strip().upper()


# Tabella nome-stato -> ID, popolata a runtime dalle righe AIDIPLO_STATEINFO esportate dal
# gioco. Serve perche' gli effetti sugli stati richiedono l'ID NUMERICO (non il nome).
LIVE_STATES = {}


def register_states(mapping):
    for name, sid in (mapping or {}).items():
        if name and sid:
            LIVE_STATES[str(name).strip().lower()] = str(sid).strip()


def _state(x):
    """Risolve l'ID NUMERICO di uno stato da un numero o da un NOME (es. 'Rome' -> '2').
    Ritorna None se non risolvibile (cosi' l'azione viene ignorata invece di fallire muta)."""
    if x is None:
        return None
    s = str(x).strip()
    if re.match(r"^\d+$", s):
        return s
    return LIVE_STATES.get(s.lower())


# Fallback MINIMO (solo se il gioco non ha ancora esportato la tabella). La
# tabella dinamica del gioco, quando disponibile, sovrascrive sempre questi.
FALLBACK_TAGS = {
    "italy": "ITA", "italia": "ITA",
    "germany": "GER", "germania": "GER", "reich": "GER", "german reich": "GER",
    "soviet union": "SOV", "ussr": "SOV", "urss": "SOV", "russia": "SOV", "unione sovietica": "SOV",
    "united states": "USA", "usa": "USA", "america": "USA", "stati uniti": "USA",
    "united kingdom": "ENG", "uk": "ENG", "england": "ENG", "britain": "ENG",
    "great britain": "ENG", "inghilterra": "ENG", "regno unito": "ENG", "gran bretagna": "ENG",
    "france": "FRA", "francia": "FRA",
    "japan": "JAP", "giappone": "JAP",
    "china": "CHI", "cina": "CHI",
    "poland": "POL", "polonia": "POL",
    "yugoslavia": "YUG", "jugoslavia": "YUG",
    "spain": "SPR", "spagna": "SPR", "republican spain": "SPR", "nationalist spain": "SPA",
    "greece": "GRE", "grecia": "GRE",
    "turkey": "TUR", "turchia": "TUR",
    "romania": "ROM", "romania ": "ROM",
    "hungary": "HUN", "ungheria": "HUN",
    "bulgaria": "BUL",
    "czechoslovakia": "CZE", "cecoslovacchia": "CZE",
    "austria": "AUS",
    "netherlands": "HOL", "holland": "HOL", "olanda": "HOL", "paesi bassi": "HOL",
    "belgium": "BEL", "belgio": "BEL",
    "switzerland": "SWI", "svizzera": "SWI",
    "sweden": "SWE", "svezia": "SWE",
    "norway": "NOR", "norvegia": "NOR",
    "denmark": "DEN", "danimarca": "DEN",
    "finland": "FIN", "finlandia": "FIN",
    "portugal": "POR", "portogallo": "POR",
    "albania": "ALB",
    "canada": "CAN",
    "australia": "AST",
    "brazil": "BRA", "brasile": "BRA",
    "mexico": "MEX", "messico": "MEX",
    "india": "RAJ", "ethiopia": "ETH", "etiopia": "ETH", "abyssinia": "ETH",
    "manchukuo": "MAN", "mongolia": "MON", "iraq": "IRQ", "iran": "PER", "persia": "PER",
    "egypt": "EGY", "egitto": "EGY", "ireland": "IRE", "irlanda": "IRE",
}


def _tag(x):
    if not x:
        return None
    x = str(x).strip()
    up = x.upper()
    if up in ("ROOT", "FROM", "THIS", "PREV"):
        return up
    if re.match(r"^[A-Z0-9]{3}$", up):
        return up
    key = x.lower()
    return LIVE_TAGS.get(key) or FALLBACK_TAGS.get(key)


def _is_player(tag):
    return tag in (None, "", "ROOT")


def _num(x, default=0):
    try:
        f = float(x)
        return int(f) if f.is_integer() else f
    except Exception:
        return default


def _clean(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def _truthy(x):
    """True per valori 'veri' robusti (bool, 1, 'yes', 'true', 'instant', 'now', 'si')."""
    if isinstance(x, bool):
        return x
    return str(x).strip().lower() in ("1", "true", "yes", "y", "instant", "now", "si", "sì", "immediate")


# Pochissimi effetti che a RUNTIME via eval_effect non si compilano (scope-sensitive):
# per questi esistono azioni dedicate (es. declare_war). NON limitiamo nient'altro:
# qualunque altro effetto valido di HoI4 (come nei focus tree) e' permesso.
BLOCKED_EFFECTS = {"declare_war_on", "create_wargoal",
                   # questi sono scope-sensitive (stato/provincia) e l'IA li sbaglia spesso
                   # via raw_effect (es. parametro 'location' inesistente): esistono azioni
                   # dedicate affidabili -> add_building, add_resource, transfer_state, ...
                   "add_building_construction", "set_building_level", "add_resource",
                   "add_extra_state_shared_building_slots"}

# Idee/spiriti validi referenziabili (quelli della mod + le leggi vanilla). Un
# add_ideas/add_timed_idea verso un'idea INVENTATA dall'IA farebbe fallire l'intero
# blocco eval_effect: percio' lo scartiamo.
KNOWN_IDEAS = ({"ai_diplo_war_drive", "ai_diplo_reform_strain", "ai_diplo_fortified",
                "ai_diplo_rival_progress"} | LAWS)


# Effetti SICURI per i bonus/malus del leader (set_leader): effetti country-scope che esistono
# SEMPRE e si eseguono via eval_effect senza errori. Tutto il resto viene scartato: un effetto
# inventato o scope-sensitive nel campo 'effects' farebbe fallire l'intero invio con
# "Had errors compiling the effect" (ed e' opzionale, non vale il rischio).
SAFE_LEADER_EFFECTS = {
    "add_stability", "add_war_support", "add_political_power", "add_manpower",
    "add_popularity", "set_stability", "set_war_support",
    "add_ideas", "add_timed_idea", "remove_ideas",
}


def _idea_tokens(s):
    m = re.search(r"idea\s*=\s*([A-Za-z_]\w*)", s)
    if m:
        return [m.group(1)]
    m = re.search(r"=\s*(.+)$", s)
    if not m:
        return []
    rest = m.group(1).strip().strip("{}").strip()
    return [w for w in re.split(r"\s+", rest) if re.match(r"^[A-Za-z_]\w*$", w)]


def _sanitize_effect(e):
    """Valida la SINTASSI di un effetto grezzo senza limitarne le capacita':
    - scarta se le graffe non sono bilanciate (malformato);
    - scarta i pochi effetti scope-sensitive che eval_effect non compila;
    - scarta add_ideas/add_timed_idea verso idee INESISTENTI (causerebbero crash).
    Tutto il resto (qualunque effetto valido di HoI4) passa."""
    s = _clean(e)
    if not s:
        return ""
    if s.count("{") != s.count("}"):
        return ""
    kw = re.split(r"[\s=]", s, 1)[0]
    if GAME_PROFILE in RESTRICTED_PROFILES:
        stat = r"(?:add_stability|add_war_support|add_political_power|add_manpower|set_stability|set_war_support)\s*=\s*-?\d+(?:\.\d+)?"
        spirit = r"ai_diplo_(?:war_drive|reform_strain|fortified|rival_progress)"
        idea = rf"(?:add_ideas|remove_ideas)\s*=\s*{spirit}"
        timed = rf"add_timed_idea\s*=\s*\{{\s*idea\s*=\s*{spirit}\s+days\s*=\s*\d+\s*\}}"
        return s if re.fullmatch(rf"(?:{stat}|{idea}|{timed})", s) else ""
    if kw in BLOCKED_EFFECTS:
        return ""
    if kw in ("add_ideas", "remove_ideas", "swap_ideas", "add_timed_idea"):
        for tok in _idea_tokens(s):
            if tok not in KNOWN_IDEAS:
                return ""
    return s


def _describe_effect(e):
    """Translates a common HoI4 effect into natural English (for the reports).
    If unrecognized, returns '' (so we never show raw syntax)."""
    s = e.strip()
    m = re.match(r"add_political_power\s*=\s*(-?\d+)", s)
    if m:
        n = int(m.group(1)); return (f"+{n}" if n >= 0 else f"{n}") + " political power"
    m = re.match(r"add_stability\s*=\s*(-?[\d.]+)", s)
    if m:
        v = float(m.group(1)); return f"{'+' if v >= 0 else ''}{round(v*100)}% stability"
    m = re.match(r"add_war_support\s*=\s*(-?[\d.]+)", s)
    if m:
        v = float(m.group(1)); return f"{'+' if v >= 0 else ''}{round(v*100)}% war support"
    m = re.match(r"add_manpower\s*=\s*(-?\d+)", s)
    if m:
        n = int(m.group(1)); return (f"+{n}" if n >= 0 else f"{n}") + " manpower"
    m = re.match(r"add_ideas\s*=\s*(\w+)", s)
    if m:
        return f"new law/national spirit ({m.group(1)})"
    m = re.match(r"add_timed_idea\s*=\s*\{\s*idea\s*=\s*(\w+)", s)
    if m:
        return f"temporary modifier ({m.group(1)})"
    return ""


def _r(commands, desc, memory=None, special=None):
    return {"commands": commands, "desc": desc, "memory": memory, "special": special}


def _act_event(act, actor, target, title, memory=None):
    """Azione diplomatica eseguita tramite EVENTO narrato (aidiplo.2).
    L'effetto sta nell'opzione dell'evento (compilato al load = robusto) e il
    testo viene riempito dalla narrazione dell'IA. 'act' sceglie l'effetto."""
    if not target:
        return None
    return _r([], f"{title} (evento narrato)", memory=memory,
              special={"type": "act_event", "act": act, "actor": actor or "ROOT",
                       "target": target, "title": title})


def build_action(name, args):
    name = (name or "").strip()
    a = args or {}
    if GAME_PROFILE in RESTRICTED_PROFILES and name not in MD_ACTIONS:
        label = {MD: "Millennium Dawn", MD_OEM: "Millennium Dawn OEM", CWIC_DEV: "CWIC", TFR: "The Fire Rises"}[GAME_PROFILE]
        return _r([], f"The {label} integration does not support '{name}' yet; "
                      "no game change was made.")

    # ---------- ESCAPE HATCH ----------
    if name == "raw_console":
        cmd = _clean(a.get("command", ""))
        return _r([cmd], f"Command: {cmd}") if cmd else None

    if name == "raw_effect":
        raw = str(a.get("effect", ""))
        # AUTO-INSTRADAMENTO: se l'IA mette in raw_effect un effetto scope-sensitive che
        # sbaglia spesso (edifici/risorse, con 'location' o scope errato), lo convertiamo
        # nell'azione DEDICATA affidabile (capital_scope) invece di scartarlo.
        mb = re.search(r"add_building_construction\s*=\s*\{([^{}]*)\}", raw)
        if mb:
            bt = re.search(r"type\s*=\s*([A-Za-z_]\w*)", mb.group(1))
            lv = re.search(r"level\s*=\s*(\d+)", mb.group(1))
            ib = re.search(r"instant_build\s*=\s*(yes|true|1)", mb.group(1))
            if bt:
                return build_action("add_building", {"target": a.get("target"),
                                    "building": bt.group(1), "level": lv.group(1) if lv else 1,
                                    "instant": bool(ib)})
        mr = re.search(r"add_resource\s*=\s*\{([^{}]*)\}", raw)
        if mr:
            rt = re.search(r"type\s*=\s*([A-Za-z_]\w*)", mr.group(1))
            am = re.search(r"amount\s*=\s*(-?\d+)", mr.group(1))
            if rt:
                return build_action("add_resource", {"target": a.get("target"),
                                    "resource": rt.group(1), "amount": am.group(1) if am else 8})
        tgt = _tag(a.get("target", "ROOT")) or "ROOT"
        eff = _sanitize_effect(raw)  # syntax validation only, no restriction
        if not eff:
            return _r([], "Invalid effect / not executable via console "
                          "(to declare war use the declare_war action)")
        return _r([f"effect {tgt} = {{ {eff} }}"], f"Effect on {tgt}: {eff}")

    # ---------- EQUIPMENT / LEND-LEASE ----------
    if name == "send_equipment":
        rec = _tag(a.get("receiver")) or "ROOT"
        # QUALSIASI tipo di equipment: _resolve_equip mappa EN/IT/sinonimi/plurali all'archetipo
        # reale (support_equipment, artillery_equipment, anti_tank_equipment, *_tank_chassis,
        # fighter_equipment, ...). add_equipment_to_stockpile accetta l'ARCHETIPO.
        arch = _resolve_equip(a.get("equipment_type") or a.get("type"))
        if not arch:
            return _r([], "That equipment type has no verified mapping for this game profile; "
                          "no equipment was transferred.")
        qty = int(_num(a.get("quantity") or a.get("amount"), 1000))
        if qty < 0:
            qty = -qty
        # MITTENTE (donatore): la nazione che FORNISCE l'equipment. In chat e' la nazione con cui
        # parla il player (la persona IA). Se c'e' un mittente REALE diverso dal destinatario, e'
        # un TRASFERIMENTO: l'equip va AGGIUNTO al destinatario e TOLTO dallo stockpile del mittente.
        sender = _tag(a.get("sender") or a.get("from") or a.get("donor") or a.get("source"))
        # TRASFERIMENTO = c'e' un mittente diverso dal destinatario: l'equip va TOLTO dal mittente,
        # CHIUNQUE esso sia, ANCHE il player (se sono io a donare, deve uscire dal MIO arsenale).
        transfer = bool(sender) and sender != rec
        # producer = il costruttore dell'equip: il mittente se e' un trasferimento, altrimenti il
        # destinatario stesso (o ROOT se il destinatario e' il player).
        prod = sender if transfer else ("ROOT" if _is_player(rec) else rec)
        where = "your arsenal" if _is_player(rec) else rec
        cmds = [f"effect {rec} = {{ add_equipment_to_stockpile = "
                f"{{ type = {arch} amount = {qty} producer = {prod} }} }}"]
        desc = f"+{qty} {arch} ({where})"
        mem = f"Sent {qty} {arch} to {where}"
        if transfer:
            # TOGLI DAVVERO l'equip dallo stockpile del mittente. Il gioco clampa a 0: una nazione
            # non puo' inviare piu' di quanto possiede -> il trasferimento e' gia' proporzionato a
            # quanto ha realmente. (La proporzione alla causa la dosa l'IA scegliendo la quantita'.)
            cmds.append(f"effect {sender} = {{ add_equipment_to_stockpile = "
                        f"{{ type = {arch} amount = -{qty} producer = {prod} }} }}")
            desc = f"+{qty} {arch} ({where}); -{qty} {arch} from {sender}'s stockpile"
            mem = f"{sender} sent {qty} {arch} to {where} (deducted from {sender}'s stockpile)"
        return _r(cmds, desc + " [needs -debug]", memory=mem)

    # ---------- STATS (direct commands if player) ----------
    if name == "add_political_power":
        t = _tag(a.get("target")) or "ROOT"
        n = _num(a.get("amount"), 100)
        if _is_player(t):
            return _r([f"pp {n}"], f"+{n} Political Power")
        return _r([f"effect {t} = {{ add_political_power = {n} }}"], f"+{n} PP to {t}")

    if name == "add_stability":
        t = _tag(a.get("target")) or "ROOT"
        n = _num(a.get("amount"), 0.1)
        if _is_player(t):
            return _r([f"st {int(round(n * 100))}"], f"Stability {int(round(n*100)):+}%")
        return _r([f"effect {t} = {{ add_stability = {n} }}"], f"Stability {n:+} to {t}")

    if name == "add_war_support":
        t = _tag(a.get("target")) or "ROOT"
        n = _num(a.get("amount"), 0.1)
        if _is_player(t):
            return _r([f"ws {int(round(n * 100))}"], f"War support {int(round(n*100)):+}%")
        return _r([f"effect {t} = {{ add_war_support = {n} }}"], f"War support {n:+} to {t}")

    if name == "add_manpower":
        t = _tag(a.get("target")) or "ROOT"
        n = _num(a.get("amount"), 50000)
        if _is_player(t):
            return _r([f"manpower {n}"], f"+{n} manpower")
        return _r([f"effect {t} = {{ add_manpower = {n} }}"], f"+{n} manpower to {t}")

    if name == "add_factories":
        t = _tag(a.get("target")) or "ROOT"
        btype = FACTORY_TYPE.get(str(a.get("kind", "military")).lower(), "arms_factory")
        n = int(_num(a.get("amount"), 2))
        # IMMEDIATO vs COSTRUZIONE: l'IA decide. instant=true -> compaiono SUBITO (es.
        # fabbriche sequestrate/donate gia' pronte); altrimenti vanno IN COSTRUZIONE e
        # maturano nel tempo (es. nuova industria costruita da zero).
        inst = "yes" if _truthy(a.get("instant")) else "no"
        word = "ready now" if inst == "yes" else "under construction (takes time)"
        return _r([f"effect {t} = {{ capital_scope = {{ add_building_construction = "
                   f"{{ type = {btype} level = {n} instant_build = {inst} }} }} }}"],
                  f"{n} {btype} in {t} — {word} [needs -debug]",
                  memory=f"{t}: {n} {btype} ({word})")

    if name == "start_plan":
        # TIMED PLAN: announce now (narrated popup); the 'payload' effects are applied
        # ONLY on completion after 'days' days (never instant).
        t = _tag(a.get("target")) or "ROOT"
        title = _clean(a.get("title", "National plan")) or "National plan"
        days = max(30, min(int(_num(a.get("days"), 150)), 365))   # max 1 anno
        payload = []
        facts = []
        bt_en = {"industrial_complex": "civilian factories", "arms_factory": "military factories",
                 "dockyard": "naval dockyards"}
        for b in (a.get("build") or []):
            bt = FACTORY_TYPE.get(str(b.get("kind", "military")).lower(), "arms_factory")
            amt = int(_num(b.get("amount"), 1))
            payload.append(f"effect {t} = {{ capital_scope = {{ add_building_construction = "
                           f"{{ type = {bt} level = {amt} instant_build = yes }} }} }}")
            facts.append(f"{amt} {bt_en.get(bt, bt)}")
        for eff in (a.get("effects") or []):
            e = _sanitize_effect(eff)   # discard unsafe/malformed effects
            if not e:
                continue
            payload.append(f"effect {t} = {{ {e} }}")
            tr = _describe_effect(e)
            if tr:
                facts.append(tr)
        # NATURAL-LANGUAGE description: use the AI's 'summary' if provided, otherwise the
        # translated list (never the raw command syntax)
        summary = _clean(a.get("summary", ""))
        if summary:
            facts_str = summary
        else:
            facts_str = ("In short: " + "; ".join(facts) + ".") if facts else ""
        stages = max(0, min(int(_num(a.get("stages"), 0)), 8))  # crossroads: 0=none, max 8
        # AZIONI DI ESITO: catalogo (es. golpe riuscito -> regime_change/set_leader/
        # rename_country) eseguite AL COMPLETAMENTO del piano, oltre a build/effects.
        final_actions = [x for x in (a.get("outcome") or a.get("final_actions") or [])
                         if isinstance(x, dict) and x.get("name")]
        return _r([], f"Plan '{title}' started ({days} days"
                      + (f", {stages} crossroads)" if stages else ")"),
                  memory=f"Plan: {title} ({days} days)",
                  special={"type": "plan", "title": title, "days": days, "stages": stages,
                           "payload": payload, "facts": facts_str, "target": t,
                           "final_actions": final_actions})

    # ---------- LAWS / MOBILIZATION (eval_effect, needs -debug) ----------
    if name == "set_law":
        t = _tag(a.get("target")) or "ROOT"
        law = str(a.get("law", "")).strip()
        if law not in LAWS:
            return _r([], f"Invalid law: {law}")
        return _r([f"effect {t} = {{ add_ideas = {law} }}"],
                  f"{t}: law {law} [needs -debug]", memory=f"{t}: law {law}")

    if name == "total_mobilization":
        t = _tag(a.get("target")) or "ROOT"
        return _r([f"effect {t} = {{ ai_diplo_total_mobilization = yes }}"],
                  f"{t}: total mobilization [needs -debug]",
                  memory=f"{t}: total mobilization")

    if name == "war_production_boost":
        t = _tag(a.get("target")) or "ROOT"
        return _r([f"effect {t} = {{ ai_diplo_war_production_boost = yes }}"],
                  f"{t}: war production boost [needs -debug]")

    if name == "fortify_borders":
        t = _tag(a.get("target")) or "ROOT"
        return _r([f"effect {t} = {{ ai_diplo_fortify_borders = yes }}"],
                  f"{t}: defensive fortification [needs -debug]")

    # ---------- REGIME / CIVIL WAR ----------
    if name == "regime_change":
        t = _tag(a.get("target")) or "ROOT"
        ideo = str(a.get("ideology", "")).lower()
        if ideo not in IDEOLOGIES:
            return _r([], f"Invalid ideology: {ideo}")
        return _r([f"effect {t} = {{ ai_diplo_regime_change_{ideo} = yes }}"],
                  f"{t}: regime change -> {ideo} [needs -debug]",
                  memory=f"{t}: regime {ideo}")

    if name == "set_leader":
        # Cambia il CAPO DI STATO. L'installazione vera avviene nell'EVENTO aidiplo.60 (lanciato
        # dal middleware via prepare_set_leader): kill_ideology_leader -> create_country_leader
        # col nome scelto -> set_politics -> set_country_leader_name = "AIDIPLO_LEADER". Il nome
        # passa SEMPRE per la CHIAVE di loc AIDIPLO_LEADER (scritta col nome scelto e ricaricata):
        # cosi' il capo esce col nome del player su QUALSIASI nazione. NB: mai rinominare col nome
        # GREZZO da console -> set_country_leader_name vuole una chiave, un nome non-chiave = vuoto.
        # Qui costruiamo solo il "special" con nome+ideologia: ci pensa main.py a chiamare l'evento.
        t = _tag(a.get("target")) or "ROOT"
        nm = _clean(a.get("name", "")).replace('"', "'")
        ideo = str(a.get("ideology", "neutrality")).lower()
        if ideo not in IDEOLOGIES:
            ideo = "neutrality"
        if not nm:
            return None
        ideo_num = {"fascism": 1, "communism": 2, "democratic": 3, "neutrality": 4}[ideo]
        extra_lines = []
        char_facts = []
        for eff in (a.get("effects") or []):
            se = _sanitize_effect(eff)
            if not se:
                continue
            # WHITELIST: solo bonus country-scope sicuri. Cosi' un effetto inventato/scope-errato
            # nel campo 'effects' non manda in "errore di compilazione" l'invio dell'evento.
            kw = re.split(r"[\s=]", se, 1)[0]
            if kw not in SAFE_LEADER_EFFECTS:
                continue
            extra_lines.append(f"effect {t} = {{ {se} }}")
            d = _describe_effect(se)
            if d:
                char_facts.append(d)
        desc = _clean(a.get("desc", ""))
        tail = (" — " + desc) if desc else ""
        if char_facts:
            tail += " (" + "; ".join(char_facts) + ")"
        return _r([], f"New head of state '{nm}' ({ideo}) in {t}{tail}",
                  memory=f"{t}: new head of state {nm} ({ideo}){(' — ' + desc) if desc else ''}",
                  special={"type": "setleader", "name": nm, "ideo_num": ideo_num,
                           "target": t, "effects": extra_lines})

    if name == "start_interactive":
        # COMPATIBILITA': se l'IA usa ancora start_interactive, NON apriamo un canale a se'
        # (gli eventi devono nascere SOLO dai bivi in gioco). Lo convertiamo in un PIANO con
        # bivi (start_plan): cosi' compare come evento in gioco con le 3 opzioni, e la 3a
        # ('scrivi tu') apre il canale evento. Un'impresa come un golpe -> almeno 5 bivi.
        t = _tag(a.get("target")) or "ROOT"
        title = _clean(a.get("title", "")) or "Developing Situation"
        situation = _clean(a.get("situation", "")) or _clean(a.get("desc", ""))
        if not situation:
            return None
        days = max(60, min(int(_num(a.get("days"), 180)), 365))
        final_actions = [x for x in (a.get("outcome") or a.get("final_actions") or [])
                         if isinstance(x, dict) and x.get("name")]
        return _r([], f"Plan '{title}' started ({days} days, 5 crossroads)",
                  memory=f"Plan: {title} ({days} days)",
                  special={"type": "plan", "title": title, "days": days, "stages": 5,
                           "payload": [], "facts": situation, "target": t,
                           "final_actions": final_actions})

    if name == "rename_country":
        # Changes the displayed NAME of the nation (dynamic cosmetic tag).
        t = _tag(a.get("target")) or "ROOT"
        nm = _clean(a.get("name", ""))
        if not nm:
            return None
        return _r([], f"The nation {t} is now called '{nm}'",
                  memory=f"{t} renamed '{nm}'",
                  special={"type": "rename", "name": nm, "target": t})

    if name == "start_civil_war":
        t = _tag(a.get("target")) or "ROOT"
        ideo = str(a.get("ideology", "")).lower()
        if ideo not in ("fascism", "communism", "democratic", "neutrality"):
            return _r([], f"Invalid civil war ideology: {ideo}")
        leader = _clean(a.get("leader", "")).replace('"', "'")
        # SPECIAL: the middleware lets the player choose which side to take, and (if
        # given) creates a named leader for the new rebel regime.
        return _r([], f"{ideo} civil war in {t}",
                  memory=f"{ideo} civil war in {t}",
                  special={"type": "civilwar", "ideology": ideo, "leader": leader, "target": t})

    # ---------- FACTIONS / DIPLOMACY ----------
    if name == "form_faction":
        t = _tag(a.get("target")) or "ROOT"
        nm = _clean(a.get("name", "")) or "Pact"
        return _r([f"effect {t} = {{ create_faction = \"{nm}\" }}"],
                  f"{t} forms the faction '{nm}' [needs -debug]", memory=f"{t} forms faction {nm}")

    if name == "add_to_faction":
        leader = _tag(a.get("faction_leader") or a.get("leader")) or "ROOT"
        member = _tag(a.get("member"))
        return _act_event(5, leader, member, "New alliance",
                          memory=f"{member} allies with {leader}")

    if name in ("non_aggression_pact", "guarantee", "military_access"):
        x = _tag(a.get("country_a") or a.get("target")) or "ROOT"
        y = _tag(a.get("country_b") or a.get("with"))
        meta = {"non_aggression_pact": (4, "Non-aggression pact"),
                "guarantee": (6, "Guarantee of independence"),
                "military_access": (7, "Military access")}[name]
        return _act_event(meta[0], x, y, meta[1], memory=f"{name} {x}-{y}")

    if name in ("improve_relations", "worsen_relations", "reduce_relations", "damage_relations"):
        x = _tag(a.get("country_a") or a.get("target")) or "ROOT"
        y = _tag(a.get("country_b") or a.get("with"))
        # gli alias *worsen/reduce/damage* implicano relazioni NEGATIVE per default
        default_pos = name == "improve_relations"
        positive = a.get("positive", default_pos)
        if not y:
            return None
        # direct add_opinion: only player<->tag and only +100
        if positive and _is_player(x) and not _is_player(y):
            return _r([f"add_opinion {y}"], f"+opinion with {y}")
        if positive and _is_player(y) and not _is_player(x):
            return _r([f"add_opinion {x}"], f"+opinion with {x}")
        mod = "ai_diplo_goodwill" if positive else "ai_diplo_distrust"
        # EFFETTO corretto = add_opinion_modifier (add_opinion e' SOLO un comando console,
        # non un effetto: come effetto non compila). Il modifier e' definito nella mod.
        verb = "improves" if positive else "worsens"
        return _r([f"effect {x} = {{ add_opinion_modifier = {{ target = {y} modifier = {mod} }} }}"],
                  f"{x} {verb} relations with {y} [needs -debug]",
                  memory=f"{x} {verb} relations with {y}")

    # ---------- WAR / TERRITORY ----------
    if name == "declare_war":
        att = _tag(a.get("attacker") or a.get("target")) or "ROOT"
        deff = _tag(a.get("defender") or a.get("against") or a.get("victim"))
        if not deff:
            return None
        return _act_event(1, att, deff, "Declaration of war",
                          memory=f"{att} at war with {deff}")

    if name == "create_wargoal":
        att = _tag(a.get("attacker") or a.get("target")) or "ROOT"
        deff = _tag(a.get("defender") or a.get("against"))
        if not deff:
            return None
        wtype = str(a.get("wargoal", "annex_everything"))
        return _r([f"effect {att} = {{ create_wargoal = "
                   f"{{ type = {wtype} target = {deff} }} }}"],
                  f"{att}: war goal on {deff} [needs -debug]")

    if name == "white_peace":
        x = _tag(a.get("country_a") or a.get("target")) or "ROOT"
        y = _tag(a.get("country_b") or a.get("with"))
        if not y:
            return None
        if _is_player(x):
            return _r([f"wp {y}"], f"White peace with {y}", memory=f"White peace with {y}")
        if _is_player(y):
            return _r([f"wp {x}"], f"White peace with {x}", memory=f"White peace with {x}")
        return _r([f"effect {x} = {{ white_peace = {y} }}"], f"White peace {x}-{y} [needs -debug]")

    if name == "transfer_state":
        rec = _tag(a.get("receiver") or a.get("to") or a.get("target")) or "ROOT"
        sid = _state(a.get("state_id") or a.get("state") or a.get("region") or a.get("name"))
        if sid is None:
            return _r([], "Cannot transfer: unknown state (give a state NAME shown in KNOWN "
                          "STATES, or its numeric ID)")
        return _r([f"effect {rec} = {{ transfer_state = {sid} }}"],
                  f"State #{sid} -> {rec} [needs -debug]", memory=f"State #{sid} to {rec}")

    if name == "annex":
        ann = _tag(a.get("annexer") or a.get("target")) or "ROOT"
        tgt = _tag(a.get("victim") or a.get("country"))
        return _act_event(2, ann, tgt, "Territorial annexation",
                          memory=f"{ann} annexes {tgt}")

    if name == "puppet":
        ovr = _tag(a.get("overlord") or a.get("target")) or "ROOT"
        sub = _tag(a.get("subject"))
        return _act_event(3, ovr, sub, "Puppet state",
                          memory=f"{sub} puppet of {ovr}")

    if name in ("end_puppet", "free_from_puppet", "free_subject",
                "grant_independence", "end_subject"):
        # FINE VASSALLAGGIO: l'effetto end_puppet va eseguito nello scope dell'OVERLORD
        # (il padrone), sul SUBJECT. Di norma il player e' il subject che ottiene liberta'
        # (subject=ROOT) e l'overlord e' la nazione che lo controlla.
        ovr = _tag(a.get("overlord") or a.get("master") or a.get("from")
                   or a.get("of") or a.get("ruler"))
        sub = _tag(a.get("subject") or a.get("target") or a.get("who")) or "ROOT"
        if not ovr or ovr == sub:
            return _r([], "Cannot end puppet status: overlord (the master nation) not specified")
        # actor = overlord (esegue l'evento e quindi end_puppet); target = subject liberato
        return _act_event(8, ovr, sub, "Independence granted",
                          memory=f"{sub} freed from {ovr}'s puppet/subject status")

    if name in ("embargo", "trade_embargo"):
        # diplomatic_relation e' scope-sensitive a runtime -> via evento dispatcher (act 9)
        x = _tag(a.get("country_a") or a.get("target")) or "ROOT"
        y = _tag(a.get("country_b") or a.get("against") or a.get("with") or a.get("on"))
        if not y:
            return None
        return _act_event(9, x, y, "Trade embargo", memory=f"{x} embargoes {y}")

    # ---------- POLITICS ----------
    if name in ("add_popularity", "boost_party"):
        t = _tag(a.get("target")) or "ROOT"
        ideo = str(a.get("ideology") or a.get("party", "")).lower()
        if ideo not in IDEOLOGIES:
            return _r([], f"Invalid ideology for popularity: {ideo}")
        amt = _num(a.get("amount") or a.get("popularity"), 0.1)
        return _r([f"effect {t} = {{ add_popularity = {{ ideology = {ideo} popularity = {amt} }} }}"],
                  f"{t}: {ideo} popularity {amt:+} [needs -debug]",
                  memory=f"{t}: {ideo} popularity {amt:+}")

    # ---------- TERRITORY (states) ----------
    if name in ("add_core", "add_state_core"):
        t = _tag(a.get("target")) or "ROOT"
        sid = _state(a.get("state_id") or a.get("state") or a.get("region") or a.get("name"))
        if sid is None:
            return _r([], "Unknown state — give a state NAME from KNOWN STATES, or its numeric ID")
        return _r([f"effect {t} = {{ add_state_core = {int(sid)} }}"],
                  f"State {int(sid)} becomes a core of {t} [needs -debug]",
                  memory=f"{t} cores state {int(sid)}")

    if name in ("add_claim", "add_state_claim"):
        t = _tag(a.get("target")) or "ROOT"
        sid = _state(a.get("state_id") or a.get("state") or a.get("region") or a.get("name"))
        if sid is None:
            return _r([], "Unknown state — give a state NAME from KNOWN STATES, or its numeric ID")
        return _r([f"effect {t} = {{ add_state_claim = {int(sid)} }}"],
                  f"{t} claims state {int(sid)} [needs -debug]",
                  memory=f"{t} claims state {int(sid)}")

    if name in ("occupy_state", "set_state_controller"):
        t = _tag(a.get("target")) or "ROOT"
        sid = _state(a.get("state_id") or a.get("state") or a.get("region") or a.get("name"))
        if sid is None:
            return _r([], "Unknown state — give a state NAME from KNOWN STATES, or its numeric ID")
        return _r([f"effect {t} = {{ set_state_controller = {int(sid)} }}"],
                  f"{t} takes control of state {int(sid)} [needs -debug]",
                  memory=f"{t} controls state {int(sid)}")

    if name in ("release_nation", "release_country", "free_nation"):
        # Crea una NAZIONE non esistente dai propri territori: libera (release_on_controlled)
        # o come fantoccio (release_puppet). NB: non fa nulla se il tag esiste gia'.
        owner = _tag(a.get("owner") or a.get("from") or a.get("target")) or "ROOT"
        rel = _tag(a.get("country") or a.get("nation") or a.get("who") or a.get("subject"))
        if not rel:
            return None
        as_pup = bool(a.get("as_puppet") or a.get("puppet"))
        eff = f"release_puppet = {rel}" if as_pup else f"release_on_controlled = {rel}"
        kind = "puppet" if as_pup else "free nation"
        return _r([f"effect {owner} = {{ {eff} }}"],
                  f"{owner} releases {rel} as a {kind} [needs -debug]",
                  memory=f"{owner} releases {rel} as {kind}")

    if name == "set_capital":
        t = _tag(a.get("target")) or "ROOT"
        sid = _state(a.get("state_id") or a.get("state") or a.get("region") or a.get("name"))
        if sid is None:
            return _r([], "Unknown state — give a state NAME from KNOWN STATES, or its numeric ID")
        return _r([f"effect {t} = {{ set_capital = {{ state = {int(sid)} }} }}"],
                  f"{t}: capital moved to state {int(sid)} [needs -debug]",
                  memory=f"{t}: capital -> state {int(sid)}")

    # ---------- FACTIONS (leave / dismantle) ----------
    if name in ("leave_faction", "exit_faction"):
        t = _tag(a.get("target")) or "ROOT"
        return _r([f"effect {t} = {{ leave_faction = yes }}"],
                  f"{t} leaves its faction [needs -debug]", memory=f"{t} leaves its faction")

    if name in ("dismantle_faction", "disband_faction"):
        t = _tag(a.get("target")) or "ROOT"
        return _r([f"effect {t} = {{ dismantle_faction = yes }}"],
                  f"{t} dismantles its faction [needs -debug]",
                  memory=f"{t} dismantles its faction")

    # ---------- MILITARY / SCIENCE ----------
    if name in ("add_nukes", "nukes"):
        t = _tag(a.get("target")) or "ROOT"
        n = int(_num(a.get("amount") or a.get("quantity"), 1))
        if _is_player(t):
            return _r([f"nuke {n}"], f"+{n} nuclear bombs", memory=f"+{n} nukes")
        return _r([f"effect {t} = {{ add_nukes = {n} }}"],
                  f"+{n} nukes to {t} [needs -debug]", memory=f"{t}: +{n} nukes")

    if name in ("add_research_slot", "research_slot"):
        t = _tag(a.get("target")) or "ROOT"
        n = int(_num(a.get("amount"), 1))
        return _r([f"effect {t} = {{ add_research_slot = {n} }}"],
                  f"{t}: +{n} research slot(s) [needs -debug]",
                  memory=f"{t}: +{n} research slot")

    if name in ("create_general", "appoint_general", "create_commander"):
        t = _tag(a.get("target")) or "ROOT"
        nm = _clean(a.get("name", "")).replace('"', "'")
        if not nm:
            return None
        role = str(a.get("role") or a.get("type") or "general").lower()
        fm = role in ("field_marshal", "fieldmarshal", "marshal", "field marshal")
        sk = max(1, min(int(_num(a.get("skill"), 3)), 9))
        eff_name = "create_field_marshal" if fm else "create_corps_commander"
        title = "field marshal" if fm else "general"
        block = (f"{eff_name} = {{ name = \"{nm}\" skill = {sk} attack_skill = {sk} "
                 f"defense_skill = {sk} planning_skill = {sk} logistics_skill = {sk} }}")
        return _r([f"effect {t} = {{ {block} }}"],
                  f"{t}: new {title} '{nm}' (skill {sk}) [needs -debug]",
                  memory=f"{t}: appointed {title} {nm}")

    # ---------- RESOURCES / BUILDINGS / ECONOMY ----------
    if name in ("add_resource", "add_resources"):
        t = _tag(a.get("target")) or "ROOT"
        res = str(a.get("resource") or a.get("type") or a.get("kind", "")).lower().strip()
        res = RESOURCE_ALIAS.get(res, res)
        amt = int(_num(a.get("amount") or a.get("quantity"), 8))
        if res not in RESOURCES:
            return _r([], f"Unknown resource '{res}' (HoI4 resources: "
                          "oil, steel, aluminium, tungsten, chromium, rubber)")
        # SCALA REALISTICA: in HoI4 anche la nazione PIU' ricca di una risorsa ne ha solo
        # qualche centinaio in TUTTO il paese (oil ~400, steel ~340, coal ~430, rubber ~250,
        # chromium ~210, aluminium ~125, tungsten ~120). Una singola azione su una regione
        # deve dunque aggiungere/togliere POCO: lo limitiamo a [-40, +40] (una grande
        # scoperta = ~30-40; un boost modesto = ~5-15). Evita aberrazioni tipo +5000.
        amt = max(-40, min(amt, 40))
        if amt == 0:
            amt = 8
        return _r([f"effect {t} = {{ capital_scope = {{ add_resource = "
                   f"{{ type = {res} amount = {amt} }} }} }}"],
                  f"{t}: {amt:+} {res} in the capital region [needs -debug]",
                  memory=f"{t}: {amt:+} {res}")

    if name in ("add_building", "build_building", "construct"):
        t = _tag(a.get("target")) or "ROOT"
        b = str(a.get("building") or a.get("type") or a.get("kind", "")).lower().strip()
        b = BUILDING_ALIAS.get(b, b)
        lvl = max(1, min(int(_num(a.get("level") or a.get("amount"), 1)), 10))
        if b not in BUILDINGS:
            return _r([], f"Unknown building '{b}' (e.g. arms_factory, industrial_complex, "
                          "dockyard, synthetic_refinery, fuel_silo, infrastructure, "
                          "anti_air_building, radar_station, rocket_site, nuclear_reactor, air_base)")
        inst = "yes" if _truthy(a.get("instant")) else "no"
        word = "ready now" if inst == "yes" else "under construction (takes time)"
        return _r([f"effect {t} = {{ capital_scope = {{ add_building_construction = "
                   f"{{ type = {b} level = {lvl} instant_build = {inst} }} }} }}"],
                  f"{t}: {lvl}x {b} in the capital region — {word} [needs -debug]",
                  memory=f"{t}: +{lvl} {b} ({word})")

    if name in ("add_command_power", "command_power"):
        t = _tag(a.get("target")) or "ROOT"
        n = int(_num(a.get("amount"), 50))
        return _r([f"effect {t} = {{ add_command_power = {n} }}"],
                  f"{t}: command power {n:+} [needs -debug]", memory=f"{t}: command power {n:+}")

    if name in ("add_experience", "military_experience", "add_xp", "experience"):
        # esperienza militare (pool): army/navy/air. Es. potenze che mandano addestratori,
        # manovre, veterani. Importi contenuti (un grande aiuto ~ 50-100; un cap a 300).
        t = _tag(a.get("target")) or "ROOT"
        kind = str(a.get("kind") or a.get("branch") or a.get("type") or "army").lower()
        n = max(-300, min(int(_num(a.get("amount"), 25)), 300))
        fld = {"army": "army_experience", "land": "army_experience",
               "navy": "navy_experience", "naval": "navy_experience", "sea": "navy_experience",
               "fleet": "navy_experience", "air": "air_experience", "airforce": "air_experience"}
        if kind in ("all", "military", "mil", "everything"):
            block = f"army_experience = {n} navy_experience = {n} air_experience = {n}"
            lbl = "army/navy/air"
        else:
            f = fld.get(kind, "army_experience")
            block = f"{f} = {n}"
            lbl = f.split("_")[0]
        return _r([f"effect {t} = {{ {block} }}"],
                  f"{t}: {lbl} experience {n:+} [needs -debug]", memory=f"{t}: {lbl} XP {n:+}")

    if name in ("add_timed_idea", "add_idea", "national_spirit", "add_modifier"):
        # spirito/idea nazionale TEMPORANEO (o legge): SOLO idee esistenti nella mod/vanilla,
        # altrimenti l'effetto fa fallire l'intero blocco. add_timed_idea come AZIONE a se'
        # (prima veniva ignorata perche' non era nel catalogo).
        t = _tag(a.get("target")) or "ROOT"
        idea = str(a.get("idea") or a.get("name") or a.get("modifier", "")).strip()
        days = int(_num(a.get("days"), 180))
        if idea not in KNOWN_IDEAS:
            return _r([], f"Unknown idea '{idea}' (use only ai_diplo_war_drive, "
                          "ai_diplo_reform_strain, ai_diplo_fortified, or a valid law)")
        if idea in LAWS:
            return _r([f"effect {t} = {{ add_ideas = {idea} }}"],
                      f"{t}: enacted {idea} [needs -debug]", memory=f"{t}: idea {idea}")
        return _r([f"effect {t} = {{ add_timed_idea = {{ idea = {idea} days = {days} }} }}"],
                  f"{t}: national spirit {idea} for {days} days [needs -debug]",
                  memory=f"{t}: timed spirit {idea} ({days}d)")

    if name in ("mobilize_army", "reinforce_army", "raise_divisions", "mobilize", "spawn_units"):
        # HoI4 non puo' materializzare divisioni "pronte" da console in modo affidabile (serve
        # un template); quindi MOBILITIAMO uomini + equipaggiamento per schierare nuove unita'.
        t = _tag(a.get("target")) or "ROOT"
        d = max(1, min(int(_num(a.get("divisions") or a.get("amount"), 5)), 50))
        inf = d * 100
        mp = d * 1000
        prod = "ROOT" if _is_player(t) else t
        return _r([f"effect {t} = {{ add_equipment_to_stockpile = "
                   f"{{ type = infantry_equipment amount = {inf} producer = {prod} }} "
                   f"add_manpower = {mp} }}"],
                  f"{t}: mobilising ~{d} divisions' worth of men and equipment [needs -debug]",
                  memory=f"{t}: mobilised ~{d} divisions")

    # ---------- SPECIALS ----------
    if name == "reject":
        return _r([], "Proposal rejected by the AI",
                  special={"type": "reject", "reason": a.get("reason", "")})

    if name == "dynamic_event":
        return _r([], "Dynamic in-game event",
                  memory=f"Event: {a.get('title','')}",
                  special={"type": "event", "title": a.get("title", "Development"),
                           "desc": a.get("desc", ""),
                           "option_a": a.get("option_a", "So be it."),
                           "option_b": a.get("option_b", "")})

    return None


def catalog_text():
    if GAME_PROFILE == TFR:
        return TFR_CATALOG
    if GAME_PROFILE == CWIC_DEV:
        return CWIC_CATALOG
    if GAME_PROFILE in (MD, MD_OEM):
        return MD_CATALOG
    return """\
ACTION CATALOG ("actions" field). Use ONLY these names. The "target" values are 3-letter
tags (GER, SOV, USA, ENG, FRA, ITA, JAP, POL, YUG...) or ROOT (the player).
Fractions (stability/war support) from 0 to 1 (e.g. 0.1 = 10%).
NUMBERS: every numeric field (days, amount, quantity, stages...) MUST be a plain number
like 180 or 0.1 — NEVER a placeholder, a unit or text (e.g. do NOT write "px", "N", "few").
PREFER the dedicated actions below over raw_effect. Do NOT invent HoI4 effect names inside
raw_effect — unknown effects fail to compile and do nothing (e.g. there is NO
"add_trade_opinion_modifier"; to embargo use the 'embargo' action, to change opinion use
'improve_relations'/'worsen_relations').

- send_equipment {receiver, equipment_type, quantity, sender}
    Transfers military equipment. equipment_type can be ANY HoI4 kind — match the request
    EXACTLY: support, artillery, anti_tank, anti_air, motorized, mechanized, light_tanks,
    medium_tanks, heavy_tanks, super_heavy_tanks, modern_tanks, fighters, heavy_fighters, cas,
    tactical_bombers, strategic_bombers, naval_bombers, scout_plane, transport_plane, convoys,
    rifles (infantry). If the player asks for "support equipment", set equipment_type=support
    (NOT rifles). 'sender' = the donor nation (when YOU, an AI nation, give equipment to the
    player or an ally, set sender to YOUR tag): the amount is then ACTUALLY DEDUCTED from the
    sender's stockpile. DOSE the 'quantity' proportionally to the sender's industrial strength
    AND to how important the cause/relationship is: a token amount for a minor ally, substantial
    aid only for a vital ally in a decisive war — never give away the bulk of one's arsenal to
    an insignificant cause.
- add_political_power {target, amount}
- add_stability {target, amount}
- add_war_support {target, amount}
- add_manpower {target, amount}
- add_factories {target, kind(civilian|military|naval), amount, instant(true/false)}
    instant=true -> factories are READY NOW (use for seized/donated, already-built plants);
    instant=false/omitted -> they go UNDER CONSTRUCTION and take time (new industry built from
    scratch). Pick the right one; do NOT also emit add_building for the SAME factories.
- start_plan {title, days, stages, summary, build:[{kind, amount}], effects:[<HoI4 effects>],
    outcome:[{name, args}]}
    TIMED PLAN for big reforms/industrializations AND multi-stage undertakings (coups,
    operations): announce immediately (narrated popup) but it resolves ONLY after 'days'
    GAME DAYS (never instant; max 365 = one year). YOU decide 'days' plausibly. Keep 'title'
    SHORT (a few words, fits an event header; no year ranges).
    PUT the maturing effects in build + effects. 'summary': NATURAL-LANGUAGE description.
    'stages' (0-8): intermediate CROSSROADS shown in game (2 preset options + a "write your
    own" one). Use stages>0 for important undertakings; for a COUP use at least 5.
    'outcome': the CATALOG ACTIONS to apply WHEN THE PLAN COMPLETES — this is where the real
    result goes. For a successful coup put here e.g. regime_change / set_leader /
    rename_country / start_civil_war, so ideology, leader and state name actually change at
    the end. (effects = raw stat changes; outcome = the decisive catalog actions.)
- set_law {target, law}  (e.g. all_adults_serve, war_economy ...)
- total_mobilization {target}
- war_production_boost {target}
- fortify_borders {target}
- regime_change {target, ideology(...)}  changes the ruling ideology. If the player gives
    a precise NAME to the new leader, use set_leader instead.
- set_leader {target, name, ideology, desc, effects:[<HoI4 effects>]}
    Changes the HEAD OF STATE and ALWAYS installs a genuinely NEW ruler in game — even when
    the ideology stays THE SAME (e.g. a fascist coup replacing Mussolini with another fascist
    leader: the game leader really changes, not just the name). It creates and promotes a new
    country leader for the chosen ideology and aligns the ruling party. ALWAYS use set_leader
    (NOT regime_change) whenever the leader/head of state is replaced — coups, successions,
    "I install X as the new leader" — giving the new leader's 'name'. CRITICAL: do NOT change the
    ideology unless the player EXPLICITLY asks for a regime/ideology change. For a plain leader or
    name change, pass the player's CURRENT ideology (the person still changes, the regime does
    NOT). Only pass a different ideology when the player clearly wants a coup/regime shift. The
    'name' is the REAL
    in-game leader name (it actually changes in the game), so ALWAYS pass the player's chosen
    name and use it in your narration. This applies to EVERY head-of-state change INCLUDING ones
    that change ideology or happen via a coup: still emit set_leader with the chosen 'name' (and
    the new 'ideology'), otherwise the new ruler appears WITHOUT the player's name.
    In 'effects', the character
    BONUSES/MALUSES PROPORTIONATE to ideology and description, using DIRECT effects that
    always exist: add_war_support = ±0.x, add_stability = ±0.x, add_political_power = ±N,
    add_manpower = ±N (e.g. a fanatical general -> positive add_war_support and negative
    add_stability; a skilled statesman -> add_political_power). For temporary modifiers use
    ONLY existing ideas (ai_diplo_war_drive, ai_diplo_reform_strain, ai_diplo_fortified): do
    NOT invent idea names. 'desc' = short description. ALWAYS use set_leader when the player
    names a new leader, and write the "message"/narration referring to that leader by name.
- rename_country {target, name}  changes the displayed NAME of the nation (e.g. a new
    regime proclaiming 'Theocratic State of Palestine'). Use it when the player renames the
    state; typically TOGETHER WITH set_leader/regime_change in a coup.
- start_civil_war {target, ideology, leader}  triggers a civil war: the player chooses
    which side to take (lead the revolutionaries or defend the government). ALWAYS pass the
    'leader' name the player chose for the new regime — it becomes the REAL in-game leader name
    (created on the player's victory). Do NOT always use it: for a coup that succeeds without war,
    use set_leader instead (see rules).
- start_interactive {title, situation, kind}  starts a free-text INTERACTIVE EVENT in a
    centre window (NO preset choices): use it for big/delicate undertakings that should take
    PLANNING and TIME instead of happening instantly — e.g. the player wants to orchestrate
    a coup, run an intelligence operation, defuse a crisis, negotiate a complex deal. Do NOT
    execute such things at once: start_interactive with a vivid opening 'situation', and the
    player will write how they proceed across several beats (the middleware drives it).
- form_faction {target, name}
- add_to_faction {faction_leader, member}
- non_aggression_pact {country_a, country_b}
- guarantee {country_a, country_b}
- military_access {country_a, country_b}
- improve_relations {country_a, country_b, positive(true/false)}  raise (or, with
    positive=false, lower) the opinion between two countries.
- worsen_relations {country_a, country_b}  lowers opinion between two countries (alias of
    improve_relations with positive=false). Use this for "we don't like you" / cooling ties.
- declare_war {attacker, defender, wargoal}
- create_wargoal {attacker, defender, wargoal}
- white_peace {country_a, country_b}
STATES (regions): the WORLD STATE context lists KNOWN STATES as Name=#ID. For any state
action you may give the state by NAME (e.g. "Rome") or by its numeric ID (e.g. 2) — the
system resolves the name. Do NOT guess IDs that are not in the list.
- transfer_state {receiver, state(name or #id)}  give that region/state to 'receiver'.
- annex {annexer, victim}
- puppet {overlord, subject}
- end_puppet {overlord, subject}  ENDS an EXISTING puppet/subject relationship: the OVERLORD
    (the master nation, e.g. ENG) grants FULL INDEPENDENCE to the SUBJECT. Use whenever a
    puppet/satellite stops being a subject — e.g. the player (usually subject=ROOT) is freed
    by their master, or the player (overlord) frees a satellite. 'overlord' = the master
    nation; 'subject' = who becomes free (default ROOT).
- release_nation {owner, country, as_puppet(true/false)}  Creates a NON-EXISTENT nation out
    of 'owner's territory — as a free nation (default) or as a puppet (as_puppet=true). Use
    for decolonization, liberating an occupied people, carving out a new state. 'country' =
    the tag/name to release (must not already exist).
- embargo {country_a, country_b}  country_a imposes a trade embargo on country_b AND worsens
    their relations automatically. This is the ONLY correct way to embargo — do NOT use
    raw_effect for it. (For "we dislike you" without a trade ban, use worsen_relations.)
- add_popularity {target, ideology(fascism|communism|democratic|neutrality), amount}
    Shifts a party's popularity (amount -1..1, e.g. 0.2 = +20%). Use for political drift,
    propaganda, a rising movement.
- add_core {target, state(name or #id)}    target gains a CORE on a state (rightful land).
- add_claim {target, state(name or #id)}   target gains a CLAIM on a state (war-goal basis).
- occupy_state {target, state(name or #id)} target takes military CONTROL of a state.
- set_capital {target, state(name or #id)}  moves target's capital to one of ITS OWN states.
- leave_faction {target}             target leaves its current faction/alliance.
- dismantle_faction {target}         target disbands the faction it leads.
- add_nukes {target, amount}         adds nuclear bombs to a nation's arsenal.
- add_research_slot {target, amount} grants extra research slots — use for new research
    centres, recruiting scientists, scientific breakthroughs. HARD CAP: the mod grants at most
    4 research slots to a given nation per game (further grants are ignored), so use sparingly
    and only when truly justified.
- add_experience {target, kind(army|navy|air|all), amount} adds (or removes, negative) MILITARY
    EXPERIENCE to a branch's pool. Use when it makes sense — e.g. a powerful nation sends
    trainers/advisors, large-scale manoeuvres, veterans returning, a training mission. Modest
    amounts (a big help ~50-100; cap 300).
- add_resource {target, resource, amount}  adds (or, negative, removes) a STRATEGIC RESOURCE.
    Valid resources ONLY: oil, steel, aluminium, tungsten, chromium, rubber. Use THIS (not
    factories!) for raw materials / mines / deposits ("find coal" -> steel; "more oil/fuel" ->
    oil; "rubber plantations" -> rubber). SCALE: HoI4 amounts are SMALL — even the richest
    nation has only a few HUNDRED of a resource in the WHOLE country. So one action adds a
    MODEST amount: ~5-15 for a normal boost, ~30-40 for a MAJOR discovery (hard cap 40). Never
    hundreds or thousands.
- add_building {target, building, level, instant(true/false)}  a STATE building. instant=true
    -> ready now; otherwise under construction (matures over time). Types:
    arms_factory, industrial_complex, dockyard, synthetic_refinery (oil from coal), fuel_silo,
    infrastructure, anti_air_building, radar_station, rocket_site, nuclear_reactor, air_base.
    Use for "refinery / radar / airfield / AA / reactor / roads", etc. (add_factories is the
    shortcut for plain factories.)
- add_command_power {target, amount}  changes command power (for army abilities/orders).
- add_timed_idea {target, idea, days}  applies a TEMPORARY national spirit. Use ONLY existing
    ideas: ai_diplo_war_drive (war effort), ai_diplo_reform_strain (the cost of reforms:
    lowers PP gain/stability/build speed for a while), ai_diplo_fortified (defensive). This is
    the way to apply a lasting cost/bonus over time (do NOT invent idea names).
- mobilize_army {target, divisions}   raises new forces: grants the men + equipment for ~N
    divisions (use for "raise/mobilise troops", army build-up). NOTE: the engine cannot
    instantly place ready templated divisions from the console, so this supplies what is
    needed to field them.
- create_general {target, name, role(general|field_marshal), skill(1-9)}  appoints a named
    commander (e.g. a famous general brought in to lead the army).
- raw_effect {target, effect}   (any HoI4 effect on one line). LAST RESORT only. For
    buildings/resources/equipment do NOT use raw_effect (it is REJECTED, and the scope is easy
    to get wrong) — use add_building / add_resource / add_factories / send_equipment instead.
    For a timed national spirit use add_timed_idea, not raw_effect.
- raw_console {command}          (any HoI4 console command)
- reject {reason}                (for anachronistic/impossible requests)
- dynamic_event {title, desc, option_a, option_b}
"""
