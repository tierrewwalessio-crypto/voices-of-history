"""Compact unified dialogue, guarded actions and opt-in strategic reviews."""
from pathlib import Path
import copy
import json
import re
import threading
import time

import game_state as gs
import prompts
from llm_client import LLMClient
from safe_actions import ActionJournal, CATALOG, command, text_only, validate, equipment_set, describe
from strategist import Strategist
from usage_budget import BudgetClient, BudgetExceeded
from profiles import request_instructions
from leader_voice import leader_voice_rules
import telemetry
from proposals import Proposals, aircraft_order


def compact_context(state, world, actor=None):
    selected = []
    if actor in world:
        selected.append(world[actor])
    others = sorted((n for tag, n in world.items() if tag not in (actor, state.get("tag"))),
                    key=lambda n: (-int(n.get("waryou", "0")), -int(n.get("maj", "0")), n.get("tag", "")))
    selected.extend(others[:7])
    # The full state dictionary remains local; no state-ID catalogue on ordinary turns.
    return gs.context_for_llm(state) + "\nSELECTED COUNTRY SNAPSHOTS (others omitted, not assumed):\n" + "\n".join(
        gs._nation_line(n) + " representative=" + n.get("leader", "unknown") + " snapshot=" + n.get("date", "unknown")
        for n in selected)


