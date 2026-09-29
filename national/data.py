"""
Nationwide EMS data: the EMS numbers rolled up per province (84), each with its cities.

Voter totals come from the pre-computed roll summary (municipal/rollsummary.py) for every
province — never a live scan of the 67.8M-row roll. EMS numbers (machinery, smart cards,
social services, sectors) are read live from generic_360_db, grouped by province (and by
city for the drill-downs). Read-only.
"""
from django.core.cache import cache
from django.db import connections

from municipal import household as hh
from municipal import rollsummary
from municipal import smartcard as sc
from municipal import social as soc
from municipal.machinery import EXT, table as machinery_table, voter_table_qualified
from municipal.regions import REGION_MAP, province_pretty, region_of, voter_table
from municipal.text import title

COORDINATOR_CODES = ('regional_coordinator', 'provincial_coordinator', 'municipal_coordinator', 'barangay_coordinator')
ROLL_TTL = 10 * 60          # the whole-country summary (41k rows) is cached briefly


def regions():
    """Region names in REGION_MAP order (Region I … BARMM)."""
    out = []
    for r in REGION_MAP.values():
        if r not in out:
            out.append(r)
    return out


def _roll():
    """{slug: {municipality_raw: {'raw', 'name', 'voters', 'precincts', 'barangays': {pretty: {'voters', 'precincts'}}}}}
    for all provinces — the same shape as rollsummary.municipalities() per province."""
    data = cache.get('nat-roll2')
    if data is None:
        data = {}
        with connections[EXT].cursor() as cur:
            cur.execute(f'SELECT province_slug, municipality, barangay, voters, precincts FROM {rollsummary.TABLE}')
            for slug, m, b, n, p in cur.fetchall():
                city = data.setdefault(slug, {}).setdefault(m, {'raw': m, 'name': title(m), 'voters': 0, 'precincts': 0,
                                                                  'barangays': {}})
                city['voters'] += n
                city['precincts'] += p
                br = city['barangays'].setdefault(title(b) or 'Unspecified', {'voters': 0, 'precincts': 0})
                br['voters'] += n
                br['precincts'] += p
        cache.set('nat-roll2', data, ROLL_TTL)
    return data


def _q(sql, params=()):
    with connections[EXT].cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def dashboard(region=''):
    """Per-province rows (with per-city detail) + totals, biggest first; `region` filters."""
    roll = _roll()
    slugs = [s for s in REGION_MAP if s in roll and (not region or REGION_MAP[s] == region)]
    provs = {}
    for slug in slugs:
        cities = {raw: {'raw': raw, 'name': c['name'], 'voters': c['voters'], 'precincts': c['precincts'],
                        'barangays': len(c['barangays']), 'supporters': 0, 'cards': 0}
                  for raw, c in roll[slug].items()}
        provs[slug] = {'slug': slug, 'name': province_pretty(slug), 'region': region_of(slug) or '',
                       'voters': sum(c['voters'] for c in cities.values()),
                       'precincts': sum(c['precincts'] for c in cities.values()),
                       'barangays': sum(c['barangays'] for c in cities.values()),
                       'cities': cities, 'supporters': 0, 'coordinators': 0, 'opposition': 0, 'cards': 0,
                       'active_cards': 0, 'records': 0, 'beneficiaries': 0, 'released': 0.0, 'sectors': {}}

    for slug, code, n in _q(f'SELECT province_slug, role_code, COUNT(*) FROM {machinery_table("nat")} GROUP BY 1, 2'):
        p = provs.get(slug)
        if not p:
            continue
        if code == 'supporter':
            p['supporters'] += n
        elif code in COORDINATOR_CODES:
            p['coordinators'] += n
        elif code == 'opposition':
            p['opposition'] += n
    # Supporters per city (drill-down): only provinces that have any, one join each.
    for slug in [s for s, p in provs.items() if p['supporters']]:
        roll_table = voter_table_qualified({'province': slug, 'table': voter_table(slug)})
        for muni, n in _q(f"SELECT v.municipality, COUNT(*) FROM {machinery_table('nat')} x JOIN {roll_table} v ON v.id = x.voter_id "
                          "WHERE x.province_slug = %s AND x.role_code = 'supporter' GROUP BY 1", [slug]):
            city = provs[slug]['cities'].get(muni)
            if city:
                city['supporters'] += n

    if sc.table_exists():
        for slug, muni, status, n in _q(f'SELECT province_slug, municipality, status, COUNT(*) FROM {sc.TABLE} GROUP BY 1, 2, 3'):
            p = provs.get(slug)
            if p and status in sc.HOLDING:
                p['cards'] += n
                p['active_cards'] += n if status == 'active' else 0
                if muni in p['cities']:
                    p['cities'][muni]['cards'] += n
    if soc.table_exists():
        for slug, n, people, released in _q(f"SELECT province_slug, COUNT(*), COUNT(DISTINCT voter_id), "
                                            f"COALESCE(SUM(CASE WHEN status = 'Released' THEN amount END), 0) "
                                            f'FROM {soc.TABLE} GROUP BY 1'):
            p = provs.get(slug)
            if p:
                p['records'], p['beneficiaries'], p['released'] = n, people, float(released or 0)
    if hh.tables_exist():
        for slug, sector, n in _q(f'SELECT province_slug, sector, COUNT(*) FROM {hh.SECTORS_TABLE} GROUP BY 1, 2'):
            p = provs.get(slug)
            if p:
                p['sectors'][sector] = n

    rows = sorted(provs.values(), key=lambda p: -p['voters'])
    main = [s for s, _ in hh.MAIN_SECTORS]
    for p in rows:
        p['coverage'] = (p['supporters'] / p['voters'] * 100) if p['voters'] else 0
        p['card_coverage'] = (p['cards'] / p['voters'] * 100) if p['voters'] else 0
        p['main_sectors'] = [p['sectors'].get(s, 0) for s in main]
        p['city_list'] = sorted(p['cities'].values(), key=lambda c: -c['voters'])
    totals = {k: sum(p[k] for p in rows) for k in ('voters', 'precincts', 'barangays', 'supporters', 'coordinators',
                                                     'opposition', 'cards', 'active_cards', 'records', 'beneficiaries',
                                                     'released')}
    totals['provinces'] = len(rows)
    totals['cities'] = sum(len(p['cities']) for p in rows)
    totals['coverage'] = (totals['supporters'] / totals['voters'] * 100) if totals['voters'] else 0
    totals['card_coverage'] = (totals['cards'] / totals['voters'] * 100) if totals['voters'] else 0
    totals['main_sectors'] = [sum(p['main_sectors'][i] for p in rows) for i in range(len(main))]
    return rows, totals
