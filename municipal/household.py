"""
Personal details + household members for the voter profile — CALOOCAN-EMS format.

Tables (generic_360_db, created by `manage.py household_setup`):

  * muni_voter_details — one row per voter, the editable "Personal Details" card.
    Columns mirror caloocan_ems_db.voter_details (home_address, birthdate, gender, mobile,
    email, org, lang, education, religion, occupation, income, status, remarks,
    updated_at/by) plus civil_status (also on smart cards / social applications), keyed
    by (province_slug, voter_id). Caloocan's free-text
    "opposition" field is left out: the Political Position card's Opposition tag is the
    single source for that.

  * muni_household_members — Caloocan's household_members (name, relationship, precinct,
    birthday, contact, scholar) listed under a voter (the household head). A member may
    be a registered voter of the same city (member_voter_id, name/precinct from the roll)
    or someone not on the roll (e.g. a minor), typed in by hand.

Every change writes an ems_voter_audit row on the voter whose profile was edited,
worded like Caloocan's activity log. Writes go only through the 'ext' connection.
"""
import datetime
import re

from django.db import connections, transaction

from .machinery import EXT, _audit, _rows, voter_table_qualified
from .text import title

DETAILS = 'muni_voter_details'
MEMBERS = 'muni_household_members'
_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Choice lists (from the PHP voter-profile form).
GENDERS = ('Male', 'Female')
CIVIL_STATUSES = ('Single', 'Married', 'Widowed', 'Separated', 'Live-in')
EDUCATION = ('Elementary', 'High School', 'Vocational', 'College Graduate', 'Post Graduate')
INCOME = ('Below ₱10,000', '₱10,000 – ₱25,000', '₱25,000 – ₱50,000', 'Above ₱50,000')
VOTER_STATUSES = ('Active', 'Inactive', 'Transferred', 'Deceased')
RELATIONSHIPS = ('Spouse', 'Son', 'Daughter', 'Father', 'Mother', 'Brother', 'Sister',
                 'Grandchild', 'Grandparent', 'Relative', 'Other')
SCHOLAR = ('Government Scholar', 'Private Scholar')

# Personal-details fields: (column, label, max length or type).
DETAIL_FIELDS = [
    ('home_address', 'Home address', 255),
    ('birthdate', 'Birthdate', 'date'),
    ('gender', 'Gender', GENDERS),
    ('civil_status', 'Civil status', CIVIL_STATUSES),
    ('mobile', 'Mobile number', 32),
    ('email', 'Email address', 191),
    ('org', 'Organization', 120),
    ('lang', 'Dialect / language', 120),
    ('education', 'Educational attainment', EDUCATION),
    ('religion', 'Religion', 120),
    ('occupation', 'Occupation', 120),
    ('income', 'Monthly income', INCOME),
    ('status', 'Voter status', VOTER_STATUSES),
    ('remarks', 'Remarks', 2000),
]
DETAIL_LABELS = {f: label for f, label, _ in DETAIL_FIELDS}

# Sectors a voter belongs to (any number). The first six are the PHP dashboard's
# "Sectoral Overview" columns; the rest are PHP ems.php's sector filter, made generic
# (its Zamboanga-specific PGZ / TABAK entries and the "NEGATIVE" tag are left out).
SECTORS_TABLE = 'muni_voter_sectors'
MAIN_SECTORS = (('Senior Citizen', 'Seniors'), ('Single Parent', 'Single Parents'), ('Youth', 'Youths'),
                ('PWD', 'PWD'), ('OFW', 'OFW'), ('TODA', 'TODA'))
SECTORS = tuple(s for s, _ in MAIN_SECTORS) + (
    'Agriculture', 'Fisheries', 'Barangay Official', 'Barangay Health Worker (BHW)', 'Barangay Nutrition Scholar (BNS)',
    'Day Care Worker (DCW)', 'Tanod', 'Lupon', 'Kababaihan', 'Leader / Coordinator', 'Teacher', 'Non-Teaching Personnel',
    'TUPAD', 'SPES')

