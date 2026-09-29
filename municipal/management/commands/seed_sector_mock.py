"""
Seed MOCK sector memberships (muni_voter_sectors, is_mock = 1) for the city's mock
Personal Details, derived from the data already there rather than at random:

  Senior Citizen  age 60+                     Youth         age 18–30
  PWD             PWD Benefits cardholders (+ a few others)
  OFW             occupation OFW              TODA          tricycle drivers / TODA members
  Single Parent   single / separated / widowed 20–55 with children in their household (+ a few)
  Agriculture     farmers / Farmers Association   BHW, Kababaihan   from the organization field
  Teacher         occupation Teacher          Leader / Coordinator   mock machinery coordinators
  plus a sprinkling of Tanod, Barangay Official, Lupon, BNS, DCW, TUPAD, SPES, Non-Teaching Personnel.

Adds one activity entry per voter ("… sectors: …", meta.seed = demo). seed_demo_mock runs
this automatically; clear_demo_mock removes it.

    python manage.py seed_sector_mock --province bulacan --city BUSTOS
"""
import datetime
import json
import random

from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from municipal import household as hh
from municipal import smartcard as sc
from municipal.regions import voter_table

MARK, SEED_TAG = 'mock-seed', 'demo'
ENCODERS = ['Administrator', 'brgy.encoder1', 'brgy.encoder2', 'mswdo.staff']


def _age(bd, today):
    return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))


class Command(BaseCommand):
    help = 'Seed mock sector memberships derived from the mock Personal Details of one city.'

    def add_arguments(self, parser):
        parser.add_argument('--province', default='bulacan')
        parser.add_argument('--city', default='BUSTOS', help='municipality exactly as on the roll')
        parser.add_argument('--seed', type=int, default=360)

    def handle(self, *args, **o):
        prov, city = o['province'].lower(), o['city'].strip()
        if not voter_table(prov):
            raise CommandError(f'Unknown province slug: {prov}')
        hh.ensure_tables()
        rng = random.Random(o['seed'])
        today = datetime.date.today()

        with connections['ext'].cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {hh.SECTORS_TABLE} WHERE province_slug = %s AND municipality = %s AND is_mock = 1',
                        [prov, city])
            if cur.fetchone()[0]:
                raise CommandError(f'{city}, {prov} already has mock sectors — run clear_demo_mock first.')
            cur.execute(f'SELECT voter_id, birthdate, gender, civil_status, occupation, org, income, updated_at '
                        f'FROM {hh.DETAILS} WHERE province_slug = %s AND municipality = %s AND is_mock = 1', [prov, city])
            people = cur.fetchall()
            if not people:
                raise CommandError(f'No mock Personal Details for {city}, {prov} — run seed_demo_mock first.')
            pwd_cards = set()
            if sc.table_exists():
                cur.execute(f"SELECT voter_id FROM {sc.TABLE} WHERE province_slug = %s AND municipality = %s "
                            "AND service = 'PWD Benefits' AND status IN ('active', 'pending')", [prov, city])
                pwd_cards = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT voter_id FROM ems_voter_political WHERE province_slug = %s AND assigned_by = %s "
                        "AND role_code IN ('municipal_coordinator', 'barangay_coordinator')", [prov, MARK])
            coordinators = {r[0] for r in cur.fetchall()}
            cur.execute(f'SELECT voter_id FROM {hh.MEMBERS} WHERE province_slug = %s AND municipality = %s '
                        "AND member_voter_id IS NULL AND relationship IN ('Son', 'Daughter')", [prov, city])
            parents = {r[0] for r in cur.fetchall()}

        rows, audit = [], []
        for vid, bd, gender, civil, occupation, org, income, at in people:
            age = _age(bd, today) if bd else None
            s = set()
            if age is not None:
                if age >= 60:
                    s.add('Senior Citizen')
                if 18 <= age <= 30:
                    s.add('Youth')
                if civil in ('Single', 'Separated', 'Widowed') and 20 <= age <= 55 and (vid in parents or rng.random() < 0.12):
                    s.add('Single Parent')
                if gender == 'Male' and 25 <= age <= 60 and rng.random() < 0.02:
                    s.add('Tanod')
                if age >= 40 and rng.random() < 0.006:
                    s.add('Lupon')
                if income == hh.INCOME[0] and 18 <= age <= 60 and rng.random() < 0.06:
                    s.add('TUPAD')
                if age <= 25 and occupation == 'Student' and rng.random() < 0.25:
                    s.add('SPES')
            if vid in pwd_cards or rng.random() < 0.015:
                s.add('PWD')
            if occupation == 'OFW':
                s.add('OFW')
            if occupation == 'Tricycle Driver' or org == 'TODA':
                s.add('TODA')
            if occupation == 'Farmer' or org == 'Farmers Association':
                s.add('Agriculture')
            if occupation == 'Teacher':
                s.add('Teacher')
            if occupation == 'Government Employee' and rng.random() < 0.2:
                s.add('Non-Teaching Personnel')
            if org == 'Barangay Health Worker':
                s.add('Barangay Health Worker (BHW)')
            if org == 'Kababaihan':
                s.add('Kababaihan')
            if vid in coordinators:
                s.add('Leader / Coordinator')
            if gender == 'Female' and rng.random() < 0.006:
                s.add('Barangay Nutrition Scholar (BNS)')
            if gender == 'Female' and rng.random() < 0.005:
                s.add('Day Care Worker (DCW)')
            if rng.random() < 0.006:
                s.add('Barangay Official')
            if not s:
                continue
            ordered = [x for x in hh.SECTORS if x in s]
            for sector in ordered:
                rows.append([prov, vid, sector, city, at, MARK])
            audit.append([prov, vid, 'details.update', ('Updated voter personal details: sectors (' + ', '.join(ordered) + ')')[:255],
                          rng.choice(ENCODERS), json.dumps({'fields': ['sectors'], 'sectors': ordered, 'mock': True, 'seed': SEED_TAG}),
                          at])

        with transaction.atomic(using='ext'), connections['ext'].cursor() as cur:
            cur.executemany(f'INSERT INTO {hh.SECTORS_TABLE} (province_slug, voter_id, sector, municipality, created_at, created_by, is_mock) '
                            'VALUES (%s, %s, %s, %s, %s, %s, 1)', rows)
            cur.executemany('INSERT INTO ems_voter_audit (province_slug, voter_id, action, description, actor, actor_ip, meta, created_at) '
                            'VALUES (%s, %s, %s, %s, %s, NULL, %s, %s)', audit)

        counts = {}
        for r in rows:
            counts[r[2]] = counts.get(r[2], 0) + 1
        self.stdout.write(self.style.SUCCESS(f'Seeded {len(rows):,} mock sector memberships for {len(audit):,} voters of {city}, {prov}:'))
        self.stdout.write('  ' + ', '.join(f'{s} {counts[s]}' for s in hh.SECTORS if s in counts))
