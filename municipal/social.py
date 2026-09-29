"""
Social services for the city/municipal EMS — Caloocan-EMS format, full application form.

Table: generic_360_db.muni_social_services (created/extended by `manage.py social_setup`).
Core columns mirror caloocan_ems_db.social_services (voter_id, assistance_type, claimant,
beneficiary, barangay, amount, status, created_at). On top of that:
  * multi-province scoping: province_slug + municipality, created_by, is_mock
  * application form: beneficiary details (birthdate, gender, civil status, contact),
    address snapshot (region, province, city, purok, street), request details
    (date requested, purpose, agency, program, remarks) and claimant details
    (relationship, contact).

Every record/status change also writes an ems_voter_audit row on the beneficiary,
worded like Caloocan's activity log ("Recorded social service: <type> (PHP n)"),
so it shows in the profile's Activity card.

Writes go only through the 'ext' connection (generic_360_db).
"""
import datetime
import re

from django.db import connections, transaction

from .machinery import EXT, _audit, _rows

TABLE = 'muni_social_services'
PAGE_SIZE = 25
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Caloocan's assistance types, with the amount ranges its data uses (PHP).
ASSISTANCE_TYPES = {
    'Medical Assistance': (3100, 20000),
    'Financial Assistance': (1700, 8000),
    'Livelihood Assistance': (7100, 28700),
    'Educational Assistance': (2900, 7500),
    'Burial Assistance': (6200, 11300),
    'Senior Citizen Support': (3500, 4700),
    'Food Pack / Relief': (600, 2600),
}
STATUSES = ('Pending', 'Approved', 'Released', 'Rejected')
DEFAULT_STATUS = 'Released'   # Caloocan's column default
GENDERS = ('Male', 'Female')
CIVIL_STATUSES = ('Single', 'Married', 'Widowed', 'Separated', 'Live-in')
# Suggestions only — the form accepts any value.
AGENCIES = ('DSWD', 'CSWD', 'MSWD', 'PSWD', 'DOH', 'PCSO', 'CHED', 'TESDA', 'DOLE', 'LGU', "Mayor's Office")
PROGRAMS = ('AICS', 'Tulong Dunong', 'Indigent', 'Sariling Sikap', 'Senior Citizen Pension',
            'Burial Assistance Program', 'Medical Assistance Program', 'Livelihood Program')
RELATIONSHIPS = ('Spouse', 'Son', 'Daughter', 'Father', 'Mother', 'Brother', 'Sister',
                 'Grandchild', 'Relative', 'Guardian', 'Other')

BASE_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  id bigint NOT NULL AUTO_INCREMENT,
  province_slug varchar(32) NOT NULL,
  municipality varchar(120) NOT NULL,
  voter_id bigint NOT NULL,
  assistance_type varchar(120) DEFAULT NULL,
  claimant varchar(255) DEFAULT NULL,
  beneficiary varchar(255) DEFAULT NULL,
  barangay varchar(120) DEFAULT NULL,
  amount decimal(12,2) DEFAULT NULL,
  status varchar(20) NOT NULL DEFAULT 'Released',
  created_at datetime DEFAULT NULL,
  created_by varchar(96) DEFAULT NULL,
  is_mock tinyint(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  KEY k_voter (province_slug, voter_id),
  KEY k_city (province_slug, municipality, created_at),
  KEY k_mock (is_mock)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Application-form columns, added to existing tables by ensure_table() (in this order).
FORM_COLUMNS = [
    # beneficiary details
    ('birthdate', 'date DEFAULT NULL'),
    ('gender', 'varchar(10) DEFAULT NULL'),
    ('civil_status', 'varchar(20) DEFAULT NULL'),
    ('contact_number', 'varchar(32) DEFAULT NULL'),
    # address snapshot
    ('region', 'varchar(96) DEFAULT NULL'),
    ('province', 'varchar(96) DEFAULT NULL'),
    ('city_municipality', 'varchar(96) DEFAULT NULL'),
    ('purok', 'varchar(96) DEFAULT NULL'),
    ('street', 'varchar(191) DEFAULT NULL'),
    # request details
    ('date_requested', 'date DEFAULT NULL'),
    ('purpose', 'text'),
    ('agency', 'varchar(64) DEFAULT NULL'),
    ('program', 'varchar(64) DEFAULT NULL'),
    ('remarks', 'text'),
    # claimant / representative
    ('claimant_relationship', 'varchar(64) DEFAULT NULL'),
    ('claimant_contact', 'varchar(32) DEFAULT NULL'),
]

# Effective request date for sorting/filtering (older rows may lack date_requested).
REQ_DATE = 'COALESCE(date_requested, DATE(created_at))'


class SocialError(Exception):
    """A validation problem to show the user."""


def peso(amount):
    return f'PHP {float(amount or 0):,.2f}'


def record_description(assistance_type, amount):
    """Caloocan's activity-log wording."""
    return f'Recorded social service: {assistance_type}' + (f' ({peso(amount)})' if amount else '')


def table_exists():
    with connections[EXT].cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM information_schema.tables '
                    'WHERE table_schema = DATABASE() AND table_name = %s', [TABLE])
        return cur.fetchone()[0] > 0


