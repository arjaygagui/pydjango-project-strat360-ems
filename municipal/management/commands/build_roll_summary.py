"""
Pre-compute voter-roll totals per municipality / barangay for the Province-Wide EMS
(generic_360_db.muni_roll_summary). Reads cvl_national only; re-run after the roll changes.

    python manage.py build_roll_summary --province bulacan
    python manage.py build_roll_summary --all              # every province, one at a time
    python manage.py build_roll_summary --all --missing    # only provinces not built yet
"""
from django.core.management.base import BaseCommand, CommandError

from municipal import rollsummary
from municipal.regions import REGION_MAP


class Command(BaseCommand):
    help = 'Build the province/municipality/barangay voter totals used by the Province-Wide EMS.'

    def add_arguments(self, parser):
        parser.add_argument('--province', help='province slug, e.g. bulacan')
        parser.add_argument('--all', action='store_true', help='every province')
        parser.add_argument('--missing', action='store_true', help='with --all: skip provinces already built')

    def handle(self, *args, **o):
        if o['province']:
            slugs = [o['province'].lower()]
        elif o['all']:
            slugs = sorted(REGION_MAP)
        else:
            raise CommandError('Give --province <slug> or --all.')
        total = 0
        for i, slug in enumerate(slugs, 1):
            if o['missing'] and rollsummary.built_at(slug):
                continue
            rows, voters, secs = rollsummary.build(slug)
            total += voters
            self.stdout.write(f'[{i}/{len(slugs)}] {slug}: {voters:,} voters, {rows:,} municipality/barangay rows ({secs:.1f} s)')
        self.stdout.write(self.style.SUCCESS(f'Done — {total:,} voters summarized.'))
