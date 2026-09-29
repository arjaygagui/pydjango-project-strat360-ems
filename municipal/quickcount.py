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

from .machinery import EXT, RDS, _rows, voter_briefs, voter_table_qualified
from .regions import province_pretty, voter_table
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
# COMELEC caps a clustered precinct at 1,000 voters; bigger "precincts" on the roll are data
# quirks (e.g. Pulilan's 0087D spans 12 barangays, up to 8,867 voters) and are left out of the ranking.
MAX_PRECINCT = 1000


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


def city_areas(municipalities):
    """Province view: per-city registered / checked-in / precincts, each city summed from its
    barangays exactly as the city page does — so the province equals the sum of its city pages.
    `municipalities` is rollsummary.municipalities()."""
    out = []
    for m in municipalities.values():
        brgys = barangays({k: {'barangay': k, 'total': v['voters'], 'precincts': v['precincts']}
                           for k, v in m['barangays'].items()})
        reg, checked = sum(b['registered'] for b in brgys), sum(b['checked'] for b in brgys)
        out.append({'barangay': m['name'], 'raw': m['raw'], 'registered': reg, 'checked': checked,
                    'precincts': sum(b['precincts'] for b in brgys), 'pct': (checked / reg * 100) if reg else 0})
    return sorted(out, key=lambda a: -a['registered'])


def province_areas(roll, slugs, names):
    """Nationwide view: per-province rows, each summed from its cities exactly as the province page
    does (so the country equals the sum of its province pages). `roll` is national.data._roll()."""
    out = []
    for slug in slugs:
        cities = city_areas(roll.get(slug, {}))
        reg, checked = sum(c['registered'] for c in cities), sum(c['checked'] for c in cities)
        out.append({'barangay': names[slug], 'raw': slug, 'registered': reg, 'checked': checked,
                    'precincts': sum(c['precincts'] for c in cities), 'pct': (checked / reg * 100) if reg else 0})
    return sorted(out, key=lambda a: -a['registered'])


def attendance(areas):
    """KPIs / meter / hourly figures from per-area rows (barangays for a city, cities for a province)."""
    registered = sum(a['registered'] for a in areas)
    checked = sum(a['checked'] for a in areas)
    rate = (checked / registered * 100) if registered else 0
    counts = hourly(checked)
    so_far = counts[:SNAPSHOT_HOUR_IDX + 1]
    peak = max(range(len(so_far)), key=lambda i: so_far[i]) if checked else 0
    minutes = SNAPSHOT_HOUR_IDX * 60
    return {
        'registered': registered, 'checked': checked, 'remaining': registered - checked, 'rate': rate,
        'target': TARGET_PCT, 'precinct_count': sum(a['precincts'] for a in areas),
        'last_hour': so_far[-1] if so_far else 0,
        'avg_hour': round(checked / len(so_far)) if so_far else 0,
        'avg_min': round(checked / minutes) if minutes else 0,
        'peak_label': HOURS[peak], 'peak_value': so_far[peak] if so_far else 0,
        'snapshot': HOURS[SNAPSHOT_HOUR_IDX], 'hourly': counts,
    }


def _precinct_rows(scope, ckey):
    """The biggest precincts by registered voters (cached — it groups the whole city roll)."""
    rows = cache.get(ckey)
    if rows is None:
        with connections[RDS].cursor() as cur:
            cur.execute(
                f"SELECT precinct, barangay, COUNT(*) reg, MIN(id) first_id FROM {scope['table']} "
                "WHERE municipality = %s AND precinct IS NOT NULL AND TRIM(precinct) <> '' AND TRIM(precinct) NOT LIKE '#%%' "
                f"GROUP BY precinct, barangay HAVING reg <= {MAX_PRECINCT} ORDER BY reg DESC LIMIT {PRECINCT_CANDIDATES}",
                [scope['municipality']])
            rows = [{**r, 'municipality': scope['municipality']} for r in _rows(cur)]
        cache.set(ckey, rows, PRECINCT_TTL)
    return rows


def top_precincts(scope, ckey):
    """The city's top precincts (see rank_precincts)."""
    return rank_precincts(scope, _precinct_rows(scope, ckey))