def ensure_table():
    """Create the table if needed and add any missing application-form columns.

    Returns the list of columns added (empty when already up to date).
    """
    added = []
    with connections[EXT].cursor() as cur:
        cur.execute(BASE_DDL)
        cur.execute('SELECT column_name FROM information_schema.columns '
                    'WHERE table_schema = DATABASE() AND table_name = %s', [TABLE])
        have = {r[0].lower() for r in cur.fetchall()}
        missing = [(n, d) for n, d in FORM_COLUMNS if n not in have]
        if missing:
            cur.execute(f'ALTER TABLE {TABLE} ' + ', '.join(f'ADD COLUMN {n} {d}' for n, d in missing))
            added = [n for n, _ in missing]
        cur.execute("SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() "
                    "AND table_name = %s AND index_name = 'k_requested'", [TABLE])
        if not cur.fetchone()[0]:
            cur.execute(f'ALTER TABLE {TABLE} ADD KEY k_requested (province_slug, municipality, date_requested)')
    return added


def _valid_date(value):
    if not value or not _DATE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def age_on(birthdate, on=None):
    if not birthdate:
        return None
    on = on or datetime.date.today()
    return on.year - birthdate.year - ((on.month, on.day) < (birthdate.month, birthdate.day))


# ---------------------------------------------------------------------------
# Reads (scoped to the chosen city — or the whole province when the scope has
# no municipality, for the Province-Wide EMS)
# ---------------------------------------------------------------------------
def _area(scope):
    """WHERE fragment + params for the scope: province (plus the city when there is one),
    a list of provinces (a region, Nationwide EMS), or everything (the country)."""
    if scope.get('province'):
        sql, params = 'province_slug = %s', [scope['province']]
        if scope.get('municipality'):
            sql += ' AND municipality = %s'
            params.append(scope['municipality'])
        return sql, params
    if scope.get('provinces') is not None:
        slugs = list(scope['provinces'])
        return (f'province_slug IN ({",".join(["%s"] * len(slugs))})', slugs) if slugs else ('0', [])
    return '1', []


def record_scope(record_id):
    """(province_slug, municipality) of a record — the national page acts in the record's own city."""
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT province_slug, municipality FROM {TABLE} WHERE id = %s', [int(record_id)])
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _rate(released, requested):
    """STRAT360 release rate: ₱ released / ₱ requested."""
    return float(released) / float(requested) * 100 if requested else 0


def city_summary(scope):
    area, params = _area(scope)
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT assistance_type, status, COUNT(*), COALESCE(SUM(amount), 0) FROM {TABLE} '
                    f'WHERE {area} GROUP BY assistance_type, status', params)
        rows = cur.fetchall()
        cur.execute(f'SELECT COUNT(DISTINCT voter_id), SUM(is_mock) FROM {TABLE} WHERE {area}', params)
        beneficiaries, mock = cur.fetchone()
    by_status = {s: 0 for s in STATUSES}
    by_type = {}
    released_amount = requested_amount = 0
    total = 0
    for atype, status, n, amt in rows:
        total += n
        requested_amount += amt
        by_status[status] = by_status.get(status, 0) + n
        t = by_type.setdefault(atype or 'Unspecified', {'count': 0, 'amount': 0, 'requested': 0})
        t['count'] += n
        t['requested'] += amt
        if status == 'Released':
            t['amount'] += amt
            released_amount += amt
    return {
        'total': total,
        'beneficiaries': beneficiaries or 0,
        'released_amount': released_amount,
        'requested_amount': requested_amount,
        'pending_amount': requested_amount - released_amount,
        'avg_amount': requested_amount / total if total else 0,
        'release_rate': _rate(released_amount, requested_amount),
        'by_status': by_status,
        'by_type': sorted(by_type.items(), key=lambda kv: -kv[1]['count']),
        'mock': int(mock or 0),
    }


