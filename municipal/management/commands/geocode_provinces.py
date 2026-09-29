"""
Locate the provinces (province centres) for the Nationwide Heat Map (OpenStreetMap Nominatim,
province names only, 1 request per second) and store them in generic_360_db.muni_barangay_geo
with municipality = '' and barangay = ''. Already-located provinces are skipped, so it is safe
to re-run; --refresh re-locates them.

    python manage.py geocode_provinces
"""
from django.core.management.base import BaseCommand
from django.db import connections

from municipal import geo
from municipal.regions import REGION_MAP, province_pretty


class Command(BaseCommand):
    help = 'Locate the provinces on OpenStreetMap for the Nationwide Heat Map.'

    def add_arguments(self, parser):
        parser.add_argument('--refresh', action='store_true', help='re-locate provinces that are already stored')

    def handle(self, *args, **o):
        geo.ensure_table()
        if o['refresh']:
            with connections['ext'].cursor() as cur:
                cur.execute(f"DELETE FROM {geo.TABLE} WHERE municipality = '' AND barangay = ''")
        provinces = {s: province_pretty(s) for s in REGION_MAP}
        self.stdout.write(f'Locating {len(provinces)} provinces (1–3 seconds each)...')
        found, approx, _ = geo.locate_provinces(provinces, log=self.stdout.write)
        self.stdout.write(self.style.SUCCESS(f'Done: {found} located, {approx} approximate.'))