def rank_precincts(scope, candidates):
    """Top precincts by (simulated) check-ins, each with a real sample voter — a cardholder when the
    precinct has one. `candidates`: [{'municipality', 'barangay', 'precinct', 'reg', 'first_id'}], plus
    'province_slug' when they come from several provinces (Nationwide); otherwise the scope's
    province. Precinct codes repeat across cities, so a precinct is keyed by province + city + code."""
    rows = []
    for r in candidates:
        code = r['precinct'].strip()
        checked = round(r['reg'] * min(0.98, turnout_factor(code, 0.72, 15)))
        rows.append({**r, 'province_slug': r.get('province_slug') or scope['province'], 'code': code, 'checked': checked})
    rows = sorted(rows, key=lambda r: (-r['checked'], r['code']))[:TOP_PRECINCTS]
    if not rows:
        return []
    holders, firsts = {}, {}
    for slug in sorted({r['province_slug'] for r in rows}):
        mine = [r for r in rows if r['province_slug'] == slug]
        pscope = {'province': slug, 'table': voter_table(slug)}
        precincts = sorted({r['precinct'] for r in mine})
        ph = ','.join(['%s'] * len(precincts))
        with connections[EXT].cursor() as cur:
            cur.execute(
                f'SELECT v.municipality, v.precinct, v.id, v.fullname, c.card_number FROM {CARDS} c '
                f'JOIN {voter_table_qualified(pscope)} v ON v.id = c.voter_id '
                "WHERE c.province_slug = %s AND c.status IN ('active', 'pending') "
                f'AND v.precinct IN ({ph}) ORDER BY c.id',
                [slug, *precincts])
            for muni, prec, vid, name, card in cur.fetchall():
                holders.setdefault((slug, muni, prec.strip()), (vid, name, card))
        ids = [r['first_id'] for r in mine if (slug, r['municipality'], r['code']) not in holders]
        if ids:
            with connections[RDS].cursor() as cur:
                cur.execute(f"SELECT id, fullname FROM {pscope['table']} WHERE id IN ({','.join(['%s'] * len(ids))})", ids)
                firsts.update({(slug, i): n for i, n in cur.fetchall()})
    out = []
    for r in rows:
        code, cap, checked, slug = r['code'], r['reg'], r['checked'], r['province_slug']
        vid, name, card = holders.get((slug, r['municipality'], code),
                                      (r['first_id'], firsts.get((slug, r['first_id']), ''), None))
        out.append({'code': code, 'barangay': title(r['barangay']), 'city': title(r['municipality']),
                    'province_slug': slug, 'province': province_pretty(slug),
                    'cap': cap, 'checked': checked,
                    'pct': round(checked / cap * 100) if cap else 0,
                    'velocity': round(checked / ((SNAPSHOT_HOUR_IDX) * 60), 1),
                    'voter_id': vid, 'voter': title(name), 'card': card})
    return out


def cardholders(scope, city_rate):
    """Cardholder check-ins by service (the "sector" chart), recent scans and the live-feed pool —
    for the city, the whole province when the scope has no municipality, or Nationwide when it has
    no province either ({'provinces': [...]} limits it to a region)."""
    national = not scope.get('province')
    if national:
        slugs = scope.get('provinces')
        area, args = (f"c.province_slug IN ({','.join(['%s'] * len(slugs))})", list(slugs)) if slugs else ('1 = 1', [])
    else:
        area, args = 'c.province_slug = %s', [scope['province']]
    if scope.get('municipality'):
        area += ' AND c.municipality = %s'
        args.append(scope['municipality'])
    live = f"{area} AND c.status IN ('active', 'pending')"
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT service, status, COUNT(*) FROM {CARDS} c WHERE {area} GROUP BY service, status', args)
        counts = cur.fetchall()
        if national:
            # Cards of many provinces: names come from each province's roll (voter_briefs).
            cols = 'c.card_number, c.barangay, c.municipality, c.status, c.voter_id, c.province_slug'
            cur.execute(f'SELECT {cols} FROM {CARDS} c WHERE {live} ORDER BY c.issued_date DESC, c.id DESC LIMIT 10', args)
            recent = _rows(cur)
            cur.execute(f'SELECT {cols} FROM {CARDS} c WHERE {live} ORDER BY RAND() LIMIT 60', args)
            pool = _rows(cur)
            briefs = voter_briefs([(r['province_slug'], r['voter_id']) for r in recent + pool])
            for r in recent + pool:
                r['fullname'] = (briefs.get((r['province_slug'], r['voter_id'])) or {}).get('fullname', '')
        else:
            joined = f'{CARDS} c JOIN {voter_table_qualified(scope)} v ON v.id = c.voter_id'
            cols = 'c.card_number, c.barangay, c.municipality, c.status, c.voter_id, c.province_slug, v.fullname'
            cur.execute(f'SELECT {cols} FROM {joined} WHERE {live} ORDER BY c.issued_date DESC, c.id DESC LIMIT 10', args)
            recent = _rows(cur)
            cur.execute(f'SELECT {cols} FROM {joined} WHERE {live} ORDER BY RAND() LIMIT 60', args)
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
        return {'card': r['card_number'], 'barangay': r['barangay'] or '', 'city': title(r['municipality']),
                'province_slug': r['province_slug'], 'province': province_pretty(r['province_slug']),
                'voter_id': r['voter_id'], 'name': title(r['fullname']), 'flag': r['status'] == 'pending'}

    recent_scans = [dict(scan(r), time=f'12:{59 - i:02d} PM') for i, r in enumerate(recent)]
    return {
        'holders': holders,
        'checked': round(holders * rate),
        'flagged': round(pending * rate),
        'by_service': [{'service': s, 'holders': n, 'checked': round(n * rate)} for s, n in by_service.items() if n],
        'recent': recent_scans,
        'pool': [scan(r) for r in pool],
    }
