"""
Heat Map (Geo-Analytics) — port of MUNICIPAL-EMS heat-map.php, with real numbers only.

PHP's page is entirely sample data (made-up barangays, coordinates, population, budget,
"↑ 4.3% MoM" deltas, purok usage and "AI forecasts"). Here every metric comes from the
EMS tables for the chosen city, per barangay:

  voters / precincts         voter roll (cvl_national, read-only, cached)
  cards / active / pending   muni_smart_cards
  supporters / coordinators / opposition   the level's machinery table (machinery.TABLES)
  beneficiaries / records / released ₱     muni_social_services
  sector members / top sectors             muni_voter_sectors
  households                 muni_household_members
  activity                   ems_voter_audit entries in the chosen period

Locations come from geo.py (stored OpenStreetMap lookups). Read-only.
"""
import datetime

from django.db import connections
from django.utils import timezone

from . import household as hh
from . import smartcard as sc
from . import social as soc
from .machinery import EXT, audit_level_sql, voter_table_qualified
from .text import title

PERIODS = {'7': 'Last 7 days', '30': 'Last 30 days', '90': 'Last 90 days', '365': 'Last 12 months'}
CARD_STATUSES = {'': 'All Card Status', 'active': 'Active', 'pending': 'Pending'}


def _since(days):
    local = timezone.localtime() - datetime.timedelta(days=days)
    return local.astimezone(datetime.timezone.utc).replace(tzinfo=None)


# A city scope groups by barangay (keys title-cased, merging spelling variants); a province
# scope (no municipality — the Province-Wide EMS) groups by city (keys = raw municipality); a
# national scope (no province: {} or {'provinces': [...]} for a region) groups by province slug.
def _level(scope):
    return 'city' if scope.get('municipality') else 'prov' if scope.get('province') else 'nat'


def _area(scope, alias=''):
    if not scope.get('province'):
        slugs = scope.get('provinces')
        if slugs:
            return f"{alias}province_slug IN ({','.join(['%s'] * len(slugs))})", list(slugs)
        return '1 = 1', []
    sql, params = f'{alias}province_slug = %s', [scope['province']]
    if scope.get('municipality'):
        sql += f' AND {alias}municipality = %s'
        params.append(scope['municipality'])
    if scope.get('barangays'):       # Barangay EMS (cards / social rows carry the barangay name)
        sql += f' AND {alias}barangay = %s'
        params.append(scope['barangay'])
    return sql, params


def _group(scope, alias=''):
    return alias + {'city': 'barangay', 'prov': 'municipality', 'nat': 'province_slug'}[_level(scope)]


def _key(scope, value):
    return title(value or '') if scope.get('municipality') else value


def social_by_barangay(scope, service=''):
    if not soc.table_exists():
        return {}
    area, params = _area(scope)
    where = [area]
    if service:
        where.append('assistance_type = %s')
        params.append(service)
    group = _group(scope)
    with connections[EXT].cursor() as cur:
        cur.execute(f"SELECT {group}, COUNT(*), COUNT(DISTINCT voter_id), "
                    f"COALESCE(SUM(CASE WHEN status = 'Released' THEN amount END), 0), "
                    f"SUM(status IN ('Pending', 'Approved')), "
                    f"COALESCE(SUM(CASE WHEN status IN ('Pending', 'Approved') THEN amount END), 0) "
                    f"FROM {soc.TABLE} WHERE {' AND '.join(where)} GROUP BY {group}", params)
        out = {}
        for key, records, people, released, open_, open_amount in cur.fetchall():
            b = out.setdefault(_key(scope, key), {'records': 0, 'beneficiaries': 0, 'released': 0.0, 'open': 0,
                                                  'open_amount': 0.0})
            b['records'] += records
            b['beneficiaries'] += people
            b['released'] += float(released or 0)
            b['open'] += int(open_ or 0)
            b['open_amount'] += float(open_amount or 0)
    return out


def activity_by_barangay(scope, days):
    level = _level(scope)
    # Same entries as that level's Transaction List (only that level's own machinery).
    hide, hide_params = audit_level_sql(level)
    if level == 'nat':           # by province: no roll join needed
        area, params = _area(scope, 'a.')
        sql = (f'SELECT a.province_slug, COUNT(*) FROM ems_voter_audit a '
               f'WHERE {area} AND {hide} AND a.created_at >= %s GROUP BY a.province_slug')
    else:
        group = _group(scope, 'v.')
        area, params = ('a.province_slug = %s AND v.municipality = %s', [scope['province'], scope['municipality']]) \
            if level == 'city' else ('a.province_slug = %s', [scope['province']])
        sql = (f'SELECT {group}, COUNT(*) FROM ems_voter_audit a JOIN {voter_table_qualified(scope)} v '
               f'ON v.id = a.voter_id WHERE {area} AND {hide} AND a.created_at >= %s GROUP BY {group}')
    with connections[EXT].cursor() as cur:
        cur.execute(sql, params + hide_params + [_since(days)])
        out = {}
        for key, n in cur.fetchall():
            k = _key(scope, key)
            out[k] = out.get(k, 0) + n
    return out


def households_by_barangay(scope):
    if not hh.tables_exist():
        return {}
    area, params = _area(scope, 'm.')
    level = _level(scope)
    group = 'v.barangay' if level == 'city' else _group(scope, 'm.')
    join = f'JOIN {voter_table_qualified(scope)} v ON v.id = m.voter_id ' if level == 'city' else ''
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT {group}, COUNT(DISTINCT m.voter_id) FROM {hh.MEMBERS} m {join}'
                    f'WHERE {area} GROUP BY {group}', params)
        out = {}
        for key, n in cur.fetchall():
            k = _key(scope, key)
            out[k] = out.get(k, 0) + n
    return out


def services_mix(scope):
    if not soc.table_exists():
        return []
    area, params = _area(scope)
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT assistance_type, COUNT(*) FROM {soc.TABLE} WHERE {area} '
                    'GROUP BY assistance_type ORDER BY COUNT(*) DESC', params)
        return [(t or 'Unspecified', n) for t, n in cur.fetchall()]


def cards_per_month(scope, months=12):
    """(labels, counts) of smart cards issued per month, oldest first, ending this month."""
    today = timezone.localdate()
    keys = []
    y, m = today.year, today.month
    for _ in range(months):
        keys.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    keys.reverse()
    counts = dict.fromkeys(keys, 0)
    if sc.table_exists():
        start = datetime.date(keys[0][0], keys[0][1], 1)
        area, params = _area(scope)
        with connections[EXT].cursor() as cur:
            cur.execute(f'SELECT YEAR(issued_date), MONTH(issued_date), COUNT(*) FROM {sc.TABLE} '
                        f'WHERE {area} AND issued_date >= %s GROUP BY 1, 2', params + [start])
            for yy, mm, n in cur.fetchall():
                if (yy, mm) in counts:
                    counts[(yy, mm)] = n
    labels = [datetime.date(y, m, 1).strftime('%b %Y') for y, m in keys]
    return labels, [counts[k] for k in keys]
