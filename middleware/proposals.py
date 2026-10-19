"""Durable, campaign-scoped equipment proposals. This module never sends commands."""
from contextlib import closing
import json
import re
import time
import uuid

from safe_actions import validate, text_only
from profiles import MD_EQUIPMENT_ALIASES, CWIC_EQUIPMENT_ALIASES, TFR_EQUIPMENT_ALIASES

AIRCRAFT_BLOCKER = ('Aircraft procurement is unsupported in this build: native production-line creation exists, '
                    'but exact-variant queue verification, finite aircraft batch stopping, and per-order delivery '
                    'accounting are unverified. No manufacturer was contacted and no game effect was submitted.')


def aircraft_order(text):
    return (not text_only(text) and
            bool(re.search(r'\b(buy|purchase|procure|order|build|produce|send|manufacture)\b', text, re.I)) and
            bool(re.search(r'\bf[- ]?22s?\b|\b(aircraft|airplanes?|fighters?|raptors?)\b', text, re.I)))


class Proposals:
    def __init__(self, journal):
        self.journal = journal
        with closing(journal.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS proposals("
                       "id TEXT PRIMARY KEY,profile TEXT,gid INTEGER,day INTEGER,player TEXT,"
                       "channel TEXT,representative TEXT,action TEXT,request TEXT,status TEXT,"
                       "tx TEXT,created REAL)")

    @staticmethod
    def identity(state, world, channel):
        actor = channel.split(':', 1)[1] if channel.startswith('leader:') else state['tag']
        return str(world.get(actor, state if actor == state['tag'] else {}).get('leader', ''))

    def pending(self, state, world, channel, profile):
        with closing(self.journal.connect()) as db, db:
            # Rollbacks, old campaigns, changed representatives and old snapshots
            # cannot authorize a new action. Other channels keep their own drafts.
            db.execute("UPDATE proposals SET status='stale' WHERE profile=? AND channel=? "
                       "AND status='proposed' AND (gid!=? OR player!=? OR day>? OR day<? OR representative!=?)",
                       (profile, channel, int(state['gid']), state['tag'], int(state['day']),
                        int(state['day'])-2, self.identity(state, world, channel)))
            rows = db.execute("SELECT id,action,request FROM proposals WHERE profile=? AND gid=? "
                              "AND player=? AND channel=? AND status='proposed' ORDER BY created DESC",
                              (profile, int(state['gid']), state['tag'], channel)).fetchall()
        return [(r[0], json.loads(r[1]), r[2]) for r in rows]

    def create(self, action, text, state, world, channel, profile):
        if text_only(text) or action.get('name') not in ('send_equipment', 'propose_equipment'):
            raise ValueError('Only explicit equipment-transfer requests can become proposals.')
        if aircraft_order(text):
            raise ValueError(AIRCRAFT_BLOCKER)
        if not isinstance(action.get('args'), dict):
            raise ValueError('Proposal arguments must be an object.')
        candidate = dict(action, name='send_equipment', args=dict(action['args']))
        equipment = candidate['args'].get('equipment_type', '')
        aliases = (MD_EQUIPMENT_ALIASES if profile in ('millennium_dawn', 'millennium_dawn_oem') else
                   CWIC_EQUIPMENT_ALIASES if profile == 'cwic_dev' else TFR_EQUIPMENT_ALIASES)
        names = [equipment] + [key for key, value in aliases.items() if value == equipment]
        if not equipment or not any(re.search(r'\b' + re.escape(name).replace('_', '[- _]') + r's?\b', text, re.I) for name in names):
            raise ValueError('Specify the equipment type; a model cannot substitute a different item.')
        quantity = candidate['args'].get('quantity')
        if quantity is not None and not re.search(r'(?<!\d)' + re.escape(str(quantity)) + r'(?!\d)', text):
            raise ValueError('The player must supply the quantity; do not infer it from a model reply.')
        candidate['args']['quantity'] = 1 if quantity is None else quantity
        # A missing quantity does not bypass country authority or equipment checks.
        validate(candidate, state, world, channel, 'accept', text, profile)
        candidate['args']['quantity'] = quantity
        proposal_id = uuid.uuid4().hex[:12]
        with closing(self.journal.connect()) as db, db:
            db.execute("UPDATE proposals SET status='superseded' WHERE profile=? AND gid=? "
                       "AND player=? AND channel=? AND status='proposed'",
                       (profile, int(state['gid']), state['tag'], channel))
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?, 'proposed',NULL,?)",
                       (proposal_id, profile, int(state['gid']), int(state['day']), state['tag'], channel,
                        self.identity(state, world, channel), json.dumps(candidate), text[:2000], time.time()))
        return proposal_id, candidate

    @staticmethod
    def describe(proposal_id, action):
        a = action['args']
        quantity = a.get('quantity')
        if quantity is None:
            return f"Proposal {proposal_id}: {a['sender']} → {a['receiver']}, {a['equipment_type']}. How many (1–500)? No effect submitted."
        return (f"Proposal {proposal_id}: {a['sender']} → {a['receiver']}, {quantity} {a['equipment_type']}. "
                f"Reply 'confirm {proposal_id}' to authorize this exact transfer, or 'cancel'. No effect submitted.")

    def reply(self, text, state, world, channel, profile):
        """Returns None for ordinary dialogue, otherwise (message, confirmed action, id)."""
        cleaned = text.strip().lower().rstrip('.!')
        match = re.fullmatch(r'(confirm|cancel|status)(?:\s+([a-f0-9]{12}))?', cleaned)
        quantity = re.fullmatch(r'(?:change(?: it)? to |make (?:it |that )?|quantity\s*[:=]?\s*)?(\d{1,6})', cleaned)
        if not match and not quantity:
            return None
        rows = self.pending(state, world, channel, profile)
        if match and match[1] == 'status':
            records = self.recent(profile, state, channel)
            if match[2]:
                records = [r for r in records if r['id'] == match[2]]
            if len(records) == 1:
                record = records[0]
                detail = ('Engine effect branch executed; delivery totals were not independently measured.'
                          if record['state'] == 'executed' else 'Only a matching engine receipt can establish application; previews have no game effect.')
                return f"Proposal {record['id']}: {record['state']}. {detail}", None, record['id']
        if match and match[2]:
            rows = [row for row in rows if row[0] == match[2]]
        if len(rows) != 1:
            return ('No single current proposal matches. Restate the equipment request; no effect submitted.', None, None)
        proposal_id, action, request = rows[0]
        if quantity:
            action['args']['quantity'] = int(quantity[1])
            try:
                # Preserve the original explicit equipment request and the exact
                # human quantity reply for revalidation after an application restart.
                new_id, action = self.create(action, request + '\nPlayer quantity update: ' + quantity[1], state, world, channel, profile)
                return self.describe(new_id, action), None, new_id
            except ValueError as error:
                return str(error) + ' Existing proposal unchanged; no effect submitted.', None, proposal_id
        if match[1] == 'status':
            return self.describe(proposal_id, action), None, proposal_id
        if match[1] == 'confirm':
            if not match[2]:
                return f"Use 'confirm {proposal_id}' for the exact proposal shown. No effect submitted.", None, proposal_id
            try:
                normalized = validate(action, state, world, channel, 'accept', request, profile)
            except ValueError as error:
                return str(error) + ' No effect submitted.', None, proposal_id
            status = 'confirmed'
        else:
            normalized, status = None, 'cancelled'
        with closing(self.journal.connect()) as db, db:
            changed = db.execute("UPDATE proposals SET status=? WHERE id=? AND status='proposed'",
                                 (status, proposal_id)).rowcount
        if not changed:
            return 'This proposal was already handled; no repeat submission.', None, proposal_id
        return f'Proposal {proposal_id}: {status}.', normalized, proposal_id

    def bind(self, proposal_id, tx):
        with closing(self.journal.connect()) as db, db:
            db.execute("UPDATE proposals SET tx=? WHERE id=? AND status='confirmed' AND tx IS NULL", (tx, proposal_id))

    def unsupported(self, text, state, world, channel, profile):
        proposal_id = uuid.uuid4().hex[:12]
        with closing(self.journal.connect()) as db, db:
            db.execute("UPDATE proposals SET status='superseded' WHERE profile=? AND gid=? AND player=? AND channel=? AND status='proposed'",
                       (profile, int(state['gid']), state['tag'], channel))
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,'unsupported',NULL,?)",
                       (proposal_id, profile, int(state['gid']), int(state['day']), state['tag'], channel,
                        self.identity(state, world, channel), json.dumps({'kind':'aircraft_procurement'}),text[:2000],time.time()))
        return f'Proposal {proposal_id}: unsupported. ' + AIRCRAFT_BLOCKER

    def recent(self, profile, state, channel):
        with closing(self.journal.connect()) as db:
            rows = db.execute("SELECT p.id,p.action,p.status,a.state,p.tx FROM proposals p "
                              "LEFT JOIN actions a ON a.id=p.tx WHERE p.profile=? AND p.gid=? "
                              "AND p.player=? AND p.channel=? AND p.day<=? ORDER BY p.created DESC LIMIT 5",
                              (profile, int(state['gid']), state['tag'], channel, int(state['day']))).fetchall()
        return [{'id': r[0], 'action': json.loads(r[1]), 'state': r[3] or r[2], 'transaction': r[4]} for r in rows]
