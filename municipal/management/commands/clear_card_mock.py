"""
Remove MOCK smart cards (is_mock = 1) — plus any activity entries written later for
those cards (e.g. a status change made in the app). Real cards are never touched.

    python manage.py clear_card_mock
    python manage.py clear_card_mock --province bulacan --city BUSTOS
"""
from django.core.management.base import BaseCommand
from django.db import connections, transaction

from municipal import smartcard


class Command(BaseCommand):
    help = 'Delete mock (is_mock=1) smart cards and their activity entries.'

    def add_arguments(self, parser):
        parser.add_argument('--province', default='')
        parser.add_argument('--city', default='')

    def handle(self, *args, **o):
        if not smartcard.table_exists():
            self.stdout.write('No muni_smart_cards table — nothing to clear.')
            return
        where, params = ['is_mock = 1'], []
        if o['province']:
            where.append('province_slug = %s')
            params.append(o['province'].lower())
        if o['city']:
            where.append('municipality = %s')
            params.append(o['city'].strip())
        wsql = ' AND '.join(where)

        with transaction.atomic(using='ext'), connections['ext'].cursor() as cur:
            cur.execute(f'SELECT id, card_number FROM {smartcard.TABLE} WHERE {wsql}', params)
            cards = cur.fetchall()
            if not cards:
                self.stdout.write('No mock cards found.')
                return
            audit_removed = 0
            for i in range(0, len(cards), 500):
                chunk = cards[i:i + 500]
                ph = ','.join(['%s'] * len(chunk))
                cur.execute(
                    "DELETE FROM ems_voter_audit WHERE action IN ('card.issue', 'card.status') "
                    f"AND JSON_UNQUOTE(JSON_EXTRACT(meta, '$.card_number')) IN ({ph})",
                    [c[1] for c in chunk])
                audit_removed += cur.rowcount
                cur.execute(f'DELETE FROM {smartcard.TABLE} WHERE is_mock = 1 AND id IN ({ph})', [c[0] for c in chunk])

        self.stdout.write(self.style.SUCCESS(
            f'Removed {len(cards)} mock smart cards and {audit_removed} activity entries.'))
