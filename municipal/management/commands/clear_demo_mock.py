"""
Remove what seed_demo_mock wrote, and nothing else:

  * ems_voter_political rows with assigned_by = 'mock-seed' (real voters that were later
    placed under a mock coordinator keep their own position; only that upline is cleared)
  * muni_voter_details / muni_voter_sectors / muni_household_members rows with is_mock = 1
    (details a person has since edited on a profile are real and stay)
  * ems_voter_audit entries with meta.seed = 'demo'

    python manage.py clear_demo_mock --province bulacan --city BUSTOS
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction

from municipal import household as hh
from municipal.machinery import voter_table_qualified
from municipal.regions import voter_table
from municipal.management.commands.seed_demo_mock import MARK, SEED_TAG


class Command(BaseCommand):
    help = 'Delete the demo mock data written by seed_demo_mock for one city.'

    def add_arguments(self, parser):
        parser.add_argument('--province', required=True)
        parser.add_argument('--city', required=True, help='municipality exactly as on the roll')

    def handle(self, *args, **o):
        prov, city = o['province'].lower(), o['city'].strip()
        table = voter_table(prov)
        if not table:
            raise CommandError(f'Unknown province slug: {prov}')
        roll = voter_table_qualified({'province': prov, 'municipality': city, 'table': table})
        in_city = f'JOIN {roll} v ON v.id = {{alias}}.voter_id AND v.municipality = %s'

        with transaction.atomic(using='ext'), connections['ext'].cursor() as cur:
            # Real positions that point at a mock upline lose just that link.
            cur.execute(
                f'UPDATE ems_voter_political r JOIN ems_voter_political m '
                f'ON m.province_slug = r.upline_province_slug AND m.voter_id = r.upline_voter_id '
                f'{in_city.format(alias="m")} '
                'SET r.upline_province_slug = NULL, r.upline_voter_id = NULL '
                'WHERE m.province_slug = %s AND m.assigned_by = %s AND (r.assigned_by IS NULL OR r.assigned_by <> %s)',
                [city, prov, MARK, MARK])
            detached = cur.rowcount
            cur.execute(f'DELETE p FROM ems_voter_political p {in_city.format(alias="p")} '
                        'WHERE p.province_slug = %s AND p.assigned_by = %s', [city, prov, MARK])
            political = cur.rowcount
            deleted = {}
            if hh.tables_exist():
                for name in (hh.SECTORS_TABLE, hh.MEMBERS, hh.DETAILS):
                    cur.execute(f'DELETE FROM {name} WHERE province_slug = %s AND municipality = %s AND is_mock = 1', [prov, city])
                    deleted[name] = cur.rowcount
            cur.execute(f'DELETE a FROM ems_voter_audit a {in_city.format(alias="a")} '
                        "WHERE a.province_slug = %s AND JSON_UNQUOTE(JSON_EXTRACT(a.meta, '$.seed')) = %s",
                        [city, prov, SEED_TAG])
            audit = cur.rowcount

        self.stdout.write(self.style.SUCCESS(
            f'Removed demo mock data for {city}, {prov}: {political:,} machinery rows'
            + (f' ({detached} real position(s) detached from a mock coordinator)' if detached else '')
            + f', {deleted.get(hh.DETAILS, 0):,} personal details, {deleted.get(hh.SECTORS_TABLE, 0):,} sector memberships, '
              f'{deleted.get(hh.MEMBERS, 0):,} household members, '
              f'{audit:,} activity entries.'))
