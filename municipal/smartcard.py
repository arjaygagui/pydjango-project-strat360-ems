"""
Smart cards for the city/municipal EMS — CALOOCAN-EMS format.

Table: generic_360_db.muni_smart_cards (created by `manage.py smartcard_setup`).
Columns mirror caloocan_ems_db.smart_cards (voter_id, card_number, status, civil_status,
gender, service, barangay, issued_date, created_at), plus province_slug + municipality
(whose card it is), created_by and is_mock (seeded demo rows).

  * one card per voter (UNIQUE province_slug + voter_id); card numbers are unique
  * card number format as Caloocan: SC-<yy>-<6-digit serial>, e.g. SC-26-000123
  * status: active | pending (Caloocan) | revoked (added so a card can be withdrawn)
  * issuing / status changes write an ems_voter_audit row on the cardholder

Writes go only through the 'ext' connection (generic_360_db).
"""
import datetime
import re

from django.db import IntegrityError, connections, transaction

from .machinery import EXT, _audit, _rows, voter_table_qualified
from .text import title

TABLE = 'muni_smart_cards'
PAGE_SIZE = 25
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Caloocan's card services.
SERVICES = ('Educational Support', 'PWD Benefits', 'Food Pack / Relief', 'Senior Discount',
            'Medical Assistance', 'Transport Subsidy')
STATUSES = ('active', 'pending', 'revoked')
STATUS_LABELS = {'active': 'Active', 'pending': 'Pending', 'revoked': 'Revoked'}
GENDERS = {'M': 'Male', 'F': 'Female'}
CIVIL_STATUSES = ('Single', 'Married', 'Widowed', 'Separated')
HOLDING = ('active', 'pending')          # counted as cardholders (revoked is not)

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  id int NOT NULL AUTO_INCREMENT,
  province_slug varchar(32) NOT NULL,
  municipality varchar(120) NOT NULL,
  voter_id int NOT NULL,
  card_number varchar(32) NOT NULL,
  status varchar(12) NOT NULL DEFAULT 'active',
  civil_status varchar(16) DEFAULT NULL,
  gender char(1) DEFAULT NULL,
  service varchar(48) DEFAULT NULL,
  barangay varchar(160) DEFAULT NULL,
  issued_date date DEFAULT NULL,
  created_at datetime DEFAULT NULL,
  created_by varchar(96) DEFAULT NULL,
  is_mock tinyint(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uq_voter (province_slug, voter_id),
  UNIQUE KEY uq_card (card_number),
  KEY k_city (province_slug, municipality, barangay),
  KEY k_status (status),
  KEY k_mock (is_mock)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


class CardError(Exception):
    """A validation problem to show the user."""


def table_exists():
    with connections[EXT].cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM information_schema.tables '
                    'WHERE table_schema = DATABASE() AND table_name = %s', [TABLE])
        return cur.fetchone()[0] > 0


def ensure_table():
    with connections[EXT].cursor() as cur:
        cur.execute(DDL)


def _valid_date(value):
    if not value or not _DATE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def next_serial(cur):
    cur.execute(f"SELECT COALESCE(MAX(CAST(SUBSTRING_INDEX(card_number, '-', -1) AS UNSIGNED)), 0) FROM {TABLE}")
    return int(cur.fetchone()[0]) + 1


def card_number(issued, serial):
    return f'SC-{issued.year % 100:02d}-{serial:06d}'


def _decorate(row):
    row['status_label'] = STATUS_LABELS.get(row['status'], row['status'])
    row['gender_label'] = GENDERS.get(row['gender'] or '', '')
    return row


