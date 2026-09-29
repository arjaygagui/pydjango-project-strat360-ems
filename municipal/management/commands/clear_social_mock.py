"""
Remove MOCK social-service records (is_mock = 1) and the activity entries written
for them. Real records are never touched.

    python manage.py clear_social_mock                          # all mock rows
    python manage.py clear_social_mock --province bulacan --city BUSTOS
"""
from django.core.management.base import BaseCommand
from django.db import connections, transaction

from municipal import social


class Command(BaseCommand):
    help = 'Delete mock (is_mock=1) social-service records and their activity entries.'

    def add_arguments(self, parser):
        parser.add_argument('--province', default='', help='limit to one province slug')
        parser.add_argument('--city', default='', help='limit to one municipality (as on the roll)')

    def handle(self, *args, **o):
        if not social.table_exists():
            self.stdout.write('No muni_social_services table — nothing to clear.')
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
            cur.execute(f'SELECT id FROM {social.TABLE} WHERE {wsql}', params)
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                self.stdout.write('No mock records found.')
                return
            audit_removed = 0
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                ph = ','.join(['%s'] * len(chunk))
                cur.execute(
                    "DELETE FROM ems_voter_audit WHERE action IN ('social.record', 'social.status') "
                    "AND CAST(JSON_UNQUOTE(JSON_EXTRACT(meta, '$.muni_social_id')) AS UNSIGNED) "
                    f'IN ({ph})', chunk)
                audit_removed += cur.rowcount
                cur.execute(f'DELETE FROM {social.TABLE} WHERE is_mock = 1 AND id IN ({ph})', chunk)

        self.stdout.write(self.style.SUCCESS(
            f'Removed {len(ids)} mock social-service records and {audit_removed} activity entries.'))