class UnifiedRuntime:
    def _reset_log_session(self):
        """A new game log invalidates cached campaign data; persistent history stays saved."""
        self.latest_state, self.world, self.states = {}, {}, {}
        self._telemetry = None
        self.diplomatic_contacts.clear()
        self.gid, self.day, self._last_day = 0, 0, 0
        self._last_gid, self._last_tag, self._ptag = None, None, ""
        self.channel, self.channel_name = None, ""
        self.event, self.pending_events = None, []
        self.plan_slots = [None] * 6
        self._pending_purge = None
        self._pending_plan_slots = set()
        self.memory.set_context(0, 0)
        if self.strategist:
            self.strategist.previous = None
            self.strategist.crises.clear()
        self.ui.clear_history()
        self.ui.set_status("Waiting for a fresh campaign export.")
        self.ui.set_header("Voices of History — load a campaign and open the Office")
        self._log("[sync] Game log restarted; cached campaign data invalidated.")

    def _setup_unified(self):
        self._strategy_busy = False
        self.journal = None
        self.strategist = None
        if self.cfg.get("unified"):
            self.journal = ActionJournal(self.cfg["actions"]["journal"])
            self.proposals = Proposals(self.journal) if self.cfg.get('proposals', {}).get('enabled') else None
            with self.journal.connect() as db:
                db.execute("UPDATE actions SET state='uncertain' WHERE state='pending'")
            cfg = self.cfg["strategist"]
            self.strategist = Strategist(self.cfg["game_profile"], cfg["settings_file"], cfg["ledger"])

    def _on_telemetry(self, packet):
        if not self.cfg.get("telemetry", {}).get("enabled") or not telemetry.matches(packet, self.latest_state):
            return
        old = getattr(self, "_telemetry", None)
        if old and telemetry.matches(old, self.latest_state) and telemetry.number(old['day'], True) > telemetry.number(packet['day'], True):
            return
        self._telemetry = packet
        self.latest_state["telemetry"] = packet

    def _unified_system(self, channel, state, world, sample=None):
        actor = channel.split(":", 1)[1] if channel.startswith(("leader:", "strategy:")) else state.get("tag")
        representative = world.get(actor, {}).get("leader", "Unknown representative") if actor != state.get("tag") else state.get("leader", "Unknown representative")
        role = (f"You represent {world.get(actor, {}).get('name', actor)}, led by {representative}. "
                f"The human represents {state.get('name')} ({state.get('tag')}). "
                "Judge requests using your own country's interests, ideology, resources and the current setting. "
                "You can bargain or refuse; friendly rhetoric is not a treaty. "
                if actor != state.get("tag") else
                "You are the player's Head of State Office. Report the live campaign and evaluate explicit orders. ")
        style = "Reply briefly and directly; give more detail only when useful or requested. "
        if channel.startswith("leader:"):
            role = (f"You are {representative}, the current leader/representative of "
                    f"{world.get(actor, {}).get('name', actor)} ({actor}), speaking directly "
                    f"with the human leader of {state.get('name')} ({state.get('tag')}). "
                    f"The active campaign date is {state.get('date', 'unknown')}. "
                    "Judge requests using your own personality, country's interests and current situation. "
                    "You can bargain or refuse; friendly rhetoric is not a treaty. ")
            if self.cfg.get("behavior", {}).get("enhanced_personalities", False):
                style = leader_voice_rules(self.cfg["game_profile"])
        if channel.startswith("press:"):
            voice = channel.split(":", 1)[1]
            role = f"Write a brief {voice} newspaper report based on the exported campaign and recorded facts. Missing events stay unknown. No game actions. "
        elif channel in ("life", "actor"):
            role = "Narrate the requested domestic roleplay scene, keeping campaign facts and hypothetical events distinct. No game actions. "
        catalog = CATALOG
        if self.cfg.get('proposals', {}).get('enabled'):
            role += ("Equipment transfers use persistent proposals: emit propose_equipment with sender, receiver, "
                     "equipment_type and quantity (null when unknown), decision=negotiate. Do not invent quantities. "
                     "A quantity-only reply fills the pending proposal; a separate exact-id confirmation authorizes it. "
                     "Aircraft production/procurement is unsupported: no verified variant queue/delivery receipts. "
                     "Never claim contact with manufacturers, queued production, applied changes or delivered equipment "
                     "without the corresponding recorded engine evidence. ")
            catalog = catalog.replace('Refusal/negotiation/information has\nactions=[].',
                                      'Refusal/information has actions=[]. Negotiation may contain only propose_equipment drafts.')
            catalog = catalog.replace('Actions need decision=accept', 'Executing actions need decision=accept')
            catalog += ('\n- propose_equipment {sender,receiver,equipment_type,quantity}: a nonexecuting transfer draft; '
                        'quantity is null when the player has not supplied it. Use decision=negotiate. '
                        'The human supplies quantity and confirms the exact proposal id in later turns.\n')
        return (role + "Use exported facts before opening lore. Do not predetermine future branches. "
                "Missing stockpiles, treaties, fronts and custom mechanics are unknown. " +
                style + self._lang_directive(sample) + "\n" +
                prompts.difficulty_block() + "\n" + catalog + "\nVerified equipment: " +
                ", ".join(sorted(equipment_set(self.cfg["game_profile"]))))

    def _unified_context(self, channel, state, world):
        actor = channel.split(":", 1)[1] if channel.startswith(("leader:", "strategy:")) else None
        context = compact_context(state, world, actor)
        if self.cfg.get("telemetry", {}).get("enabled"):
            context += "\n" + telemetry.context(state)
        if channel.startswith("leader:"):
            context += "\n" + gs.diplomatic_context(state, world, {"target": actor, "player": state.get("tag")})
        if self.lore:
            lore_channel = "leader:" + actor if actor and actor != state.get("tag") else "decree"
            context += "\n" + self.lore.briefing(state, world, lore_channel)
        context += "\nExecution: " + ("DRY RUN: previews only." if self.injector.dry_run else "LIVE: require engine confirmation.")
        return context

    def _unified_turn(self, text, channel, channel_name):
        state, world = copy.deepcopy(self.latest_state), copy.deepcopy(self.world)
        if (not state.get("live") or not int(state.get("gid", 0))
                or state.get("profile", "vanilla") != self.cfg["game_profile"]):
            raise ValueError("Synchronize the selected unified profile before sending a request.")
        proposals = getattr(self, 'proposals', None)
        if proposals:
            if aircraft_order(text) and (channel == 'decree' or channel.startswith('leader:')):
                local = (proposals.unsupported(text, state, world, channel, self.cfg['game_profile']), None, None)
            else:
                local = proposals.reply(text, state, world, channel, self.cfg['game_profile'])
            if local:
                message, normalized, proposal_id = local
                if normalized:
                    if not self.auto_exec:
                        message += ' Automatic action execution is disabled; no effect submitted.'
                    else:
                        tx = self._submit_unified(normalized, state, world)
                        proposals.bind(proposal_id, tx)
                        message += ' See the transaction status below; confirmation is not execution.'
                self.memory.add_turn(channel, text, message, int(state['gid']), int(state['day']))
                self.q.put(('assistant', {'name': channel_name or 'Office', 'message': message}))
                return
        if channel.startswith("leader:"):
            actor = channel.split(":", 1)[1]
            contact = gs.diplomatic_contact({"target": actor, "player": state["tag"]}, state, world)
            channel_name = contact["tleader"]
            self.q.put(("diplomatic_identity", {"channel": channel, "tag": actor,
                        "name": channel_name, "country": contact["tname"]}))
        history = self.memory.get_history(channel, 12, gameid=state["gid"], day=state["day"])
        # Large old turns are bounded individually; the unabridged records stay in SQLite.
        history = [{"role": m["role"], "content": m["content"][:1600]} for m in history]
        context = self._unified_context(channel, state, world)
        context += "\nRECORDED FACTS:\n" + self.memory.facts_block(limit=10, gameid=state["gid"], day=state["day"])[:2400]
        context += ("\nOlder narrative records can describe proposals or previews. They are not proof of a completed action. "
                    "Only matching engine results establish execution. Recent unified action states:\n" +
                    json.dumps(self.journal.recent(self.cfg["game_profile"], int(state["gid"]), limit=5, day=int(state["day"]))))
        if proposals:
            proposals.pending(state, world, channel, self.cfg['game_profile'])
            context += '\nPERSISTENT PROPOSALS:\n' + json.dumps(proposals.recent(self.cfg['game_profile'], state, channel))
        system = {"role": "system", "content": self._unified_system(channel, state, world, text)}
        user = {"role": "user", "content": context + "\nCURRENT PLAYER MESSAGE:\n" + text}
        max_chars = self.llm.budget.settings["max_input_chars"] - len(request_instructions(self.cfg))
        schema = getattr(self.llm, "response_schema", None)
        if isinstance(schema, dict):
            max_chars -= len(json.dumps(schema, separators=(",", ":")))
        while history and sum(len(m["content"]) for m in (system, *history, user)) > max_chars:
            history = history[2:]  # Drop an older exchange, preserving the current message.
        messages = [system, *history, user]
        raw = self.llm.interactive_chat(messages)
        parsed = self._parse_unified_reply(raw, channel)
        message = parsed.get("message") if isinstance(parsed.get("message"), str) else LLMClient.loose_text(raw)
        if re.search(r'\b(queued|delivered|applied|executed|ordered|contacted|placed (?:the|an?|your) order)\b', message, re.I):
            # Execution claims come from the journal/engine receipt path, never prose.
            message = 'Execution is not established by this reply. Check the recorded transaction status; unsupported requests have no game effect.'
        # Persist the actual exchange before action processing; never make an extraction request.
        if (str(self.latest_state.get("gid")) != str(state.get("gid"))
                or self.latest_state.get("tag") != state.get("tag")
                or int(self.latest_state.get("day", 0)) < int(state["day"])
                or (channel.startswith("leader:") and self.world.get(actor, {}).get("leader") != world.get(actor, {}).get("leader"))):
            self.q.put(("sysnote", "A previous-campaign reply was discarded after the campaign changed."))
            return
        self.memory.add_turn(channel, text, message, int(state["gid"]), int(state["day"]))
        if self.latest_state.get("date") != state.get("date"):
            self.q.put(("sysnote", "This reply uses the campaign snapshot from " + state.get("date", "unknown")))
        self.q.put(("assistant", {"name": channel_name or "Office", "message": message}))
        self._unified_actions(parsed, text, channel, state, world)

    def _parse_unified_reply(self, raw, channel):
        parsed = LLMClient.parse_obj(raw)
        valid = (isinstance(parsed.get("message"), str)
                 and parsed.get("decision") in ("inform", "negotiate", "refuse", "accept")
                 and isinstance(parsed.get("actions"), list))
        if not valid:
            self._log(f"[unified-response] channel={channel} structured=False actions=0")
            self.q.put(("sysnote", "This reply has no valid structured action record. No game effect was queued; it will not be retried automatically."))
            return {"message": LLMClient.loose_text(raw), "decision": "inform", "actions": []}
        self._log(f"[unified-response] channel={channel} structured=True decision={parsed['decision']} actions={len(parsed['actions'])}")
        if parsed["decision"] == "accept" and not parsed["actions"]:
            self.q.put(("sysnote", "The representative accepted in text but proposed no supported game action. No effect was queued."))
        return parsed

    def _unified_actions(self, parsed, text, channel, state, world, automatic=False):
        actions = parsed.get("actions", [])
        if not isinstance(actions, list) or text_only(text):
            return
        if not self.auto_exec:
            return
        seen = set()
        for action in actions[:1 if automatic else 2]:
            if not isinstance(action, dict):
                continue
            try:
                proposals = getattr(self, 'proposals', None)
                if proposals and action.get('name') in ('send_equipment', 'propose_equipment') and not automatic:
                    if parsed.get('decision') not in ('accept', 'negotiate'):
                        raise ValueError('This reply does not propose an equipment transfer.')
                    proposal_id, candidate = proposals.create(action, text, state, world, channel, self.cfg['game_profile'])
                    self.q.put(('action', proposals.describe(proposal_id, candidate)))
                    continue
                normalized = validate(action, state, world, channel, parsed.get("decision"), text,
                                      self.cfg["game_profile"], automatic)
                signature = json.dumps(normalized, sort_keys=True)
                if signature in seen:
                    continue
                seen.add(signature)
                self._submit_unified(normalized, state, world)
            except ValueError as error:
                self.q.put(("action", "Action not applied: " + str(error)))

    def _submit_unified(self, normalized, state, world):
        preview = self.injector.dry_run or not self.cfg.get('actions', {}).get('live_approved', False)
        tx = self.journal.create(normalized, state, self.cfg['game_profile'], preview)
        compiled = command(normalized, tx, state['gid'])
        self._log(f"[unified-action] {tx} {'preview' if preview else 'pending'} {json.dumps(normalized, sort_keys=True)}")
        self.q.put(('action', ('Preview only: ' if preview else 'Awaiting game confirmation: ') + describe(normalized)))
        if not preview:
            self._deliver_unified(tx, compiled, state, world, normalized['actor'])
        return tx

    def _deliver_unified(self, tx, compiled, state, world, actor):
        """Single delivery attempt. Unknown delivery is never retried automatically."""
        with self._inject_lock:
            current = self.latest_state
            fresh = (str(current.get("gid")) == str(state.get("gid")) and current.get("tag") == state.get("tag")
                     and abs(int(current.get("day", 0)) - int(state.get("day", 0))) <= 2)
            if actor != state.get("tag"):
                fresh = fresh and self.world.get(actor, {}).get("leader") == world.get(actor, {}).get("leader")
            if not fresh or self.injector.dry_run or not self.cfg["actions"].get("live_approved"):
                self.journal.set_status(tx, "cancelled")
                self.q.put(("action", "A stale or unapproved action was cancelled."))
                return
            # The user can return to the game without another model request. Never steal focus.
            deadline = time.monotonic() + 30
            if not self.injector.can_inject_now():
                self.q.put(("action", "Awaiting delivery: hide the Voices panel or return to HOI4 within 30 seconds. No engine receipt yet."))
            while not self.injector.can_inject_now() and self.running and time.monotonic() < deadline:
                time.sleep(0.25)
            current = self.latest_state
            if (not self.running or not self.injector.can_inject_now()
                    or current.get("gid") != state.get("gid") or current.get("tag") != state.get("tag")
                    or abs(int(current.get("day", 0)) - int(state.get("day", 0))) > 2
                    or (actor != state.get("tag") and self.world.get(actor, {}).get("leader") != world.get(actor, {}).get("leader"))):
                self.journal.set_status(tx, "not_delivered")
                self.q.put(("action", "The action was not delivered before its waiting period or campaign context expired."))
                return
            if not self.injector.write_run_file([compiled]) or not self.injector.execute():
                self.journal.set_status(tx, "uncertain")
                self.q.put(("action", "Delivery is unconfirmed. It will not be retried automatically."))
                return
            # Leave confirmation to matching game.log output, not keyboard success.
            def expire():
                self.journal.set_status(tx, "uncertain")
            timer = threading.Timer(15, expire)
            timer.daemon = True
            timer.start()

    def _strategy_tick(self, state):
        if not state.get("live") or not self.strategist or self._strategy_busy or self.ui.busy:
            return
        review = self.strategist.due(state, self.world)
        if review:
            self._strategy_busy = True
            threading.Thread(target=self._strategy_review, args=(review, copy.deepcopy(state), copy.deepcopy(self.world)), daemon=True).start()

    def _strategy_review(self, review, state, world):
        actor = review["actor"]
        channel = "strategy:" + actor
        try:
            history = self.strategist.history(review["gid"], actor, review["day"])
            content = (self._unified_context(channel, state, world) + "\nYOUR OWN PREVIOUS REVIEWS:\n" + json.dumps(history) +
                       "\nREVIEW TRIGGER:\n" + review["reason"] +
                       "\nChoose hold, infantry or mobile production emphasis for your own country for 14 days. "
                       "The existing game AI continues all operations. Prefer hold if evidence is inadequate. "
                       "No free resources, treaties or wars. At most ONE ai_priority action, decision=accept. "
                       "Explain one current national interest and why this emphasis follows from the evidence. "
                       "Keep future story branches unconfirmed. Reply in 80-140 words.")
            raw = self.llm.chat([{"role": "system", "content": self._unified_system(channel, state, world)},
                                 {"role": "user", "content": content}], purpose="automatic")
            parsed = self._parse_unified_reply(raw, channel)
            message = str(parsed.get("message", "No structured strategic reply received."))
            status = "preview" if self.injector.dry_run else "proposed"
            if (self.latest_state.get("gid") != state.get("gid") or not self.strategist.settings["enabled"]
                    or actor not in self.strategist.settings["countries"]):
                status = "discarded"
            else:
                self._unified_actions(parsed, review["reason"], channel, state, world, automatic=True)
                self.q.put(("sysnote", f"AI review — {actor} ({status}): {message}"))
            self.strategist.finish(review, message, status)
        except BudgetExceeded as error:
            self.strategist.finish(review, "", "budget_blocked")
            self.q.put(("sysnote", str(error)))
        except Exception as error:
            self.strategist.finish(review, "Review failed; no automatic retry.", "failed")
            self.q.put(("sysnote", f"AI review stopped: {error}"))
        finally:
            self._strategy_busy = False

    def usage_from_ui(self):
        if not isinstance(self.llm, BudgetClient):
            return
        snapshot = self.llm.budget.snapshot()
        pilot = self.strategist.settings if self.strategist else None
        countries = [n for tag, n in self.world.items() if tag != self.latest_state.get("tag") and n.get("maj") == "1"]
        def save(tags, enabled):
            if self.strategist:
                self.strategist.save(tags, enabled, self.latest_state, self.world)
        self.ui.show_usage(snapshot, pilot, countries, save,
                           self.journal.recent(self.cfg["game_profile"], self.gid, day=self.day) if self.journal else [])

    def unified_news(self, kind):
        self._park_active_event()
        self.channel, self.channel_name = "press:" + kind, "Campaign press"
        self.ui.set_header("Voices of History — campaign press")
        self._show_channel_history()
        self._raise()
        self.ui.set_busy(True)
        threading.Thread(target=self._do_llm, args=("Write a short current campaign report. Take no actions.", self.channel, self.channel_name, ""), daemon=True).start()
