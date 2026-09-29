"""
Create generic_360_db.muni_voter_details and muni_household_members (Caloocan-format
personal details + household members for the voter profile), or bring older copies up to
date (adds the is_mock flag). Safe to re-run.

    python manage.py household_setup
"""
from django.core.management.base import BaseCommand

from municipal import household


class Command(BaseCommand):
    help = 'Create / upgrade the muni_voter_details + muni_household_members tables in generic_360_db (idempotent).'

    def handle(self, *args, **options):
        changes = household.ensure_tables()
        for line in changes or ['both tables already up to date — nothing changed.']:
            self.stdout.write(self.style.SUCCESS(line))