# ---------------------------------------------------------------------------
# Reads (scoped to the chosen city — or the whole province when the scope has
# no municipality, for the Province-Wide EMS; or several provinces / the whole
# country when it has 'provinces' / nothing, for the Nationwide EMS)
# ---------------------------------------------------------------------------
def _area(scope, alias=''):
    """WHERE fragment + params for the scope: province (plus the city when there is one),
    a list of provinces (a region), or everything (the country)."""
    if scope.get('province'):
        sql, params = f'{alias}province_slug = %s', [scope['province']]
        if scope.get('municipality'):
            sql += f' AND {alias}municipality = %s'
            params.append(scope['municipality'])
        return sql, params
    if scope.get('provinces') is not None:
        slugs = list(scope['provinces'])
        return (f'{alias}province_slug IN ({",".join(["%s"] * len(slugs))})', slugs) if slugs else ('0', [])
    return '1', []


def province_counts(scope):
    """National: {province_slug: {'holders', 'active', 'pending', 'revoked', 'cities': {municipality_raw: holders}}}."""
    area, params = _area(scope)
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT province_slug, municipality, status, COUNT(*) FROM {TABLE} WHERE {area} GROUP BY 1, 2, 3', params)
        rows = cur.fetchall()
    out = {}
    for slug, muni, status, n in rows:
        p = out.setdefault(slug, {'holders': 0, 'active': 0, 'pending': 0, 'revoked': 0, 'cities': {}})
        p[status] = p.get(status, 0) + n
        if status in HOLDING:
            p['holders'] += n
            p['cities'][muni] = p['cities'].get(muni, 0) + n
    return out


def national_cards(scope, search='', status='', gender='', service='', page=1):
    """National directory page (newest first). Names come from each card's own province roll via
    voter_briefs — there is no single table to join across 84 provinces — so search is by card number."""
    from .machinery import voter_briefs
    area, params = _area(scope)
    where = [area]
    if search:
        where.append('card_number LIKE %s')
        params.append(f'%{search}%')
    if status in STATUSES:
        where.append('status = %s')
        params.append(status)
    if gender in GENDERS:
        where.append('gender = %s')
        params.append(gender)
    if service in SERVICES:
        where.append('service = %s')
        params.append(service)
    wsql = ' AND '.join(where)
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {TABLE} WHERE {wsql}', params)
        total = cur.fetchone()[0]
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(1, page), pages)
        cur.execute(f'SELECT * FROM {TABLE} WHERE {wsql} ORDER BY issued_date DESC, id DESC '
                    f'LIMIT {PAGE_SIZE} OFFSET {(page - 1) * PAGE_SIZE}', params)
        rows = _rows(cur)
    briefs = voter_briefs([(r['province_slug'], r['voter_id']) for r in rows])
    for r in rows:
        b = briefs.get((r['province_slug'], r['voter_id']))
        r['name'] = b['name'] if b else f'Voter #{r["voter_id"]}'
        r['city'] = title(r['municipality'])
        _decorate(r)
    return rows, total, page, pages


def card_scope(card_id):
    """(province_slug, municipality) of a card — the national page acts in the card's own city."""
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT province_slug, municipality FROM {TABLE} WHERE id = %s', [int(card_id)])
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def card_for(province, vid):
    if not table_exists():
        return None
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT * FROM {TABLE} WHERE province_slug = %s AND voter_id = %s',
                    [province, int(vid)])
        rows = _rows(cur)
    return _decorate(rows[0]) if rows else None


def city_summary(scope):
    area, params = _area(scope)
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT status, service, gender, COUNT(*) FROM {TABLE} '
                    f'WHERE {area} GROUP BY status, service, gender', params)
        rows = cur.fetchall()
        cur.execute(f'SELECT SUM(is_mock) FROM {TABLE} WHERE {area}', params)
        mock = cur.fetchone()[0]
    by_status = {s: 0 for s in STATUSES}
    by_service, by_gender = {}, {'M': 0, 'F': 0}
    for status, service, gender, n in rows:
        by_status[status] = by_status.get(status, 0) + n
        if status in HOLDING:
            by_service[service or 'Unspecified'] = by_service.get(service or 'Unspecified', 0) + n
            if gender in by_gender:
                by_gender[gender] += n
    holders = sum(by_status.get(s, 0) for s in HOLDING)
    return {
        'holders': holders,
        'by_status': by_status,
        'active_pct': (by_status['active'] / holders * 100) if holders else 0,
        'by_service': sorted(by_service.items(), key=lambda kv: -kv[1]),
        'services_used': len(by_service),
        'by_gender': by_gender,
        'mock': int(mock or 0),
    }


