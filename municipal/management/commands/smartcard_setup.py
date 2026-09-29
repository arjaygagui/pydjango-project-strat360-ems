"""
Create generic_360_db.muni_smart_cards (Caloocan-format smart cards for the
city/municipal EMS). Safe to re-run.

    python manage.py smartcard_setup
"""
from django.core.management.base import BaseCommand

from municipal import smartcard


class Command(BaseCommand):
    help = 'Create the muni_smart_cards table in generic_360_db (idempotent).'

    def handle(self, *args, **options):
        existed = smartcard.table_exists()
        smartcard.ensure_table()
        self.stdout.write(self.style.SUCCESS(
            f'{smartcard.TABLE}: ' + ('already existed — left unchanged.' if existed else 'created in generic_360_db.')))
