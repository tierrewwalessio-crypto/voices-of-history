"""
main.py - ORCHESTRATORE del middleware HOI4 AI Diplomacy
Ciclo a tre vie:
  HoI4 (game.log)  ->  questo middleware (+ memoria + Player2)  ->  HoI4 (console)

- Un thread "tailer" legge in tempo reale il game.log di HoI4.
- Le righe AIDIPLO_STATE aggiornano il contesto del mondo.
- Le righe AIDIPLO_OPEN / AIDIPLO_DECREE_OPEN aprono il canale di chat giusto.
- Quando il giocatore scrive, chiamiamo Player2, leggiamo il JSON di risposta,
  mostriamo il messaggio e ESEGUIAMO le azioni nel gioco via console.
"""
import os
import argparse
import sys
import re
import json
import queue
import threading
import time

import game_state as gs
import telemetry
import functions
from llm_client import LLMClient
from providers import configured_client
from memory import Memory
from injector import Injector
from overlay import ChatOverlay
import prompts
from profiles import TFR, request_instructions
from profile_lore import make_lore
from usage_budget import BudgetClient
from safe_actions import text_only, describe
from unified_runtime import UnifiedRuntime, compact_context


def _app_dir():
    """Cartella dell'applicazione: accanto all'EXE quando 'congelato' (build PyInstaller),
    altrimenti accanto allo script. Qui vivono config.json, mode.txt e memory.db (scrivibili)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resource_dir():
    """Cartella delle risorse impacchettate dentro l'EXE (PyInstaller _MEIPASS); fuori
    dall'EXE coincide con la cartella dello script. Usata come ripiego di sola lettura."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _win_documents_dirs():
    """La cartella 'Documents' REALE letta dal registro di Windows. È l'unica fonte
    affidabile quando OneDrive reindirizza i Documenti: riflette esattamente dove il
    sistema (e quindi HoI4) mette la cartella Documents dell'utente."""
    out = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            for name in ("Personal",
                         "{F42EE2D3-909F-4907-8871-4C22FC0BF756}"):  # Documents (KNOWNFOLDER)
                try:
                    val, _ = winreg.QueryValueEx(k, name)
                    p = os.path.expandvars(val)
                    if p and p not in out:
                        out.append(p)
                except OSError:
                    pass
    except Exception:
        pass
    return out


def _hoi4_doc_dirs():
    """TUTTE le possibili cartelle '.../Documents' (Documenti reale da registro + varianti
    OneDrive + nome localizzato), in ordine di affidabilità."""
    up = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    docs, seen = [], set()

    def add(d):
        if d and d not in seen:
            seen.add(d)
            docs.append(d)

    # 1) la Documents REALE dal registro (gestisce OneDrive correttamente)
    for d in _win_documents_dirs():
        add(d)
    # 2) varianti OneDrive + profilo, con nome EN e IT
    bases = []
    for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        v = os.environ.get(env)
        if v:
            bases.append(v)
    bases += [os.path.join(up, "OneDrive"), up]
    for b in bases:
        for name in ("Documents", "Documenti"):
            add(os.path.join(b, name))
    return docs


def _all_hoi4_userdirs():
    """Tutte le cartelle utente '.../Hearts of Iron IV' candidate (una per Documents)."""
    out, seen = [], set()
    for d in _hoi4_doc_dirs():
        hoi = os.path.join(d, "Paradox Interactive", "Hearts of Iron IV")
        key = os.path.normcase(os.path.normpath(hoi))
        if key not in seen:
            seen.add(key)
            out.append(hoi)
    return out


def _best_hoi4_userdir(configured_dir=None):
    """La cartella utente di HoI4 GIUSTA, scelta automaticamente e in modo robusto:
      1) quella con il game.log PIÙ RECENTE  -> è la partita che sta girando ORA (così
         una copia vecchia in Documenti locali non vince sulla reale su OneDrive);
      2) se nessun game.log, quella con settings.txt (la cartella utente vera);
      3) altrimenti la prima cartella 'Hearts of Iron IV' esistente;
      4) come ultima spiaggia, quella configurata."""
    cands = _all_hoi4_userdirs()
    if configured_dir:
        key = os.path.normcase(os.path.normpath(configured_dir))
        if key not in {os.path.normcase(os.path.normpath(c)) for c in cands}:
            cands.insert(0, configured_dir)
    # 1) game.log più recente
    best, best_m = None, -1.0
    for hoi in cands:
        try:
            m = os.path.getmtime(os.path.join(hoi, "logs", "game.log"))
        except OSError:
            continue
        if m > best_m:
            best, best_m = hoi, m
    if best:
        return best
    # 2) settings.txt, poi 3) qualsiasi cartella HoI4 esistente
    for hoi in cands:
        if os.path.isfile(os.path.join(hoi, "settings.txt")):
            return hoi
    for hoi in cands:
        if os.path.isdir(hoi):
            return hoi
    return configured_dir


def _autodetect(cfg):
    """Individua da solo la cartella utente di HoI4 (gestendo OneDrive/Documenti localizzati)
    e ne deriva game.log, la localisation della mod e — di conseguenza — la cartella dove va
    scritto il file di comandi 'aidiplo.txt'. Salva anche la lista di candidate per l'injector."""
    conf_log = cfg["paths"].get("game_log")
    conf_dir = os.path.dirname(os.path.dirname(conf_log)) if conf_log else None
    userdir = _best_hoi4_userdir(conf_dir)
    if userdir:
        cfg["paths"]["game_log"] = os.path.join(userdir, "logs", "game.log")
        if not os.path.isfile(cfg["paths"].get("mod_localisation", "")) and not cfg.get("game_profile"):
            cfg["paths"]["mod_localisation"] = os.path.join(
                userdir, "mod", "HOI4_AI_Diplomacy", "localisation", "english",
                "ai_diplo_l_english.yml")
    # per l'injector: tutte le cartelle candidate, così può ri-scegliere la più fresca
    # al momento di scrivere aidiplo.txt (utile se il gioco parte DOPO il middleware).
    cfg["paths"]["_hoi4_userdirs"] = [userdir] if userdir else []
    return cfg


def load_config(config_path=None):
    here = _app_dir()
    cfg_path = os.path.abspath(config_path) if config_path else os.path.join(here, "config.json")
    if not config_path and not os.path.exists(cfg_path):
        # EXE senza config.json accanto: usa quello di default impacchettato dentro l'exe
        bundled = os.path.join(_resource_dir(), "config.json")
        if os.path.exists(bundled):
            cfg_path = bundled
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # espandi le variabili d'ambiente nei percorsi
    for k, v in cfg["paths"].items():
        if isinstance(v, str):
            cfg["paths"][k] = os.path.expandvars(v)
    for section, keys in (("usage", ("ledger",)), ("actions", ("journal",)),
                          ("strategist", ("settings_file", "ledger"))):
        for key in keys:
            value = cfg.get(section, {}).get(key)
            if value and not os.path.isabs(value):
                cfg[section][key] = os.path.join(here, value)
    # autorilevamento (gestisce Documenti su OneDrive)
    if cfg.get("paths", {}).get("auto_detect", True):
        cfg = _autodetect(cfg)
    # il db di memoria sta accanto all'app (exe o script) se il percorso e' relativo
    if not os.path.isabs(cfg["paths"]["memory_db"]):
        cfg["paths"]["memory_db"] = os.path.join(_app_dir(), cfg["paths"]["memory_db"])
    return cfg


def saved_realism_mode(path):
    try:
        with open(path, encoding="utf-8") as stream:
            saved = stream.read().strip()
        return saved if saved in prompts.DIFFICULTY_MODES else None
    except (OSError, UnicodeError):
        return None


def load_realism_settings(cfg):
    behavior = cfg.get("behavior", {})
    path = behavior.get("realism_mode_file", "mode.txt")
    if not os.path.isabs(path):
        path = os.path.join(_app_dir(), path)
    default = behavior.get("default_realism_mode", "realistic")
    mode = default if default in prompts.DIFFICULTY_MODES else "realistic"
    mode = saved_realism_mode(path) or mode
    return path, mode