def barangay_counts(scope):
    """{barangay_pretty: {'holders', 'active', 'pending', 'revoked'}} for the city."""
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT barangay, status, COUNT(*) FROM {TABLE} '
                    'WHERE province_slug = %s AND municipality = %s GROUP BY barangay, status',
                    [scope['province'], scope['municipality']])
        rows = cur.fetchall()
    out = {}
    for brgy, status, n in rows:
        b = out.setdefault(brgy or 'Unspecified', {'holders': 0, 'active': 0, 'pending': 0, 'revoked': 0})
        b[status] = b.get(status, 0) + n
        if status in HOLDING:
            b['holders'] += n
    return out


def municipality_counts(province):
    """{municipality_raw: {'holders', 'active', 'pending', 'revoked', 'barangays': {pretty: holders}}}
    for the whole province (the Province-Wide ranking and its barangay drill-down)."""
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT municipality, barangay, status, COUNT(*) FROM {TABLE} '
                    'WHERE province_slug = %s GROUP BY municipality, barangay, status', [province])
        rows = cur.fetchall()
    out = {}
    for muni, brgy, status, n in rows:
        m = out.setdefault(muni, {'holders': 0, 'active': 0, 'pending': 0, 'revoked': 0, 'barangays': {}})
        m[status] = m.get(status, 0) + n
        if status in HOLDING:
            m['holders'] += n
            key = brgy or 'Unspecified'
            m['barangays'][key] = m['barangays'].get(key, 0) + n
    return out


def city_cards(scope, search='', barangay='', status='', gender='', service='', page=1, city=''):
    """One page of the city's (or province's) cards, newest issue first, with the cardholder's name.
    `city` narrows a province-wide list to one municipality.

    Names live on the voter roll, so the list joins cvl_national (read-only) on 'ext'.
    """
    area, params = _area(scope, 'c.')
    where = [area]
    if city and not scope.get('municipality'):
        where.append('c.municipality = %s')
        params.append(city)
    if search:
        where.append('(c.card_number LIKE %s OR v.fullname LIKE %s)')
        params += [f'%{search}%', f'{search}%']
    if barangay:
        where.append('c.barangay = %s')
        params.append(barangay)
    if status in STATUSES:
        where.append('c.status = %s')
        params.append(status)
    if gender in GENDERS:
        where.append('c.gender = %s')
        params.append(gender)
    if service in SERVICES:
        where.append('c.service = %s')
        params.append(service)
    wsql = ' AND '.join(where)
    joined = f'{TABLE} c JOIN {voter_table_qualified(scope)} v ON v.id = c.voter_id'
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {joined} WHERE {wsql}', params)
        total = cur.fetchone()[0]
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(1, page), pages)
        cur.execute(f'SELECT c.*, v.fullname FROM {joined} WHERE {wsql} ORDER BY c.issued_date DESC, c.id DESC '
                    f'LIMIT {PAGE_SIZE} OFFSET {(page - 1) * PAGE_SIZE}', params)
        rows = []
        for r in _rows(cur):
            r['name'] = title(r.pop('fullname'))
            r['city'] = title(r['municipality'])
            rows.append(_decorate(r))
    return rows, total, page, pages


def holders_among(scope, voter_ids):
    """{voter_id: card row} for the given ids (used to badge voters-list rows)."""
    ids = [int(i) for i in voter_ids]
    if not ids or not table_exists():
        return {}
    ph = ','.join(['%s'] * len(ids))
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT voter_id, card_number, status FROM {TABLE} '
                    f'WHERE province_slug = %s AND voter_id IN ({ph})', [scope['province'], *ids])
        return {r['voter_id']: _decorate({**r, 'gender': None}) for r in _rows(cur)}