SECTORS_DDL = f"""
CREATE TABLE IF NOT EXISTS {SECTORS_TABLE} (
  province_slug varchar(32) NOT NULL,
  voter_id int NOT NULL,
  sector varchar(64) NOT NULL,
  municipality varchar(120) NOT NULL,
  created_at datetime DEFAULT NULL,
  created_by varchar(96) DEFAULT NULL,
  is_mock tinyint(1) NOT NULL DEFAULT 0,
  PRIMARY KEY (province_slug, voter_id, sector),
  KEY k_city (province_slug, municipality, sector),
  KEY k_mock (is_mock)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

DETAILS_DDL = f"""
CREATE TABLE IF NOT EXISTS {DETAILS} (
  province_slug varchar(32) NOT NULL,
  voter_id int NOT NULL,
  municipality varchar(120) NOT NULL,
  home_address varchar(255) DEFAULT NULL,
  birthdate date DEFAULT NULL,
  gender varchar(20) DEFAULT NULL,
  civil_status varchar(20) DEFAULT NULL,
  mobile varchar(32) DEFAULT NULL,
  email varchar(191) DEFAULT NULL,
  org varchar(120) DEFAULT NULL,
  lang varchar(120) DEFAULT NULL,
  education varchar(40) DEFAULT NULL,
  religion varchar(120) DEFAULT NULL,
  occupation varchar(120) DEFAULT NULL,
  income varchar(40) DEFAULT NULL,
  status varchar(40) DEFAULT NULL,
  remarks text,
  updated_at datetime DEFAULT NULL,
  updated_by varchar(96) DEFAULT NULL,
  PRIMARY KEY (province_slug, voter_id),
  KEY k_city (province_slug, municipality)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

MEMBERS_DDL = f"""
CREATE TABLE IF NOT EXISTS {MEMBERS} (
  id bigint NOT NULL AUTO_INCREMENT,
  province_slug varchar(32) NOT NULL,
  municipality varchar(120) NOT NULL,
  voter_id int NOT NULL,
  member_voter_id int DEFAULT NULL,
  name varchar(255) NOT NULL,
  relationship varchar(60) DEFAULT NULL,
  precinct varchar(60) DEFAULT NULL,
  birthday date DEFAULT NULL,
  contact varchar(60) DEFAULT NULL,
  scholar varchar(60) DEFAULT NULL,
  created_at datetime DEFAULT NULL,
  created_by varchar(96) DEFAULT NULL,
  PRIMARY KEY (id),
  KEY k_voter (province_slug, voter_id),
  KEY k_member (province_slug, member_voter_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


class HouseholdError(Exception):
    """A validation problem to show the user."""


def _table_exists(name):
    with connections[EXT].cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM information_schema.tables '
                    'WHERE table_schema = DATABASE() AND table_name = %s', [name])
        return cur.fetchone()[0] > 0


def tables_exist():
    return _table_exists(DETAILS) and _table_exists(MEMBERS) and _table_exists(SECTORS_TABLE)


def ensure_tables():
    """Create both tables if needed, and add the is_mock flag (seeded demo rows) to older
    copies. Returns the list of changes made (empty when already up to date)."""
    changes = []
    with connections[EXT].cursor() as cur:
        for name, ddl in ((DETAILS, DETAILS_DDL), (MEMBERS, MEMBERS_DDL), (SECTORS_TABLE, SECTORS_DDL)):
            if not _table_exists(name):
                cur.execute(ddl)
                changes.append(f'{name}: created')
            cur.execute('SELECT COUNT(*) FROM information_schema.columns '
                        'WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s', [name, 'is_mock'])
            if not cur.fetchone()[0]:
                cur.execute(f'ALTER TABLE {name} ADD COLUMN is_mock tinyint(1) NOT NULL DEFAULT 0, ADD KEY k_mock (is_mock)')
                changes.append(f'{name}: added is_mock')
    return changes


def _valid_date(value):
    if not value or not _DATE.match(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Personal details
# ---------------------------------------------------------------------------
def details_for(province, vid):
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT * FROM {DETAILS} WHERE province_slug = %s AND voter_id = %s',
                    [province, int(vid)])
        rows = _rows(cur)
    return rows[0] if rows else None


FLAGGED_STATUSES = ('Inactive', 'Transferred', 'Deceased')     # everyone else counts as Active
AGE_BUCKETS = (('18-25', 18, 25), ('26-35', 26, 35), ('36-45', 36, 45), ('46-55', 46, 55), ('56+', 56, 200))


def _age(bd, today=None):
    today = today or datetime.date.today()
    return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))


def details_among(province, voter_ids):
    """{voter_id: {'gender', 'birthdate', 'age', 'org', 'status', 'religion'}} for a page of voters."""
    ids = [int(i) for i in voter_ids]
    if not ids or not _table_exists(DETAILS):
        return {}
    ph = ','.join(['%s'] * len(ids))
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT voter_id, gender, birthdate, org, status, religion FROM {DETAILS} '
                    f'WHERE province_slug = %s AND voter_id IN ({ph})', [province, *ids])
        rows = _rows(cur)
    today = datetime.date.today()
    return {r['voter_id']: {**r, 'age': _age(r['birthdate'], today) if r['birthdate'] else None} for r in rows}


def city_details_summary(scope):
    """Counts from saved Personal Details for the city: status, age buckets, gender, religions."""
    empty = {'saved': 0, 'status': {}, 'flagged': 0, 'ages': {b[0]: 0 for b in AGE_BUCKETS}, 'with_birthdate': 0,
             'genders': {}, 'religions': []}
    if not _table_exists(DETAILS):
        return empty
    city = [scope['province'], scope['municipality']]
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT COUNT(*), status, gender FROM {DETAILS} WHERE province_slug = %s AND municipality = %s '
                    'GROUP BY status, gender', city)
        rows = cur.fetchall()
        cur.execute(f'SELECT birthdate FROM {DETAILS} WHERE province_slug = %s AND municipality = %s '
                    'AND birthdate IS NOT NULL', city)
        birthdates = [r[0] for r in cur.fetchall()]
        cur.execute(f"SELECT DISTINCT religion FROM {DETAILS} WHERE province_slug = %s AND municipality = %s "
                    "AND religion IS NOT NULL AND religion <> '' ORDER BY religion", city)
        religions = [r[0] for r in cur.fetchall()]
    out = dict(empty, religions=religions, with_birthdate=len(birthdates))
    for n, status, gender in rows:
        out['saved'] += n
        if status:
            out['status'][status] = out['status'].get(status, 0) + n
        if gender:
            out['genders'][gender] = out['genders'].get(gender, 0) + n
    out['flagged'] = sum(out['status'].get(s, 0) for s in FLAGGED_STATUSES)
    today = datetime.date.today()
    ages = dict(out['ages'])
    for bd in birthdates:
        a = _age(bd, today)
        for label, lo, hi in AGE_BUCKETS:
            if lo <= a <= hi:
                ages[label] += 1
                break
    out['ages'] = ages
    return out


def flagged_by_barangay(scope):
    """{barangay_pretty: n} voters whose saved status is Inactive / Transferred / Deceased."""
    ph = ','.join(['%s'] * len(FLAGGED_STATUSES))
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT v.barangay, COUNT(*) FROM {DETAILS} d JOIN {voter_table_qualified(scope)} v '
                    f'ON v.id = d.voter_id WHERE d.province_slug = %s AND d.municipality = %s '
                    f'AND d.status IN ({ph}) GROUP BY v.barangay',
                    [scope['province'], scope['municipality'], *FLAGGED_STATUSES])
        out = {}
        for brgy, n in cur.fetchall():
            out[title(brgy)] = out.get(title(brgy), 0) + n
    return out


def _clean_details(data):
    out = {}
    today = datetime.date.today()
    for field, label, rule in DETAIL_FIELDS:
        value = (data.get(field) or '').strip()
        if not value:
            out[field] = None
        elif rule == 'date':
            d = _valid_date(value)
            if not d:
                raise HouseholdError(f'{label}: enter a valid date.')
            if d > today:
                raise HouseholdError(f'{label} cannot be in the future.')
            out[field] = d
        elif isinstance(rule, tuple):
            if value not in rule:
                raise HouseholdError(f'{label}: choose from the list.')
            out[field] = value
        else:
            out[field] = value[:rule]
    if out['email'] and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', out['email']):
        raise HouseholdError('Email address: enter a valid email.')
    return out


def save_details(scope, voter, data, actor, ip):
    """Upsert the voter's personal details. Returns the labels of the fields that changed."""
    clean = _clean_details(data)
    raw = data.getlist('sectors') if hasattr(data, 'getlist') else (data.get('sectors') or [])
    if isinstance(raw, str):
        raw = [raw]
    sectors = {s for s in raw if s in SECTORS}
    province, vid = scope['province'], int(voter['id'])
    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        cur.execute(f'SELECT * FROM {DETAILS} WHERE province_slug = %s AND voter_id = %s FOR UPDATE',
                    [province, vid])
        rows = _rows(cur)
        before = rows[0] if rows else {}
        cur.execute(f'SELECT sector FROM {SECTORS_TABLE} WHERE province_slug = %s AND voter_id = %s FOR UPDATE',
                    [province, vid])
        sectors_before = {r[0] for r in cur.fetchall()}
        fields = [f for f in clean if (before.get(f) or None) != clean[f]]
        changed = [DETAIL_LABELS[f] for f in fields]
        if sectors != sectors_before:
            changed.append('Sectors')
        if not changed:
            return []
        cols = list(clean)
        cur.execute(
            f'INSERT INTO {DETAILS} (province_slug, voter_id, municipality, {", ".join(cols)}, updated_at, updated_by) '
            f'VALUES (%s, %s, %s, {", ".join(["%s"] * len(cols))}, NOW(), %s) '
            f'ON DUPLICATE KEY UPDATE {", ".join(f"{c} = VALUES({c})" for c in cols)}, '
            'updated_at = VALUES(updated_at), updated_by = VALUES(updated_by), '
            'is_mock = 0',          # edited by a person: no longer demo data, clear_demo_mock keeps it
            [province, vid, scope['municipality'], *[clean[c] for c in cols], actor])
        if sectors != sectors_before:
            # Replaced as a whole set; rows written by a person are real (is_mock = 0).
            cur.execute(f'DELETE FROM {SECTORS_TABLE} WHERE province_slug = %s AND voter_id = %s', [province, vid])
            cur.executemany(
                f'INSERT INTO {SECTORS_TABLE} (province_slug, voter_id, sector, municipality, created_at, created_by) '
                'VALUES (%s, %s, %s, %s, NOW(), %s)',
                [[province, vid, s, scope['municipality'], actor] for s in sorted(sectors)])
            fields.append('sectors')
        _audit(cur, province, vid, 'details.update',
               'Updated voter personal details: ' + ', '.join(changed).lower(),
               {'fields': fields, 'sectors': sorted(sectors)} if 'sectors' in fields else {'fields': fields},
               actor, ip)
    return changed


def sectors_for(province, vid):
    if not _table_exists(SECTORS_TABLE):
        return []
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT sector FROM {SECTORS_TABLE} WHERE province_slug = %s AND voter_id = %s',
                    [province, int(vid)])
        found = {r[0] for r in cur.fetchall()}
    return [s for s in SECTORS if s in found] + sorted(found - set(SECTORS))


