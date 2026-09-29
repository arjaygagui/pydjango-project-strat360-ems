"""
Seed MOCK demo data for one city so every page has something to show:

  * political machinery (national ems_voter_political, shared with CVL-NATIONAL):
    1 city/municipal coordinator, 1–2 barangay coordinators per barangay, supporters
    (about half the city's smart cardholders + random voters) and some opposition.
    Rows are marked assigned_by = 'mock-seed' — that is the only way to tell them apart.
  * Personal Details (muni_voter_details, is_mock = 1) for everyone above, the city's
    cardholders and social-service beneficiaries, plus random voters up to --details.
    Kept consistent with the other features (card gender / civil status; Senior Discount
    cardholders are 60+). A few voters are Inactive / Transferred / Deceased.
  * Households (muni_household_members, is_mock = 1): --households heads with 1–4 members —
    same-surname voters of the same barangay plus unregistered children.
  * Sectors (muni_voter_sectors, is_mock = 1) derived from the above — see seed_sector_mock.
  * Activity log (ems_voter_audit) entries for all of it — and "card issued" entries for the
    city's mock smart cards, which were seeded without any — flagged meta.mock / meta.seed = demo.

Mock smart cards and social services have their own seeders (seed_card_mock,
seed_social_mock). Remove everything this command wrote with:

    python manage.py clear_demo_mock --province bulacan --city BUSTOS

    python manage.py seed_demo_mock --province bulacan --city BUSTOS
"""
import datetime
import json
import random

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from municipal import household as hh
from municipal import smartcard as sc
from municipal import social as soc
from municipal.machinery import roles, voter_table_qualified
from municipal.regions import voter_table
from municipal.text import title

MARK = 'mock-seed'          # ems_voter_political.assigned_by for seeded rows
SEED_TAG = 'demo'           # ems_voter_audit.meta.seed for seeded rows
ENCODERS = ['Administrator', 'brgy.encoder1', 'brgy.encoder2', 'mswdo.staff']

AGE_W = [((18, 25), 15), ((26, 35), 22), ((36, 45), 21), ((46, 59), 22), ((60, 85), 20)]
CIVIL_W = [('Single', 34), ('Married', 46), ('Widowed', 9), ('Separated', 5), ('Live-in', 6)]
RELIGION_W = [('Roman Catholic', 78), ('Iglesia ni Cristo', 8), ('Christian', 6), ('Aglipay', 3),
              ('Jesus Is Lord', 2), ('Protestant', 1), ('Islam', 1), ('Seventh Day Adventist', 1)]
EDUCATION_W = [('Elementary', 15), ('High School', 38), ('Vocational', 12), ('College Graduate', 30), ('Post Graduate', 5)]
INCOME_W = [(hh.INCOME[0], 38), (hh.INCOME[1], 40), (hh.INCOME[2], 16), (hh.INCOME[3], 6)]
OCCUPATION_W = [('Farmer', 14), ('Homemaker', 14), ('Vendor', 10), ('Self-employed', 9), ('Tricycle Driver', 8),
                ('Factory Worker', 8), ('Construction Worker', 7), ('Retired', 7), ('Government Employee', 6),
                ('Student', 5), ('Teacher', 4), ('OFW', 4), ('Unemployed', 4)]
ORGS = ['TODA', '4Ps Beneficiary', 'Kababaihan', 'Sangguniang Kabataan', 'Farmers Association',
        'Barangay Health Worker', 'Parents-Teachers Association']
LANG_W = [('Tagalog', 70), ('Tagalog, English', 30)]
STATUS_W = [('Active', 960), ('Inactive', 20), ('Transferred', 13), ('Deceased', 7)]
CHILD_F = ['Maria', 'Angel', 'Princess', 'Nicole', 'Jasmine', 'Kristine', 'Camille', 'Trisha', 'Bea', 'Andrea', 'Janelle', 'Rica']
CHILD_M = ['Juan', 'Joshua', 'Angelo', 'Christian', 'Carlo', 'Miguel', 'Paolo', 'Renz', 'Jericho', 'Kevin', 'Nathan', 'Rafael']


def pick(rng, weights):
    return rng.choices([k for k, _ in weights], [w for _, w in weights])[0]


