"""
Pre-computed voter-roll totals per (province, municipality, barangay).

Province-wide GROUP BYs over cvl_national are too slow to run on every page view
(Bulacan 2.2M voters: ~7 s; NCR 7.5M: ~27 s), and the roll changes rarely, so the
Province-Wide EMS reads these totals from generic_360_db.muni_roll_summary instead.
Build / refresh with `manage.py build_roll_summary --province bulacan` (or --all).

cvl_national is only read; the summary lives in generic_360_db like all EMS data.
"""
import datetime

from django.db import connections, transaction

from .machinery import EXT, RDS
from .regions import voter_table
from .text import title

TABLE = 'muni_roll_summary'

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  province_slug varchar(32) NOT NULL,
  municipality varchar(160) NOT NULL,
  barangay varchar(160) NOT NULL,
  voters int NOT NULL,
  precincts int NOT NULL,
  built_at datetime NOT NULL,
  PRIMARY KEY (province_slug, municipality, barangay)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
"""
# utf8mb4_bin: roll values that differ only by case/spacing (e.g. ' BONGA MAYOR' vs
# 'BONGA MAYOR') stay separate rows, exactly as GROUP BY on the roll returns them.

# Per-precinct totals from the same scan (Quick Count's top precincts; a province-wide
# GROUP BY precinct on the roll takes ~20 s for NCR). first_id = a sample voter of the precinct.
PRECINCTS = 'muni_roll_precincts'
PRECINCTS_DDL = f"""
CREATE TABLE IF NOT EXISTS {PRECINCTS} (
  province_slug varchar(32) NOT NULL,
  municipality varchar(160) NOT NULL,
  barangay varchar(160) NOT NULL,
  precinct varchar(100) NOT NULL,
  voters int NOT NULL,
  first_id bigint NOT NULL,
  built_at datetime NOT NULL,
  PRIMARY KEY (province_slug, municipality, barangay, precinct),
  KEY k_size (province_slug, voters)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
"""


def ensure_table():
    with connections[EXT].cursor() as cur:
        cur.execute(DDL)
        cur.execute(PRECINCTS_DDL)


def table_exists():
    with connections[EXT].cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s',
                    [TABLE])
        return cur.fetchone()[0] > 0


def build(slug):
    """Recompute one province from the roll. Returns (rows, voters, seconds)."""
    table = voter_table(slug)
    if not table:
        raise ValueError(f'Unknown province slug: {slug}')
    ensure_table()
    started = datetime.datetime.now()
    # One scan at precinct level; barangay rows (voters, distinct precincts) are rolled up from it.
    with connections[RDS].cursor() as cur:
        cur.execute(f'SELECT municipality, barangay, precinct, COUNT(*), MIN(id) FROM {table} '
                    'WHERE municipality IS NOT NULL GROUP BY municipality, barangay, precinct')
        groups = cur.fetchall()
    brgys, precincts = {}, []
    for m, b, p, n, first in groups:
        key = (m, b or '')
        row = brgys.setdefault(key, [0, 0])
        row[0] += n
        if p is not None:                        # COUNT(DISTINCT precinct) ignores NULL
            row[1] += 1
            if p.strip():
                precincts.append((m, b or '', p, n, first))
    rows = [(m, b, n, p) for (m, b), (n, p) in brgys.items()]
    built = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        cur.execute(f'DELETE FROM {TABLE} WHERE province_slug = %s', [slug])
        for i in range(0, len(rows), 1000):
            cur.executemany(f'INSERT INTO {TABLE} (province_slug, municipality, barangay, voters, precincts, built_at) '
                            'VALUES (%s, %s, %s, %s, %s, %s)', [[slug, m, b, n, p, built] for m, b, n, p in rows[i:i + 1000]])
        cur.execute(f'DELETE FROM {PRECINCTS} WHERE province_slug = %s', [slug])
        for i in range(0, len(precincts), 1000):
            cur.executemany(f'INSERT INTO {PRECINCTS} (province_slug, municipality, barangay, precinct, voters, first_id, built_at) '
                            'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                            [[slug, m, b, p, n, f, built] for m, b, p, n, f in precincts[i:i + 1000]])
    return len(rows), sum(r[2] for r in rows), (datetime.datetime.now() - started).total_seconds()


def precincts_built(slug):
    """True once this province's precinct totals exist (added after the first summary build)."""
    with connections[EXT].cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s',
                    [PRECINCTS])
        if not cur.fetchone()[0]:
            return False
        cur.execute(f'SELECT 1 FROM {PRECINCTS} WHERE province_slug = %s LIMIT 1', [slug])
        return cur.fetchone() is not None


# Spreadsheet error values that ended up in the roll's precinct column (e.g. '#REF!' in Davao de Oro)
# are not precincts: they are left out of every precinct ranking.
NOT_A_PRECINCT = "TRIM(precinct) NOT LIKE '#%%'"


def largest_precincts(slug, limit, max_voters):
    """[{'municipality', 'barangay', 'precinct', 'reg', 'first_id'}] — the province's biggest precincts
    of at most `max_voters`."""
    if not precincts_built(slug):
        return []
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT municipality, barangay, precinct, voters, first_id FROM {PRECINCTS} '
                    f'WHERE province_slug = %s AND voters <= %s AND {NOT_A_PRECINCT} ORDER BY voters DESC LIMIT %s',
                    [slug, int(max_voters), int(limit)])
        return [{'municipality': m, 'barangay': b, 'precinct': p, 'reg': n, 'first_id': f}
                for m, b, p, n, f in cur.fetchall()]


def largest_precincts_in(slugs, limit, max_voters):
    """largest_precincts() over several provinces (Nationwide / a region), each row with its
    'province_slug'. One query (~0.3 s over the whole country)."""
    if not slugs:
        return []
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT province_slug, municipality, barangay, precinct, voters, first_id FROM {PRECINCTS} '
                    f"WHERE province_slug IN ({','.join(['%s'] * len(slugs))}) AND voters <= %s AND {NOT_A_PRECINCT} "
                    'ORDER BY voters DESC LIMIT %s', [*slugs, int(max_voters), int(limit)])
        return [{'province_slug': s, 'municipality': m, 'barangay': b, 'precinct': p, 'reg': n, 'first_id': f}
                for s, m, b, p, n, f in cur.fetchall()]


def built_at(slug):
    if not table_exists():
        return None
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT MAX(built_at) FROM {TABLE} WHERE province_slug = %s', [slug])
        return cur.fetchone()[0]


def province_rows(slug):
    """[(municipality_raw, barangay_raw, voters, precincts)] for a province (empty if not built)."""
    if not table_exists():
        return []
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT municipality, barangay, voters, precincts FROM {TABLE} WHERE province_slug = %s', [slug])
        return cur.fetchall()


def municipalities(slug):
    """{municipality_raw: {'raw', 'name', 'voters', 'precincts', 'barangays': {pretty: {'voters', 'precincts'}}}}"""
    out = {}
    for m, b, n, p in province_rows(slug):
        mun = out.setdefault(m, {'raw': m, 'name': title(m), 'voters': 0, 'precincts': 0, 'barangays': {}})
        mun['voters'] += n
        mun['precincts'] += p
        br = mun['barangays'].setdefault(title(b) or 'Unspecified', {'voters': 0, 'precincts': 0})
        br['voters'] += n
        br['precincts'] += p
    return out