def sectors_among(province, voter_ids):
    """{voter_id: [sector, ...]} (in SECTORS order) for a page of voters."""
    ids = [int(i) for i in voter_ids]
    if not ids or not _table_exists(SECTORS_TABLE):
        return {}
    ph = ','.join(['%s'] * len(ids))
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT voter_id, sector FROM {SECTORS_TABLE} WHERE province_slug = %s AND voter_id IN ({ph})',
                    [province, *ids])
        rows = cur.fetchall()
    order = {s: i for i, s in enumerate(SECTORS)}
    out = {}
    for vid, sector in rows:
        out.setdefault(vid, []).append(sector)
    return {vid: sorted(v, key=lambda s: order.get(s, 99)) for vid, v in out.items()}


def sector_counts(scope):
    """({barangay_pretty: {sector: n}}, {sector: {'Male': n, 'Female': n, 'total': n}}) for the city."""
    if not _table_exists(SECTORS_TABLE):
        return {}, {}
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT v.barangay, s.sector, d.gender, COUNT(*) FROM {SECTORS_TABLE} s '
                    f'JOIN {voter_table_qualified(scope)} v ON v.id = s.voter_id '
                    f'LEFT JOIN {DETAILS} d ON d.province_slug = s.province_slug AND d.voter_id = s.voter_id '
                    'WHERE s.province_slug = %s AND s.municipality = %s GROUP BY v.barangay, s.sector, d.gender',
                    [scope['province'], scope['municipality']])
        rows = cur.fetchall()
    by_brgy, by_sector = {}, {}
    for brgy, sector, gender, n in rows:
        b = by_brgy.setdefault(title(brgy), {})
        b[sector] = b.get(sector, 0) + n
        s = by_sector.setdefault(sector, {'Male': 0, 'Female': 0, 'total': 0})
        s['total'] += n
        if gender in s:
            s[gender] += n
    return by_brgy, by_sector