def city_barangays(scope):
    area, params = _area(scope)
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT DISTINCT barangay FROM {TABLE} WHERE {area} '
                    'AND barangay IS NOT NULL ORDER BY barangay', params)
        return [r[0] for r in cur.fetchall()]


def card_city(province, card_id):
    """The municipality a card belongs to (so the Province-Wide page can act in that city's scope)."""
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT municipality FROM {TABLE} WHERE id = %s AND province_slug = %s', [int(card_id), province])
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def issue(scope, voter, data, actor, ip):
    """Issue a card to `voter` (a machinery.voter_brief in scope). Returns the card number."""
    service = (data.get('service') or '').strip()
    if service not in SERVICES:
        raise CardError('Choose the service the card is for.')
    gender = (data.get('gender') or '').strip()
    if gender and gender not in GENDERS:
        raise CardError('Choose a valid gender.')
    civil = (data.get('civil_status') or '').strip()
    if civil and civil not in CIVIL_STATUSES:
        raise CardError('Choose a valid civil status.')
    status = (data.get('status') or 'active').strip()
    if status not in ('active', 'pending'):
        raise CardError('A new card is either Active or Pending.')
    issued = (data.get('issued_date') or '').strip()
    if not _valid_date(issued):
        raise CardError('Enter the issue date.')
    issued_d = datetime.date.fromisoformat(issued)
    if issued_d > datetime.date.today():
        raise CardError('The issue date cannot be in the future.')

    for _attempt in range(3):   # retry if two cards race for the same serial
        try:
            with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
                cur.execute(f'SELECT card_number, status FROM {TABLE} WHERE province_slug = %s AND voter_id = %s',
                            [scope['province'], int(voter['id'])])
                existing = cur.fetchone()
                if existing:
                    raise CardError(f'This voter already has card {existing[0]} ({STATUS_LABELS.get(existing[1], existing[1])}).')
                number = card_number(issued_d, next_serial(cur))
                cur.execute(
                    f'INSERT INTO {TABLE} (province_slug, municipality, voter_id, card_number, status, '
                    'civil_status, gender, service, barangay, issued_date, created_at, created_by, is_mock) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, 0)',
                    [scope['province'], scope['municipality'], int(voter['id']), number, status,
                     civil or None, gender or None, service, voter['barangay_pretty'] or None, issued, actor])
                _audit(cur, scope['province'], voter['id'], 'card.issue',
                       f'Smart card {number} issued ({service}, {STATUS_LABELS[status]})',
                       {'card_number': number, 'service': service, 'status': status, 'issued_date': issued},
                       actor, ip)
            return number
        except IntegrityError as e:
            if 'uq_voter' in str(e):
                raise CardError('This voter already has a smart card.')
            continue    # uq_card collision: take the next serial
    raise CardError('Could not allocate a card number — please try again.')


def set_status(scope, card_id, status, actor, ip):
    if status not in STATUSES:
        raise CardError('Choose a valid status.')
    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        cur.execute(f'SELECT id, voter_id, card_number, status FROM {TABLE} '
                    'WHERE id = %s AND province_slug = %s AND municipality = %s FOR UPDATE',
                    [int(card_id), scope['province'], scope['municipality']])
        rows = _rows(cur)
        if not rows:
            raise CardError('That card was not found in this city.')
        row = rows[0]
        if row['status'] == status:
            return False
        cur.execute(f'UPDATE {TABLE} SET status = %s WHERE id = %s', [status, int(card_id)])
        verb = {'active': 'activated', 'pending': 'set to Pending', 'revoked': 'revoked'}[status]
        _audit(cur, scope['province'], row['voter_id'], 'card.status',
               f'Smart card {row["card_number"]} {verb} (was {STATUS_LABELS.get(row["status"], row["status"])})',
               {'card_number': row['card_number'], 'from': row['status'], 'to': status}, actor, ip)
    return True
