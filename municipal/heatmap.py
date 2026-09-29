"""
Heat Map (Geo-Analytics) — port of MUNICIPAL-EMS heat-map.php, with real numbers only.

PHP's page is entirely sample data (made-up barangays, coordinates, population, budget,
"↑ 4.3% MoM" deltas, purok usage and "AI forecasts"). Here every metric comes from the
EMS tables for the chosen city, per barangay:

  voters / precincts         voter roll (cvl_national, read-only, cached)
  cards / active / pending   muni_smart_cards
  supporters / coordinators / opposition   ems_voter_political (national machinery)
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
from .machinery import EXT, voter_table_qualified
from .text import title

PERIODS = {'7': 'Last 7 days', '30': 'Last 30 days', '90': 'Last 90 days', '365': 'Last 12 months'}
CARD_STATUSES = {'': 'All Card Status', 'active': 'Active', 'pending': 'Pending'}


def _since(days):
    local = timezone.localtime() - datetime.timedelta(days=days)
    return local.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def social_by_barangay(scope, service=''):
    if not soc.table_exists():
        return {}
    where, params = ['province_slug = %s', 'municipality = %s'], [scope['province'], scope['municipality']]
    if service:
        where.append('assistance_type = %s')
        params.append(service)
    with connections[EXT].cursor() as cur:
        cur.execute(f"SELECT barangay, COUNT(*), COUNT(DISTINCT voter_id), "
                    f"COALESCE(SUM(CASE WHEN status = 'Released' THEN amount END), 0), "
                    f"SUM(status IN ('Pending', 'Approved')), "
                    f"COALESCE(SUM(CASE WHEN status IN ('Pending', 'Approved') THEN amount END), 0) "
                    f"FROM {soc.TABLE} WHERE {' AND '.join(where)} GROUP BY barangay", params)
        out = {}
        for brgy, records, people, released, open_, open_amount in cur.fetchall():
            b = out.setdefault(title(brgy or ''), {'records': 0, 'beneficiaries': 0, 'released': 0.0, 'open': 0,
                                                   'open_amount': 0.0})
            b['records'] += records
            b['beneficiaries'] += people
            b['released'] += float(released or 0)
            b['open'] += int(open_ or 0)
            b['open_amount'] += float(open_amount or 0)
    return out


def activity_by_barangay(scope, days):
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT v.barangay, COUNT(*) FROM ems_voter_audit a JOIN {voter_table_qualified(scope)} v '
                    'ON v.id = a.voter_id WHERE a.province_slug = %s AND v.municipality = %s AND a.created_at >= %s '
                    'GROUP BY v.barangay', [scope['province'], scope['municipality'], _since(days)])
        out = {}
        for brgy, n in cur.fetchall():
            out[title(brgy)] = out.get(title(brgy), 0) + n
    return out


def households_by_barangay(scope):
    if not hh.tables_exist():
        return {}
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT v.barangay, COUNT(DISTINCT m.voter_id) FROM {hh.MEMBERS} m '
                    f'JOIN {voter_table_qualified(scope)} v ON v.id = m.voter_id '
                    'WHERE m.province_slug = %s AND m.municipality = %s GROUP BY v.barangay',
                    [scope['province'], scope['municipality']])
        out = {}
        for brgy, n in cur.fetchall():
            out[title(brgy)] = out.get(title(brgy), 0) + n
    return out


def services_mix(scope):
    if not soc.table_exists():
        return []
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT assistance_type, COUNT(*) FROM {soc.TABLE} WHERE province_slug = %s AND municipality = %s '
                    'GROUP BY assistance_type ORDER BY COUNT(*) DESC', [scope['province'], scope['municipality']])
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
        with connections[EXT].cursor() as cur:
            cur.execute(f'SELECT YEAR(issued_date), MONTH(issued_date), COUNT(*) FROM {sc.TABLE} '
                        'WHERE province_slug = %s AND municipality = %s AND issued_date >= %s GROUP BY 1, 2',
                        [scope['province'], scope['municipality'], start])
            for yy, mm, n in cur.fetchall():
                if (yy, mm) in counts:
                    counts[(yy, mm)] = n
    labels = [datetime.date(y, m, 1).strftime('%b %Y') for y, m in keys]
    return labels, [counts[k] for k in keys]