class App(UnifiedRuntime):
    def __init__(self, cfg):
        self.cfg = cfg
        functions.configure_profile(cfg.get("game_profile", "vanilla"))
        prompts.configure_features(cfg)
        self.lore = make_lore(cfg)
        self.q = queue.Queue()
        self.running = True

        self.llm = configured_client(cfg)
        self.memory = Memory(cfg["paths"]["memory_db"])
        self.injector = Injector(cfg, logger=self._log)

        # --- modalita' realismo (Realistic / Plausible / Sandbox), persistente ---
        self.mode_file, self.difficulty = load_realism_settings(cfg)
        prompts.set_difficulty(self.difficulty)

        # --- LINGUA dell'IA (scelta dal player al PRIMO avvio, testo libero), persistente.
        #     Se impostata, i prompt dicono all'IA di scrivere SEMPRE in quella lingua (anche il
        #     primissimo messaggio), eliminando la rilevazione automatica che a volte finiva in
        #     cinese/giapponese. Vuota = ripiego sulla lingua del messaggio del player. ---
        self.lang_file = os.path.join(_app_dir(), "language.txt")
        self.ai_language = ""
        try:
            if os.path.isfile(self.lang_file):
                self.ai_language = open(self.lang_file, encoding="utf-8").read().strip()
        except Exception:
            pass
        prompts.set_language(self.ai_language)

        self.ui = ChatOverlay(on_send=self.on_send, on_close=self.shutdown,
                              on_decree=self.decree_from_ui,
                              on_life=self.life_from_ui,
                              on_test=self.test_injection,
                              on_event_send=self.event_from_ui,
                              on_set_mode=self.set_mode_from_ui,
                              current_mode=self.difficulty,
                              realism_choices=prompts.realism_mode_options(),
                              on_event_close=self.on_event_close_from_ui,
                              on_diplomacy=self.diplomacy_from_ui,
                              on_usage=self.usage_from_ui if isinstance(self.llm, BudgetClient) else None,
                              prompt_for_mode=(saved_realism_mode(self.mode_file) is None if cfg.get("unified") else True))

        self.latest_state = {}
        self.world = {}              # snapshot reale delle nazioni rilevanti {tag: dati}
        self.diplomatic_contacts = {}  # target tag -> verified in-game channel identity
        self.states = {}             # regioni note {id: {id,name,owner,mine}} per gli effetti sugli stati
        # CATCH-UP: all'avvio (e a ogni nuova sessione) il log esistente e' STORIA gia' avvenuta;
        # la assorbiamo solo per il contesto (stato/gid/nazioni/regioni) SENZA rieseguire eventi,
        # canali o avanzamenti di piano. Poi si passa in LIVE. Evita: eventi completati all'avvio,
        # vecchi canali riaperti, "tempo che scorre" da replay.
        self._caught_up = False
        # ANTI-DOPPIONI: trigger di azione (PLAN_DONE/CHOICE/OPEN/...) e dispatch identici
        # visti pochi istanti fa vengono soppressi, cosi' eventi e conseguenze non escono
        # piu' volte contemporaneamente ne' si sommano.
        self._trig_seen = {}         # payload trigger -> timestamp
        self._dispatch_seen = {}     # firma comandi -> timestamp
        self.day = 0                 # giorno di gioco (saved var)
        self.gid = 0                 # ID partita (saved var) per la memoria
        self._last_gid = None
        self._last_day = 0
        self._ptag = ""              # tag della nazione del player (es. POR): isolamento robusto
        self._last_tag = None        # ultimo tag visto: un cambio = partita diversa
        self.plan_slots = [None] * 6  # piani a tempo (timing nativo HoI4, 6 slot)
        # ANTI-SPAM dei piani: ogni "passo" logico (bivio) e ogni annuncio di piano vengono
        # mostrati UNA SOLA VOLTA per partita. Se un riallineamento transitorio (o una lettura
        # spuria di gid/day) ricarica il piano, NON ri-spariamo lo stesso evento — era la causa
        # degli eventi a raffica.
        self._fired_steps = set()         # (gid, slot, stage_idx) dei bivi gia' mostrati
        self._fired_plan_starts = set()   # (gid, slot, created_day) annunci gia' mostrati
        self._stage_busy = set()          # slot il cui bivio si sta generando proprio ora
        self._plan_advance = {}           # slot -> (giorno_di_gioco, timestamp) dell'ultimo avanzamento
        self._done_plan_ids = set()       # id dei piani GIA' completati: non ricompletano mai piu'
        self.awaiting_choice = None   # SOLO guerra civile ("civilwar") / legacy senza slot
        # BIVI CONCORRENTI: ogni slot ha il SUO evento in gioco (aidiplo.40-45) e la scelta
        # torna gia' etichettata con lo slot -> niente piu' variabile globale unica che
        # attribuiva la risposta al piano sbagliato quando due bivi erano aperti insieme.
        self._pending_plan_slots = set()   # slot con un bivio attualmente aperto in gioco
        # PURGE DIFFERITA del "futuro abbandonato": armata quando il player carica un save
        # PRECEDENTE della stessa campagna; eseguita solo se il tempo RIPARTE da li' (cosi'
        # sbirciare un vecchio save e tornare a quello recente non cancella nulla).
        self._pending_purge = None
        self.pending_civilwar = None  # guerra civile in attesa della scelta del lato
        self.channel = None          # "leader:GER" oppure "decree"
        self.channel_name = ""
        self._last_player_text = ""  # ultimo messaggio del player (per classificare costo/guadagno PP)
        self._awaiting_leader_name = False  # True dopo 'Impose Head of State': il prossimo SEND = nome
        self.system_prompt = ""
        self.max_history = cfg["behavior"].get("max_history", 40)
        self.auto_exec = cfg["behavior"].get("auto_execute_actions", True)

        # --- iniezione robusta (digitazione UNICA, niente doppioni) ---
        # Il file viene digitato una sola volta; se HoI4 non e' in primo piano si ritenta
        # SOLO a portarlo a fuoco (non si ri-esegue mai il file gia' digitato).
        self._inject_lock = threading.Lock()
        inj = cfg.get("injection", {})
        self.inject_retries = int(inj.get("retries", 4))

        # --- eventi interattivi (finestra centrale) ---
        self.event = None            # evento interattivo attivo {channel, title, kind, role, history}
        self.pending_events = []     # continuazioni programmate [{fire_day, event, kickoff_msg}]
        self._event_counter = 0      # id progressivo: ogni evento ha il suo canale "event:N"
        ev = cfg.get("events", {})
        self.events_enabled = ev.get("enabled", True)
        self.ambient_every_days = int(ev.get("ambient_every_days", 365))  # eventi ambient: rari (max ~1/anno)
        self._next_ambient_day = None
        self.event_player = ""
        self._setup_unified()

        # thread di background
        threading.Thread(target=self._tail_log, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        self.ui.after(150, self._pump)
        self.q.put(("sysnote", self.injector.status_text()))

    # ---------------- logging ----------------
    def _log(self, msg):
        print(msg, flush=True)

    # ---------------- thread: tail del game.log ----------------
    def _tail_log(self):
        path = self.cfg["paths"]["game_log"]
        pos = 0
        buf = ""
        announced = False
        while self.running:
            try:
                if not os.path.exists(path):
                    if not announced:
                        self.q.put(("sysnote", f"game.log not found:\n{path}\n"
                                    "Launch HoI4 and load a game (it will be created)."))
                        announced = True
                    time.sleep(1.5)
                    continue
                size = os.path.getsize(path)
                if size < pos:
                    pos = 0          # nuova sessione: il log e' stato troncato
                    buf = ""
                    self._caught_up = False   # riassorbe in silenzio l'inizio della nuova sessione
                    self.q.put(("log_reset", None))
                # Lettura a BLOCCO (NON 'for line in f' + f.tell(): quel mix usa un buffer di
                # read-ahead che restituisce posizioni inaffidabili -> ri-lettura/replay del log,
                # che faceva "avanzare il tempo" a gioco in pausa). f.read()+f.tell() e' affidabile.
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                if chunk:
                    buf += chunk
                    parts = buf.split("\n")
                    buf = parts.pop()            # tiene l'ultima riga incompleta (HoI4 sta scrivendo)
                    for line in parts:
                        self._handle_log_line(line)
                # dopo il PRIMO assorbimento del log esistente, passa in modalita' LIVE
                self._caught_up = True
            except Exception as e:
                self._log(f"[tailer] {e}")
            time.sleep(0.5)

    def _recent_dup(self, store, key, window):
        """True se 'key' e' gia' stato visto da meno di 'window' secondi (e aggiorna il
        timestamp). Usato per sopprimere i doppioni simultanei senza bloccare le ripetizioni
        legittime (che avvengono a distanza di secondi/minuti reali)."""
        now = time.time()
        last = store.get(key, 0)
        store[key] = now
        if len(store) > 300:
            for k in [k for k, v in store.items() if now - v > 60]:
                store.pop(k, None)
        return (now - last) < window

    def _handle_log_line(self, line):
        if "AIDIPLO_TELEMETRY|" in line:
            packet = telemetry.parse(line)
            if packet:
                self.q.put(("telemetry", packet))
            return
        if self._caught_up and getattr(self, "journal", None):
            result = self.journal.receive(line)
            if result:
                self.q.put(("action_result", result))
                return
        if self._caught_up and "VOICES_TICK|" in line:
            match = re.search(r"VOICES_TICK\|gid=([\d.,]+)\|day=([\d.,]+)", line)
            if match:
                self.q.put(("strategy_tick", tuple(int(re.sub(r"\D", "", s)) for s in match.groups())))
            return
        # --- DATI (sempre processati, anche in catch-up: servono per il CONTESTO) ---
        tg = gs.parse_tag_line(line)
        if tg:
            functions.register_tags({tg[1]: tg[0]})  # name -> tag
            return
        st = gs.parse_state_line(line)
        if st:
            # marca se lo stato e' LIVE o di catch-up (lo decidiamo QUI, nel thread tailer,
            # dove _caught_up e' corretto: _on_state gira async e potrebbe vederlo gia' True)
            st["live"] = self._caught_up
            self.q.put(("state", st))
            return
        nat = gs.parse_nation(line)
        if nat is not None and "AIDIPLO_NATION|" in line:
            self.q.put(("nation", nat))
            return
        si = gs.parse_stateinfo(line)
        if si is not None and "AIDIPLO_STATEINFO|" in line:
            self.q.put(("stateinfo", si))
            return
        wd = gs.parse_world(line)
        if wd is not None and "AIDIPLO_WORLD|" in line:
            if self._caught_up and not self._recent_dup(self._trig_seen, line.strip(), 1.5):
                self.q.put(("world", wd))   # eventi mondiali: solo in LIVE (in catch-up sono storia)
            return
        # --- AZIONI / UI: SOLO in modalita' LIVE. Durante il catch-up iniziale del log esistente
        # sono click/eventi STORICI gia' avvenuti: NON vanno rieseguiti (niente vecchi canali
        # riaperti, niente eventi completati all'avvio). ---
        if not self._caught_up:
            return
        # anti-doppioni dei trigger d'azione live (stessa riga entro 1.5s reali)
        for _mark in ("AIDIPLO_PLAN_DONE|", "AIDIPLO_CHOICE|", "AIDIPLO_OPEN|", "AIDIPLO_ACTOR_OPEN|",
                      "AIDIPLO_LIFE_OPEN|", "AIDIPLO_DECREE_OPEN|", "AIDIPLO_NEWS|", "AIDIPLO_FIXLEADER|"):
            _i = line.find(_mark)
            if _i != -1 and self._recent_dup(self._trig_seen, line[_i:].strip(), 1.5):
                return
        # SUCCESSIONE DEI PIANI = TIMER NATIVO DI HoI4. Quando un piano parte (o a ogni bivio) il
        # middleware schedula in gioco 'country_event = { id = aidiplo.3<slot> days = <segment> }';
        # alla scadenza HoI4 logga AIDIPLO_PLAN_DONE e qui avanziamo il piano (bivio o
        # completamento) e ri-schediamo il timer successivo. E' HoI4 a scandire il tempo
        # (rispetta pausa/velocita'/salvataggi): niente piu' completamenti anticipati o a raffica.
        pd = gs.parse_plan_done(line)
        if pd is not None:
            self.q.put(("plan_done", pd))
            return
        op = gs.parse_open_line(line)
        if op:
            self.q.put(("open", op))
            return
        ch = gs.parse_choice(line)
        if ch is not None:
            self.q.put(("choice", ch))
            return
        if "AIDIPLO_NEWS|" in line:
            m = re.search(r"type=(\w+)", line)
            self.q.put(("news", m.group(1).lower() if m else "state"))
            return
        if "AIDIPLO_LEADERNOW|" in line:
            who = line.split("AIDIPLO_LEADERNOW|", 1)[1].strip()
            self.q.put(("sysnote", f"[diagnostic] Head of state right after the decision: '{who}'. "
                                   f"If this is NOT 'New Leader', the predefined character was NOT "
                                   f"promoted (tell me this name)."))
            return
        if "AIDIPLO_FIXLEADER|" in line:
            self.q.put(("fixleader", {}))   # fallback manuale: chiedi il nome e imponi il capo
            return
        lf = gs.parse_life_open(line)
        if lf is not None and "AIDIPLO_LIFE_OPEN|" in line:
            self.q.put(("life", lf))
            return
        if "AIDIPLO_ACTOR_OPEN|" in line:
            self.q.put(("actor", gs.parse_actor_open(line) or {}))
            return
        if gs.is_decree_open(line):
            self.q.put(("decree", {}))

    # ---------------- thread: provider availability ----------------
    def _heartbeat_loop(self):
        first = True
        while self.running:
            ok = self.llm.heartbeat()
            if first:
                if self.cfg.get("provider") == "chatgpt_plan":
                    note = ("ChatGPT plan connected" if ok else
                            "ChatGPT plan is not connected. Run Connect-ChatGPT.cmd and sign in.")
                else:
                    note = ("Local model endpoint reachable" if ok else
                            f"Local model endpoint not reachable at {self.llm.base_url}.")
                self.q.put(("sysnote", note))
                first = False
            time.sleep(60)

    # ---------------- Tk: pump della coda ----------------
    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "state":
                    self._on_state(payload)
                elif kind == "log_reset":
                    self._reset_log_session()
                elif kind == "telemetry":
                    self._on_telemetry(payload)
                elif kind == "open":
                    self._on_open(payload)
                elif kind == "decree":
                    self._on_decree()
                elif kind == "life":
                    self._on_life(payload)
                elif kind == "actor":
                    self._on_actor(payload)
                elif kind == "plan_done":
                    self._on_plan_done(payload)
                elif kind == "choice":
                    self._on_choice(payload)
                elif kind == "news":
                    if self.cfg.get("unified"):
                        if not self.ui.busy:
                            self.unified_news(payload or "state")
                    else:
                        threading.Thread(target=self._newspaper, args=(payload or "state",),
                                         daemon=True).start()
                elif kind == "strategy_tick":
                    if payload == (self.gid, self.day):
                        self._strategy_tick(self.latest_state)
                elif kind == "action_result":
                    if payload["profile"] == self.cfg.get("game_profile") and payload["gid"] == self.gid:
                        description = describe(payload["action"])
                        self.ui.append_action(f"Engine result: {payload['status']} — {description}")
                        if payload["status"] in ("applied", "executed"):
                            self.memory.remember_fact("engine_action", f"Engine reported {payload['status']} for a request from day {payload['day']}: {description}")
                elif kind == "fixleader":
                    self._on_fix_leader()
                elif kind == "world":
                    self._on_world(payload)
                elif kind == "nation":
                    self.world[payload["tag"]] = payload
                elif kind == "stateinfo":
                    # merge: il dizionario completo (id+nome) non deve cancellare il
                    # proprietario/mine eventualmente gia' noto per quello stato.
                    self.states.setdefault(payload["id"], {}).update(payload)
                    if payload.get("name"):
                        functions.register_states({payload["name"]: payload["id"]})
                elif kind == "assistant":
                    self.ui.append_ai(payload["name"], payload["message"])
                elif kind == "diplomatic_identity":
                    if self.channel == payload["channel"]:
                        self.channel_name = payload["name"]
                        self.ui.set_header(f"Dialogue with {payload['name']} — "
                                           f"{payload['country']} ({payload['tag']})")
                elif kind == "action":
                    self.ui.append_action(payload)
                elif kind == "sysnote":
                    self.ui.append_system(payload)
                elif kind == "busy":
                    self.ui.set_busy(payload)
                elif kind == "thinking":
                    self.ui.append_thinking()
                elif kind == "event_open":
                    self._open_event_channel(payload)
                elif kind == "ev_show":
                    self.ui.event_show(payload)
                elif kind == "ev_ai":
                    self.ui.event_append_ai(payload)
                elif kind == "ev_sys":
                    self.ui.event_append_sys(payload)
                elif kind == "ev_busy":
                    self.ui.event_set_busy(payload)
                elif kind == "ev_thinking":
                    self.ui.event_append_thinking()
                elif kind == "ev_close":
                    self.ui.event_close()
                elif kind == "ev_autoclose":
                    # lascia tempo di leggere la chiusura della fase, poi chiude da sola
                    self.ui.after(8000, self.ui.event_close)
                elif kind == "panel_hide":
                    self.ui.hide_panel()
                elif kind == "autohide_panel":
                    # e' appena partito un evento/azione in gioco: l'iniezione richiede HoI4 in
                    # primo piano, ma il player potrebbe non sapere di dover chiudere la chat.
                    # Riduciamo da soli il pannello alla linguetta e riportiamo HoI4 a fuoco
                    # (appena parte l'evento: ~1,5s, non 5s) se il player non sta scrivendo.
                    self.ui.after(1500, self._autohide_after_plan)
        except queue.Empty:
            pass
        if self.running:
            self.ui.after(150, self._pump)

    def _on_fix_leader(self):
        if self.cfg.get("unified"):
            self.q.put(("sysnote", "Leader replacement is unavailable in unified mode."))
            return
        """FALLBACK MANUALE (decisione aid_fix_leader). NIENTE popup (su HoI4 borderless usciva
        'buggato'/vuoto): usiamo la CASELLA DI SCRITTURA del pannello, che e' sempre visibile.
        Mettiamo il pannello in 'attesa nome': il PROSSIMO testo inviato col SEND e' il nome."""
        self._awaiting_leader_name = True
        try:
            self.ui.show_panel()                 # pannello (e casella) ben visibili
        except Exception:
            pass
        self.ui.append_highlight(
            "IMPOSE HEAD OF STATE — write the FULL NAME of the new head of state in the text box "
            "below (next to SEND) and press SEND. The game will then save and reopen by itself to "
            "apply the name. (Type 'cancel' to abort.)")

    def _apply_fix_leader_name(self, nm):
        """Riceve il nome dal pannello, scrive la chiave loc e INSEDIA il NOSTRO personaggio
        predefinito (gia' reclutato all'avvio della partita): aidiplo.64 lo ri-recluta per sicurezza,
        aidiplo.65 lo PROMUOVE = priorita' assoluta su qualsiasi ideologia (scavalca anche Mussolini).
        Poi SALVA e riavvia HoI4: al ricaricamento il personaggio rilegge il nome dalla chiave loc
        -> esce il NOME SCELTO. Funziona per ogni nazione e ogni ideologia."""
        nm = (nm or "").replace('"', "'").replace("\n", " ").strip()
        if not nm or nm.lower() in ("cancel", "annulla", "annullare"):
            self.ui.append_system("Head-of-state change cancelled.")
            return
        nm = nm[:32]
        tag = self.latest_state.get("tag", "") or "ROOT"
        try:
            self.injector._set_loc_keys({"AIDIPLO_LEADER": nm})
        except Exception as e:
            self._log(f"[fixleader] loc: {e}")
        # cartella dei salvataggi = fratello di '.../logs' (per capire quando 'savegame' ha scritto)
        save_dir = None
        try:
            gl = self.cfg["paths"]["game_log"]
            save_dir = os.path.join(os.path.dirname(os.path.dirname(gl)), "save games")
        except Exception as e:
            self._log(f"[fixleader] save_dir: {e}")
        baseline = self._latest_save_mtime(save_dir)
        # reload loc -> aidiplo.64 (recluta, backup) -> aidiplo.65 (promuove = priorita' assoluta) -> savegame
        self._dispatch(["reload loc", f"event aidiplo.64 {tag}",
                        f"event aidiplo.65 {tag}", "savegame"])
        self.ui.append_action(
            f"Head of state '{nm}' installed and the game is SAVING. To apply the name, HoI4 will "
            f"now close and reopen by itself - when the launcher appears, click PLAY then CONTINUE "
            f"(last save). The leader will then carry the name you chose.")
        try:
            t = threading.Timer(0.5, self._restart_game_for_name, args=(save_dir, baseline))
            t.daemon = True
            t.start()
        except Exception as e:
            self._log(f"[fixleader] restart timer: {e}")

    def _latest_save_mtime(self, save_dir):
        """mtime piu' recente tra i salvataggi .hoi4 (per capire quando 'savegame' ha scritto)."""
        try:
            import glob
            files = glob.glob(os.path.join(save_dir, "*.hoi4")) if save_dir else []
            return max((os.path.getmtime(f) for f in files), default=0.0)
        except Exception:
            return 0.0

    def _restart_game_for_name(self, save_dir=None, baseline=0.0):
        """Aspetta che 'savegame' abbia scritto un NUOVO salvataggio (cosi' non si perdono
        progressi), poi chiude HoI4 e lo riapre via Steam. Se non riesce a confermare il
        salvataggio, NON chiude e avvisa il player di riavviare a mano con Continua."""
        import subprocess
        # 1) attendi un salvataggio piu' recente del baseline (max ~90s) e che sia 'stabile'
        confirmed = False
        if save_dir:
            import glob
            deadline = time.time() + 90.0
            last_size = -1
            stable = 0
            while time.time() < deadline:
                time.sleep(2.0)
                if self._latest_save_mtime(save_dir) <= baseline + 0.5:
                    continue
                try:
                    newest = max(glob.glob(os.path.join(save_dir, "*.hoi4")),
                                 key=os.path.getmtime)
                    sz = os.path.getsize(newest)
                except Exception:
                    sz = -1
                if sz > 0 and sz == last_size:
                    stable += 1
                    if stable >= 2:          # dimensione ferma per ~4s = scrittura finita
                        confirmed = True
                        break
                else:
                    stable = 0
                    last_size = sz
        if not confirmed:
            self.q.put(("sysnote",
                        "I could not confirm the save automatically, so to be safe I will NOT "
                        "close HoI4. Please SAVE, then exit and reopen with CONTINUE: the head of "
                        "state will then have the chosen name."))
            return
        # 2) chiudi HoI4
        try:
            subprocess.run(["taskkill", "/IM", "hoi4.exe", "/F"],
                           creationflags=0x08000000, timeout=15)
        except Exception as e:
            self._log(f"[restart] taskkill: {e}")
        time.sleep(3)
        # 3) riapri via Steam
        try:
            os.startfile("steam://rungameid/394360")   # riavvia HoI4 via Steam
        except Exception as e:
            self._log(f"[restart] launch: {e}")
        self.q.put(("action", "HoI4 is reopening. In the launcher click PLAY, then CONTINUE (last "
                              "save): the head of state will now have the chosen name."))

    def _autohide_after_plan(self):
        """Riduce il pannello alla linguetta IA e riporta HoI4 in primo piano, cosi' l'evento
        del piano (che si inietta solo con HoI4 attivo) parte subito. Non fa nulla se il player
        ha gia' chiuso il pannello o sta scrivendo un messaggio."""
        try:
            if not self.ui.can_autohide():
                return
            self.ui.hide_panel()
            threading.Thread(target=self.injector._focus_game, daemon=True).start()
        except Exception:
            pass

    def _on_world(self, text):
        """Grande sviluppo in gioco (guerra / capitolazione / cambio di governo).
        Lo memorizziamo come 'fatto' cosi' l'IA che gestisce eventi, player e giornali
        resta coerente con cio' che accade nel mondo. Deduplica le ripetizioni (la
        rilettura del log all'avvio non deve riempire la memoria di doppioni)."""
        text = (text or "").strip()
        if not text:
            return
        seen = getattr(self, "_world_seen", None)
        if seen is None:
            seen = self._world_seen = set()
        key = f"{self.gid}:{self.day}:{text}"
        if key in seen:
            return
        seen.add(key)
        try:
            self.memory.set_context(self.gid, self.day)
            self.memory.remember_fact("world", text)
        except Exception as e:
            self._log(f"[world] {e}")
        self._log(f"[world] {text}")
        if getattr(self, "strategist", None):
            self.strategist.crisis(text)

    def _on_state(self, st):
        previous = self.latest_state
        self.latest_state = st
        if telemetry.matches(getattr(self, "_telemetry", None), st):
            st["telemetry"] = self._telemetry
        # mostra SEMPRE lo stato corrente nella barra
        self.ui.set_status(
            f"{st.get('name','?')} ({st.get('tag','?')}) | {st.get('date','?')} | "
            f"Stab {st.get('stab','?')}% WS {st.get('ws','?')}% PP {st.get('pp','?')} | "
            f"Tension {st.get('wt','?')}%")
        if any(previous.get(k) != st.get(k) for k in ("tag", "day", "gid")):
            self._log(f"[sync] {st.get('tag', '?')} date={st.get('date', '?')} "
                      f"day={st.get('day', '?')} gid={st.get('gid', '?')}")
        # day/gid di QUESTA riga. Robusto al FORMATO: alcune locale di HoI4 scrivono i numeri
        # grandi col separatore delle migliaia (es. "1.234.567"), quindi teniamo solo le cifre
        # prima di convertire (cosi' il gid viene letto bene e la memoria resta isolata per
        # partita). Una lettura non valida -> 0: la trattiamo come "sconosciuta" e NON
        # aggiorniamo gli ultimi valori buoni, cosi' uno 0 transitorio non viene mai scambiato
        # per "nuova partita / salto indietro nel tempo" (che azzererebbe/riavvierebbe i piani).
        # IMPORTANTISSIMO: NON usciamo MAI qui per gid<=0 — bloccherebbe l'avanzamento dei piani
        # (i bivi e la conclusione non scatterebbero piu'). I piani vivono in memoria e devono
        # avanzare comunque; il gid serve solo a isolare la memoria fra partite.
        def _digits(v):
            s = re.sub(r"[^\d]", "", str(v if v is not None else ""))
            try:
                return int(s) if s else 0
            except Exception:
                return 0
        d_read = _digits(st.get("day"))
        g_read = _digits(st.get("gid"))
        if g_read > 0:
            self.gid = g_read
        if d_read > 0:
            self.day = d_read
        if g_read > 0:
            st["gid"] = str(g_read)
        if d_read > 0:
            st["day"] = str(d_read)
        # tag della nazione del player (es. POR). E' SEMPRE presente e affidabile, quindi e'
        # l'ancora migliore per capire "di quale partita stiamo parlando": un piano nato in
        # un'altra nazione (es. Afghanistan/Kabul) non deve MAI comparire o avanzare ora.
        ptag = (st.get("tag") or "").strip().upper()
        if ptag:
            self._ptag = ptag
        self.memory.set_context(self.gid, self.day)
        # PURGE DIFFERITA (vedi __init__): se dopo un salto indietro il TEMPO E' RIPARTITO
        # dal punto caricato, il vecchio futuro e' ufficialmente un ramo abbandonato ->
        # dimenticalo (con la guardia sul timestamp: le memorie nuove restano). Se invece
        # il player e' TORNATO al save recente (giorno >= com'era) o ha cambiato partita,
        # disarma senza cancellare nulla.
        pp = self._pending_purge
        if pp:
            if self.gid != pp["gid"] or (d_read > 0 and self.day >= pp["prev_day"]):
                self._pending_purge = None       # altra partita / tornati al save recente
            elif d_read > 0 and pp["day"] < self.day <= pp["day"] + 3:
                # il tempo e' RIPARTITO naturalmente (+1 giorno, tolleranza 3) dal punto
                # caricato: il vecchio futuro e' DAVVERO un ramo abbandonato -> dimenticalo.
                self.memory.forget_future(pp["day"], pp["ts"])
                self._pending_purge = None
                self._log(f"[memoria] ramo abbandonato dimenticato (dopo il giorno {pp['day']})")
            elif d_read > 0 and self.day > pp["day"] + 3:
                # SALTO in avanti (caricato un TERZO save intermedio): prudenza, non
                # cancellare nulla — quelle memorie appartengono alla timeline caricata.
                self._pending_purge = None
        # cambio REALE di partita / salvataggio caricato. Tre segnali, tutti robusti:
        #  - gid diverso (su lettura valida, mai su uno 0 spurio)
        #  - TAG del player diverso (cambio nazione = quasi certamente nuova partita)
        #  - giorno tornato indietro (salvataggio precedente caricato)
        new_game = (self._last_gid is not None and g_read > 0 and g_read != self._last_gid)
        tag_changed = (self._last_tag is not None and ptag and ptag != self._last_tag)
        went_back = (self._last_gid is not None and d_read > 0 and self._last_day > 0
                     and d_read < self._last_day)
        if self._last_gid is None or new_game or tag_changed or went_back:
            self._reconcile_plans()
            # se e' un VERO cambio (non il primo avvio): ripristina la chat visibile
            if self._last_gid is not None and (new_game or tag_changed or went_back):
                if went_back and not new_game and not tag_changed:
                    # SALTO INDIETRO nella STESSA campagna: arma la purge differita del
                    # futuro ora sospeso. time.time() = spartiacque: tutto cio' che e'
                    # stato scritto PRIMA di adesso con day > oggi appartiene al vecchio
                    # ramo; cio' che verra' scritto d'ora in poi e' la nuova timeline.
                    self._pending_purge = {"gid": self.gid, "day": self.day,
                                           "prev_day": self._last_day,
                                           "ts": time.time()}
                else:
                    self._pending_purge = None   # altra partita: nessuna purge da fare
                # le guardie anti-spam appartengono alla partita precedente: azzerale
                self._fired_steps.clear()
                self._fired_plan_starts.clear()
                self._stage_busy.clear()
                self.channel = None
                self.channel_name = ""
                self.diplomatic_contacts.clear()
                self.world = {}      # lo snapshot del mondo appartiene alla partita precedente
                self.states = {}     # idem per le regioni note
                self.ui.clear_history()
                self.ui.set_header("Voices of History — open a channel")
                self.ui.append_system("Game changed or save loaded: "
                                      "chat history reset.")
                # azzera anche gli eventi interattivi della partita precedente
                self.event = None
                self.pending_events = []
                self._next_ambient_day = None
                self.q.put(("ev_close", None))
        self._last_gid = self.gid
        if ptag:
            self._last_tag = ptag
        if d_read > 0:
            self._last_day = self.day
        # NB: la successione dei piani NON e' piu' guidata dal giorno qui: la scandiscono i
        # TIMER NATIVI di HoI4 (eventi aidiplo.3X), letti come AIDIPLO_PLAN_DONE. Vedi _on_plan_done.

    def _plan_is_current(self, p):
        """Un piano appartiene alla partita CORRENTE solo se il TAG della nazione del player
        combacia (segnale sempre affidabile). Per i piani 'legacy' senza tag, lo accettiamo solo
        se il gid POSITIVO combacia. Cosi' un piano nato in un'altra nazione (es. Kabul =
        Afghanistan) non compare ne' avanza mai in un'altra partita (es. Portogallo), anche se il
        gid non e' affidabile o vale 0."""
        ptag = (self._ptag or "").upper()
        tag = (str(p.get("tag") or "")).upper()
        if tag:
            return bool(ptag) and tag == ptag and (not p.get("gid") or p.get("gid") == self.gid)
        return self.gid > 0 and p.get("gid") == self.gid

    def _reconcile_plans(self):
        """Riallinea i piani al salvataggio caricato: scarta i piani di un 'futuro' annullato,
        ricarica i pendenti (della PARTITA corrente) e completa subito quelli ormai SCADUTI il
        cui timer in-game potrebbe non scattare piu' (es. il middleware era spento quando il
        timer e' scaduto). La successione vera e' guidata dai TIMER NATIVI di HoI4 (aidiplo.3X)."""
        try:
            self.memory.discard_future_plans(self.day)   # piani di un FUTURO annullato: via
            self.memory.revive_loaded_plans(self.day)     # save precedente: riattiva i non-finiti
            self.plan_slots = [None] * len(self.plan_slots)
            for p in self.memory.active_plans():
                slot = p.get("slot", -1)
                if not (0 <= slot < len(self.plan_slots)):
                    continue
                if not self._plan_is_current(p):
                    continue
                created = int(p.get("created_day", self.day) or self.day)
                seg = max(1, int(p.get("segment", 60) or 60))
                st_total = int(p.get("stages_total", 0))
                eta = int(p.get("eta_day", created + seg * (st_total + 1))
                          or (created + seg * (st_total + 1)))
                # STADIO ri-derivato dai giorni REALMENTE trascorsi (robusto al salva/carica:
                # caricando un save PRECEDENTE lo stadio torna indietro, uno successivo avanza).
                # NON usiamo lo stadio salvato nel DB: apparterrebbe a una timeline piu' avanti.
                elapsed = max(0, self.day - created)
                p["stage_idx"] = min(st_total, elapsed // seg)
                # RIAVVOLGI anche lo storico: le scelte fatte ai bivi DOPO lo stadio
                # ricalcolato appartengono alla timeline abbandonata. Senza questa
                # pulizia i bivi futuri venivano generati "come conseguenza" di scelte
                # mai avvenute in questo ramo (e la risposta gia' registrata bloccava
                # il bonus del bivio quando ri-usciva: answered_stage fantasma).
                cl = p.get("choices_log") or []
                if cl:
                    keep = []
                    for e in cl:
                        m = re.match(r"Crossroads (\d+):", str(e))
                        if m is None or int(m.group(1)) <= p["stage_idx"]:
                            keep.append(e)
                    p["choices_log"] = keep
                try:
                    if int(p.get("answered_stage")) > p["stage_idx"]:
                        p["answered_stage"] = None
                except Exception:
                    pass
                # questo passo (e quelli futuri) potra' ri-uscire: pulisci le guardie anti-spam
                self._done_plan_ids.discard(p.get("id"))
                self._fired_steps = {k for k in self._fired_steps
                                     if not (isinstance(k, tuple) and len(k) == 3 and k[1] == slot)}
                if self.day >= eta:
                    # tempo TOTALE davvero trascorso: completa adesso (timer perso o gia' passato)
                    if p.get("id"):
                        self._done_plan_ids.add(p.get("id"))
                    self.memory.mark_plan_done(p.get("id"))
                    threading.Thread(target=self._complete_plan, args=(p,), daemon=True).start()
                else:
                    self._persist_plan_meta(p)            # salva lo stadio ri-derivato
                    self.plan_slots[slot] = p
        except Exception as e:
            self._log(f"[reconcile] {e}")

    def _persist_plan_meta(self, plan):
        """Salva (per id univoco) TUTTO lo stato del piano, incluso lo STORICO DELLE SCELTE ai
        bivi ('choices_log'). Cosi' le scelte sopravvivono a salva/carica e vengono lette dai
        bivi successivi e dal completamento."""
        if not plan or not plan.get("id"):
            return
        self.memory.update_plan_meta(plan.get("id"), {
            "stages_total": plan.get("stages_total", 0),
            "stage_idx": plan.get("stage_idx", 0),
            "segment": plan.get("segment", 60),
            "role": plan.get("role", "leader"),
            "final_actions": plan.get("final_actions", []),
            "gid": plan.get("gid"), "tag": plan.get("tag"),
            "answered_stage": plan.get("answered_stage"),
            "choices_log": plan.get("choices_log", [])})

    def _on_plan_done(self, slot):
        if self.cfg.get("unified"):
            self.q.put(("sysnote", "A legacy plan timer was received. Unified mode does not execute legacy plan rewards."))
            return
        if not (0 <= slot < len(self.plan_slots)):
            return
        plan = self.plan_slots[slot] or next(
            (p for p in self.memory.active_plans()
             if p.get("slot") == slot and self._plan_is_current(p)), None)
        if not plan or not self._plan_is_current(plan):
            return
        if plan.get("id") in self._done_plan_ids:   # gia' completato: stop, niente repliche
            self.plan_slots[slot] = None
            return
        # ANTI-DOPPIONE (bivi doppi nello STESSO istante): un secondo PLAN_DONE per lo stesso slot
        # nello STESSO giorno di gioco (o entro pochi secondi reali) e' un timer doppio o una riga
        # di log riletta -> lo ignoriamo. I passi legittimi distano 'segment' (>=7) giorni, quindi
        # non cadono mai nello stesso giorno: cosi' non si avanza due stadi insieme.
        prev = self._plan_advance.get(slot)
        now = time.time()
        if prev and (prev[0] == self.day or now - prev[1] < 3.0):
            return
        self._plan_advance[slot] = (self.day, now)
        stages_total = int(plan.get("stages_total", 0))
        stage_idx = int(plan.get("stage_idx", 0))
        if stage_idx < stages_total:
            # BIVIO intermedio: avanza lo stadio e chiedi una scelta. Programma SUBITO il
            # prossimo passo (next_day) prima di mostrare il bivio, cosi' il controllo
            # giornaliero non ri-scatta lo stesso piano.
            seg = int(plan.get("segment", 60) or 60)
            # ANTI-SPAM (definitivo): questo passo e' identificato da (partita, slot, stage_idx).
            # Se lo abbiamo GIA' mostrato — o lo stiamo generando proprio adesso — NON lo
            # ri-mostriamo: avanziamo solo la prossima scadenza. Cosi' anche se qualcosa
            # ricaricasse il piano a uno stadio gia' fatto, lo stesso bivio non esce a raffica.
            step_key = (self.gid, slot, stage_idx)
            if step_key in self._fired_steps or slot in self._stage_busy:
                plan["next_day"] = self.day + seg
                self.plan_slots[slot] = plan
                return
            self._fired_steps.add(step_key)
            self._stage_busy.add(slot)
            plan["stage_idx"] = stage_idx + 1
            self.plan_slots[slot] = plan
            # salva lo stato avanzato PER ID univoco (gid/tag + storico scelte): il piano resta
            # legato alla sua partita e reconcile lo riprende allo stadio giusto, mai da capo.
            self._persist_plan_meta(plan)
            threading.Thread(target=self._stage_decision, args=(slot, plan), daemon=True).start()
        else:
            # COMPLETAMENTO: segna finito (per id), ricorda l'id come completato e libera lo slot
            # PRIMA di lanciare la narrazione, cosi' non puo' MAI ricompletarsi.
            if plan.get("id"):
                self._done_plan_ids.add(plan.get("id"))
            self.plan_slots[slot] = None
            self.memory.mark_plan_done(plan.get("id"))
            threading.Thread(target=self._complete_plan, args=(plan,), daemon=True).start()

    def _stage_decision(self, slot, plan):
        ctx = self._conversation_context("narration")
        whose = ("the clandestine scheme of the NON-government figure the player is acting as "
                 "(a faction/general/conspirator), against or apart from the sitting "
                 "government — frame the crossroads from THEIR side, not the State's"
                 ) if plan.get("role") == "actor" else "the player's national plan"
        sysp = (prompts.GROUNDING_RULE + "\n"
                "You are the narrator of an ONGOING PLAN in Hearts of Iron IV, concerning "
                + whose + ". The plan is still IN PROGRESS and has NOT succeeded yet: this is "
                "a PREPARATION step, so do NOT narrate as if it were already accomplished "
                "(the current government is still in charge). Generate a "
                "SPECIFIC CROSSROADS GROUNDED in actors/factions/conditions that ACTUALLY "
                "exist in this game and the real situation (no invented 'rebels' or random "
                "groups), with concrete details consistent with how far along the plan is.\n"
                "MAKE IT A REAL DILEMMA, not a bland 'be bold vs be cautious': the two options "
                "must be DISTINCT strategic paths with GENUINE TRADE-OFFS (e.g. speed but "
                "exposure/instability vs. patience but a window closing; co-opt a rival vs. "
                "sideline them; lean on the army vs. the street; a costly bribe vs. blackmail). "
                "Each option should make the player THINK — it advances the plan but with a "
                "different price, risk or side-effect, and ties to the current situation. Avoid "
                "trivial or interchangeable choices. A poor or reckless choice may set the plan "
                "BACK (a small negative effect / lost ground), not only help it — keep setbacks "
                "occasional and fair (see the OUTCOMES failure rule). Give each option already a "
                "SMALL partial effect via effect_a/effect_b (a gain, a cost, or a setback).\n"
                "IMPORTANT: option_a/option_b must be SHORT labels (max ~5 words, like a "
                "button caption) — put all the detail and the trade-offs in 'message'. "
                + self._lang_directive() +
                " Reply ONLY in JSON: {\"message\":\"<3-5 specific sentences laying out "
                "the dilemma and what each path costs/risks>\","
                "\"option_a\":\"<choice>\",\"option_b\":\"<choice>\","
                "\"effect_a\":\"<ONE HoI4 effect on one line or empty>\","
                "\"effect_b\":\"<same>\"}. Effects must be ONLY of a safe and SMALL type: "
                "add_political_power, add_stability, add_war_support, add_manpower, "
                "add_timed_idea (e.g. add_stability = -0.03; a setback can be a small minus). "
                "VARY the effect to fit THIS choice (do not repeat the same one every crossroads) "
                "and keep it SMALL — these are only partial steps; the BIG payoff comes at the "
                "plan's completion, not here. No complex effects.")
        prior = plan.get("choices_log") or []
        prior_txt = ""
        if prior:
            prior_txt = ("\nPLAYER'S CHOICES SO FAR IN THIS PLAN (this crossroads MUST be a DIRECT "
                         "CONSEQUENCE of them — the situation, the factions involved and the stakes "
                         "follow from what the player already decided; do NOT contradict or ignore "
                         "them):\n- " + "\n- ".join(str(x) for x in prior[-8:]))
        usr = (f"Plan: '{plan['title']}', crossroads {plan.get('stage_idx')}/{plan.get('stages_total')}. "
               f"What it will bring: {plan.get('facts','')}{prior_txt}\n{ctx}")
        msg, a, b, ea, eb = ("The plan is at a crossroads.", "Accelerate (bold)",
                             "Proceed with caution", "", "")
        try:
            obj = LLMClient.parse_obj(self.llm.chat(
                [{"role": "system", "content": sysp}, {"role": "user", "content": usr}]))
            msg = obj.get("message") or msg
            a = obj.get("option_a") or a
            b = obj.get("option_b") or b
            ea = (obj.get("effect_a") or "").strip()
            eb = (obj.get("effect_b") or "").strip()
        except Exception as e:
            self._log(f"[bivio] {e}")
        plan["choice_effects"] = {"1": ea, "2": eb}
        plan["stage_situation"] = msg          # serve se il player sceglie "scrivi tu" (c=3)
        plan["opt_a"] = a                      # testo delle opzioni: serve a RICORDARE la scelta
        plan["opt_b"] = b
        self.plan_slots[slot] = plan
        self._pending_plan_slots.add(slot)
        try:
            # Evento bivio DEDICATO a questo slot (aidiplo.4<slot>): testo e scelta non
            # interferiscono MAI con i bivi degli altri piani aperti nello stesso momento.
            native_slot = slot if self.cfg.get("behavior", {}).get("per_slot_events", False) else None
            if native_slot is None:
                self.awaiting_choice = slot
            cmds = self.injector.prepare_decision_event(plan["title"], msg, a, b, slot=native_slot)
            # TIMER NATIVO HoI4: schedula il PROSSIMO passo (bivio o completamento) fra 'segment'
            # giorni di gioco. E' HoI4 a contare il tempo (rispetta pausa/velocita'/salvataggi):
            # cosi' i bivi escono distanziati e il completamento solo DOPO l'ultimo bivio.
            seg = max(7, int(plan.get("segment", 60) or 60))
            cmds.append(f"eval_effect ROOT = {{ country_event = {{ id = aidiplo.3{slot} "
                        f"days = {seg} }} }}")
            self._dispatch(cmds)
            self.q.put(("assistant", {"name": "Crossroads",
                                      "message": (f"{msg}\n  A) {a}\n  B) {b}\n  C) (write your own move)\n"
                                                  "(An event has appeared IN HoI4 — pick A or B there; "
                                                  "for C, write your move here.)")}))
        finally:
            self._stage_busy.discard(slot)     # il bivio e' stato mostrato: libera lo slot

    def _on_choice(self, payload):
        if self.cfg.get("unified"):
            return
        # payload: {"slot": int|None, "c": 1|2|3}. slot!=None arriva dai bivi PER-SLOT
        # (aidiplo.40-45): la scelta e' gia' etichettata col piano giusto. slot=None arriva
        # dal canale condiviso aidiplo.46 (guerra civile / compatibilita' col vecchio flusso).
        if isinstance(payload, dict):
            c = payload.get("c")
            slot = payload.get("slot")
        else:                                  # compatibilita': vecchio payload intero
            c, slot = payload, None
        if c not in (1, 2, 3):
            return
        if slot is None:
            # canale condiviso: guerra civile (scelta del lato)...
            if self.pending_civilwar is not None and self.awaiting_choice == "civilwar":
                cw = self.pending_civilwar
                self.pending_civilwar = None
                self.awaiting_choice = None
                self._resolve_civilwar(cw, c)
                return
            # ...oppure il vecchio flusso legacy a variabile globale
            slot = self.awaiting_choice
            self.awaiting_choice = None
        if not isinstance(slot, int) or not (0 <= slot < len(self.plan_slots)):
            return
        self._pending_plan_slots.discard(slot)
        plan = self.plan_slots[slot]
        if not plan:
            return
        stage = plan.get("stage_idx", "?")
        # ANTI-DOPPIONE: ogni bivio accetta UNA sola risposta. Righe di log rilette o
        # doppioni non riapplicano il bonus ne' registrano scelte fantasma; persiste nel
        # DB (answered_stage), quindi regge anche a riavvii del middleware.
        if plan.get("answered_stage") == stage:
            return
        plan["answered_stage"] = stage
        sit = (plan.get("stage_situation") or "").strip()
        if c == 3:
            # 3a opzione: il player prende in mano la situazione e SCRIVE -> apre il canale
            # evento del middleware su QUESTO bivio; cio' che scrive influenza i bivi dopo
            # (le conseguenze vengono ricordate e lette dagli stage successivi).
            seed = ((sit or f"The plan '{plan['title']}' is at a crossroads.") +
                    "\n\nYou take personal command at this crossroads. Describe exactly "
                    "what you do; your decisions here will shape what follows.")
            entry = f"Crossroads {stage}: «{sit[:140]}» -> the player took PERSONAL COMMAND (wrote their own move)"
            log = plan.get("choices_log") or []
            log.append(entry)
            plan["choices_log"] = log
            self.plan_slots[slot] = plan
            self._persist_plan_meta(plan)
            self.q.put(("action", f"Personal command on plan '{plan['title']}'"))
            self.memory.remember_fact("choice", f"Plan '{plan['title']}' — {entry}")
            self._start_event(f"{plan['title']} — your move", seed, kind="plan_stage",
                              role=plan.get("role", "leader"))
            return
        # scelta A/B: registra COSA ha scelto (il testo dell'opzione), non solo l'etichetta, cosi'
        # i bivi successivi e il completamento ne tengono conto davvero.
        chosen = (plan.get("opt_a") if c == 1 else plan.get("opt_b")) or ("A" if c == 1 else "B")
        entry = f"Crossroads {stage}: «{sit[:140]}» -> chose: {chosen}"
        log = plan.get("choices_log") or []
        log.append(entry)
        plan["choices_log"] = log
        self.plan_slots[slot] = plan
        self._persist_plan_meta(plan)
        self.q.put(("action", f"Plan '{plan['title']}' — chose: {chosen}"))
        self.memory.remember_fact("choice", f"Plan '{plan['title']}' — {entry}")
        eff = self._safe_crossroad_effect((plan.get("choice_effects") or {}).get(str(c), ""))
        if eff:
            self._dispatch([f"effect ROOT = {{ {eff} }}"])

    def _safe_crossroad_effect(self, eff):
        """Valida e RICOSTRUISCE l'effetto di un bivio in forma pulita, cosi' non puo' MAI dare
        'errore di compilazione' in gioco. Ammette solo un piccolo set sicuro (stat numeriche +
        spirito nazionale temporaneo con idea NOTA). Qualsiasi altra cosa -> '' (ignorata)."""
        s = (eff or "").strip()
        if not s:
            return ""
        m = re.match(r"(add_political_power|add_stability|add_war_support|add_manpower|"
                     r"add_command_power|add_war_support)\s*=\s*(-?\d+(?:\.\d+)?)", s)
        if m:
            return f"{m.group(1)} = {m.group(2)}"
        # spirito nazionale temporaneo: SOLO idee esistenti (qualsiasi ordine idea/days)
        m = re.match(r"add_timed_idea\s*=\s*\{(.+)\}", s)
        if m:
            body = m.group(1)
            im = re.search(r"idea\s*=\s*(\w+)", body)
            dm = re.search(r"days\s*=\s*(\d+)", body)
            if im and im.group(1) in functions.KNOWN_IDEAS:
                days = dm.group(1) if dm else "90"
                return f"add_timed_idea = {{ idea = {im.group(1)} days = {days} }}"
        return ""

    def _resolve_civilwar(self, cw, c):
        """Applies the side choice in a civil war VIA EVENTS (atomic), not the 'civilwar'
        console command: an event runs start_civil_war exactly once, so concurrent/duplicate
        triggers can never spawn two identical rebel factions. c==1 lead the revolution
        (player flips to the new ideology + new leader via aidiplo.60, the OLD regime rebels
        via aidiplo.61); c==2 defend the government (the new-ideology revolutionaries rebel)."""
        tag = cw.get("target", "ROOT")
        new_ideo = cw.get("ideology", "neutrality")
        leader = (cw.get("leader", "") or "").replace('"', "'")
        code = {"fascism": 1, "communism": 2, "democratic": 3, "neutrality": 4}
        new_code = code.get(new_ideo, 4)
        oid = str(self.latest_state.get("ideo", "4"))
        old_code = int(oid) if oid in ("1", "2", "3", "4") else 4
        cmds = []
        if c == 1:
            # il player diventa la NUOVA ideologia con un nuovo leader nominato (aidiplo.60),
            # poi il VECCHIO regime/lealisti insorgono come ribelli (aidiplo.61).
            if leader:
                self.injector._set_loc_keys({"AIDIPLO_LEADER": leader})
                cmds.append("reload loc")
            cmds.append(f"eval_effect {tag} = {{ set_variable = {{ aidiplo_newideo = {new_code} }} }}")
            # guerra civile: NON resettiamo i focus da questo flag (evita reset da valore stantio)
            cmds.append(f"eval_effect {tag} = {{ set_variable = {{ aidiplo_radical = 0 }} }}")
            # set_politics (60) -> retire raw (toglie il capo del gioco) -> create (63) col nome scelto.
            cmds.append(f"event aidiplo.60 {tag}")
            cmds.append("retire_country_leader")
            cmds.append("retire_country_leader")
            cmds.append("retire_country_leader")
            cmds.append("retire_country_leader")
            cmds.append(f"event aidiplo.63 {tag}")
            cmds.append(f"eval_effect {tag} = {{ set_variable = {{ aidiplo_cw_ideo = {old_code} }} }}")
            cmds.append(f"event aidiplo.61 {tag}")
            self.q.put(("action", f"{new_ideo} revolution: you lead the new regime"))
        else:
            # il player resta al governo; i rivoluzionari (nuova ideologia) insorgono.
            cmds.append(f"eval_effect {tag} = {{ set_variable = {{ aidiplo_cw_ideo = {new_code} }} }}")
            cmds.append(f"event aidiplo.61 {tag}")
            self.q.put(("action", f"Defend the government against the {new_ideo} rebels"))
        # GUERRA CIVILE = di fatto una rifondazione dello Stato: SUBITO DOPO la scelta dello
        # schieramento offriamo SEMPRE al player il reset dei focus (evento aidiplo.62), perche'
        # il vecchio albero dei focus non ha piu' senso dopo lo scontro interno. Va in coda, cosi'
        # arriva dopo l'eventuale cambio di ideologia/leader (aidiplo.60/61).
        cmds.append(f"event aidiplo.62 {tag}")
        self._dispatch(cmds)
        # il NOME del nuovo capo (guida della rivoluzione) lo mette l'evento aidiplo.60 via
        # set_country_leader_name = "AIDIPLO_LEADER" (chiave gia' scritta col nome sopra).
        self.memory.remember_fact("civil war",
                                  f"{tag}: chose {'revolution' if c == 1 else 'government'} ({new_ideo})")

    def _lang_directive(self, sample=None):
        """Istruzione di LINGUA. 'sample' = un campione del testo del player su cui fissare la
        lingua. Nei prompt di CHAT passiamo il messaggio CORRENTE (cosi' anche la PRIMISSIMA
        risposta della partita usa la lingua giusta: al primo messaggio la memoria e' ancora
        vuota e l'IA tirava a indovinare, finendo a volte in cinese/giapponese). Nei prompt
        AUTONOMI (giornale, bivi, completamenti) non c'e' un messaggio corrente: ripieghiamo sul
        PRIMO messaggio del player della partita.
        Se il player ha scelto una LINGUA FISSA al primo avvio, quella vince SEMPRE."""
        L = getattr(self, "ai_language", "")
        if L:
            return ("LANGUAGE (ABSOLUTE): write EVERY player-facing text — including your VERY "
                    "FIRST message — ONLY in " + L + ". Always use " + L + " regardless of the "
                    "player's input language or the nations discussed. Never use another language "
                    "and never mix languages. The internal 'reason' field may stay in English.")
        try:
            s = (sample or "").strip().replace("\n", " ")
            if not s:
                s = (self.memory.first_player_message() or "").strip().replace("\n", " ")
            s = s[:300]
        except Exception:
            s = ""
        if not s:
            return ("LANGUAGE: write everything in the SAME language the player is using. "
                    "Never switch to a different language than the player's.")
        return ("LANGUAGE (STRICT, decide BEFORE writing): FIRST read the player's words below "
                "and detect their language, THEN write EVERY player-facing text (title, message, "
                "narration) ONLY in that SAME language. Match it exactly. The nations/topic "
                "discussed must NOT change the output language. Player's words: \"" + s + "\".")

    def _newspaper(self, kind="state"):
        """Editoriale di giornale ON-DEMAND. Il player sceglie la TESTATA dalle decisioni:
        'state' (stampa di regime/governo), 'neutral' (stampa indipendente/estera equilibrata),
        'opposition' (stampa di opposizione/critica/clandestina). Ognuna ha voce e taglio diversi
        ma TUTTE devono restare aderenti ai fatti reali (nessuna invenzione)."""
        ctx = self._conversation_context("press")
        facts = self.memory.facts_block()
        ideo = self.latest_state.get("ideology_name", "?")
        voice = {
            "state": ("the STATE / PRO-GOVERNMENT newspaper: loyal to the current regime. It "
                      "frames events FAVOURABLY for the government and the leader, celebrates "
                      "successes, downplays setbacks, and rallies the nation. In a dictatorship "
                      "it is open propaganda; in a democracy it is the establishment paper."),
            "neutral": ("an INDEPENDENT / FOREIGN-NEUTRAL newspaper: balanced and factual, neither "
                        "cheerleading nor attacking. It reports developments soberly, weighs pros "
                        "and cons, and gives the measured outside view."),
            "opposition": ("the OPPOSITION / DISSIDENT press: critical of the current government. "
                           "It highlights failures, costs, repression, broken promises and risks, "
                           "and voices the grievances of those who oppose the regime (in a "
                           "dictatorship it is underground/banned; in a democracy it is the "
                           "opposition paper). Critical in TONE but still truthful."),
        }.get(kind, "the state newspaper")
        sysp = ("You are a JOURNALIST in the year shown by CURRENT WORLD STATE, writing the editorial of " + voice + "\n\n"
                + prompts.GROUNDING_RULE +
                "\nReport ONLY what the WORLD STATE, the WORLD SNAPSHOT and the GAME HISTORY "
                "MEMORY below actually show. Comment on those real developments only. If a war "
                "has just been declared, write that war has broken out / fighting is beginning "
                "— do NOT invent battles won, regions or cities captured, enemies defeated or "
                "casualty figures that are not in the data (a paper may be biased in TONE — "
                "favourable, neutral or hostile — but must NEVER fabricate concrete events that "
                "did not happen). If little of note has happened, write about the mood, politics "
                "or preparations instead.\n" + self._lang_directive() + "\nReply ONLY in JSON "
                '{"title":"<masthead or headline>","message":"<4-6 sentence article>"}.')
        usr = f"Regime: {ideo}. Paper type: {kind}.\n{ctx}\n{facts}"
        title, body = "The National Gazette", "The main news of the day."
        try:
            obj = LLMClient.parse_obj(self.llm.chat(
                [{"role": "system", "content": sysp}, {"role": "user", "content": usr}]))
            title = obj.get("title") or title
            body = obj.get("message") or body
        except Exception as e:
            self._log(f"[news] {e}")
        cmds = self.injector.prepare_dynamic_event(title, body, "Read.", "")
        self._dispatch(cmds)
        self.q.put(("assistant", {"name": "Press - " + title, "message": body}))

    def _find_civilwar_action(self, actions):
        """Cerca un'azione start_civil_war in una lista di azioni (ricompense o final_actions).
        Ritorna (cw_dict_oppure_None, lista_senza_quell'azione). Usa build_action per validare
        l'ideologia e ricavare l'eventuale leader, come nel percorso live."""
        cw, rest = None, []
        for a in (actions or []):
            if cw is None and isinstance(a, dict) and str(a.get("name")) == "start_civil_war":
                try:
                    res = functions.build_action(a.get("name"), a.get("args")) or {}
                    sp = res.get("special") or {}
                    if sp.get("type") == "civilwar":
                        cw = {"ideology": sp.get("ideology", "neutrality"),
                              "leader": sp.get("leader", ""),
                              "target": sp.get("target", "ROOT")}
                        continue   # rimossa dalla lista: la gestiamo a parte
                except Exception:
                    pass
            rest.append(a)
        return cw, rest

    def _complete_plan(self, plan):
        title = plan.get("title", "Plan")
        facts = plan.get("facts", "")
        narr = f"The plan '{title}' has come to fruition."
        verdict = "success"
        reward_actions = []
        eff_summary = ""           # spiegazione effetti in LINGUAGGIO NATURALE, lingua del player
        try:
            ctx = self._conversation_context("plan")
            mem = self.memory.facts_block()
            sysp = (prompts.GROUNDING_RULE + "\n" + prompts.OUTCOMES_RULE + "\n"
                    "You are the narrating voice of Hearts of Iron IV, resolving the FINAL "
                    "OUTCOME of a plan/undertaking that has now run its course. JUDGE whether it "
                    "SUCCEEDS, partially succeeds, or FAILS — based on how sound the player's plan "
                    "and their recorded crossroad choices were against the real situation "
                    "(PP/stability/support, the opposition, the timing). Most well-run plans "
                    "succeed; weak, rushed or reckless ones can fail. Keep failure UNCOMMON "
                    "(about one in four or fewer; more only for clearly foolish play). This is the "
                    "MAIN PAYOFF of the whole plan, so the reward ACTIONS must be the BIGGEST, most "
                    "MEANINGFUL of the run (bigger than any single crossroad), SCALED to the verdict: "
                    "FULL and substantial for SUCCESS, clearly REDUCED for PARTIAL, NONE (only "
                    "setbacks) for FAILURE.\n"
                    "THE REWARD MUST MATCH THE PLAN'S INTENDED RESULT shown below — this is "
                    "mandatory, pick the EXACT catalog effects that DELIVER what the plan promised: "
                    "an INDUSTRIAL/factory plan MUST give factories (add_factories) and/or "
                    "buildings/infrastructure (add_building: industrial_complex, arms_factory, "
                    "dockyard, infrastructure, synthetic_refinery, radar_station, air_base...); a "
                    "RESOURCE plan MUST give resources (add_resource); a REARMAMENT plan MUST give "
                    "equipment/divisions (send_equipment / add_equipment / add divisions); a "
                    "TERRITORIAL plan land/cores; a POLITICAL plan stability/war support/political "
                    "power/laws. NEVER substitute a generic stability or political-power reward for a "
                    "concrete industrial/military/resource one — that is WRONG. Use the WHOLE catalog "
                    "and combine SEVERAL fitting effects when sensible (e.g. an industrialisation "
                    "delivers civilian AND military factories AND some infrastructure). "
                    "Setbacks for PARTIAL/FAILURE = catalog numeric actions with NEGATIVE amounts "
                    "(e.g. add_political_power amount -120, add_stability amount -0.06).\n"
                    "CIVIL WAR — CRITICAL: if this plan was a COUP, UPRISING, SECESSION, REVOLUTION "
                    "or any attempt to seize/split the state, and the outcome is a CONTESTED split "
                    "(the country divides and the two sides actually FIGHT — not a clean, instant, "
                    "bloodless takeover), you MUST include the action {\"name\":\"start_civil_war\","
                    "\"args\":{\"ideology\":\"fascism|communism|democratic|neutrality\",\"leader\":"
                    "\"<name of the figure leading the breakaway side, if any>\"}}. This is what makes "
                    "the war REALLY break out and lets the player CHOOSE which side to fight on. NEVER "
                    "describe a civil war, insurrection or divided country in the narration WITHOUT "
                    "emitting this action — describing it in prose alone does nothing in-game. (Use it "
                    "for SUCCESS and PARTIAL; on FAILURE the attempt is simply crushed, no civil war.)\n"
                    + self._lang_directive() + "\n" + functions.catalog_text() + "\n"
                    "Reply ONLY in JSON: {\"verdict\":\"success|partial|failure\", \"message\":\"<2-4 "
                    "sentences narrating the REAL OUTCOME — do NOT restate the plan's original "
                    "description/summary, only what came of it, in the player's language; write PROSE "
                    "ONLY, NEVER any command, code, function name or '= {...}' syntax in the text>\", "
                    "\"actions\":[{\"name\":\"<catalog action>\",\"args\":{...}}, ...], "
                    "\"effects\":\"<a SHORT plain-language summary, IN THE PLAYER'S LANGUAGE, of the "
                    "CONCRETE results of those actions, for the player to read in (parentheses) — e.g. "
                    "'stability falls; +8 tungsten in the capital region; the Reform Strain national "
                    "spirit for 90 days'. Use EVERYDAY words and the real names of things; NEVER "
                    "function names, NEVER tokens like 'ai_diplo_...', NEVER 'ROOT', NEVER code or "
                    "'= {...}'. Separate items with ';'.>\"}. The actions are the realized rewards "
                    "(and, for partial/failure, the setbacks); they will be REALLY executed. Never "
                    "invent action names; never absurd amounts. The 'effects' text MUST match the "
                    "actions you emit.")
            clog = plan.get("choices_log") or []
            clog_txt = ("THE PLAYER'S CROSSROAD CHOICES DURING THIS PLAN (weigh them heavily: a "
                        "string of sound, decisive choices -> success and bigger rewards; reckless "
                        "or contradictory ones -> partial/failure):\n- " + "\n- ".join(str(x) for x in clog)
                        ) if clog else "The player made no explicit crossroad choices."
            usr = (f"Plan completed: '{title}'. Intended result: {facts}\n"
                   f"{clog_txt}\n\nGame history:\n{mem}\n{ctx}")
            obj = LLMClient.parse_obj(self.llm.chat(
                [{"role": "system", "content": sysp}, {"role": "user", "content": usr}]))
            narr = self._clean_narration(obj.get("message")) or narr
            v = str(obj.get("verdict", "success")).strip().lower()
            if v in ("success", "partial", "failure"):
                verdict = v
            reward_actions = obj.get("actions") or []
            if not isinstance(reward_actions, list):
                reward_actions = []
            eff_summary = self._clean_effect_desc(obj.get("effects") or "")
        except Exception as e:
            self._log(f"[plan] outcome failed: {e}")
        label = {"success": "Completed", "partly achieved": "Partly achieved",
                 "partial": "Partly achieved", "failure": "Failed"}.get(verdict, "Completed")
        # FALLIMENTO: nessuna ricompensa, solo eventuali contraccolpi (azioni con importo negativo).
        if verdict == "failure":
            reward_actions = [a for a in reward_actions if self._is_setback_action(a)]
        # GUERRA CIVILE come ESITO del piano (golpe/insurrezione/secessione): l'azione
        # start_civil_war e' uno 'special' e quindi NON passa dalle ricompense normali; la
        # intercettiamo qui (dalle ricompense dell'IA o dai final_actions del piano) e, dopo aver
        # raccontato l'esito, faremo SCEGLIERE al player il lato — esattamente come nel percorso
        # live. Vale per SUCCESS e PARTIAL; su FAILURE il tentativo viene solo represso.
        plan_final = plan.get("final_actions") or []
        civilwar = None
        if verdict in ("success", "partial"):
            civilwar, reward_actions = self._find_civilwar_action(reward_actions)
            if civilwar is None:
                civilwar, plan_final = self._find_civilwar_action(plan_final)
        # Trasforma le azioni di catalogo in COMANDI VALIDATI (che eseguono DAVVERO) e in
        # DESCRIZIONI PULITE leggibili (mai il comando grezzo). Le 'special' (regime/leader/eventi/
        # piani) NON vanno qui: gli esiti di regime passano da final_actions.
        reward_cmds, reward_descs = self._resolve_reward_actions(reward_actions)
        # RICOMPENSA PRINCIPALE = payload PIANIFICATO (gia' coerente col tipo di piano), scalato:
        #  - SUCCESSO: payload pieno + azioni-bonus dell'IA.
        #  - PARZIALE: payload APPLICATO COMUNQUE (la ricompensa fondamentale del piano arriva: un
        #    piano industriale deve dare fabbriche anche se solo in parte riuscito) + contraccolpi.
        #  - FALLIMENTO: niente payload, solo contraccolpi.
        payload_cmds = list(plan.get("payload", [])) if verdict in ("success", "partial") else []
        # RETE DI SICUREZZA — RICOMPENSA COERENTE COL TIPO: se il piano e' di un tipo concreto
        # (industria, infrastruttura, risorse, riarmo) ma NE' il payload NE' le azioni dell'IA hanno
        # prodotto un comando di quel tipo (es. e' uscita solo stabilita'/PP), aggiungiamo una
        # ricompensa di catalogo sensata e proporzionata di quel tipo: cosi' la ricompensa non e'
        # mai "fuori tema".
        supp_cmds, supp_descs = ([], [])
        if verdict in ("success", "partial"):
            supp_cmds, supp_descs = self._supplement_reward_by_type(
                plan, payload_cmds + reward_cmds, partial=(verdict == "partial"))
        # RIASSUNTO effetti fra (parentesi), SEPARATO dalla narrazione: preferiamo la spiegazione
        # in LINGUAGGIO NATURALE e nella LINGUA DEL PLAYER dell'IA ('effects'); se manca, descrizioni
        # PULITE del catalogo. La ricompensa-rete (se c'e') viene comunque aggiunta in coda.
        if eff_summary:
            tech_parts = [eff_summary]
        else:
            tech_parts = list(reward_descs)
            if verdict == "success":                    # esiti di catalogo (regime/leader/rinomina)
                for fa in plan_final:
                    try:
                        r2 = functions.build_action(fa.get("name"), fa.get("args")) or {}
                        if r2.get("desc"):
                            tech_parts.append(self._clean_effect_desc(r2["desc"]))
                    except Exception:
                        pass
        tech_parts += list(supp_descs)
        tech_parts = [p.strip() for p in tech_parts if p and p.strip()]
        tech = ("\n( " + "; ".join(tech_parts) + " )") if tech_parts else ""
        full = narr + tech
        cmds = self.injector.prepare_dynamic_event(f"{label}: {title}", full, "So be it.", "")
        cmds += payload_cmds + reward_cmds + supp_cmds
        self._dispatch(cmds)
        # ESITO di catalogo (golpe riuscito -> regime_change / set_leader / rename_country):
        # applicato SOLO a SUCCESSO pieno (un esito parziale non rovescia davvero il regime).
        # Se l'esito e' una GUERRA CIVILE, NON applichiamo qui un regime change "pulito": la
        # transizione la gestisce la scelta del lato (aidiplo.60/61) qui sotto.
        if plan_final and verdict == "success" and not civilwar:
            self._execute_actions(plan_final, narr)
        self.q.put(("assistant", {"name": "Chronicle", "message": f"[{verdict.upper()}] {full}"}))
        self.memory.remember_fact("plan", f"Plan '{title}' -> {verdict}")
        # GUERRA CIVILE: il piano (golpe/insurrezione) ha SPACCATO il paese -> ora il player sceglie
        # il lato, ESATTAMENTE come nel percorso live. Mettiamo in attesa la scelta e iniettiamo
        # l'evento-decisione con le due opzioni; alla scelta partira' _resolve_civilwar.
        if civilwar and verdict in ("success", "partial"):
            self.pending_civilwar = {"ideology": civilwar.get("ideology", "neutrality"),
                                     "leader": civilwar.get("leader", ""),
                                     "target": civilwar.get("target", "ROOT")}
            self.awaiting_choice = "civilwar"
            self.q.put(("action", f"Civil war ({civilwar.get('ideology','')}): choose your side"))
            self._dispatch(self.injector.prepare_decision_event(
                "Civil war",
                "The plan has split the nation into open civil war. Which side will you take?",
                "Lead the revolution", "Defend the legitimate government"))

    def _resolve_reward_actions(self, actions):
        """Trasforma azioni di catalogo (ricompense del piano) in (comandi_validati, descrizioni
        pulite). Esegue SOLO azioni dirette: le 'special' (regime/leader/eventi/piani) sono escluse
        qui (gli esiti di regime passano da final_actions). Le descrizioni sono quelle pulite di
        build_action, MAI il comando grezzo -> nel testo dell'evento non compaiono piu' comandi."""
        import functions as _fn
        cmds, descs, seen = [], [], set()
        for a in (actions or []):
            if not isinstance(a, dict):
                continue
            try:
                sig = json.dumps([a.get("name"), a.get("args")], sort_keys=True, default=str)
            except Exception:
                sig = str((a.get("name"), a.get("args")))
            if sig in seen:
                continue
            seen.add(sig)
            try:
                res = _fn.build_action(a.get("name"), a.get("args")) or {}
            except Exception:
                res = {}
            if not res or res.get("special"):
                continue
            c = res.get("commands", [])
            if c:
                cmds += c
                d = res.get("desc")
                if d:
                    descs.append(self._clean_effect_desc(d))
        return cmds, [d for d in descs if d]

    # Nomi leggibili degli "spiriti nazionali"/idee della mod (i token grezzi non vanno mostrati).
    _IDEA_NAMES = {
        "ai_diplo_war_drive": "War Drive",
        "ai_diplo_reform_strain": "Reform Strain",
        "ai_diplo_fortified": "Fortified Defensive Line",
    }

    def _clean_effect_desc(self, d):
        """Ripulisce una descrizione di effetto per mostrarla al player: toglie '[needs -debug]',
        il prefisso del tag ('ROOT:' / 'GER:'), e sostituisce i token grezzi delle idee con nomi
        leggibili. NON traduce: la traduzione nella lingua del player la fa l'IA col campo
        'effects' (questa resta il ripiego pulito in inglese)."""
        s = re.sub(r"\s*\[needs -debug\]", "", str(d or "")).strip()
        s = re.sub(r"^(?:ROOT|[A-Z]{3})\s*:\s*", "", s)       # via il prefisso del tag
        for tok, name in self._IDEA_NAMES.items():
            s = s.replace(tok, name)
        return s.strip(" ;,-")

    def _supplement_reward_by_type(self, plan, cmds_so_far, partial=False):
        """RETE DI SICUREZZA per le ricompense dei piani: deduce il TIPO di piano da titolo+facts e,
        se la ricompensa prodotta finora NON contiene gia' un comando di quel tipo, aggiunge una
        ricompensa di catalogo sensata e proporzionata (evita che un piano industriale dia 'solo
        stabilita''). Ritorna (comandi, descrizioni_pulite). Conservativa: scatta solo quando manca
        del tutto l'effetto di quel tipo."""
        import functions as _fn
        blob = ((plan.get("title", "") or "") + " " + (plan.get("facts", "") or "")).lower()
        have = " ".join(cmds_so_far).lower()
        tgt = plan.get("target") or self.latest_state.get("tag", "") or "ROOT"
        out_cmds, out_descs = [], []

        def add(name, args):
            try:
                r = _fn.build_action(name, args) or {}
            except Exception:
                return
            if not r or r.get("special"):
                return
            c = r.get("commands", [])
            if c:
                out_cmds.extend(c)
                if r.get("desc"):
                    out_descs.append(self._clean_effect_desc(r["desc"]))

        def hasw(words):
            return any(w in blob for w in words)

        # INDUSTRIA / FABBRICHE
        if hasw(("industri", "factor", "fabbric", "manufact", "arsenal", "rearm", "riarmo",
                 "war production", "produzione bellica", "five-year", "quinquennal")) \
           and not any(k in have for k in ("add_building_construction", "arms_factory",
                                           "industrial_complex", "dockyard")):
            mil = hasw(("military", "militar", "arms", "weapon", "bellic", "rearm", "riarmo", "naval"))
            add("add_factories", {"target": tgt, "kind": "military" if mil else "civilian",
                                  "amount": 2 if partial else 4})
        # INFRASTRUTTURA / EDIFICI speciali
        if hasw(("infrastrutt", "infrastructure", "railway", "ferrov", "road", "strad", "refiner",
                 "raffiner", "radar", "airbase", "air base", "aeroport", "harbour", "porto")) \
           and "add_building" not in have and "add_building_construction" not in have:
            btype = ("synthetic_refinery" if hasw(("refiner", "raffiner")) else
                     "radar_station" if "radar" in blob else
                     "air_base" if hasw(("airbase", "air base", "aeroport")) else
                     "infrastructure")
            add("add_building", {"target": tgt, "building": btype, "level": 1 if partial else 2})
        # RISORSE
        if hasw(("risors", "resource", "oil", "petrol", "steel", "acciaio", "tungsten", "rubber",
                 "gomma", "alumin", "allumin", "chrom", "cromo", "mine", "miniera")) \
           and "add_resource" not in have:
            res = ("oil" if hasw(("oil", "petrol")) else
                   "tungsten" if "tungsten" in blob else
                   "rubber" if hasw(("rubber", "gomma")) else
                   "aluminium" if hasw(("alumin", "allumin")) else
                   "chromium" if hasw(("chrom", "cromo")) else "steel")
            add("add_resource", {"target": tgt, "resource": res, "amount": 4 if partial else 8})
        # RIARMO / EQUIPAGGIAMENTO (send_equipment senza mittente = aggiunge senza sottrarre)
        if hasw(("equipaggiam", "equipment", "fucil", "rifle", "weapons", "armament", "munizion",
                 "tank", "carri", "aircraft", "aerei", "guns")) \
           and "add_equipment_to_stockpile" not in have:
            arch = ("artillery_equipment" if hasw(("artiller", "guns", "cannon")) else
                    "support_equipment" if hasw(("support", "supporto")) else
                    "infantry_equipment")
            add("send_equipment", {"receiver": tgt, "equipment_type": arch,
                                   "quantity": 2000 if partial else 5000})
        return out_cmds, out_descs

    def _is_setback_action(self, a):
        """True se l'azione e' un contraccolpo (importo negativo): da tenere anche col fallimento."""
        try:
            import functions as _fn
            args = a.get("args") or {}
            for k in ("amount", "value", "delta", "qty"):
                if k in args and _fn._num(args.get(k), 0) < 0:
                    return True
        except Exception:
            pass
        return False

    def _clean_narration(self, text):
        """Difesa extra per i testi mostrati al player: toglie eventuali formule di RIFIUTO del
        modello e frammenti di COMANDO grezzo finiti nella prosa (con la lingua forzata non
        dovrebbero piu' uscire, ma cosi' non possono comunque contaminare l'evento)."""
        s = (text or "").strip()
        if not s:
            return ""
        low = s.lower()
        for bad in ("as an ai language model", "as a language model", "i am an ai",
                    "作为一个", "人工智能语言模型", "language model"):
            i = low.find(bad)
            if i != -1:
                s = s[:i].strip()
                low = s.lower()
        # togli blocchi che sembrano comandi grezzi: 'nome = { ... }'
        s = re.sub(r"[A-Za-z_]\w*\s*=\s*\{[^{}]*\}", "", s)
        return s.strip(" ;,-\n\t")

    # ---------------- EVENTI INTERATTIVI (ogni evento = un CANALE del pannello) ----------------
    def _event_can_open(self):
        """Non aprire eventi se ce n'e' gia' uno attivo, o se c'e' un popup di gioco in
        sospeso (bivio piano / scelta guerra civile), o se l'IA sta gia' rispondendo."""
        return (self.events_enabled and self.event is None
                and self.awaiting_choice is None and self.pending_civilwar is None
                and not self._pending_plan_slots
                and not self.ui.busy and bool(self.latest_state))

    def _start_event(self, title, situation, kind="", role="leader"):
        # si arriva qui SOLO dalla 3a opzione di un bivio in gioco (c=3): apre/sostituisce
        # il canale evento per quel bivio. Niente apertura automatica altrove.
        self._event_counter += 1
        ch = f"event:{self._event_counter}"
        title = title or "Developing Situation"
        self.event = {"channel": ch, "title": title, "kind": kind, "role": role,
                      "history": [{"role": "assistant", "content": situation}],
                      "story_done": False, "last_phase_done": False, "last_next": 0}
        self.memory.add_message(ch, "assistant", situation)
        # apre un NUOVO canale nel pannello (con la sua cronologia)
        self.q.put(("event_open", {"channel": ch, "title": title}))

    # vecchia callback della finestra centrale (non piu' usata: eventi = canali)
    def event_from_ui(self, text):
        return
    def on_event_close_from_ui(self):
        return

    def _park_active_event(self):
        """Il player ha lasciato il canale dell'evento (ha aperto un altro canale): lo
        chiudiamo e basta. NON si riapre da solo: il piano prosegue comunque con i suoi
        BIVI in gioco, dove il player puo' di nuovo scegliere 'scrivi tu' (3a opzione)."""
        self.event = None

    def _do_event_turn(self, text, kickoff=False):
        """Un turno dell'evento, nel CANALE corrente (aperto SOLO dalla 3a opzione di un
        bivio). Il player scrive la sua mossa per quel bivio; l'IA ne narra l'esito e
        applica le conseguenze. Il piano prosegue poi con i suoi bivi in gioco."""
        try:
            ev = self.event
            if ev is None:
                return
            ch = ev["channel"]
            if text:
                ev["history"].append({"role": "user", "content": text})
                self.memory.add_message(ch, "user", text)
            ptag = self.latest_state.get("tag", "ROOT")
            pname = self.latest_state.get("name", "our nation")
            leader = self.latest_state.get("leader", "the Head of State")
            sysp = prompts.build_crossroad_system(pname, ptag, leader,
                                                  ev.get("role", "leader"), ev["title"])
            head = (f"CROSSROADS of plan: {ev['title']}\n{self._conversation_context(ch)}\n\n"
                    f"{self.memory.facts_block()}")
            msgs = ([{"role": "system", "content": sysp},
                     {"role": "user", "content": head}] + ev["history"][-12:])
            raw = self.llm.chat(msgs)
            obj = LLMClient.parse_obj(raw)
            # MAI perdere la risposta: se il JSON non si parsa, mostra il testo grezzo
            narr = (obj.get("narration") or "").strip() or LLMClient.loose_text(raw) or "..."
            ev["history"].append({"role": "assistant", "content": narr})
            self.memory.add_message(ch, "assistant", narr)
            self.q.put(("assistant", {"name": ev["title"], "message": narr}))
            # conseguenze: MAI azioni che avvierebbero una NUOVA catena (start_plan/interactive);
            # questa e' solo la risoluzione del bivio corrente.
            acts = [a for a in (obj.get("actions") or [])
                    if a.get("name") not in ("start_plan", "start_interactive")]
            if acts:
                self._execute_actions(acts, "")
            news = (obj.get("newspaper") or "").strip()
            if news:
                self._dispatch(self.injector.prepare_dynamic_event(ev["title"], news, "Read.", ""))
            # ricorda la mossa+esito: cosi' i BIVI SUCCESSIVI dello stesso piano ne tengono conto
            if text:
                self.memory.remember_fact("crossroad",
                    f"In plan '{ev['title']}', player's move: {text[:120]} | result: {narr[:160]}")
        except Exception as e:
            self.q.put(("sysnote", f"Event error: {e}"))
        finally:
            self.q.put(("busy", False))

    def _open_event_channel(self, info):
        """Apre (sul pannello) il canale di un evento interattivo: header, cronologia di
        QUEL evento, pannello in primo piano. self.event e' gia' impostato da chi chiama."""
        self.channel = info["channel"]
        self.channel_name = info["title"]
        self.ui.set_header(f"Event — {info['title']}")
        self._show_channel_history()
        self.ui.set_busy(False)
        self._raise()

    def _on_open(self, info):
        try:
            contact = gs.diplomatic_contact(info, self.latest_state, self.world)
        except ValueError as exc:
            self.ui.append_system(str(exc))
            return
        self._park_active_event()   # se stava giocando un evento, lo riprende piu' avanti
        tag, tname, tleader = contact["target"], contact["tname"], contact["tleader"]
        self.diplomatic_contacts[tag] = contact
        self.channel = f"leader:{tag}"
        self.channel_name = tleader
        player_name = self.latest_state.get("name", "our nation")
        self.system_prompt = prompts.build_leader_system(tag, tname, tleader, player_name,
                                                        contact["player"])
        self.ui.set_header(f"Dialogue with {tleader} — {tname} ({tag})")
        self._show_channel_history()
        self.ui.append_system(f"Diplomatic channel opened with {tleader} ({tname}).")
        self._log(f"[diplomacy] player={contact['player']} target={tag} leader={tleader} "
                  f"source={info.get('source', 'game_button')}")
        self._raise()

    def _conversation_context(self, channel):
        if isinstance(getattr(self, "llm", None), BudgetClient):
            actor = channel.split(":", 1)[1] if channel.startswith("leader:") else None
            context = compact_context(self.latest_state, self.world, actor)
        else:
            context = gs.context_for_llm(self.latest_state, self.world, self.states)
        if channel.startswith("leader:"):
            tag = channel.split(":", 1)[1]
            contact = self.diplomatic_contacts.get(tag, {})
            context += "\n\n" + gs.diplomatic_context(self.latest_state, self.world, contact)
        lore = getattr(self, "lore", None)
        if lore:
            context += "\n\n" + lore.briefing(self.latest_state, self.world, channel)
        return context

    def _on_decree(self):
        self._park_active_event()
        ptag = self.latest_state.get("tag", "ROOT")
        pname = self.latest_state.get("name", "our nation")
        self.channel = "decree"
        self.channel_name = "Supreme Command"
        self.system_prompt = prompts.build_decree_system(ptag, pname)
        self.ui.set_header("Head of State Office / Supreme Command")
        self._show_channel_history()
        self.ui.append_system("Dictate your orders in natural language.")
        self._raise()

    def _on_life(self, info):
        self._park_active_event()
        ptag = info.get("tag") or self.latest_state.get("tag", "ROOT")
        pname = self.latest_state.get("name", "our nation")
        leader = info.get("leader") or self.latest_state.get("leader", "the Head of State")
        self.channel = "life"
        self.channel_name = leader
        self.system_prompt = prompts.build_leaderlife_system(ptag, pname, leader)
        self.ui.set_header(f"Life of the Head of State — {leader} ({pname})")
        self._show_channel_history()
        self.ui.append_system("Tell what your leader does today: speeches, "
                              "meetings, inspections, journeys, personal decisions...")
        self._raise()

    def _on_actor(self, info):
        self._park_active_event()
        ptag = (info or {}).get("tag") or self.latest_state.get("tag", "ROOT")
        pname = self.latest_state.get("name", "our nation")
        self.channel = "actor"
        self.channel_name = "Underground"
        self.system_prompt = prompts.build_actor_system(ptag, pname)
        self.ui.set_header(f"Act as another figure — {pname}")
        self._show_channel_history()
        self.ui.append_system("You are NOT the head of state here. First tell WHO you are "
                              "inside the nation (a faction, a general, a conspirator, a "
                              "movement) and what you intend to do — e.g. plotting a coup "
                              "against the current government.")
        self._raise()

    def _show_channel_history(self):
        """Shows ONLY the current channel's history (for this game, up to the current
        day). Isolates conversations: opening France does not show the chat with the
        United Kingdom, etc. Single shared panel."""
        self._awaiting_leader_name = False   # aprendo un canale, annulla l'attesa del nome capo
        self.ui.clear_history()
        # Anti-leak safety: until the current game is identified (valid gid from the
        # export), do NOT load any history, so another campaign's chat cannot appear.
        # The history is filtered by gid+day anyway.
        if self.gid <= 0:
            return
        for m in self.memory.get_history(self.channel, self.max_history):
            if m.get("role") == "user":
                self.ui.append_player(m.get("content", ""))
            else:
                self.ui.append_ai(self.channel_name, m.get("content", ""))

    def _raise(self):
        try:
            self.ui.show_panel()
        except Exception:
            pass

    # ---------------- commands from the overlay buttons ----------------
    def set_mode_from_ui(self, mode):
        if mode not in ("realistic", "plausible", "sandbox"):
            return
        self.difficulty = mode
        prompts.set_difficulty(mode)
        try:
            os.makedirs(os.path.dirname(self.mode_file), exist_ok=True)
            open(self.mode_file, "w", encoding="utf-8").write(mode)
        except Exception:
            pass
        label = next((label for label, key, _ in prompts.realism_mode_options() if key == mode), mode)
        self.q.put(("sysnote", f"Realism mode set to: {label}."))
        # PRIMO AVVIO: SUBITO DOPO aver scelto la modalita' (e DOPO che il selettore difficolta'
        # si e' chiuso), chiedi in che LINGUA deve parlare l'IA. Schedulato con after() cosi' il
        # box lingua esce DOPO la schermata difficolta', non sopra/prima di essa.
        if not self.ai_language:
            try:
                self.ui.after(250, self._ask_language)
            except Exception:
                self._ask_language()

    def _ask_language(self):
        """Chiede al player (testo libero, nessuna scelta predefinita) in che lingua l'IA deve
        parlare/scrivere. La risposta viene salvata e iniettata nei prompt: cosi' anche il PRIMO
        messaggio dell'IA esce nella lingua giusta (niente piu' cinese 'a caso')."""
        lang = None
        try:
            from tkinter import simpledialog
            lang = simpledialog.askstring(
                "AI language",
                "In which language should the AI speak and write?\n"
                "Type it freely (no preset list), e.g.: Italiano, English, Espanol, Deutsch, "
                "Francais, Portugues, 日本語, ...",
                parent=getattr(self.ui, "root", None))
        except Exception as e:
            self._log(f"[language] {e}")
        if lang and lang.strip():
            self.ai_language = lang.strip()[:40]
            prompts.set_language(self.ai_language)
            try:
                open(self.lang_file, "w", encoding="utf-8").write(self.ai_language)
            except Exception as e:
                self._log(f"[language] save: {e}")
            self.q.put(("sysnote", f"The AI will always speak in: {self.ai_language}."))
        else:
            # niente scelta: ripiego sulla lingua del messaggio del player (verra' richiesto al
            # prossimo avvio, dato che non salviamo nulla).
            self.q.put(("sysnote", "No language chosen — the AI will follow the language you "
                                   "write in. You'll be asked again next start."))

    def decree_from_ui(self):
        self._on_decree()

    def diplomacy_from_ui(self):
        if self.gid <= 0:
            self.ui.append_system("Load your campaign and open Head of State Office to sync its countries.")
            return
        countries = gs.diplomatic_roster(self.latest_state, self.world)
        if not countries:
            self.ui.append_system("No foreign countries are synchronized yet. Open Head of State Office "
                                  "from the in-game Decisions menu, then try again.")
            return
        campaign = (self.gid, self.latest_state.get("tag"))
        def selected(contact):
            if campaign != (self.gid, self.latest_state.get("tag")):
                self.ui.append_system("Campaign changed while choosing a country. Open the country list again.")
                return
            self._on_open(dict(contact, source="country_picker"))
        self.ui.choose_country(countries, selected)

    def life_from_ui(self):
        self._on_life({})

    def test_injection(self):
        if self.cfg.get("unified"):
            self.ui.append_system("The old +500 test is disabled. Use an approved, bounded unified action test.")
            return
        self.ui.append_system(
            "TEST running: bringing HoI4 to the foreground and running 'pp 500' + "
            "'eval_effect add_political_power = 500'. PP +500 = direct commands ok; "
            "+1000 = -debug also ok. (Fallback: console -> run aidiplo.txt)")

        def run():
            self.injector.write_run_file(["pp 500", "eval_effect add_political_power = 500"])
            self.injector.execute()
        threading.Thread(target=run, daemon=True).start()

    # ---------------- invio messaggio ----------------
    def on_send(self, text):
        # FALLBACK 'Impose Head of State': se stiamo aspettando il NOME, QUESTO invio E' il nome
        # (non va all'IA). Usa la casella del pannello, sempre visibile (niente popup buggato).
        if getattr(self, "_awaiting_leader_name", False):
            self._awaiting_leader_name = False
            self._apply_fix_leader_name(text)
            return
        if not self.channel:
            self.ui.append_system("Open a channel first: in HoI4 use the DECISION "
                                  "'Head of State Office', or open another country's "
                                  "diplomacy screen and click 'Talk to the Leader (AI)'.")
            return
        if self.channel.startswith("event:"):     # canale di un evento interattivo
            self.ui.set_busy(True)
            threading.Thread(target=self._do_event_turn, args=(text,), daemon=True).start()
            return
        self.ui.set_busy(True)
        threading.Thread(target=self._do_llm, args=(text, self.channel,
                         self.channel_name, self.system_prompt), daemon=True).start()

    def _do_llm(self, text, channel, channel_name, system_prompt):
        try:
            if getattr(self, "cfg", {}).get("unified"):
                self._unified_turn(text, channel, channel_name)
                return
            turn_state = dict(self.latest_state)
            no_actions = text_only(text)
            if channel.startswith("leader:"):
                tag = channel.split(":", 1)[1]
                contact = gs.diplomatic_contact(self.diplomatic_contacts.get(tag, {}),
                                                self.latest_state, self.world)
                channel_name = contact["tleader"]
                system_prompt = prompts.build_leader_system(
                    tag, contact["tname"], channel_name,
                    self.latest_state.get("name", "our nation"), contact["player"])
                self.q.put(("diplomatic_identity", {"channel": channel, "tag": tag,
                            "name": channel_name, "country": contact["tname"]}))
            history = self.memory.get_history(channel, self.max_history)
            system_prompt = prompts.refresh_realism(system_prompt)
            context = self._conversation_context(channel)
            wrapped = prompts.wrap_user(text, context, self.memory.facts_block())
            # RINFORZO LINGUA (anche in chat): gli fissiamo la lingua sul messaggio CORRENTE del
            # player, cosi' anche la PRIMISSIMA risposta della partita e' nella lingua giusta
            # (prima, al primo messaggio, la memoria era vuota e l'IA tirava a indovinare -> a
            # volte cinese/giapponese).
            wrapped += "\n\n[" + self._lang_directive(text) + "]"
            # RINFORZO MIRATO: nel canale "act as another figure", finche' non c'e' un piano
            # attivo, l'IA deve LANCIARE subito l'impresa (start_plan) alla prima descrizione,
            # senza aspettare un secondo messaggio di conferma. (Quando un piano e' gia' attivo
            # il promemoria sparisce: niente piani doppi.)
            if not no_actions and channel == "actor" and not any(self.plan_slots):
                wrapped += ("\n\n[SYSTEM REMINDER: if the player has described ANY plot, coup or "
                            "undertaking — even merely planning it — you MUST emit a start_plan "
                            "action NOW in this reply (stages >= 5 for a coup). Do NOT wait for a "
                            "confirmation message, do NOT only narrate or say it 'is in "
                            "preparation'. Launch it.]")
            elif not no_actions and channel == "decree" and self._looks_like_plan_launch(text, ""):
                wrapped += ("\n\n[SYSTEM REMINDER: the player is asking to LAUNCH a long-term plan/"
                            "undertaking (industrialisation, rearmament, a programme that unfolds "
                            "over time). You MUST emit a start_plan action in THIS reply, with a "
                            "sensible 'stages' (e.g. 2-4) and 'days', plus the immediate COST. Do "
                            "NOT merely narrate that 'the plan begins' without the start_plan "
                            "action — the start_plan is what makes the in-game crossroad events "
                            "appear over time.]")
            messages = ([{"role": "system", "content": system_prompt}] +
                        history + [{"role": "user", "content": wrapped}])
            if isinstance(self.llm, BudgetClient):
                ceiling = self.llm.budget.settings["max_input_chars"] - len(request_instructions(self.cfg))
                while history and sum(len(m["content"]) for m in messages) > ceiling:
                    history = history[2:]
                    messages = [messages[0], *history, messages[-1]]

            raw = (self.llm.interactive_chat(messages) if isinstance(self.llm, BudgetClient)
                   else self.llm.chat(messages))
            if (self.latest_state.get("gid") != turn_state.get("gid")
                    or self.latest_state.get("tag") != turn_state.get("tag")
                    or int(self.latest_state.get("day", 0)) < int(turn_state.get("day", 0))):
                self.q.put(("sysnote", "A reply from the previous campaign snapshot was discarded."))
                return
            parsed = LLMClient.parse_ai_json(raw)
            msg = parsed.get("message", "")
            actions = parsed.get("actions", []) or []

            name = channel_name or "AI"
            self.q.put(("assistant", {"name": name, "message": msg}))

            # SAFETY NET (fixes "only chat, no in-game event/effects"): a weak local model
            # sometimes writes ONLY the narration and FORGETS the structured "actions" JSON, so
            # nothing reaches the game. When that happens in a channel where an action is
            # normally expected (orders to the staff, launching a plot, a narrated reform), we
            # ask the model ONCE more to turn its OWN narration into the concrete in-game
            # action(s) — same channel rules, so start_plan / immediate-vs-deferred / costs all
            # still apply. If the narration was pure talk, it returns no actions and nothing runs.
            if (not no_actions and getattr(self, "cfg", {}).get("behavior", {}).get("action_extraction", not getattr(self, "cfg", {}).get("unified", False))
                    and self.auto_exec and not actions and self._needs_action_extraction(channel, msg)):
                recovered = self._extract_actions(channel, text, msg, system_prompt)
                if recovered:
                    actions = recovered
                    self._log(f"[extract] recovered {len(recovered)} omitted action(s)")

            # ULTIMA RETE: se il player ha CHIESTO esplicitamente un piano/impresa a lungo termine
            # ma il modello non ha emesso NESSUNA azione (ne' alla prima risposta ne' in
            # estrazione), creiamo NOI il start_plan dalla narrazione: cosi' un piano richiesto
            # PARTE SEMPRE (annuncio + bivi scanditi nel tempo + completamento), invece di restare
            # solo chiacchiere in chat. I bivi e la ricompensa li genera comunque l'IA strada
            # facendo. Solo nei canali dove un piano ha senso e solo se non ce n'e' gia' uno.
            if (not no_actions and self.auto_exec and not actions and channel in ("decree", "actor")
                    and self._looks_like_plan_launch(text, msg)
                    and not self._is_rejection(msg)):
                stg = 5 if channel == "actor" else 3
                actions = [{"name": "start_plan", "args": {
                    "title": self._derive_plan_title(text), "summary": (msg or text),
                    "days": 150, "stages": stg}}]
                self._log("[plan] synthesized start_plan (model emitted no action)")

            # PROPOSTA RIFIUTATA -> NIENTE PIANO/CROSSROAD. Se la risposta dell'IA e' un RIFIUTO
            # netto (il consiglio respinge, impossibile sotto questo regime, ...), non ha senso
            # avviare un piano a tappe: rimuoviamo eventuali start_plan (sia sintetizzati sia
            # emessi per errore dal modello mentre narrava il rifiuto). Le altre azioni restano.
            if actions and self._is_rejection(msg):
                kept = [a for a in actions if (a.get("name") or "") != "start_plan"]
                if len(kept) != len(actions):
                    self._log("[reject] AI response is a rejection -> dropped start_plan (no crossroad)")
                actions = kept

            # EQUIPMENT da un'altra nazione: nel canale di dialogo con un leader straniero, se l'IA
            # non indica il mittente lo deduciamo (= la nazione con cui parla il player), cosi'
            # l'equip viene DAVVERO sottratto a chi lo manda; e se ha dimenticato/sbagliato il tipo
            # lo deduciamo da cio' che il player ha chiesto (es. "support equipment").
            self._augment_equipment_actions(actions, channel, text, msg)
            # niente cambi di IDEOLOGIA non richiesti: un semplice cambio di capo/nome mantiene
            # l'ideologia attuale, a meno che il player non chieda esplicitamente un cambio regime.
            self._guard_leader_ideology(actions, text, msg)
            # gli SLOT DI RICERCA non si concedono SUBITO: rappresentano centri di ricerca da
            # costruire -> li spostiamo nell'esito di un piano a tempo (arrivano alla FINE).
            actions = self._defer_research_slots(actions, msg)

            # persist AFTER the optional extraction so the extra turn is not duplicated in history
            if int(turn_state.get("gid", 0)) > 0:
                self.memory.add_turn(channel, text, msg, int(turn_state["gid"]), int(turn_state.get("day", 0)))
            else:
                self.memory.add_message(channel, "user", text)
                self.memory.add_message(channel, "assistant", msg)

            if self.auto_exec and not no_actions:
                self._last_player_text = text or ""   # per classificare costo/guadagno di PP
                self._execute_actions(actions, msg)
        except Exception as e:
            self.q.put(("sysnote", f"LLM error: {e}"))
        finally:
            self.q.put(("busy", False))

    def _needs_action_extraction(self, channel, ai_message):
        """Decide whether to run the action-extraction safety net when the first reply produced
        NO actions. We run it for every channel where an action can be expected; the extraction
        itself reuses each channel's OWN rules (e.g. the BILATERAL rule for diplomacy, the
        'apply effects only if justified' rule for the head-of-state's life), so it returns
        nothing while merely negotiating/chatting and fires only when a real change happened."""
        if channel == "actor":
            return not any(self.plan_slots)        # this channel exists to LAUNCH a plan
        # decree (orders), foreign-leader DIALOGUE (concluded pacts/alliances/cessions/aid or a
        # unilateral declaration of war) and the head-of-state's LIFE (a speech granting war
        # support, a reform...) all routinely produce real game actions that the weak local
        # model often narrates but forgets to emit as JSON. Run the safety net for them too.
        return channel == "decree" or channel == "life" or channel.startswith("leader:")

    # parole che indicano che il player vuole lanciare un PIANO / impresa a lungo termine
    _PLAN_WORDS = ("piano", "plan", "programma", "program", "operazione", "operation",
                   "campagna", "campaign", "complotto", "cospiraz", "conspir", "colpo di stato",
                   "coup", "golpe", "putsch", "industrializ", "potenziamento", "rearm", "riarmo",
                   "five-year", "quinquennale", "fortific", "progetto", "project", "iniziativa",
                   # progetti che si COSTRUISCONO nel tempo -> devono diventare piani a tappe
                   "minier", "mining", "miniera", "giacimen", "deposit", "estrazion", "extraction",
                   "scavare", "trivell", "ricerca", "research", "centro di ricerca", "research centre",
                   "research center", "esplora", "prospect", "sviluppo industriale", "raffineria",
                   "refinery", "cantier", "costruzione di")

    def _looks_like_plan_launch(self, text, msg):
        """True se il messaggio del player (o la narrazione) descrive l'avvio di un piano/impresa
        a lungo termine: usato SOLO come ultima rete quando il modello non emette nessuna azione."""
        blob = ((text or "") + " " + (msg or "")).lower()
        return any(w in blob for w in self._PLAN_WORDS)

    # marcatori FORTI di rifiuto (IT/EN). Solo segnali netti: evitiamo "non/cannot/won't" generici
    # che comparirebbero anche in frasi di accettazione ("non sara' facile, ma lo faremo").
    _REJECT_WORDS = ("respin", "rifiut", "impossibil", "inaccettabil", "non se ne parla",
                     "non se ne fa", "fuori discussione", "non possiamo accett", "non posso accett",
                     "non accett", "vietat", "proibit", "negiam", "respingiam", "declin",
                     "we reject", "rejected", "we refuse", "refused", "refusal", "denied",
                     "we deny", "we decline", "out of the question", "unacceptable", "forbidden",
                     "cannot be done", "will not allow", "won't allow", "not under")

    def _is_rejection(self, msg):
        """True se la risposta dell'IA e' un RIFIUTO netto della proposta del player. Usato per NON
        avviare piani/crossroad su una proposta respinta."""
        blob = (msg or "").lower()
        return any(w in blob for w in self._REJECT_WORDS)

    # parole che indicano che il player VUOLE davvero cambiare ideologia/ordinamento (IT/EN)
    _IDEO_CHANGE_WORDS = ("comunis", "communis", "bolsce", "bolshe", "marxis", "fascis", "nazis",
                          "nazional", "democra", "monarch", "regno", "king", "royal", "corona",
                          "imperatore", "emperor", "repubblic", "republic", "ideolog", "regime",
                          "sovi", "soviet", "dittatur", "dictator", "autorit", "teocra", "theocra",
                          "rivoluz", "revolution", "golpe", "coup", "rovescia", "overthrow")

    # parole che indicano un COLPO DI STATO (presa del potere forzata/irregolare): se accompagnato
    # da un cambio di capo/governo, offriamo il reset dei focus (lo Stato e' di fatto rifondato).
    _COUP_WORDS = ("golpe", "colpo di stato", "coup", "putsch", "overthrow", "rovesci", "depost",
                   "depone", "depongo", "insurrezion", "uprising", "seize power", "seized power",
                   "presa del potere", "marcia su roma", "march on", "junta", "giunta militare",
                   "pronunciamiento", "takeover", "take over the government", "usurp", "spodest")

    def _guard_leader_ideology(self, actions, text, msg):
        """Evita cambi di IDEOLOGIA non richiesti. Se il player NON chiede esplicitamente un
        cambio di regime/ideologia, una set_leader (cambio del capo/del nome) deve MANTENERE
        l'ideologia ATTUALE: forziamo args['ideology'] a quella corrente, cosi' set_politics nel
        gioco non sposta il partito al governo."""
        if not actions:
            return
        # IMPORTANTE: guardiamo SOLO il messaggio del PLAYER, non la narrazione dell'IA: l'IA
        # cita quasi sempre l'ideologia ("il nuovo leader fascista...", "regime"...), e se la
        # includessimo il guard si auto-disattiverebbe sempre. L'intento di cambiare regime sta
        # nelle parole del player.
        blob = (text or "").lower()
        if any(w in blob for w in self._IDEO_CHANGE_WORDS):
            return                                   # cambio esplicitamente voluto: non toccare
        try:
            cur_name = {1: "fascism", 2: "communism", 3: "democratic",
                        4: "neutrality"}[int(float(self.latest_state.get("ideo")))]
        except Exception:
            return                                   # ideologia attuale ignota: non forzare nulla
        for a in actions:
            if isinstance(a, dict) and (a.get("name") or "") == "set_leader":
                args = a.setdefault("args", {})
                if isinstance(args, dict):
                    args["ideology"] = cur_name

    def _defer_research_slots(self, actions, msg):
        """Uno SLOT DI RICERCA non va concesso ALL'ISTANTE da un decreto: rappresenta un centro di
        ricerca / l'addestramento di scienziati, cosa che si COSTRUISCE nel tempo. Se l'IA lo mette
        come azione IMMEDIATA, lo togliamo dall'immediato e lo consegniamo come ESITO di un piano a
        tempo (arriva alla FINE). Gli altri effetti immediati (i costi: PP, stabilita', spirito) e un
        eventuale start_plan gia' presente restano. Ritorna la lista azioni ripulita."""
        if not actions:
            return actions
        slot_total = 0
        rest = []
        for a in actions:
            if isinstance(a, dict) and a.get("name") in ("add_research_slot", "research_slot"):
                amt = int(functions._num((a.get("args") or {}).get("amount"), 1))
                if amt > 0:
                    slot_total += amt
                    continue                      # rimuovi dalle azioni immediate
            rest.append(a)
        if slot_total <= 0:
            return actions                        # niente slot positivi immediati: lascia com'era
        tag = self.latest_state.get("tag", "") or "ROOT"
        slot_act = {"name": "add_research_slot", "args": {"amount": slot_total, "target": tag}}
        # se nella stessa risposta c'e' gia' un piano, aggiungi lo slot al SUO esito; altrimenti
        # crea un piano dedicato che lo consegna al completamento.
        plan = next((a for a in rest if isinstance(a, dict) and a.get("name") == "start_plan"), None)
        if plan is not None:
            pa = plan.setdefault("args", {})
            oc = pa.get("outcome") or pa.get("final_actions") or []
            if not isinstance(oc, list):
                oc = []
            oc.append(slot_act)
            pa["outcome"] = oc
        else:
            rest.append({"name": "start_plan", "args": {
                "title": "Research Programme",
                "days": 180, "stages": 0,
                "summary": (str(msg or "").strip()[:200] or
                            "A new research effort gets under way; it will bear fruit once built."),
                "outcome": [slot_act]}})
            self.q.put(("action", "A research slot is earned when the facility is BUILT: started as "
                                  "a timed plan, granted at completion (not instantly)."))
        return rest

    def _augment_equipment_actions(self, actions, channel, text, msg):
        """Rete per send_equipment nel dialogo con un leader straniero: (1) se manca il MITTENTE e
        il player sta RICEVENDO, il mittente = la nazione con cui parla (cosi' l'equip viene
        sottratto a chi lo invia); (2) se il tipo manca o e' generico (infantry) ma il player ha
        chiesto un tipo preciso (es. 'support'), lo correggiamo dal testo."""
        try:
            import functions
        except Exception:
            return
        if not actions or not channel.startswith("leader:"):
            return
        partner = channel.split(":", 1)[1].strip().upper()
        my = (self.latest_state.get("tag", "") or "").upper()
        for a in actions:
            if not isinstance(a, dict) or (a.get("name") or "") != "send_equipment":
                continue
            args = a.setdefault("args", {})
            if not isinstance(args, dict):
                continue
            # (1) mittente di default = il partner, MA solo se il player e' il DESTINATARIO
            # (non quando e' il player a donare AL partner).
            has_sender = any(args.get(k) for k in ("sender", "from", "donor", "source"))
            rec = str(args.get("receiver") or "ROOT").strip().upper()
            player_is_receiver = rec in ("", "ROOT") or rec == my
            if not has_sender and player_is_receiver and partner and partner != my:
                args["sender"] = partner
            # (2) tipo dedotto dal testo se mancante o rimasto generico (infantry)
            given = args.get("equipment_type") or args.get("type")
            if not given or functions._resolve_equip(given) == "infantry_equipment":
                hinted = functions.infer_equipment_type((text or "") + " " + (msg or ""))
                if hinted:
                    args["equipment_type"] = hinted

    def _derive_plan_title(self, text):
        """Titolo breve per il piano sintetizzato, ricavato dal messaggio del player: prende
        cio' che viene DOPO 'piano/plan ... per/di/to/for', cosi' il titolo e' l'oggetto del
        piano (es. 'industrializzazione militare di...') e non il preambolo."""
        base = (text or "").strip()
        m = re.search(r"(?i)\b(piano|plan|programma|program|operazione|operation|progetto|"
                      r"project|campagna|campaign)\b[^,.]*?\b(per|di|della|del|dello|delle|dei|"
                      r"degli|sulla|sul|to|for|of|on)\b\s+(.+)", base)
        if m:
            base = m.group(3)
        base = base.strip(" .:-—") or "National Plan"
        return base[:48]

    def _extract_actions(self, channel, user_text, ai_message, system_prompt):
        """SECOND PASS: feed the model its OWN narration back and ask it to output the concrete
        in-game action(s) that make it real. Reuses the full channel ruleset (same system
        prompt), so start_plan / immediate-vs-over-time / mandatory costs all apply. Returns a
        list (possibly empty if the narration warranted no action)."""
        try:
            history = self.memory.get_history(channel, self.max_history)
            context = self._conversation_context(channel)
            wrapped = prompts.wrap_user(user_text, context, self.memory.facts_block())
            nudge = (
                "[SYSTEM: your previous reply had narration but emitted NO actions. If that "
                "narration implies ANY concrete in-game change — a reform, a cost "
                "(add_political_power negative / -stability), gained or seized factories or "
                "resources, a declared war, an embargo, a coup or leader change, troops raised, "
                "a launched long-term programme — you MUST now output the JSON action(s) that "
                "make it REAL, using the SAME schema (only the 'actions' array matters here; "
                "'message' may be empty). Anything that unfolds OVER TIME (industrialisation, "
                "rearmament, a clandestine operation, fortifying a frontier) MUST be a start_plan: "
                "its rewards mature at completion and ONLY the immediate cost applies now — do NOT "
                "grant the payoff up front. Always include the proportionate immediate COST. "
                "DIPLOMACY: a BILATERAL deal (non-aggression pact, alliance/faction, guarantee, "
                "military access, territorial cession, trade, lend-lease) becomes real ONLY once "
                "BOTH sides have AGREED and CONCLUDED it — if you are still proposing or bargaining, "
                "output no actions; a UNILATERAL act (declaring war, sending aid you chose to give) "
                "fires as soon as the decider commits. Use the 3-letter tags from the system prompt "
                "(the player is ROOT). If the narration was pure conversation/advice/refusal/ongoing "
                'negotiation with NO concluded change, reply with {"actions": []}.]')
            messages = ([{"role": "system", "content": system_prompt}] + history +
                        [{"role": "user", "content": wrapped},
                         {"role": "assistant", "content": ai_message},
                         {"role": "user", "content": nudge}])
            raw = self.llm.chat(messages)
            return LLMClient.parse_ai_json(raw).get("actions", []) or []
        except Exception as e:
            self._log(f"[extract] failed: {e}")
            return []

    def _schedule_dispatch(self, cmds, delay=2.5):
        """Spedisce 'cmds' come invio SEPARATO dopo 'delay' secondi. Serve per i comandi che
        DEVONO girare in un tick successivo: es. l'EVENTO di cambio leader, che deve partire DOPO
        che 'reload loc' (inviato prima) ha caricato la chiave del nome — altrimenti la chiave si
        risolve in stringa vuota e il capo esce senza nome."""
        cmds = [c for c in (cmds or []) if c and c.strip()]
        if not cmds:
            return
        def _go():
            if self.running:
                self._dispatch(cmds)
        try:
            t = threading.Timer(delay, _go)
            t.daemon = True
            t.start()
        except Exception as e:
            self._log(f"[sched] {e}")

    def _dispatch(self, cmds):
        """SELF-HEALING delivery to the game: writes the command file, runs it, and
        retries by itself until the game CONFIRMS execution (ACK in game.log) — so the
        player never has to type 'run' manually if the first injection mistimes. Adds an
        ACK line and substitutes ROOT with the player's tag. Non-blocking."""
        if getattr(self, "cfg", {}).get("unified"):
            self.q.put(("action", "A legacy action was blocked. Unified mode uses typed, journaled actions only."))
            return
        cmds = [c for c in (cmds or []) if c and c.strip()]
        if not cmds:
            return
        ptag = self.latest_state.get("tag", "")
        if re.match(r"^[A-Z]{3}$", ptag or ""):
            cmds = [re.sub(r"\bROOT\b", ptag, c) for c in cmds]
        # ANTI-DOPPIONI: lo STESSO identico evento/comando iniettato due volte a brevissima
        # distanza viene soppresso. IMPORTANTE: i comandi degli eventi sono sempre uguali
        # ("reload loc"+"event aidiplo.X") perche' il testo sta nella localizzazione; percio'
        # la firma include anche il CONTENUTO (injector.loc_sig), cosi' eventi DIVERSI con gli
        # stessi comandi NON vengono soppressi (es. un bivio o un completamento di piano).
        dkey = "\n".join(cmds) + "\x00" + self.injector.loc_sig()
        # Gli EVENTI popup non devono MAI uscire due volte: per loro la finestra anti-doppione e'
        # AMPIA (un timer raddoppiato o una riga di log riletta puo' far ripartire lo STESSO
        # identico evento anche qualche secondo dopo, oltre 1.5s). Eventi DIVERSI hanno testo
        # diverso -> firma (loc_sig) diversa -> NON vengono soppressi. Per i comandi/effetti
        # normali la finestra resta STRETTA (cosi' due effetti uguali voluti dal player passano).
        is_event = any("event aidiplo." in c for c in cmds)
        window = 15.0 if is_event else 1.5
        if self._recent_dup(self._dispatch_seen, dkey, window):
            self._log("[dispatch] duplicate event/command suppressed")
            return
        threading.Thread(target=self._dispatch_run, args=(cmds,), daemon=True).start()

    def _dispatch_run(self, cmds):
        # Scrive il file UNA volta e lo digita UNA SOLA volta (niente doppioni). Se HoI4 non
        # e' in primo piano perche' l'utente e' su un'ALTRA app (alt-tab), NON perdiamo il
        # comando e NON rubiamo il focus: aspettiamo, e digitiamo appena HoI4 (o il nostro
        # overlay) torna in primo piano. Cosi' il 'run' non va mai fatto a mano.
        with self._inject_lock:
            self.injector.write_run_file(cmds)
            if self.injector.dry_run:
                self.q.put(("action", "Dry run: commands were displayed without changing the game."))
                return
            deadline = time.time() + 600          # pazienza: fino a 10 minuti
            warned = False
            while time.time() < deadline and self.running:
                if self.injector.can_inject_now():
                    if self.injector.execute():     # porta HoI4 a fuoco e digita una volta
                        return
                    time.sleep(0.4)
                else:
                    # l'utente e' su una terza app: aspetta che torni su HoI4/overlay
                    if not warned:
                        self.q.put(("action", "Command queued — it will run as soon as HoI4 "
                                              "is in focus again."))
                        warned = True
                    time.sleep(0.7)
            self.q.put(("action", f"(if it doesn't run: open console -> {self.injector.manual_hint()})"))

    # Parole che indicano azioni che PRODUCONO potere politico (propaganda / consolidamento del
    # potere del capo/partito): non costano PP e non vanno mai rifiutate per PP.
    _PP_GAIN_WORDS = (
        "propagand", "discors", "speech", "comiz", "rally", "rallies", "address the nation",
        "address to the nation", "public address", "radio address", "broadcast", "morale",
        "cerimon", "ceremon", "commemora", "parat", "parade", "celebr", "manifestazion",
        "demonstration", "alla folla", "alla nazione", "to the crowd", "to the people",
        "lettere dei soldati", "soldiers' letters", "soldiers letters", "culto", "cult of",
        "personality cult", "immagine", "image of strength",
        # leggi/atti che ACCENTRANO il potere del capo o del partito
        "pieni poteri", "full powers", "enabling act", "emergency powers", "poteri d'emergenza",
        "poteri di emergenza", "partito unico", "one-party", "one party", "single-party",
        "single party", "accentr", "consolidat", "consolida il potere", "rafforza il potere",
        "rafforzare il potere", "tighten his grip", "tighten her grip", "grip on power",
        "seize power", "increase his power", "increase her power", "censur", "censor",
        "soppress", "suppress the opposition", "purg", "purge", "epura", "repress", "repressione",
    )
    # Parole che indicano azioni che SPENDONO potere politico (riforme contro resistenze, impegno
    # di risorse/stato/militare, approvazioni): costano PP e si possono rifiutare se insufficiente.
    _PP_COST_WORDS = (
        "riforma", "riforme", "reform", "nazionalizz", "nationaliz", "mobilit", "mobiliz",
        "mobilitazione", "approvazione", "approval", "parlament", "parliament", "consiglio",
        "council", "gabinetto", "cabinet", "impegno militare", "military commitment",
        "schieram", "deploy", "war effort", "sforzo bellico", "costruzione", "build program",
        "industri", "risors", "resource", "impopolar", "unpopular", "tass", "tax", "leva",
        "conscription", "coscriz", "requisiz", "requisition", "razionament", "rationing",
        "nationalise", "nationalize", "espropri", "expropriat",
    )

    def _pp_intent(self, text):
        """Classifica l'intento rispetto al POTERE POLITICO:
        - 'gain' : lo PRODUCE (propaganda, discorsi, morale, leggi che accentrano il potere del
                   capo/partito) -> non costa PP, non si rifiuta mai per PP.
        - 'cost' : lo SPENDE (riforme contro resistenze, approvazioni, risorse/industria/militare,
                   misure impopolari) -> costa PP e si puo' rifiutare.
        - None   : indeterminato (si applica il default per canale)."""
        blob = (text or "").lower()
        gain = any(w in blob for w in self._PP_GAIN_WORDS)
        cost = any(w in blob for w in self._PP_COST_WORDS)
        if gain and not cost:
            return "gain"
        if cost and not gain:
            return "cost"
        return None

    def _reject_for_pp(self, have, need, attempted=""):
        """Nega una riforma/azione perche' il player NON ha abbastanza potere politico. L'evento
        in gioco mostra una NARRAZIONE in-character del rifiuto (scritta dall'IA, coerente con la
        misura tentata e con l'ideologia) PIU' il motivo tecnico fra parentesi. NON applica nessuna
        conseguenza (niente PP in negativo)."""
        ideo = str(self.latest_state.get("ideo", "4"))
        fallback = {
            "1": "The Grand Council and the party hierarchy refuse to back the measure now: the "
                 "leadership lacks the political capital to impose it over their objections.",
            "2": "The Party Politburo blocks the measure: the political capital to carry the "
                 "cadres and the apparatus with it simply is not there yet.",
            "3": "Parliament and the opposition vote the measure down: there is not enough "
                 "political capital to push it through against their resistance.",
            "4": "The ruling council and the ministries refuse to enact the measure: there is "
                 "not enough political capital to force it through right now.",
        }.get(ideo, "The government cannot enact the measure right now.")
        body = fallback
        # NARRAZIONE su misura: l'IA descrive in-character il BLOCCO della misura tentata.
        try:
            sysp = (prompts.GROUNDING_RULE + "\n"
                    "The player's government TRIED to enact a reform/decree/move but DOES NOT have "
                    "enough political power to carry it through. Write a SHORT (2-3 sentences) "
                    "in-character account of the measure being BLOCKED/REJECTED, fitting the regime: "
                    "a DEMOCRACY -> parliament or the opposition vote it down, or public protests "
                    "force a retreat; a DICTATORSHIP -> the ruling council/party hierarchy/ministries "
                    "refuse, or it would spark unrest the regime cannot risk. Reference the SPECIFIC "
                    "measure they tried. Make clear it did NOT pass. Do NOT state numbers (the "
                    "technical reason is appended separately).\n" + self._lang_directive() +
                    '\nReply ONLY in JSON {"message":"<2-3 sentences>"}.')
            usr = (f"Regime ideology code: {ideo} (1 fascist, 2 communist, 3 democratic, 4 "
                   f"non-aligned). The measure the player tried to enact:\n{attempted or '(a reform)'}")
            obj = LLMClient.parse_obj(self.llm.chat(
                [{"role": "system", "content": sysp}, {"role": "user", "content": usr}]))
            body = (obj.get("message") or "").strip() or fallback
        except Exception as e:
            self._log(f"[reject] {e}")
        msg = (body + f" (Rejected — insufficient political power: {have} available, "
                      f"{need} required.)")
        self.q.put(("action", f"REJECTED: insufficient political power ({have}/{need})"))
        self._dispatch(self.injector.prepare_reject(msg))
        self.q.put(("assistant", {"name": "Supreme Command", "message": msg}))

    def _execute_actions(self, actions, narration=""):
        import functions
        # ANTI-DOPPIONI: se l'IA elenca la STESSA azione (stesso nome+argomenti) piu' volte,
        # la eseguiamo una sola volta (evita conseguenze sommate e popup ripetuti).
        seen_acts = set()
        deduped = []
        for a in (actions or []):
            if not isinstance(a, dict):
                continue
            try:
                sig = json.dumps([a.get("name"), a.get("args")], sort_keys=True, default=str)
            except Exception:
                sig = str((a.get("name"), a.get("args")))
            if sig in seen_acts:
                continue
            seen_acts.add(sig)
            deduped.append(a)
        actions = deduped
        # CAP RESEARCH SLOT: la mod non puo' concedere piu' di 4 slot di ricerca a una stessa
        # nazione in una partita (persistente). Limitiamo/azzeriamo l'azione di conseguenza.
        ptag = self.latest_state.get("tag", "") or "ROOT"
        capped = []
        for a in actions:
            if a.get("name") in ("add_research_slot", "research_slot"):
                args = dict(a.get("args") or {})
                tgt = (str(args.get("target") or "ROOT").upper())
                key = "rslots:" + (ptag if tgt in ("ROOT", "") else tgt)
                granted = self.memory.counter_get(key)
                want = max(1, int(functions._num(args.get("amount"), 1)))
                allowed = max(0, 4 - granted)
                give = min(want, allowed)
                if give <= 0:
                    self.q.put(("action", "Research slots: cap of 4 per nation reached — none added"))
                    continue
                args["amount"] = give
                a = {"name": a.get("name"), "args": args}
                self.memory.counter_add(key, give)
            capped.append(a)
        actions = capped
        # ECONOMIA DEL POTERE POLITICO: alcune azioni lo PRODUCONO (propaganda, discorsi, morale,
        # leggi che accentrano il potere del capo/partito) -> non costano PP e NON si rifiutano mai
        # per PP; altre lo SPENDONO (riforme contro resistenze, approvazioni di consiglio/parlamento,
        # impegno di risorse/industria/militare, misure impopolari) -> costano PP e si possono
        # rifiutare. Classifichiamo dall'intento (testo del player + narrazione) e dal canale.
        intent = self._pp_intent((self._last_player_text or "") + " " + (narration or ""))
        if intent is None and self.channel == "life":
            intent = "gain"     # la VITA del capo (discorsi, gesti pubblici, propaganda) crea consenso
        if intent == "gain":
            # qui un add_political_power NEGATIVO e' un errore del modello: il gesto PRODUCE PP,
            # quindi lo rendiamo POSITIVO; e NON applichiamo nessun controllo di affordability.
            for a in actions:
                if a.get("name") in ("add_political_power", "political_power"):
                    args = a.setdefault("args", {})
                    if isinstance(args, dict):
                        amt = functions._num(args.get("amount"), 0)
                        if amt < 0:
                            args["amount"] = abs(amt)
        else:
            # AFFORDABILITY: se il COSTO in PP manderebbe il player SOTTO ZERO, NON applichiamo
            # l'azione: la NEGHIAMO con un evento RP coerente con l'ideologia (consiglio/parlamento/
            # proteste) e indichiamo il motivo concreto fra parentesi.
            try:
                cur_pp = int(float(self.latest_state.get("pp", 0)))
            except Exception:
                cur_pp = 0
            my = (self.latest_state.get("tag", "") or "").upper()
            pp_delta = 0
            for a in actions:
                if a.get("name") in ("add_political_power", "political_power"):
                    args = a.get("args") or {}
                    tgt = str(args.get("target") or "ROOT").upper()
                    if tgt in ("ROOT", "") or tgt == my:
                        pp_delta += int(functions._num(args.get("amount"), 0))
            if pp_delta < 0 and cur_pp + pp_delta < 0:
                self._reject_for_pp(cur_pp, -pp_delta, narration)
                return
        console_cmds = []
        had_direct = False
        for act in actions:
            res = functions.build_action(act.get("name"), act.get("args"))
            if not res:
                self.q.put(("action", f"(action ignored: {act.get('name')})"))
                continue

            special = res.get("special")
            if special:
                if special["type"] == "reject":
                    self.q.put(("action", f"REJECTED: {special.get('reason','')}"))
                    console_cmds += self.injector.prepare_reject(special.get("reason", ""))
                elif special["type"] == "event":
                    self.q.put(("action", f"EVENT: {special.get('title','')}"))
                    console_cmds += self.injector.prepare_dynamic_event(
                        special.get("title", ""), special.get("desc", ""),
                        special.get("option_a", ""), special.get("option_b", ""))
                elif special["type"] == "act_event":
                    self.q.put(("action", f"EVENT: {special.get('title','')}"))
                    console_cmds += self.injector.prepare_act_event(
                        special["act"], special["actor"], special["target"],
                        special.get("title", ""), narration)
                    if res.get("memory"):
                        self.memory.remember_fact("agreement", res["memory"])
                elif special["type"] == "rename":
                    self.q.put(("action", f"Nation renamed: '{special.get('name','')}'"))
                    console_cmds += self.injector.prepare_rename(
                        special.get("name", ""), special.get("target", "ROOT"))
                    if res.get("memory"):
                        self.memory.remember_fact("event", res["memory"])
                elif special["type"] == "setleader":
                    self.q.put(("action", f"New head of state: '{special.get('name','')}'"))
                    # CAMBIO RADICALE = l'ideologia cambia DAVVERO (non solo la persona): in tal
                    # caso aidiplo.60 resettera' i focus al ramo generico (lo Stato precedente non
                    # ha piu' senso). Confrontiamo la nuova ideologia con quella attuale del player.
                    try:
                        cur_ideo = int(float(self.latest_state.get("ideo", 0)))
                    except Exception:
                        cur_ideo = 0
                    new_ideo = int(special.get("ideo_num", 4) or 4)
                    radical = (special.get("target", "ROOT") in ("ROOT", "")
                               and cur_ideo in (1, 2, 3, 4) and new_ideo != cur_ideo)
                    leader_cmds = self.injector.prepare_set_leader(
                        special.get("name", ""), special.get("ideo_num", 4),
                        special.get("target", "ROOT"), radical=radical)
                    # UN SOLO invio ORDINATO (come i titoli degli eventi dinamici, che funzionano:
                    # reload loc + event nello STESSO run): reload loc -> set_variable -> event
                    # aidiplo.60, che fa set_politics + recruit_character + promote_character del
                    # nostro capo (priorita' assoluta) col nome appena ricaricato. NIENTE due fasi
                    # separate: separare reload loc dall'evento rompeva la risoluzione del nome.
                    console_cmds += leader_cmds + (special.get("effects", []) or [])
                    if res.get("memory"):
                        self.memory.remember_fact("event", res["memory"])
                elif special["type"] == "interactive":
                    self.q.put(("action", f"Interactive event started: '{special.get('title','')}'"))
                    role = "actor" if self.channel == "actor" else "leader"
                    self._start_event(special.get("title", ""), special.get("situation", ""),
                                      special.get("kind", ""), role)
                    if res.get("memory"):
                        self.memory.remember_fact("event", res["memory"])
                elif special["type"] == "civilwar":
                    self.pending_civilwar = {"ideology": special["ideology"],
                                             "leader": special.get("leader", ""),
                                             "target": special.get("target", "ROOT")}
                    self.awaiting_choice = "civilwar"
                    self.q.put(("action", f"Civil war ({special['ideology']}): choose your side"))
                    console_cmds += self.injector.prepare_decision_event(
                        "Civil war",
                        narration or "The nation is on the brink of civil war. Which side will you take?",
                        "Lead the revolution", "Defend the legitimate government")
                    if res.get("memory"):
                        self.memory.remember_fact("event", res["memory"])
                elif special["type"] == "plan":
                    ai_days = max(30, min(int(special.get("days", 150)), 365))   # max 1 anno
                    stages = max(0, min(int(special.get("stages", 0)), 8))
                    # ruolo: se il piano nasce dal canale "act as another figure", i bivi e
                    # gli eventi sono riferiti a QUELLA figura, non al capo di Stato.
                    role = "actor" if self.channel == "actor" else "leader"
                    if role == "actor":
                        stages = max(5, stages)   # un complotto/golpe richiede almeno 5 bivi
                    # TIMING DETERMINISTICO E SEMPRE BEN DISTANZIATO: il piano e' diviso in
                    # (stages+1) "battute" di uguale durata 'segment'. I bivi cadono alle battute
                    # 1..stages; il COMPLETAMENTO alla battuta (stages+1). Cosi' il completamento
                    # e' SEMPRE una battuta intera DOPO l'ultimo bivio: non possono MAI coincidere.
                    n = stages + 1
                    if stages == 0:
                        segment = ai_days                       # unica battuta: dura quanto richiesto
                    else:
                        segment = max(21, min(ai_days // n, 90)) # ogni battuta ~3 settimane..3 mesi
                        if segment * n > 365:                    # totale al massimo ~1 anno
                            segment = max(7, 365 // n)
                    eta = self.day + segment * n                 # giorno del COMPLETAMENTO
                    days = segment * n                           # durata effettiva (per il log)
                    slot = next((i for i, s in enumerate(self.plan_slots) if s is None), None)
                    # ANTI-SPAM: l'annuncio "Begins: ..." di QUESTO piano (stessa partita,
                    # stesso titolo, stesso giorno) si mostra una sola volta.
                    start_key = ("planstart", self.gid, special.get("title", ""), self.day)
                    if start_key not in self._fired_plan_starts:
                        self._fired_plan_starts.add(start_key)
                        console_cmds += self.injector.prepare_dynamic_event(
                            f"Begins: {special['title']}", narration, "To work!", "")
                    if slot is None:
                        self.q.put(("action", f"PLAN '{special['title']}' (slots full: applying now)"))
                        console_cmds += special.get("payload", [])
                    else:
                        # La successione e' scandita dai TIMER NATIVI di HoI4: ogni piano/bivio
                        # schedula in gioco 'country_event = { id = aidiplo.3<slot> days = segment }'
                        # e alla scadenza HoI4 logga AIDIPLO_PLAN_DONE -> avanziamo il passo. HoI4
                        # conta il tempo (rispetta pausa/velocita'/salvataggi): i bivi escono
                        # distanziati di 'segment' giorni e il completamento solo dopo l'ultimo.
                        meta = {"stages_total": stages, "stage_idx": 0, "segment": segment,
                                "role": role, "final_actions": special.get("final_actions", []),
                                "gid": self.gid, "tag": self._ptag}
                        plan = {"slot": slot, "title": special["title"],
                                "payload": special.get("payload", []),
                                "facts": special.get("facts", ""),
                                "created_day": self.day, "eta_day": eta,
                                "gid": self.gid, "tag": self._ptag}
                        plan.update(meta)
                        pid = self.memory.add_plan(slot, special["title"], special.get("payload", []),
                                                   self.day, eta, special.get("facts", ""), meta)
                        plan["id"] = pid          # chiave stabile per salvare/segnare-finito
                        self.plan_slots[slot] = plan
                        # primo timer nativo: il primo bivio/passo scatta fra 'segment' giorni
                        console_cmds.append(f"eval_effect ROOT = {{ country_event = {{ "
                                            f"id = aidiplo.3{slot} days = {segment} }} }}")
                        self.q.put(("action", f"PLAN '{special['title']}' started: {days} days"
                                              + (f", {stages} crossroads" if stages else "")))
                    if res.get("memory"):
                        self.memory.remember_fact("plan", res["memory"])
                continue

            if res.get("desc"):
                self.q.put(("action", self._clean_effect_desc(res["desc"])))
            cmds = res.get("commands", [])
            if cmds:
                had_direct = True
            console_cmds += cmds
            if res.get("memory"):
                self.memory.remember_fact("agreement", res["memory"])

        # INTERNAL actions (Supreme Command / Leader's Life): if direct commands
        # were executed, ALSO show an AI-narrated event.
        if had_direct and narration and self.channel in ("decree", "life"):
            console_cmds += self.injector.prepare_dynamic_event(
                "Act of government", narration, "Carry on.", "")

        # CAMBIO RADICALE DI REGIME -> offri al player il RESET dei focus (evento aidiplo.62).
        # Lo lanciamo NOI dal middleware (non da aidiplo.60) cosi' copriamo OGNI percorso:
        # set_leader E regime_change. "Radicale" = l'ideologia di governo cambia DAVVERO, OPPURE
        # l'IA marca il cambio come una rifondazione (args 'refound'=true: es. stessa ideologia ma
        # nuovo ordinamento, una repubblica che diventa monarchia). Solo per il player.
        try:
            cur_ideo = int(float(self.latest_state.get("ideo", 0)))
        except Exception:
            cur_ideo = 0
        IDC = {"fascism": 1, "communism": 2, "democratic": 3, "neutrality": 4}
        radical = False
        for a in actions:
            if a.get("name") not in ("set_leader", "regime_change"):
                continue
            args = a.get("args") or {}
            tgt = str(args.get("target") or "ROOT").upper()
            if tgt not in ("ROOT", "", my):
                continue
            new_code = IDC.get(str(args.get("ideology", "")).lower(), 0)
            if (new_code and cur_ideo in (1, 2, 3, 4) and new_code != cur_ideo) \
                    or functions._truthy(args.get("refound")):
                radical = True
        # COLPO DI STATO: anche SENZA cambio d'ideologia, un GOLPE rifonda di fatto lo Stato (il
        # vecchio albero dei focus non ha piu' senso) -> offriamo comunque il reset dei focus. Lo
        # riconosciamo da un cambio di capo/governo (set_leader/regime_change su ROOT) insieme a un
        # contesto di golpe: parole-chiave nel testo del player/narrazione, o il canale "act as
        # another figure" (che trama CONTRO il governo in carica).
        coup_blob = ((self._last_player_text or "") + " " + (narration or "")).lower()
        is_coup = (self.channel == "actor") or any(w in coup_blob for w in self._COUP_WORDS)
        leader_change = any(
            (a.get("name") in ("set_leader", "regime_change")
             and str((a.get("args") or {}).get("target") or "ROOT").upper() in ("ROOT", "", my))
            for a in actions if isinstance(a, dict))
        if leader_change and is_coup:
            radical = True
        if radical:
            console_cmds.append("event aidiplo.62 ROOT")   # 'event' non richiede -debug

        if console_cmds:
            self._dispatch(console_cmds)
            # appena parte l'evento/azione in gioco, riduci da solo il pannello e riporta HoI4 a
            # fuoco (l'iniezione avviene solo con HoI4 attivo). No-op se il pannello e' gia' chiuso.
            self.q.put(("autohide_panel", None))

    # ---------------- chiusura ----------------
    def shutdown(self):
        self.running = False
        try:
            self.ui.root.destroy()
        except Exception:
            pass

    def run(self):
        self.ui.run()


def main():
    parser = argparse.ArgumentParser(description="Voices of History source middleware")
    parser.add_argument("--config", help="Path to a separate game profile configuration")
    parser.add_argument("--userdir", help="Pin log and command paths to an explicit HOI4 user directory")
    parser.add_argument("--characterization-source", help="Selected mod source resolved by the Paradox Voices launcher")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.userdir:
        userdir = os.path.abspath(args.userdir)
        cfg["paths"]["game_log"] = os.path.join(userdir, "logs", "game.log")
        cfg["paths"]["_hoi4_userdirs"] = [userdir]
        loc = cfg["paths"]["mod_localisation"].replace("\\", "/")
        if "/mod/" not in loc:
            raise ValueError("The localization path needs a mod-relative suffix for --userdir.")
        cfg["paths"]["mod_localisation"] = os.path.join(userdir, "mod", loc.split("/mod/", 1)[1])
    if args.characterization_source:
        cfg["characterization"] = {"source": args.characterization_source}
    glog = cfg["paths"]["game_log"]
    print("=" * 60)
    print(" HOI4 AI MOD VOICES OF HISTORY - middleware started")
    print(" game.log :", glog)
    print("           (found)" if os.path.exists(glog) else
          "           (not present yet: launch HoI4 and load a game)")
    print(" mod loc  :", cfg["paths"]["mod_localisation"])
    print(" provider :", cfg.get("provider", "local"))
    print(" profile  :", cfg.get("game_profile", "vanilla"))
    if args.characterization_source:
        print(" local character source :", args.characterization_source)
    if cfg.get("provider") != "chatgpt_plan":
        print(" endpoint :", cfg["player2"]["base_url"])
    print(" dry_run  :", cfg["injection"]["dry_run"])
    print("=" * 60)
    print(" Reminder: launch HoI4 in -debug mode so the commands work.")
    print("=" * 60)
    try:
        App(cfg).run()
    except ImportError as e:
        print("\n[ERROR] A module is missing:", e)
        print("If the error mentions 'tkinter', reinstall Python from python.org")
        print("(the python.org build includes tkinter; the Microsoft Store one")
        print(" sometimes does not). Then relaunch START_MIDDLEWARE.bat.")
        input("\nPress Enter to close...")


if __name__ == "__main__":
    main()

# v1.1: bivi per-slot (aidiplo.40-45) — le scelte dei piani concorrenti non si mescolano piu'.
