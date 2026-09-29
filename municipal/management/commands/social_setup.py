"""
Create / upgrade generic_360_db.muni_social_services (Caloocan-format social services
for the city/municipal EMS). Safe to re-run.

  * creates the table if missing
  * adds any missing application-form columns (birthdate, gender, address, purpose, ...)
  * backfills only DERIVABLE fields on existing rows: date_requested (from created_at,
    in PH time) and the region / province / city snapshot (from the province slug and
    municipality). Personal details (birthdate, gender, contact...) are never invented.

    python manage.py social_setup
"""
from django.core.management.base import BaseCommand
from django.db import connections

from municipal import social
from municipal.regions import province_pretty, region_of
from municipal.text import title


class Command(BaseCommand):
    help = 'Create/upgrade muni_social_services in generic_360_db and backfill derivable fields.'

    def handle(self, *args, **options):
        existed = social.table_exists()
        added = social.ensure_table()
        if not existed:
            self.stdout.write(self.style.SUCCESS(f'{social.TABLE}: created in generic_360_db.'))
        elif added:
            self.stdout.write(self.style.SUCCESS(f'{social.TABLE}: added {len(added)} column(s): {", ".join(added)}'))
        else:
            self.stdout.write(f'{social.TABLE}: already up to date.')

        with connections['ext'].cursor() as cur:
            cur.execute(f"UPDATE {social.TABLE} SET date_requested = DATE(CONVERT_TZ(created_at, '+00:00', '+08:00')) "
                        'WHERE date_requested IS NULL AND created_at IS NOT NULL')
            dated = cur.rowcount
            cur.execute(f'SELECT DISTINCT province_slug, municipality FROM {social.TABLE} '
                        'WHERE region IS NULL OR province IS NULL OR city_municipality IS NULL')
            places = cur.fetchall()
            located = 0
            for slug, muni in places:
                cur.execute(
                    f'UPDATE {social.TABLE} SET region = COALESCE(region, %s), province = COALESCE(province, %s), '
                    'city_municipality = COALESCE(city_municipality, %s) '
                    'WHERE province_slug = %s AND municipality = %s',
                    [region_of(slug), province_pretty(slug), title(muni), slug, muni])
                located += cur.rowcount
        if dated or located:
            self.stdout.write(f'  backfilled date_requested on {dated} row(s), address snapshot on {located} row(s).')
