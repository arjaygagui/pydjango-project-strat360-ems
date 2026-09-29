"""
Quick Count (voting day · live attendance) — port of CALOOCAN-EMS quick-count.php.

REAL: barangays, registered voters, precincts (voter roll, cvl_national read-only) and
smart cardholders (names, card numbers, services, barangays — generic_360_db).

SIMULATED: attendance. There is no check-in feed yet, so — like Caloocan — each barangay
gets a stable turnout factor from a CRC32 of its name (55%–74%), shown as a 12 PM snapshot
of polls that open at 6 AM. Everything else on the page is derived from those numbers
(no hard-coded "+482 in last hour" style figures). Flagged scans are scans of cards that
are still Pending. Nothing here writes to any database.
"""
import zlib

from django.core.cache import cache
from django.db import connections

from .machinery import EXT, RDS, _rows, voter_table_qualified
from .smartcard import HOLDING, SERVICES, TABLE as CARDS
from .text import title

POLLS_OPEN_HOUR = 6          # 6 AM
SNAPSHOT_HOUR_IDX = 6        # 12 PM (index into HOURS)
HOURS = ['6 AM', '7 AM', '8 AM', '9 AM', '10 AM', '11 AM', '12 PM', '1 PM', '2 PM', '3 PM', '4 PM', '5 PM', '6 PM']
HOUR_WEIGHTS = [0.06, 0.13, 0.19, 0.17, 0.15, 0.16, 0.14]   # Caloocan's 6 AM–12 PM curve (sums to 1)
TARGET_PCT = 78.0            # campaign target shown on the Turnout Rate card
TOP_PRECINCTS = 8
PRECINCT_CANDIDATES = 60     # biggest precincts considered; ranked by (simulated) check-ins
PRECINCT_TTL = 6 * 3600


def turnout_factor(key, base=0.55, spread=20):
    """Stable pseudo-random turnout for a barangay/precinct (same formula as the PHP crc32)."""
    return base + (zlib.crc32(key.encode('utf-8')) % spread) / 100


def barangays(brgy_totals):
    """Per-barangay registered / checked-in / precincts, biggest first."""
    out = []
    for b in sorted(brgy_totals.values(), key=lambda t: -t['total']):
        checked = round(b['total'] * turnout_factor(b['barangay']))
        out.append({'barangay': b['barangay'], 'registered': b['total'], 'checked': checked,
                    'precincts': b['precincts'], 'pct': (checked / b['total'] * 100) if b['total'] else 0})
    return out


def hourly(total_checked):
    counts = [round(total_checked * w) for w in HOUR_WEIGHTS]
    return counts + [0] * (len(HOURS) - len(counts))


def _precinct_rows(scope, ckey):
    """The biggest precincts by registered voters (cached — it groups the whole city roll)."""
    rows = cache.get(ckey)
    if rows is None:
        with connections[RDS].cursor() as cur:
            cur.execute(
                f"SELECT precinct, barangay, COUNT(*) reg, MIN(id) first_id FROM {scope['table']} "
                "WHERE municipality = %s AND precinct IS NOT NULL AND TRIM(precinct) <> '' "
                f"GROUP BY precinct, barangay ORDER BY reg DESC LIMIT {PRECINCT_CANDIDATES}",
                [scope['municipality']])
            rows = _rows(cur)
        cache.set(ckey, rows, PRECINCT_TTL)
    return rows


def top_precincts(scope, ckey):
    """Top precincts by check-ins, each with a real sample voter (a cardholder when it has one)."""
    rows = []
    for r in _precinct_rows(scope, ckey):
        code = r['precinct'].strip()
        checked = round(r['reg'] * min(0.98, turnout_factor(code, 0.72, 15)))
        rows.append({**r, 'code': code, 'checked': checked})
    rows = sorted(rows, key=lambda r: (-r['checked'], r['code']))[:TOP_PRECINCTS]
    if not rows:
        return []
    precincts = [r['precinct'] for r in rows]
    ph = ','.join(['%s'] * len(precincts))
    holders = {}
    with connections[EXT].cursor() as cur:
        cur.execute(
            f'SELECT v.precinct, v.id, v.fullname, c.card_number FROM {CARDS} c '
            f'JOIN {voter_table_qualified(scope)} v ON v.id = c.voter_id '
            "WHERE c.province_slug = %s AND c.municipality = %s AND c.status IN ('active', 'pending') "
            f'AND v.precinct IN ({ph}) ORDER BY c.id',
            [scope['province'], scope['municipality'], *precincts])
        for prec, vid, name, card in cur.fetchall():
            holders.setdefault(prec, (vid, name, card))
    ids = [r['first_id'] for r in rows if r['precinct'] not in holders]
    firsts = {}
    if ids:
        with connections[RDS].cursor() as cur:
            cur.execute(f"SELECT id, fullname FROM {scope['table']} WHERE id IN ({','.join(['%s'] * len(ids))})", ids)
            firsts = dict(cur.fetchall())
    out = []
    for r in rows:
        code, cap, checked = r['code'], r['reg'], r['checked']
        vid, name, card = holders.get(r['precinct'], (r['first_id'], firsts.get(r['first_id'], ''), None))
        out.append({'code': code, 'barangay': title(r['barangay']), 'cap': cap, 'checked': checked,
                    'pct': round(checked / cap * 100) if cap else 0,
                    'velocity': round(checked / ((SNAPSHOT_HOUR_IDX) * 60), 1),
                    'voter_id': vid, 'voter': title(name), 'card': card})
    return out


def cardholders(scope, city_rate):
    """Cardholder check-ins by service (the "sector" chart), recent scans and the live-feed pool."""
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT service, status, COUNT(*) FROM {CARDS} WHERE province_slug = %s AND municipality = %s '
                    'GROUP BY service, status', [scope['province'], scope['municipality']])
        counts = cur.fetchall()
        joined = f'{CARDS} c JOIN {voter_table_qualified(scope)} v ON v.id = c.voter_id'
        city = "c.province_slug = %s AND c.municipality = %s AND c.status IN ('active', 'pending')"
        cur.execute(f'SELECT c.card_number, c.barangay, c.status, c.voter_id, v.fullname FROM {joined} '
                    f'WHERE {city} ORDER BY c.issued_date DESC, c.id DESC LIMIT 10',
                    [scope['province'], scope['municipality']])
        recent = _rows(cur)
        cur.execute(f'SELECT c.card_number, c.barangay, c.status, c.voter_id, v.fullname FROM {joined} '
                    f'WHERE {city} ORDER BY RAND() LIMIT 60', [scope['province'], scope['municipality']])
        pool = _rows(cur)

    by_service = {s: 0 for s in SERVICES}
    holders = pending = 0
    for service, status, n in counts:
        if status in HOLDING:
            by_service[service or 'Unspecified'] = by_service.get(service or 'Unspecified', 0) + n
            holders += n
            pending += n if status == 'pending' else 0
    rate = city_rate / 100

    def scan(r):
        return {'card': r['card_number'], 'barangay': r['barangay'] or '', 'voter_id': r['voter_id'],
                'name': title(r['fullname']), 'flag': r['status'] == 'pending'}

    recent_scans = [dict(scan(r), time=f'12:{59 - i:02d} PM') for i, r in enumerate(recent)]
    return {
        'holders': holders,
        'checked': round(holders * rate),
        'flagged': round(pending * rate),
        'by_service': [{'service': s, 'holders': n, 'checked': round(n * rate)} for s, n in by_service.items() if n],
        'recent': recent_scans,
        'pool': [scan(r) for r in pool],
    }