class Command(BaseCommand):
    help = 'Seed mock demo data (machinery, personal details, households, activity) for one city.'

    def add_arguments(self, parser):
        parser.add_argument('--province', default='bulacan')
        parser.add_argument('--city', default='BUSTOS', help='municipality exactly as on the roll')
        parser.add_argument('--details', type=int, default=2500, help='voters with Personal Details (total)')
        parser.add_argument('--households', type=int, default=80)
        parser.add_argument('--days', type=int, default=180, help='spread activity over the past N days')
        parser.add_argument('--seed', type=int, default=360)

    # ------------------------------------------------------------------
    def handle(self, *args, **o):
        prov, city = o['province'].lower(), o['city'].strip()
        table = voter_table(prov)
        if not table:
            raise CommandError(f'Unknown province slug: {prov}')
        scope = {'province': prov, 'municipality': city, 'table': table}
        roll = voter_table_qualified(scope)
        rng = random.Random(o['seed'])
        self.rng = rng
        self.now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)   # DB stores UTC
        self.days = max(1, o['days'])
        hh.ensure_tables()
        rs = roles()

        with connections['ext'].cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {hh.DETAILS} WHERE province_slug = %s AND municipality = %s AND is_mock = 1',
                        [prov, city])
            has_details = cur.fetchone()[0]
            cur.execute(f'SELECT COUNT(*) FROM ems_voter_political p JOIN {roll} v ON v.id = p.voter_id '
                        'WHERE p.province_slug = %s AND v.municipality = %s AND p.assigned_by = %s', [prov, city, MARK])
            if has_details or cur.fetchone()[0]:
                raise CommandError(f'{city}, {prov} already has demo mock data — run clear_demo_mock first.')

            # Barangays (roll values can differ only by spacing/case: group by the pretty name).
            cur.execute(f'SELECT barangay, COUNT(*) FROM {roll} WHERE municipality = %s GROUP BY barangay', [city])
            brgys = {}
            for raw, n in cur.fetchall():
                b = brgys.setdefault(title(raw), {'raw': [], 'voters': 0})
                b['raw'].append(raw)
                b['voters'] += n
            if not brgys:
                raise CommandError(f'No voters found for {city} in {table} — check the city name.')
            total_voters = sum(b['voters'] for b in brgys.values())

            cur.execute(f'SELECT voter_id FROM ems_voter_political p JOIN {roll} v ON v.id = p.voter_id '
                        'WHERE p.province_slug = %s AND v.municipality = %s', [prov, city])
            taken = {r[0] for r in cur.fetchall()}                     # already in the machinery (real)
            cur.execute(f'SELECT voter_id FROM {hh.DETAILS} WHERE province_slug = %s AND municipality = %s', [prov, city])
            has_real_details = {r[0] for r in cur.fetchall()}
            cards = {}
            if sc.table_exists():
                cur.execute(f"SELECT voter_id, barangay, service, gender, civil_status, card_number, status, issued_date, is_mock "
                            f"FROM {sc.TABLE} WHERE province_slug = %s AND municipality = %s", [prov, city])
                for vid, b, service, g, civil, number, status, issued, mock in cur.fetchall():
                    cards[vid] = {'barangay': b, 'service': service, 'gender': g, 'civil': civil, 'number': number,
                                  'status': status, 'issued': issued, 'mock': mock}
            social = {}
            if soc.table_exists():
                cur.execute(f'SELECT voter_id, assistance_type FROM {soc.TABLE} WHERE province_slug = %s AND municipality = %s',
                            [prov, city])
                for vid, atype in cur.fetchall():
                    social.setdefault(vid, set()).add(atype)

            # Random voters per barangay (not already in the machinery), enough for every role + details.
            detail_target = max(o['details'], len(cards) + len(social))
            pool, names = {}, {}
            for bname, b in brgys.items():
                share = b['voters'] / total_voters
                need = int(b['voters'] * 0.07) + int(detail_target * share) + 20
                ph = ','.join(['%s'] * len(b['raw']))
                cur.execute(f'SELECT v.id, v.fullname FROM {roll} v '
                            f'LEFT JOIN ems_voter_political p ON p.province_slug = %s AND p.voter_id = v.id '
                            f'WHERE v.municipality = %s AND v.barangay IN ({ph}) AND p.voter_id IS NULL '
                            f'ORDER BY RAND(%s) LIMIT {need}', [prov, city, *b['raw'], o['seed']])
                rows = cur.fetchall()
                pool[bname] = [r[0] for r in rows]
                names.update({r[0]: r[1] for r in rows})
            ids = list(cards) + list(social)
            if ids:
                cur.execute(f"SELECT id, fullname, barangay FROM {roll} WHERE id IN ({','.join(['%s'] * len(ids))})", ids)
                for vid, fullname, raw in cur.fetchall():
                    names[vid] = fullname
                    if vid in cards:
                        cards[vid]['barangay'] = title(raw)

        # ---------------- machinery ----------------
        used = set(taken)
        political, audit = [], []

        def take(bname, prefer=()):
            for vid in list(prefer) + pool[bname]:
                if vid not in used:
                    used.add(vid)
                    return vid
            return None

        big_brgy = max(brgys, key=lambda b: brgys[b]['voters'])
        muni_coord = take(big_brgy)
        political.append((muni_coord, 'municipal_coordinator', None, self.when(170, 150)))
        brgy_coords = {}
        for bname, b in brgys.items():
            coords = [take(bname) for _ in range(2 if b['voters'] > 5000 else 1)]
            brgy_coords[bname] = [c for c in coords if c]
            for c in brgy_coords[bname]:
                political.append((c, 'barangay_coordinator', muni_coord, self.when(150, 120)))
        holders_by_brgy = {}
        for vid, c in cards.items():
            if c['status'] in sc.HOLDING and vid not in used:
                holders_by_brgy.setdefault(title(c['barangay'] or ''), []).append(vid)
        for bname, b in brgys.items():
            carded = holders_by_brgy.get(bname, [])
            rng.shuffle(carded)
            carded = carded[:len(carded) // 2]                    # about half the cardholders are supporters
            n_supp = max(len(carded), round(b['voters'] * rng.uniform(0.015, 0.05)))
            n_opp = round(b['voters'] * rng.uniform(0.001, 0.005))
            for i in range(n_supp):
                vid = take(bname, carded[i:i + 1] if i < len(carded) else ())
                if vid:
                    political.append((vid, 'supporter', rng.choice(brgy_coords[bname]) if brgy_coords[bname] else None,
                                      self.when(115)))
            for _ in range(n_opp):
                vid = take(bname)
                if vid:
                    political.append((vid, 'opposition', None, self.when(120)))
        for vid, code, upline, at in political:
            audit.append(self.audit(prov, vid, 'political.assign', f'Tagged as {rs[code]["label"]}',
                                    {'role': code, 'upline_id': upline}, at))

        # ---------------- personal details ----------------
        roles_of = {vid: code for vid, code, _, _ in political}
        people = [vid for vid, *_ in political] + list(cards) + list(social)
        for bname in brgys:                                        # random extras up to the target
            people += [v for v in pool[bname] if v not in used]
        seen, detail_ids = set(), []
        for vid in people:
            if vid not in seen and vid not in has_real_details:
                seen.add(vid)
                detail_ids.append(vid)
        detail_ids = detail_ids[:max(detail_target, len(political) + len(cards) + len(social))]

        details = {}
        for vid in detail_ids:
            card = cards.get(vid)
            senior = (card and card['service'] == 'Senior Discount') or 'Senior Citizen Support' in social.get(vid, ())
            lo, hi = (60, 85) if senior else pick(rng, AGE_W)
            age = rng.randint(lo, hi)
            birth = datetime.date.today() - datetime.timedelta(days=age * 365 + rng.randint(0, 364))
            gender = {'M': 'Male', 'F': 'Female'}.get(card and card['gender']) or rng.choice(hh.GENDERS)
            civil = (card and card['civil']) or pick(rng, CIVIL_W if age < 60 else [('Married', 50), ('Widowed', 35), ('Single', 10), ('Separated', 5)])
            if civil not in hh.CIVIL_STATUSES:
                civil = 'Married'
            occupation = 'Retired' if age >= 65 and rng.random() < 0.6 else ('Student' if age <= 22 and rng.random() < 0.5 else pick(rng, OCCUPATION_W))
            org = 'Senior Citizens Association' if age >= 60 and rng.random() < 0.6 else (rng.choice(ORGS) if rng.random() < 0.35 else None)
            status = 'Active' if (vid in roles_of or card) else pick(rng, STATUS_W)
            details[vid] = {
                'birthdate': birth, 'gender': gender, 'civil_status': civil, 'religion': pick(rng, RELIGION_W),
                'education': pick(rng, EDUCATION_W), 'occupation': occupation, 'income': pick(rng, INCOME_W),
                'lang': pick(rng, LANG_W), 'org': org, 'status': status, 'at': self.when(120),
            }
            fields = ['gender', 'birthdate', 'civil status', 'religion', 'educational attainment', 'occupation',
                      'monthly income', 'dialect / language'] + (['organization'] if org else []) + ['voter status']
            audit.append(self.audit(prov, vid, 'details.update', 'Updated voter personal details: ' + ', '.join(fields),
                                    {'fields': fields}, details[vid]['at']))

        # ---------------- households ----------------
        members, member_ids = [], set()
        heads_pool = [vid for vid, code, *_ in political if code != 'opposition'] + [v for v in detail_ids if v not in roles_of]
        heads, head_set = [], set()
        for vid in heads_pool:
            d = details.get(vid)
            if d and vid not in head_set and d['status'] == 'Active' and datetime.date.today().year - d['birthdate'].year >= 25:
                heads.append(vid)
                head_set.add(vid)
            if len(heads) >= o['households']:
                break
        with connections['ext'].cursor() as cur:
            for head in heads:
                fullname = names.get(head) or ''
                surname = fullname.split(',')[0].strip()
                bname = next((b for b in brgys if head in pool[b]), None) or title((cards.get(head) or {}).get('barangay') or '')
                raws = brgys.get(bname, {}).get('raw', [])
                relatives = []
                if surname and raws:
                    ph = ','.join(['%s'] * len(raws))
                    cur.execute(f'SELECT id, fullname, precinct FROM {roll} WHERE municipality = %s AND barangay IN ({ph}) '
                                f'AND fullname LIKE %s AND id <> %s ORDER BY RAND(%s) LIMIT 4',
                                [city, *raws, surname + ',%', head, o['seed']])
                    relatives = [r for r in cur.fetchall() if r[0] not in member_ids and r[0] not in head_set
                                 and (details.get(r[0]) or {}).get('status') != 'Deceased']
                at = self.when(90)
                n_reg = min(len(relatives), rng.choice([0, 1, 1, 2]))
                head_age = datetime.date.today().year - details[head]['birthdate'].year
                for i, (rid, rname, rprec) in enumerate(relatives[:n_reg]):
                    rel = 'Spouse' if i == 0 and rng.random() < 0.6 else self.relation(details.get(rid), head_age)
                    member_ids.add(rid)
                    members.append([prov, city, head, rid, title(rname), rel, (rprec or '').strip() or None, None, None, None, at])
                n_kids = rng.choice([0, 1, 1, 2, 2, 3]) if head_age < 60 else rng.choice([0, 0, 1])
                if n_reg + n_kids == 0:
                    n_kids = 1
                kid_names = set()
                for _ in range(n_kids):
                    girl = rng.random() < 0.5
                    first = rng.choice([n for n in (CHILD_F if girl else CHILD_M) if n not in kid_names])
                    kid_names.add(first)
                    kid_age = rng.randint(1, 17)
                    birthday = datetime.date.today() - datetime.timedelta(days=kid_age * 365 + rng.randint(0, 364))
                    scholar = None
                    if kid_age >= 12:
                        scholar = pick(rng, [(None, 55), ('Government Scholar', 30), ('Private Scholar', 15)])
                    kid = f'{first} {title(surname) or "Dela Cruz"}'
                    members.append([prov, city, head, None, kid, 'Daughter' if girl else 'Son', None, birthday, None, scholar, at])
        for m in members:
            prov_, _, head, rid, name, rel, *_rest, at = m
            audit.append(self.audit(prov, head, 'household.member_add', f'Added household member: {name} ({rel})',
                                    {'member_voter_id': rid}, at))
            if rid:
                audit.append(self.audit(prov, rid, 'household.member_add',
                                        f'Added to the household of {title(names.get(head, ""))} ({rel})', {'head_voter_id': head}, at))

        # ---------------- card-issued entries for the city's mock cards ----------------
        with connections['ext'].cursor() as cur:
            cur.execute("SELECT JSON_UNQUOTE(JSON_EXTRACT(meta, '$.card_number')) FROM ems_voter_audit "
                        "WHERE province_slug = %s AND action = 'card.issue'", [prov])
            logged = {r[0] for r in cur.fetchall()}
        for vid, c in cards.items():
            if c['mock'] and c['number'] not in logged:
                at = datetime.datetime.combine(c['issued'], datetime.time(rng.randint(0, 9), rng.randint(0, 59)))
                audit.append(self.audit(prov, vid, 'card.issue',
                                        f'Smart card {c["number"]} issued ({c["service"]}, {sc.STATUS_LABELS[c["status"]] if c["status"] in sc.STATUS_LABELS else c["status"]})',
                                        {'card_number': c['number'], 'service': c['service'], 'status': c['status']}, at))

        # ---------------- write ----------------
        with transaction.atomic(using='ext'), connections['ext'].cursor() as cur:
            cur.executemany(
                'INSERT INTO ems_voter_political (province_slug, voter_id, role_code, upline_province_slug, upline_voter_id, '
                'assigned_at, assigned_by) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                [[prov, vid, code, prov if up else None, up, at, MARK] for vid, code, up, at in political])
            cols = ['birthdate', 'gender', 'civil_status', 'religion', 'education', 'occupation', 'income', 'lang', 'org', 'status']
            cur.executemany(
                f'INSERT INTO {hh.DETAILS} (province_slug, voter_id, municipality, {", ".join(cols)}, updated_at, updated_by, is_mock) '
                f'VALUES (%s, %s, %s, {", ".join(["%s"] * len(cols))}, %s, %s, 1)',
                [[prov, vid, city, *[d[c] for c in cols], d['at'], MARK] for vid, d in details.items()])
            cur.executemany(
                f'INSERT INTO {hh.MEMBERS} (province_slug, municipality, voter_id, member_voter_id, name, relationship, '
                'precinct, birthday, contact, scholar, created_at, created_by, is_mock) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)',
                [m[:10] + [m[10], MARK] for m in members])
            for i in range(0, len(audit), 1000):
                cur.executemany(
                    'INSERT INTO ems_voter_audit (province_slug, voter_id, action, description, actor, actor_ip, meta, created_at) '
                    'VALUES (%s, %s, %s, %s, %s, NULL, %s, %s)', audit[i:i + 1000])

        count = lambda code: sum(1 for p in political if p[1] == code)
        self.stdout.write(self.style.SUCCESS(f'Seeded demo mock data for {title(city)}, {prov} ({total_voters:,} voters):'))
        self.stdout.write(f'  machinery: {len(political):,} — municipal {count("municipal_coordinator")}, barangay coordinators '
                          f'{count("barangay_coordinator")}, supporters {count("supporter"):,}, opposition {count("opposition")}')
        statuses = [d['status'] for d in details.values()]
        self.stdout.write(f'  personal details: {len(details):,} — ' + ', '.join(f'{s} {statuses.count(s)}' for s in hh.VOTER_STATUSES))
        self.stdout.write(f'  households: {len(heads)} heads, {len(members)} members '
                          f'({sum(1 for m in members if m[3])} registered, {sum(1 for m in members if not m[3])} unregistered)')
        self.stdout.write(f'  activity log: {len(audit):,} entries')
        call_command('seed_sector_mock', province=prov, city=city, seed=o['seed'], stdout=self.stdout)

    # ------------------------------------------------------------------
    def when(self, max_days, min_days=0):
        """A UTC timestamp between max_days and min_days ago (never in the future)."""
        seconds = self.rng.randint(min_days * 86400, max(min_days * 86400, max_days * 86400 - 1))
        return self.now - datetime.timedelta(seconds=min(seconds, self.days * 86400))

    def audit(self, prov, vid, action, description, meta, at):
        return [prov, vid, action, description[:255], self.rng.choice(ENCODERS),
                json.dumps({**meta, 'mock': True, 'seed': SEED_TAG}, default=str), at]

    def relation(self, detail, head_age):
        g = detail and detail.get('gender')
        if head_age >= 45:
            return {'Male': 'Son', 'Female': 'Daughter'}.get(g, 'Relative')
        return {'Male': 'Brother', 'Female': 'Sister'}.get(g, 'Relative')