# ---------------------------------------------------------------------------
# Household members
# ---------------------------------------------------------------------------
def members_for(scope, vid):
    """The voter's household members; roll-linked members get their current roll name/precinct."""
    joined = (f'{MEMBERS} m LEFT JOIN {voter_table_qualified(scope)} v '
              'ON v.id = m.member_voter_id AND m.member_voter_id IS NOT NULL')
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT m.*, v.fullname AS roll_name, v.precinct AS roll_precinct, v.municipality AS roll_city '
                    f'FROM {joined} WHERE m.province_slug = %s AND m.voter_id = %s ORDER BY m.id',
                    [scope['province'], int(vid)])
        rows = _rows(cur)
    for r in rows:
        r['registered'] = bool(r['member_voter_id'] and r['roll_name'])
        if r['registered']:
            r['name'] = title(r['roll_name'])
            r['precinct'] = (r['roll_precinct'] or '').strip() or r['precinct']
            r['linkable'] = r['roll_city'] == scope['municipality']
    return rows


def household_of(scope, vid):
    """If this voter is listed as a member of someone else's household: {id, name, relationship}."""
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT m.voter_id, m.relationship, v.fullname, v.municipality FROM {MEMBERS} m '
                    f'JOIN {voter_table_qualified(scope)} v ON v.id = m.voter_id '
                    'WHERE m.province_slug = %s AND m.member_voter_id = %s LIMIT 1',
                    [scope['province'], int(vid)])
        row = cur.fetchone()
    if not row:
        return None
    return {'id': row[0], 'relationship': row[1], 'name': title(row[2]),
            'linkable': row[3] == scope['municipality']}


