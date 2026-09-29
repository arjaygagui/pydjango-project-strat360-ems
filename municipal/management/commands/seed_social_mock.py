"""
Seed MOCK social-service records on real voters of one city, in the same format
as CALOOCAN-EMS's seeded social_services (claimant = beneficiary = the voter,
barangay from their record, Caloocan's types / amount ranges / status mix), plus
one Caloocan-worded activity entry per record in ems_voter_audit.

Every row is flagged: muni_social_services.is_mock = 1 and the audit meta has
"mock": true, so `manage.py clear_social_mock` removes exactly these and nothing else.

    python manage.py seed_social_mock --province bulacan --city BUSTOS --count 100
"""
import datetime
import json
import random
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from municipal import social
from municipal.regions import province_pretty, region_of, voter_table
from municipal.text import title

PH = ZoneInfo('Asia/Manila')
# Caloocan's mix (per 50 rows), scaled to --count.
TYPE_WEIGHTS = [('Livelihood Assistance', 10), ('Medical Assistance', 10), ('Financial Assistance', 9),
                ('Senior Citizen Support', 8), ('Food Pack / Relief', 5), ('Educational Assistance', 5),
                ('Burial Assistance', 3)]
STATUS_WEIGHTS = [('Released', 29), ('Approved', 11), ('Pending', 5), ('Rejected', 5)]
# Application-form details per type: (agencies, programs, purposes). Puroks are Purok 1–7.
DETAILS = {
    'Medical Assistance': (['DSWD', 'PCSO', 'DOH', 'MSWD'], ['Medical Assistance Program', 'AICS'],
                           ['Hospital bill', 'Maintenance medicines', 'Laboratory and diagnostic tests', 'Dialysis sessions']),
    'Financial Assistance': (['DSWD', 'MSWD'], ['AICS', 'Indigent'],
                             ['Emergency expenses after a family crisis', 'House repair after typhoon', 'Utility arrears']),
    'Livelihood Assistance': (['DOLE', 'TESDA', 'MSWD'], ['Livelihood Program', 'Sariling Sikap'],
                              ['Sari-sari store capital', 'Food cart starter kit', 'Tricycle repair', 'Backyard hog raising']),
    'Educational Assistance': (['CHED', 'MSWD', "Mayor's Office"], ['Tulong Dunong'],
                               ['Tuition fee', 'School supplies and uniform', 'Board exam review fee']),
    'Burial Assistance': (['MSWD', 'LGU'], ['Burial Assistance Program'], ['Funeral and burial expenses']),
    'Senior Citizen Support': (['MSWD', 'LGU'], ['Senior Citizen Pension'], ['Quarterly social pension', 'Birthday cash gift']),
    'Food Pack / Relief': (['MSWD', 'LGU'], ['Indigent'], ['Relief pack after flooding', 'Family food pack']),
}


def spread(weights, n, rng):
    """Deal `n` items in proportion to `weights` (largest-remainder), shuffled."""
    total = sum(w for _, w in weights)
    raw = [(k, w * n / total) for k, w in weights]
    counts = {k: int(x) for k, x in raw}
    for k, _ in sorted(raw, key=lambda kx: kx[1] - int(kx[1]), reverse=True)[:n - sum(counts.values())]:
        counts[k] += 1
    items = [k for k, c in counts.items() for _ in range(c)]
    rng.shuffle(items)
    return items


class Command(BaseCommand):
    help = 'Seed mock (is_mock=1) Caloocan-format social-service records on real voters of one city.'

    def add_arguments(self, parser):
        parser.add_argument('--province', default='bulacan', help='province slug, e.g. bulacan')
        parser.add_argument('--city', default='BUSTOS', help='municipality exactly as on the roll, e.g. BUSTOS')
        parser.add_argument('--count', type=int, default=100)
        parser.add_argument('--days', type=int, default=120, help='spread created_at over the last N days')
        parser.add_argument('--seed', type=int, default=360, help='random seed (reproducible)')

    def handle(self, *args, **o):
        prov, city, count = o['province'].lower(), o['city'].strip(), o['count']
        table = voter_table(prov)
        if not table:
            raise CommandError(f'Unknown province slug: {prov}')
        if not 1 <= count <= 1000:
            raise CommandError('--count must be between 1 and 1000')

        social.ensure_table()
        with connections['ext'].cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM {social.TABLE} WHERE province_slug = %s AND municipality = %s '
                        'AND is_mock = 1', [prov, city])
            if cur.fetchone()[0]:
                raise CommandError(f'{city}, {prov} already has mock records — run clear_social_mock first.')

        # Random real voters from the city (read-only roll).
        with connections['rds'].cursor() as cur:
            cur.execute(f'SELECT id, fullname, barangay FROM {table} WHERE municipality = %s '
                        'ORDER BY RAND(%s) LIMIT %s', [city, o['seed'], count])
            voters = cur.fetchall()
        if len(voters) < count:
            raise CommandError(f'Only {len(voters)} voters found for {city} in {table} — check the city name.')

        rng = random.Random(o['seed'])
        types = spread(TYPE_WEIGHTS, count, rng)
        statuses = spread(STATUS_WEIGHTS, count, rng)
        today = datetime.datetime.now(PH).replace(second=0, microsecond=0)

        with transaction.atomic(using='ext'), connections['ext'].cursor() as cur:
            for (vid, fullname, barangay), atype, status in zip(voters, types, statuses):
                lo, hi = social.ASSISTANCE_TYPES[atype]
                amount = rng.randint(lo // 100, hi // 100) * 100
                when_ph = (today - datetime.timedelta(days=rng.randint(0, o['days'])))\
                    .replace(hour=rng.randint(8, 16), minute=rng.randint(0, 59))
                when_utc = when_ph.astimezone(datetime.timezone.utc).replace(tzinfo=None)
                name = title(fullname)
                agencies, programs, purposes = DETAILS[atype]
                cur.execute(
                    f'INSERT INTO {social.TABLE} (province_slug, municipality, voter_id, assistance_type, '
                    'claimant, beneficiary, barangay, amount, status, created_at, created_by, is_mock, '
                    'region, province, city_municipality, date_requested, purok, agency, program, purpose) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s)',
                    [prov, city, vid, atype, name, name, title(barangay) or None, amount, status,
                     when_utc, 'Administrator',
                     region_of(prov), province_pretty(prov), title(city), when_ph.date(),
                     f'Purok {rng.randint(1, 7)}', rng.choice(agencies), rng.choice(programs), rng.choice(purposes)],
                )
                new_id = cur.lastrowid
                cur.execute(
                    'INSERT INTO ems_voter_audit (province_slug, voter_id, action, description, actor, '
                    'actor_ip, meta, created_at) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s)',
                    [prov, vid, 'social.record', social.record_description(atype, amount), 'Administrator',
                     json.dumps({'mock': True, 'muni_social_id': new_id, 'type': atype,
                                 'amount': amount, 'status': status}), when_utc],
                )

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {count} mock social-service records (+{count} activity entries) '
            f'for {title(city)}, {prov}.'))
        self.stdout.write('  types: ' + ', '.join(f'{t} {types.count(t)}' for t, _ in TYPE_WEIGHTS))
        self.stdout.write('  status: ' + ', '.join(f'{s} {statuses.count(s)}' for s, _ in STATUS_WEIGHTS))
