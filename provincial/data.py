"""
Province-Wide EMS data: the municipal modules' numbers rolled up per city/municipality.

Voter totals come from the pre-computed summary (municipal/rollsummary.py) — never a live
province-wide scan. Everything else (machinery, smart cards, social services, sectors) is
read live from generic_360_db, grouped by municipality (and barangay for drill-downs).
Read-only.
"""
from django.db import connections

from municipal import household as hh
from municipal import rollsummary
from municipal import smartcard as sc
from municipal import social as soc
from municipal.machinery import EXT, _schema, RDS
from municipal.regions import province_pretty, region_of, voter_table
from municipal.text import title

COORDINATOR_CODES = ('municipal_coordinator', 'barangay_coordinator')


def pscope(request):
    """The chosen province from the session, or None."""
    p = request.session.get('prov') or {}
    slug = (p.get('province') or '').lower()
    table = voter_table(slug)
    if not table:
        return None
    return {'province': slug, 'province_name': province_pretty(slug), 'region': region_of(slug), 'table': table}


def _q(sql, params):
    with connections[EXT].cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def dashboard(ps):
    """Per-municipality rows (with per-barangay detail) + province totals."""
    slug, roll = ps['province'], f'{_schema(RDS)}.{ps["table"]}'
    munis = rollsummary.municipalities(slug)
    for m in munis.values():
        m.update(supporters=0, coordinators=0, opposition=0, cards=0, active_cards=0, records=0, beneficiaries=0,
                 released=0.0, sectors={})
        for b in m['barangays'].values():
            b.update(supporters=0, cards=0)

    def brgy(m, name):
        return m['barangays'].setdefault(title(name) or 'Unspecified', {'voters': 0, 'precincts': 0, 'supporters': 0, 'cards': 0})

    for mun, b, code, n in _q(f'SELECT v.municipality, v.barangay, p.role_code, COUNT(*) FROM ems_voter_political p '
                              f'JOIN {roll} v ON v.id = p.voter_id WHERE p.province_slug = %s GROUP BY 1, 2, 3', [slug]):
        m = munis.get(mun)
        if not m:
            continue
        if code == 'supporter':
            m['supporters'] += n
            brgy(m, b)['supporters'] += n
        elif code in COORDINATOR_CODES:
            m['coordinators'] += n
        elif code == 'opposition':
            m['opposition'] += n

    if sc.table_exists():
        for mun, b, status, n in _q(f'SELECT municipality, barangay, status, COUNT(*) FROM {sc.TABLE} '
                                    'WHERE province_slug = %s GROUP BY 1, 2, 3', [slug]):
            m = munis.get(mun)
            if m and status in sc.HOLDING:
                m['cards'] += n
                m['active_cards'] += n if status == 'active' else 0
                brgy(m, b)['cards'] += n

    if soc.table_exists():
        for mun, n, people, released in _q(f"SELECT municipality, COUNT(*), COUNT(DISTINCT voter_id), "
                                           f"COALESCE(SUM(CASE WHEN status = 'Released' THEN amount END), 0) "
                                           f'FROM {soc.TABLE} WHERE province_slug = %s GROUP BY 1', [slug]):
            m = munis.get(mun)
            if m:
                m['records'], m['beneficiaries'], m['released'] = n, people, float(released or 0)

    if hh.tables_exist():
        for mun, sector, n in _q(f'SELECT municipality, sector, COUNT(*) FROM {hh.SECTORS_TABLE} '
                                 'WHERE province_slug = %s GROUP BY 1, 2', [slug]):
            m = munis.get(mun)
            if m:
                m['sectors'][sector] = n

    rows = sorted(munis.values(), key=lambda m: -m['voters'])
    main = [s for s, _ in hh.MAIN_SECTORS]
    for m in rows:
        m['coverage'] = (m['supporters'] / m['voters'] * 100) if m['voters'] else 0
        m['card_coverage'] = (m['cards'] / m['voters'] * 100) if m['voters'] else 0
        m['main_sectors'] = [m['sectors'].get(s, 0) for s in main]
        m['barangay_list'] = sorted(({'name': k, **v} for k, v in m['barangays'].items()), key=lambda b: -b['voters'])
    totals = {k: sum(m[k] for m in rows) for k in ('voters', 'precincts', 'supporters', 'coordinators', 'opposition',
                                                     'cards', 'active_cards', 'records', 'beneficiaries', 'released')}
    totals['barangays'] = sum(len(m['barangays']) for m in rows)
    totals['municipalities'] = len(rows)
    totals['coverage'] = (totals['supporters'] / totals['voters'] * 100) if totals['voters'] else 0
    totals['card_coverage'] = (totals['cards'] / totals['voters'] * 100) if totals['voters'] else 0
    totals['main_sectors'] = [sum(m['main_sectors'][i] for m in rows) for i in range(len(main))]
    return rows, totals


def extreme(rows, pick, field, cov):
    """Municipality with the most/least `field` (ties counted), like the city dashboard."""
    if not rows:
        return None
    value = pick(r[field] for r in rows)
    tied = [r for r in rows if r[field] == value]
    return {'name': tied[0]['name'], 'value': value, 'voters': tied[0]['voters'], 'coverage': tied[0][cov], 'more': len(tied) - 1}
