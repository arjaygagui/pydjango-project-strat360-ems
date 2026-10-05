"""
Create the per-level machinery tables in generic_360_db (see municipal/machinery.py):

    brgy_voter_political   Barangay EMS
    muni_voter_political   City / Municipal EMS
    prov_voter_political   Province-Wide EMS
    (ems_voter_political   Nationwide EMS — already exists, shared with CVL-NATIONAL)

They are created LIKE ems_voter_political (same columns, keys, collation) plus the
foreign key to ems_political_role. Existing tables are left alone.

--move-city-mock moves the City EMS's demo rows (assigned_by = 'mock-seed', written by
seed_demo_mock before the split) from ems_voter_political into muni_voter_political, in one
transaction. It refuses if any row outside the mock set points at a mock row (or the other
way round), so no chain is ever split across tables.

    python manage.py machinery_setup                    # brgy_, muni_ and prov_voter_political
    python manage.py machinery_setup --move-city-mock
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from municipal import machinery as mach
from municipal.management.commands.seed_demo_mock import MARK


class Command(BaseCommand):
    help = 'Create the per-level machinery tables (and move the City EMS demo rows into its table).'

    def add_arguments(self, parser):
        parser.add_argument('--move-city-mock', action='store_true',
                            help="move assigned_by = 'mock-seed' rows from ems_voter_political to muni_voter_political")

    def handle(self, *args, **o):
        created = mach.ensure_tables()
        self.stdout.write(f'Created: {", ".join(created)}' if created else 'Tables already exist.')
        if not o['move_city_mock']:
            return

        nat, city = mach.table('nat'), mach.table('city')
        with transaction.atomic(using='ext'), connections['ext'].cursor() as cur:
            cur.execute(
                f'SELECT COUNT(*) FROM {nat} a JOIN {nat} b '
                'ON b.province_slug = a.upline_province_slug AND b.voter_id = a.upline_voter_id '
                'WHERE (a.assigned_by = %s) <> (b.assigned_by = %s)', [MARK, MARK])
            if cur.fetchone()[0]:
                raise CommandError('Some national rows are linked to mock rows — not moving anything.')
            cur.execute(
                f'SELECT COUNT(*) FROM {city} c JOIN {nat} n ON n.province_slug = c.province_slug '
                'AND n.voter_id = c.voter_id WHERE n.assigned_by = %s', [MARK])
            if cur.fetchone()[0]:
                raise CommandError(f'{city} already holds some of these voters — not moving anything.')
            cur.execute(f'INSERT INTO {city} SELECT * FROM {nat} WHERE assigned_by = %s', [MARK])
            moved = cur.rowcount
            cur.execute(f'DELETE FROM {nat} WHERE assigned_by = %s', [MARK])
            if cur.rowcount != moved:
                raise CommandError('Row counts differ — rolled back.')
        self.stdout.write(self.style.SUCCESS(f'Moved {moved:,} City EMS demo rows from {nat} to {city}.'))
