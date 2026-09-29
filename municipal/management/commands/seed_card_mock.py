"""
Seed MOCK smart cards on real voters of one city, in the same format as
CALOOCAN-EMS's seeded smart_cards: SC-<yy>-<serial> numbers, Caloocan's status /
service / gender / civil-status mix, barangay from the voter's record, issue dates
spread over the past --months. Like Caloocan's seed, no activity entries are written.

Every row is flagged is_mock = 1, so `manage.py clear_card_mock` removes exactly these.

    python manage.py seed_card_mock --province bulacan --city BUSTOS --count 300
"""
import datetime
import random

from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from municipal import smartcard
from municipal.machinery import voter_table_qualified
from municipal.regions import voter_table
from municipal.text import title
from municipal.management.commands.seed_social_mock import spread

# Caloocan's mix (per its 3,000 cards).
STATUS_WEIGHTS = [('active', 2524), ('pending', 476)]
SERVICE_WEIGHTS = [('Educational Support', 527), ('PWD Benefits', 519), ('Food Pack / Relief', 503),
                   ('Senior Discount', 501), ('Medical Assistance', 498), ('Transport Subsidy', 452)]
GENDER_WEIGHTS = [('F', 1506), ('M', 1494)]
CIVIL_WEIGHTS = [('Single', 781), ('Separated', 759), ('Widowed', 733), ('Married', 727)]


class Command(BaseCommand):
    help = 'Seed mock (is_mock=1) Caloocan-format smart cards on real voters of one city.'

    def add_arguments(self, parser):
        parser.add_argument('--province', default='bulacan')
        parser.add_argument('--city', default='BUSTOS', help='municipality exactly as on the roll')
        parser.add_argument('--count', type=int, default=300)
        parser.add_argument('--months', type=int, default=30, help='spread issue dates over the past N months')
        parser.add_argument('--seed', type=int, default=360)

    def handle(self, *args, **o):
        prov, city, count = o['province'].lower(), o['city'].strip(), o['count']
        table = voter_table(prov)
        if not table:
            raise CommandError(f'Unknown province slug: {prov}')
        if not 1 <= count <= 5000:
            raise CommandError('--count must be between 1 and 5000')

        smartcard.ensure_table()
        scope = {'province': prov, 'municipality': city, 'table': table}
        with connections['ext'].cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {smartcard.TABLE} WHERE province_slug = %s AND municipality = %s '
                        'AND is_mock = 1', [prov, city])
            if cur.fetchone()[0]:
                raise CommandError(f'{city}, {prov} already has mock cards — run clear_card_mock first.')
            # Random real voters of the city who do not already hold a card.
            cur.execute(
                f'SELECT v.id, v.barangay FROM {voter_table_qualified(scope)} v '
                f'LEFT JOIN {smartcard.TABLE} c ON c.province_slug = %s AND c.voter_id = v.id '
                'WHERE v.municipality = %s AND c.id IS NULL ORDER BY RAND(%s) LIMIT %s',
                [prov, city, o['seed'], count])
            voters = cur.fetchall()
        if len(voters) < count:
            raise CommandError(f'Only {len(voters)} eligible voters found for {city} — check the city name.')

        rng = random.Random(o['seed'])
        statuses = spread(STATUS_WEIGHTS, count, rng)
        services = spread(SERVICE_WEIGHTS, count, rng)
        genders = spread(GENDER_WEIGHTS, count, rng)
        civils = spread(CIVIL_WEIGHTS, count, rng)
        today = datetime.date.today()
        span = max(1, o['months'] * 30)
        issued = sorted(today - datetime.timedelta(days=rng.randint(1, span)) for _ in range(count))

        with transaction.atomic(using='ext'), connections['ext'].cursor() as cur:
            serial = smartcard.next_serial(cur)
            rows = []
            for i, ((vid, brgy), when) in enumerate(zip(voters, issued)):
                rows.append([prov, city, vid, smartcard.card_number(when, serial + i), statuses[i],
                             civils[i], genders[i], services[i], title(brgy) or None, when, 'Administrator'])
            cur.executemany(
                f'INSERT INTO {smartcard.TABLE} (province_slug, municipality, voter_id, card_number, status, '
                'civil_status, gender, service, barangay, issued_date, created_at, created_by, is_mock) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(), %s, 1)', rows)

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {count} mock smart cards for {title(city)}, {prov} '
            f'({rows[0][3]} … {rows[-1][3]}).'))
        for label, items, weights in (('status', statuses, STATUS_WEIGHTS), ('service', services, SERVICE_WEIGHTS),
                                      ('gender', genders, GENDER_WEIGHTS), ('civil', civils, CIVIL_WEIGHTS)):
            self.stdout.write(f'  {label}: ' + ', '.join(f'{k} {items.count(k)}' for k, _ in weights))
