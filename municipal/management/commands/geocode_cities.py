"""
Locate a province's cities / municipalities (town centres) for the Province-Wide Heat Map
(OpenStreetMap Nominatim, place names only, 1 request per second) and store them in
generic_360_db.muni_barangay_geo with barangay = ''. Already-located towns are skipped, so it
is safe to re-run; --refresh re-locates them.

    python manage.py geocode_cities --province bulacan
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from municipal import geo, rollsummary
from municipal.regions import province_pretty, voter_table


class Command(BaseCommand):
    help = "Locate a province's cities / municipalities on OpenStreetMap for the Province-Wide Heat Map."

    def add_arguments(self, parser):
        parser.add_argument('--province', required=True)
        parser.add_argument('--refresh', action='store_true', help='re-locate towns that are already stored')

    def handle(self, *args, **o):
        prov = o['province'].lower()
        if not voter_table(prov):
            raise CommandError(f'Unknown province slug: {prov}')
        towns = sorted(rollsummary.municipalities(prov))
        if not towns:
            raise CommandError(f'No roll summary for {prov} yet — run build_roll_summary --province {prov} first.')
        geo.ensure_table()
        if o['refresh']:
            with connections['ext'].cursor() as cur:
                cur.execute(f"DELETE FROM {geo.TABLE} WHERE province_slug = %s AND barangay = '' AND municipality <> ''", [prov])
        self.stdout.write(f'Locating {len(towns)} cities / municipalities of {province_pretty(prov)} (about 2 seconds each)...')
        found, approx, _ = geo.locate_cities(prov, towns, province_pretty(prov), log=self.stdout.write)
        self.stdout.write(self.style.SUCCESS(f'Done: {found} located, {approx} placed near the province centre (approximate), '
                                             f'{len(towns) - found - approx} already stored.'))