def add_member(scope, voter, data, actor, ip):
    """Add a member under `voter`'s household. Returns the member's display name."""
    province, vid = scope['province'], int(voter['id'])
    rel = (data.get('relationship') or '').strip()
    if rel not in RELATIONSHIPS:
        raise HouseholdError('Choose the relationship.')
    scholar = (data.get('scholar') or '').strip()
    if scholar and scholar not in SCHOLAR:
        raise HouseholdError('Choose a valid scholar status.')
    birthday = None
    if (data.get('birthday') or '').strip():
        birthday = _valid_date(data['birthday'].strip())
        if not birthday or birthday > datetime.date.today():
            raise HouseholdError('Birthday: enter a valid past date.')
    contact = (data.get('contact') or '').strip()[:60] or None

    raw = (data.get('member_voter_id') or '').strip()
    member_vid = int(raw) if raw.isdigit() else None
    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        if member_vid:
            if member_vid == vid:
                raise HouseholdError('A voter cannot be their own household member.')
            cur.execute(f'SELECT fullname, precinct, municipality FROM {voter_table_qualified(scope)} WHERE id = %s',
                        [member_vid])
            row = cur.fetchone()
            if not row or row[2] != scope['municipality']:
                raise HouseholdError('Pick the member from this city\'s voter roll.')
            name, precinct = title(row[0]), (row[1] or '').strip() or None
            cur.execute(f'SELECT m.voter_id, v.fullname FROM {MEMBERS} m '
                        f'JOIN {voter_table_qualified(scope)} v ON v.id = m.voter_id '
                        'WHERE m.province_slug = %s AND m.member_voter_id = %s LIMIT 1 FOR UPDATE OF m',
                        [province, member_vid])
            dup = cur.fetchone()
            if dup:
                where = 'this household' if dup[0] == vid else f"{title(dup[1])}'s household"
                raise HouseholdError(f'{name} is already listed in {where}.')
        else:
            name = re.sub(r'\s+', ' ', (data.get('name') or '').strip())[:255]
            if len(name) < 2:
                raise HouseholdError('Enter the member\'s full name, or pick them from the voter roll.')
            precinct = None      # not on the roll
        cur.execute(
            f'INSERT INTO {MEMBERS} (province_slug, municipality, voter_id, member_voter_id, name, relationship, '
            'precinct, birthday, contact, scholar, created_at, created_by) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)',
            [province, scope['municipality'], vid, member_vid, name, rel, precinct, birthday, contact,
             scholar or None, actor])
        member_id = cur.lastrowid
        _audit(cur, province, vid, 'household.member_add', f'Added household member: {name} ({rel})',
               {'member_id': member_id, 'member_voter_id': member_vid}, actor, ip)
        if member_vid:
            _audit(cur, province, member_vid, 'household.member_add',
                   f'Added to the household of {voter["name"]} ({rel})',
                   {'member_id': member_id, 'head_voter_id': vid}, actor, ip)
    return name


def remove_member(scope, voter, member_id, actor, ip):
    """Remove a member from `voter`'s household. Returns the removed name."""
    province, vid = scope['province'], int(voter['id'])
    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        cur.execute(f'SELECT id, name, relationship, member_voter_id FROM {MEMBERS} '
                    'WHERE id = %s AND province_slug = %s AND voter_id = %s FOR UPDATE',
                    [int(member_id), province, vid])
        rows = _rows(cur)
        if not rows:
            raise HouseholdError('That household member was not found.')
        m = rows[0]
        cur.execute(f'DELETE FROM {MEMBERS} WHERE id = %s', [m['id']])
        _audit(cur, province, vid, 'household.member_remove',
               f'Removed household member: {m["name"]} ({m["relationship"]})',
               {'member_id': m['id'], 'member_voter_id': m['member_voter_id']}, actor, ip)
        if m['member_voter_id']:
            _audit(cur, province, m['member_voter_id'], 'household.member_remove',
                   f'Removed from the household of {voter["name"]}', {'member_id': m['id'], 'head_voter_id': vid},
                   actor, ip)
    return m['name']