def area_breakdown(scope):
    """Rankings with a drill-down, as in the PHP pages: a city ranks its barangays (drilling into
    puroks); a province ranks its cities (drilling into barangays).

    [{'key', 'requests', 'requested', 'released', 'release_rate', 'children': [{'name', 'requests', 'share'}]}]
    """
    area, params = _area(scope)
    outer, inner = ('barangay', 'purok') if scope.get('municipality') else \
        ('municipality', 'barangay') if scope.get('province') else ('province_slug', 'municipality')
    with connections[EXT].cursor() as cur:
        cur.execute(f"SELECT {outer}, {inner}, COUNT(*), COALESCE(SUM(amount), 0), "
                    f"COALESCE(SUM(CASE WHEN status = 'Released' THEN amount END), 0) "
                    f'FROM {TABLE} WHERE {area} GROUP BY {outer}, {inner}', params)
        rows = cur.fetchall()
    out = {}
    for key, child, n, requested, released in rows:
        a = out.setdefault(key or '', {'key': key or '', 'requests': 0, 'requested': 0, 'released': 0, 'children': {}})
        a['requests'] += n
        a['requested'] += requested
        a['released'] += released
        name = (child or '').strip() or ('No purok' if inner == 'purok' else 'Unspecified')
        a['children'][name] = a['children'].get(name, 0) + n
    ranking = sorted(out.values(), key=lambda a: (-a['requests'], -a['requested']))
    for a in ranking:
        a['release_rate'] = _rate(a['released'], a['requested'])
        a['children'] = sorted(({'name': k, 'requests': n, 'share': n / a['requests'] * 100}
                                for k, n in a['children'].items()), key=lambda c: -c['requests'])
    return ranking


def city_barangays(scope, city=''):
    """Barangays that have records (for the directory filter)."""
    area, params = _area(scope)
    if city and not scope.get('municipality'):
        area += ' AND municipality = %s'
        params.append(city)
    with connections[EXT].cursor() as cur:
        cur.execute(f"SELECT DISTINCT barangay FROM {TABLE} WHERE {area} AND barangay IS NOT NULL AND barangay <> '' "
                    'ORDER BY barangay', params)
        return [r[0] for r in cur.fetchall()]


def record_city(province, record_id):
    """The municipality a record belongs to (so the Province-Wide page can act in that city's scope)."""
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT municipality FROM {TABLE} WHERE id = %s AND province_slug = %s', [int(record_id), province])
        row = cur.fetchone()
    return row[0] if row else None


def city_records(scope, search='', atype='', status='', date_from='', date_to='', page=1, city='', barangay=''):
    """One page of records, newest request first. `city` narrows a province-wide list."""
    area, params = _area(scope)
    where = [area]
    if city and not scope.get('municipality'):
        where.append('municipality = %s')
        params.append(city)
    if barangay:
        where.append('barangay = %s')
        params.append(barangay)
    if search:
        where.append('(beneficiary LIKE %s OR claimant LIKE %s OR id = %s)')
        params += [f'%{search}%', f'%{search}%', int(search) if search.isdigit() else 0]
    if atype:
        where.append('assistance_type = %s')
        params.append(atype)
    if status in STATUSES:
        where.append('status = %s')
        params.append(status)
    if _valid_date(date_from):
        where.append(f'{REQ_DATE} >= %s')
        params.append(date_from)
    if _valid_date(date_to):
        where.append(f'{REQ_DATE} <= %s')
        params.append(date_to)
    wsql = ' AND '.join(where)
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM {TABLE} WHERE {wsql}', params)
        total, total_amount = cur.fetchone()
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(1, page), pages)
        cur.execute(
            f'SELECT *, {REQ_DATE} AS req_date FROM {TABLE} WHERE {wsql} '
            f'ORDER BY req_date DESC, created_at DESC, id DESC '
            f'LIMIT {PAGE_SIZE} OFFSET {(page - 1) * PAGE_SIZE}',
            params,
        )
        rows = _rows(cur)
    return rows, total, total_amount, page, pages


