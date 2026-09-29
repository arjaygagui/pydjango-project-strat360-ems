"""
Locate a city's barangays for the Heat Map (OpenStreetMap Nominatim, place names only,
1 request per second) and store them in generic_360_db.muni_barangay_geo. Already-located
barangays are skipped, so it is safe to re-run; --refresh re-locates everything.

    python manage.py geocode_barangays --province bulacan --city BUSTOS
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from municipal import geo
from municipal.regions import province_pretty, voter_table
from municipal.text import title


class Command(BaseCommand):
    help = "Locate a city's barangays on OpenStreetMap for the Heat Map."

    def add_arguments(self, parser):
        parser.add_argument('--province', required=True)
        parser.add_argument('--city', required=True, help='municipality exactly as on the roll')
        parser.add_argument('--refresh', action='store_true', help='re-locate barangays that are already stored')

    def handle(self, *args, **o):
        prov, city = o['province'].lower(), o['city'].strip()
        table = voter_table(prov)
        if not table:
            raise CommandError(f'Unknown province slug: {prov}')
        scope = {'province': prov, 'municipality': city, 'table': table}
        with connections['rds'].cursor() as cur:
            cur.execute(f'SELECT DISTINCT barangay FROM {table} WHERE municipality = %s', [city])
            barangays = sorted({title(r[0]) for r in cur.fetchall() if r[0]})
        if not barangays:
            raise CommandError(f'No barangays found for {city} — check the city name.')
        geo.ensure_table()
        if o['refresh']:
            with connections['ext'].cursor() as cur:
                cur.execute(f"DELETE FROM {geo.TABLE} WHERE province_slug = %s AND municipality = %s AND barangay <> ''",
                            [prov, city])
        self.stdout.write(f'Locating {len(barangays)} barangays of {geo.city_label(scope)}, {province_pretty(prov)} '
                          '(about 2 seconds each)...')
        found, approx, _ = geo.locate(scope, barangays, province_pretty(prov), log=self.stdout.write)
        self.stdout.write(self.style.SUCCESS(f'Done: {found} located on OpenStreetMap, {approx} placed near the town centre '
                                             f'(approximate), {len(barangays) - found - approx} already stored.'))