def for_voter(province, vid):
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT *, {REQ_DATE} AS req_date FROM {TABLE} '
                    'WHERE province_slug = %s AND voter_id = %s '
                    'ORDER BY req_date DESC, created_at DESC, id DESC LIMIT 100', [province, int(vid)])
        return _rows(cur)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _clean(data):
    """Validate the application form. Returns a dict of column -> value."""
    def text(key, n):
        return (data.get(key) or '').strip()[:n] or None

    atype = (data.get('assistance_type') or '').strip()
    if atype not in ASSISTANCE_TYPES:
        raise SocialError('Choose the type of assistance.')
    try:
        amount = round(float(data.get('amount') or 0), 2)
    except ValueError:
        raise SocialError('Enter a valid amount.')
    if amount < 0 or amount > 10_000_000:
        raise SocialError('Enter a valid amount.')
    status = (data.get('status') or DEFAULT_STATUS).strip()
    if status not in STATUSES:
        raise SocialError('Choose a valid status.')

    date_requested = (data.get('date_requested') or '').strip()
    if not _valid_date(date_requested):
        raise SocialError('Enter the date requested.')
    if datetime.date.fromisoformat(date_requested) > datetime.date.today():
        raise SocialError('The date requested cannot be in the future.')

    birthdate = (data.get('birthdate') or '').strip()
    if birthdate:
        if not _valid_date(birthdate):
            raise SocialError('Enter a valid birthdate, or leave it blank.')
        bd = datetime.date.fromisoformat(birthdate)
        if bd > datetime.date.today() or bd.year < 1900:
            raise SocialError('Enter a valid birthdate, or leave it blank.')

    gender = (data.get('gender') or '').strip()
    if gender and gender not in GENDERS:
        raise SocialError('Choose a valid gender.')
    civil = (data.get('civil_status') or '').strip()
    if civil and civil not in CIVIL_STATUSES:
        raise SocialError('Choose a valid civil status.')

    by_other = (data.get('claimed_by_other') or '') in ('1', 'on', 'true')
    claimant = text('claimant', 255) if by_other else None
    if by_other and not claimant:
        raise SocialError("Enter the claimant's name, or untick “claimed by someone else”.")

    return {
        'assistance_type': atype, 'amount': amount, 'status': status,
        'date_requested': date_requested, 'birthdate': birthdate or None,
        'gender': gender or None, 'civil_status': civil or None,
        'contact_number': text('contact_number', 32),
        'region': text('region', 96), 'province': text('province', 96),
        'city_municipality': text('city_municipality', 96),
        'barangay_text': text('barangay', 120),
        'purok': text('purok', 96), 'street': text('street', 191),
        'purpose': (data.get('purpose') or '').strip() or None,
        'agency': text('agency', 64), 'program': text('program', 64),
        'remarks': (data.get('remarks') or '').strip() or None,
        'claimant': claimant,
        'claimant_relationship': text('claimant_relationship', 64) if by_other else None,
        'claimant_contact': text('claimant_contact', 32) if by_other else None,
        'beneficiary': text('beneficiary', 255),
    }


def record(scope, voter, data, actor, ip):
    """Record a social-service application for `voter` (a machinery.voter_brief in scope)."""
    f = _clean(data)
    beneficiary = f['beneficiary'] or voter['name']
    claimant = f['claimant'] or beneficiary          # Caloocan: claimant = beneficiary by default

    cols = ['province_slug', 'municipality', 'voter_id', 'assistance_type', 'claimant', 'beneficiary',
            'barangay', 'amount', 'status', 'created_by', 'is_mock',
            'birthdate', 'gender', 'civil_status', 'contact_number',
            'region', 'province', 'city_municipality', 'purok', 'street',
            'date_requested', 'purpose', 'agency', 'program', 'remarks',
            'claimant_relationship', 'claimant_contact']
    vals = [scope['province'], scope['municipality'], int(voter['id']), f['assistance_type'], claimant,
            beneficiary, f['barangay_text'] or voter['barangay_pretty'] or None, f['amount'], f['status'],
            actor, 0,
            f['birthdate'], f['gender'], f['civil_status'], f['contact_number'],
            f['region'], f['province'], f['city_municipality'], f['purok'], f['street'],
            f['date_requested'], f['purpose'], f['agency'], f['program'], f['remarks'],
            f['claimant_relationship'], f['claimant_contact']]

    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        cur.execute(
            f'INSERT INTO {TABLE} ({", ".join(cols)}, created_at) '
            f'VALUES ({", ".join(["%s"] * len(cols))}, NOW())', vals)
        new_id = cur.lastrowid
        _audit(cur, scope['province'], voter['id'], 'social.record',
               record_description(f['assistance_type'], f['amount']),
               {'muni_social_id': new_id, 'type': f['assistance_type'], 'amount': f['amount'],
                'status': f['status'], 'date_requested': f['date_requested']},
               actor, ip)
    return new_id


def set_status(scope, record_id, status, actor, ip):
    """Move a record in this city to another status."""
    if status not in STATUSES:
        raise SocialError('Choose a valid status.')
    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        cur.execute(f'SELECT id, voter_id, status, assistance_type FROM {TABLE} '
                    'WHERE id = %s AND province_slug = %s AND municipality = %s FOR UPDATE',
                    [int(record_id), scope['province'], scope['municipality']])
        rows = _rows(cur)
        if not rows:
            raise SocialError('That record was not found in this city.')
        row = rows[0]
        if row['status'] == status:
            return False
        cur.execute(f'UPDATE {TABLE} SET status = %s WHERE id = %s', [status, int(record_id)])
        _audit(cur, scope['province'], row['voter_id'], 'social.status',
               f'Social service #{record_id} ({row["assistance_type"]}) marked {status} '
               f'(was {row["status"]})',
               {'muni_social_id': int(record_id), 'from': row['status'], 'to': status},
               actor, ip)
    return True
