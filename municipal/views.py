"""
Views — the Python port of the PHP municipal-EMS pages.

Databases:
  * 'rds' (cvl_national, read-only)  — voter roll: raw SQL on the province's cvl_<slug> table.
  * 'ext' (generic_360_db)           — EMS transactions: political machinery + audit trail,
                                       shared with CVL-NATIONAL (see municipal/machinery.py).
  * 'default' (local SQLite)         — Django logins/sessions only.
"""
import csv
import hashlib
import json
import math
import re
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.cache import cache
from django.db import connections
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import ai
from . import geo
from . import heatmap as hm
from . import household as hh
from . import machinery as mach
from . import quickcount as qc
from . import smartcard as sc
from . import social as soc
from . import transactions as tx
from .models import UserProfile
from .regions import province_pretty, region_of, regions_with_provinces, voter_table
from .text import title as _title

CITY_LIST_TTL = 24 * 3600   # municipality list per province
CITY_DATA_TTL = 6 * 3600    # dashboard / list totals per municipality

# Positions offered in a city-level app (regional/provincial are managed nationally).
CITY_ROLE_CODES = ('municipal_coordinator', 'barangay_coordinator', 'supporter', 'opposition')
COORDINATOR_CODES = ('regional_coordinator', 'provincial_coordinator',
                     'municipal_coordinator', 'barangay_coordinator')


def _voter_cursor():
    """A cursor on the read-only RDS voter database."""
    return connections['rds'].cursor()


def _dictfetchall(cursor):
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _ids(csv):
    out = []
    for part in (csv or '').split(','):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _ckey(prefix, slug, municipality=''):
    """Cache key safe for any municipality name (spaces, Ñ, etc.)."""
    digest = hashlib.md5(municipality.encode('utf-8')).hexdigest() if municipality else ''
    return f'{prefix}:{slug}:{digest}'


def _actor(request):
    """Who is making the change — recorded in assigned_by and the audit trail."""
    return request.user.get_username() if request.user.is_authenticated else 'Administrator'


def _ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    ip = fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR', '')
    return ip[:45] or None


def _municipalities(slug):
    """Cached [{value, name, count}] for a whitelisted province slug."""
    table = voter_table(slug)
    if not table:
        return []
    key = _ckey('munis', slug)
    data = cache.get(key)
    if data is None:
        with _voter_cursor() as cur:
            cur.execute(
                f"SELECT municipality, COUNT(*) FROM {table} "
                "WHERE municipality IS NOT NULL AND TRIM(municipality) <> '' "
                "GROUP BY municipality ORDER BY municipality"
            )
            data = [{'value': r[0], 'name': _title(r[0]), 'count': r[1]} for r in cur.fetchall()]
        cache.set(key, data, CITY_LIST_TTL)
    return data


def _scope(request):
    """Resolve the chosen province + city from the session (or None)."""
    m = request.session.get('muni') or {}
    slug = (m.get('province') or '').lower()
    table = voter_table(slug)
    if not table or not m.get('municipality'):
        return None
    return {
        'province': slug,
        'province_name': province_pretty(slug),
        'region': region_of(slug),
        'municipality': m['municipality'],
        'municipality_pretty': _title(m['municipality']),
        'table': table,
    }


def _city_voter(scope, voter_id):
    """A voter's brief if they belong to the scoped city, else None."""
    b = mach.voter_brief(scope['province'], voter_id)
    return b if b and b['municipality'] == scope['municipality'] else None


# ---------------------------------------------------------------------------
# Landing — choose the EMS level (STRAT360-EMS landing.php)
# ---------------------------------------------------------------------------
def landing(request):
    """Home after sign-in: National / Province-Wide / City & Municipal / Barangay.

    A level links out only once it is built; the others show "Coming soon".
    """
    scope = _scope(request)
    last_prov = (request.session.get('prov') or {}).get('province', '')
    last_prov = last_prov if voter_table(last_prov) else ''
    modes = [
        {'key': 'national', 'letter': 'N', 'icon': 'fa-flag', 'scope': 'National Version', 'title': 'Nationwide Strat360 EMS',
         'text': 'Oversee elections across the entire country. Aggregate results from every region and province in a single national command center.',
         'features': ['Country → Region → Province drill-down', 'National quick count & live tallies',
                      'Multi-region rankings & rollups', 'Nationwide heat map & analytics'],
         'cta': 'Enter National Strat360 EMS', 'url': None},
        {'key': 'provincial', 'letter': 'P', 'icon': 'fa-landmark', 'scope': 'Provincial Version', 'title': 'Province-Wide Strat360 EMS',
         'text': 'Monitor an entire province across its cities and municipalities. Drill from province down to barangay-level results in one unified view.',
         'features': ['Region → Province → Municipality → Barangay drill-down', 'Multi-municipality rankings & rollups',
                      'Province-wide quick count & heat map', 'Cross-LGU social services oversight'],
         'cta': 'Enter Provincial Strat360 EMS', 'url': reverse('prov_select'),
         'current': f'Last: {province_pretty(last_prov)}' if last_prov else ''},
        {'key': 'municipal', 'letter': 'M', 'icon': 'fa-city', 'scope': 'Municipality / City Version', 'title': 'City & Municipality Strat360 EMS',
         'text': 'Run the system for a single city or municipality. Focused on barangay and precinct-level data — exactly what local LGUs need on election day.',
         'features': ['Single-LGU dashboard & analytics', 'Barangay & precinct breakdowns',
                      'Voter, supporter & cardholder rolls', 'Beneficiary & social services tracking'],
         'cta': 'Enter Municipal Strat360 EMS', 'url': reverse('select_city'),
         'current': f'Last: {scope["municipality_pretty"]}' if scope else ''},
        {'key': 'barangay', 'letter': 'B', 'icon': 'fa-house-flag', 'scope': 'Barangay Version', 'title': 'Barangay Strat360 EMS',
         'text': 'Manage a single barangay at the grassroots level. Built for purok and household-level data — the closest view of voters and residents on the ground.',
         'features': ['Single-barangay dashboard & analytics', 'Purok & household breakdowns',
                      'Resident, voter & supporter rolls', 'Beneficiary & social services tracking'],
         'cta': 'Enter Barangay Strat360 EMS', 'url': None},
    ]
    live = [m for m in modes if m['url']]
    return render(request, 'municipal/landing.html', {
        'modes': modes,
        'shortcuts': [(m['letter'], m['scope'].split()[0].title()) for m in live],
        'shortcut_urls': {m['letter']: m['url'] for m in live},
    })


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def select_city(request):
    if request.method == 'POST':
        slug = (request.POST.get('province') or '').lower()
        chosen = request.POST.get('municipality', '')
        valid = {m['value'] for m in _municipalities(slug)}
        if voter_table(slug) and chosen in valid:
            request.session['muni'] = {'province': slug, 'municipality': chosen}
            return redirect('dashboard')
        messages.error(request, 'Please choose a valid province and city.')

    current = _scope(request)
    return render(request, 'municipal/select_city.html', {
        'scope': current,
        'regions': regions_with_provinces(),
        'current': {
            'region': current['region'] if current else '',
            'province': current['province'] if current else '',
            'municipality': current['municipality'] if current else '',
        },
    })


def api_municipalities(request):
    """JSON list of cities/municipalities for one province (cascading picker)."""
    slug = (request.GET.get('province') or '').lower()
    if not voter_table(slug):
        return JsonResponse({'ok': False, 'error': 'Unknown province'}, status=400)
    return JsonResponse({'ok': True, 'data': _municipalities(slug)})


def _barangay_totals(scope):
    """{barangay_pretty: {'barangay', 'total', 'precincts'}} for the city (voter roll, cached)."""
    key = _ckey('dash', scope['province'], scope['municipality'])
    rows = cache.get(key)
    if rows is None:
        with _voter_cursor() as cur:
            cur.execute(
                f"SELECT barangay, COUNT(*) voters, COUNT(DISTINCT precinct) precincts "
                f"FROM {scope['table']} WHERE municipality = %s "
                "GROUP BY barangay ORDER BY voters DESC",
                [scope['municipality']],
            )
            rows = _dictfetchall(cur)
        cache.set(key, rows, CITY_DATA_TTL)
    # Merge rows whose raw names differ only by spacing/case (e.g. ' CHANARIAN' vs 'CHANARIAN').
    merged = {}
    for r in rows:
        name = _title(r['barangay'])
        t = merged.setdefault(name, {'barangay': name, 'precincts': 0, 'total': 0})
        t['precincts'] += r['precincts']
        t['total'] += r['voters']
    return merged


def _precinct_counts(scope):
    """[{'barangay', 'precinct', 'voters'}] for the city (voter roll, cached)."""
    key = _ckey('precs', scope['province'], scope['municipality'])
    rows = cache.get(key)
    if rows is None:
        with _voter_cursor() as cur:
            cur.execute(
                f"SELECT barangay, precinct, COUNT(*) FROM {scope['table']} WHERE municipality = %s "
                "GROUP BY barangay, precinct",
                [scope['municipality']],
            )
            merged = {}
            for brgy, prec, n in cur.fetchall():
                k = (_title(brgy), (prec or '').strip())
                merged[k] = merged.get(k, 0) + n
        rows = [{'barangay': b, 'precinct': p, 'voters': n} for (b, p), n in sorted(merged.items())]
        cache.set(key, rows, CITY_DATA_TTL)
    return rows


def dashboard(request):
    """City Analytics — the PHP index.php layout, fed with real data only."""
    scope = _scope(request)
    if not scope:
        return redirect('select_city')

    # Machinery + card counts come live from generic_360_db, so changes show up immediately.
    by_brgy, totals = mach.city_counts(scope)
    cards = sc.barangay_counts(scope) if sc.table_exists() else {}
    top = sorted(_barangay_totals(scope).values(), key=lambda t: t['total'], reverse=True)
    for t in top:
        c = by_brgy.get(t['barangay'], {})
        t['supporters'] = c.get('supporter', 0)
        t['coordinators'] = sum(c.get(code, 0) for code in COORDINATOR_CODES)
        t['opposition'] = c.get('opposition', 0)
        t['coverage'] = (t['supporters'] / t['total'] * 100) if t['total'] else 0
        t['cardholders'] = cards.get(t['barangay'], {}).get('holders', 0)
        t['active_cards'] = cards.get(t['barangay'], {}).get('active', 0)
        t['card_coverage'] = (t['cardholders'] / t['total'] * 100) if t['total'] else 0

    # Sectoral Overview (PHP index.php): members of each main sector per barangay.
    sector_brgy, _ = hh.sector_counts(scope) if hh.tables_exist() else ({}, {})
    main = [s for s, _ in hh.MAIN_SECTORS]
    for t in top:
        t['sectors'] = [sector_brgy.get(t['barangay'], {}).get(s, 0) for s in main]
    sector_totals = [sum(t['sectors'][i] for t in top) for i in range(len(main))]

    precincts = {}
    for p in _precinct_counts(scope):
        precincts.setdefault(p['barangay'], []).append({'precinct': p['precinct'] or '—', 'voters': p['voters']})

    total_voters = sum(t['total'] for t in top)
    total_supporters = totals.get('supporter', 0)
    total_cardholders = sum(t['cardholders'] for t in top)
    ctx = {
        'scope': scope,
        'top': top,
        'by_supporters': sorted(top, key=lambda t: (-t['supporters'], -t['total'])),
        'by_cards': sorted(top, key=lambda t: (-t['cardholders'], -t['total'])),
        'total_voters': total_voters,
        'total_barangays': len(top),
        'total_precincts': sum(t['precincts'] for t in top),
        'total_supporters': total_supporters,
        'supporter_pct': (total_supporters / total_voters * 100) if total_voters else 0,
        'total_coordinators': sum(totals.get(code, 0) for code in COORDINATOR_CODES),
        'total_opposition': totals.get('opposition', 0),
        'total_cardholders': total_cardholders,
        'active_cards': sum(c.get('active', 0) for c in cards.values()),
        'cardholder_pct': (total_cardholders / total_voters * 100) if total_voters else 0,
        'most': _extreme(top, max),
        'least': _extreme(top, min),
        'most_cards': _extreme(top, max, 'cardholders', 'card_coverage'),
        'least_cards': _extreme(top, min, 'cardholders', 'card_coverage'),
        'sectors': [label for _, label in hh.MAIN_SECTORS],
        'sector_totals': sector_totals,
        'has_sectors': any(sector_totals),
        'chart': {
            'labels': [t['barangay'] for t in top],
            'voters': [t['total'] for t in top],
            'supporters': [t['supporters'] for t in top],
            'cardholders': [t['cardholders'] for t in top],
        },
        'precincts': precincts,
    }
    return render(request, 'municipal/dashboard.html', ctx)


def _extreme(top, pick, field='supporters', cov_field='coverage'):
    """Barangay with the most/least of `field` (zero counts included), plus tie count."""
    if not top:
        return None
    value = pick(t[field] for t in top)
    tied = [t for t in top if t[field] == value]
    first = tied[0]
    return {
        'barangay': first['barangay'],
        'value': value,
        'voters': first['total'],
        'coverage': first[cov_field],
        'more': len(tied) - 1,
    }


def _city_meta(scope):
    """Cached unfiltered total + barangay list for the scoped city."""
    key = _ckey('meta', scope['province'], scope['municipality'])
    meta = cache.get(key)
    if meta is None:
        with _voter_cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {scope['table']} WHERE municipality = %s",
                        [scope['municipality']])
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT DISTINCT barangay FROM {scope['table']} WHERE municipality = %s "
                "ORDER BY barangay",
                [scope['municipality']],
            )
            barangays = [r[0] for r in cur.fetchall()]
        meta = {'total': total, 'barangays': barangays}
        cache.set(key, meta, CITY_DATA_TTL)
    return meta


def voters_list(request):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    table, muni = scope['table'], scope['municipality']
    meta = _city_meta(scope)

    f = {k: request.GET.get(k, '').strip() for k in VOTER_FILTERS}
    try:
        page = max(1, int(request.GET.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    per = 15

    ext = mach._schema('ext')
    hh_ready = hh.tables_exist()
    details = f'{ext}.{hh.DETAILS}'

    def on_details(cond, *args):
        """EXISTS over this voter's saved Personal Details."""
        return (f'EXISTS (SELECT 1 FROM {details} d WHERE d.province_slug = %s AND d.voter_id = {table}.id AND {cond})',
                [scope['province'], *args])

    where = ['municipality = %s']
    params = [muni]
    active = []           # (label, value) for the "Filtered Result" panel

    def add(sql, args, label=None, value=None):
        where.append(sql)
        params.extend(args)
        if label:
            active.append((label, value))

    if f['q']:
        for term in f['q'].split():
            add('fullname LIKE %s', [f'%{term}%'])
        active.append(('Voter', f['q']))
    if f['kw']:
        if f['ln']:
            add('fullname LIKE %s', [f['kw'] + '%'], 'Lastname', f['kw'])
        else:
            like = f'%{f["kw"]}%'
            add('(fullname LIKE %s OR address LIKE %s OR precinct LIKE %s)', [like, like, like], 'Keyword', f['kw'])
    if f['barangay']:
        add('barangay = %s', [f['barangay']], 'Barangay', _title(f['barangay']))
    if f['precinct']:
        add('TRIM(precinct) = %s', [f['precinct']], 'Precinct', f['precinct'])
    if f['card'] in ('with', 'without') and sc.table_exists():
        # Like Caloocan's ems.php filter; the card table is only read here.
        exists = (f"EXISTS (SELECT 1 FROM {ext}.{sc.TABLE} c "
                  f"WHERE c.province_slug = %s AND c.voter_id = {table}.id AND c.status IN ('active', 'pending'))")
        add(exists if f['card'] == 'with' else f'NOT {exists}', [scope['province']],
            'Card', 'With card' if f['card'] == 'with' else 'Without card')
    else:
        f['card'] = ''
    if hh_ready:
        bd = hh._valid_date(f['bd'])
        if bd:
            add(*on_details('d.birthdate = %s', bd), 'Birthdate', bd.strftime('%b %d, %Y'))
        else:
            f['bd'] = ''
        if f['religion']:
            add(*on_details('d.religion = %s', f['religion']), 'Religion', f['religion'])
        if f['sector'] in hh.SECTORS:
            add(f'EXISTS (SELECT 1 FROM {ext}.{hh.SECTORS_TABLE} sx WHERE sx.province_slug = %s '
                f'AND sx.voter_id = {table}.id AND sx.sector = %s)', [scope['province'], f['sector']], 'Sector', f['sector'])
        else:
            f['sector'] = ''
        if f['household'] == 'leader':
            add(f'EXISTS (SELECT 1 FROM {ext}.{hh.MEMBERS} m WHERE m.province_slug = %s AND m.voter_id = {table}.id)',
                [scope['province']], 'Household', 'Household leader')
        else:
            f['household'] = ''
        if f['status'] == 'Active':
            flagged = ','.join(['%s'] * len(hh.FLAGGED_STATUSES))
            sql, args = on_details(f'd.status IN ({flagged})', *hh.FLAGGED_STATUSES)
            add(f'NOT {sql}', args, 'Voting status', 'Active')
        elif f['status'] in hh.FLAGGED_STATUSES:
            add(*on_details('d.status = %s', f['status']), 'Voting status', f['status'])
        else:
            f['status'] = ''
        bucket = next((b for b in hh.AGE_BUCKETS if b[0] == f['age']), None)
        if bucket:
            add(*on_details('d.birthdate IS NOT NULL AND TIMESTAMPDIFF(YEAR, d.birthdate, CURDATE()) BETWEEN %s AND %s',
                            bucket[1], bucket[2]), 'Age', bucket[0])
        else:
            f['age'] = ''
    else:
        f.update(bd='', religion='', household='', status='', age='', sector='')
    f['ln'] = 'on' if f['ln'] else ''
    wsql = ' AND '.join(where)
    filtered = len(where) > 1
    # Alphabetical when filtering. Unfiltered, read straight off idx_muni_brgy (municipality,
    # barangay [, id]): ORDER BY id alone made MySQL walk the whole province's primary key
    # (2.2s for Quezon City 5th District vs 0.14s this way).
    order = 'fullname' if filtered else 'barangay, id'

    with _voter_cursor() as cur:
        if filtered:
            cur.execute(f'SELECT COUNT(*) FROM {table} WHERE {wsql}', params)
            total = cur.fetchone()[0]
        else:
            total = meta['total']
        pages = max(1, math.ceil(total / per)) if total else 1
        page = min(page, pages)
        cur.execute(
            f'SELECT id, fullname, barangay, precinct FROM {table} WHERE {wsql} '
            f'ORDER BY {order} LIMIT %s OFFSET %s',
            params + [per, (page - 1) * per],
        )
        rows = _dictfetchall(cur)

    ids = [r['id'] for r in rows]
    holders = sc.holders_among(scope, ids)
    positions = mach.positions_among(scope['province'], ids)
    info = hh.details_among(scope['province'], ids) if hh_ready else {}
    sectors = hh.sectors_among(scope['province'], ids) if hh_ready else {}
    voters = []
    for r in rows:
        d = info.get(r['id'], {})
        voters.append({
            'id': r['id'], 'name': _title(r['fullname']),
            'barangay': _title(r['barangay']),
            'precinct': (r['precinct'] or '').strip(),
            'card': holders.get(r['id']),
            'pos': positions.get(r['id']),
            'sex': {'Male': 'M', 'Female': 'F'}.get(d.get('gender'), ''),
            'birthdate': d.get('birthdate'), 'age': d.get('age'),
            'org': d.get('org') or '', 'status': d.get('status') or '',
            'sectors': sectors.get(r['id'], []),
        })

    # KPIs + charts: the roll has no status/age/sector data; saved Personal Details do.
    summary = hh.city_details_summary(scope)      # empty counts when the table isn't set up
    flagged_by_brgy = hh.flagged_by_barangay(scope) if hh_ready else {}
    brgy_rows = sorted(_barangay_totals(scope).values(), key=lambda t: -t['total'])
    for b in brgy_rows:
        b['active'] = b['total'] - flagged_by_brgy.get(b['barangay'], 0)
    flagged = summary['flagged']

    _, by_sector = hh.sector_counts(scope) if hh_ready else ({}, {})
    main = [s for s, _ in hh.MAIN_SECTORS]

    precincts = _precinct_counts(scope)
    if f['barangay']:
        precincts = [p for p in precincts if p['barangay'] == _title(f['barangay'])]
    precinct_options = sorted({p['precinct'] for p in precincts if p['precinct']})

    keep = {k: v for k, v in f.items() if v}
    base_qs = urlencode(keep)
    ctx = {
        'scope': scope,
        'f': f,
        'active_filters': active,
        'filtered': filtered,
        'voters': voters,
        'total': total,
        'page': page,
        'pages': pages,
        'offset': (page - 1) * per,
        'showing_from': (page - 1) * per + 1 if total else 0,
        'showing_to': min(page * per, total),
        'barangays': [{'raw': b, 'pretty': _title(b)} for b in meta['barangays']],
        'precinct_options': precinct_options,
        'religions': sorted(set(RELIGIONS) | set(summary['religions'])),
        'sector_options': hh.SECTORS,
        'sector_members': sum(s['total'] for s in by_sector.values()),
        'statuses': ('Active',) + hh.FLAGGED_STATUSES,
        'age_buckets': [b[0] for b in hh.AGE_BUCKETS],
        'hh_ready': hh_ready,
        'kpi': {
            'total': meta['total'], 'brgys': len(meta['barangays']),
            'active': meta['total'] - flagged,
            'active_pct': ((meta['total'] - flagged) / meta['total'] * 100) if meta['total'] else 0,
            'deactivated': flagged, 'saved': summary['saved'],
        },
        'top_active': brgy_rows[:8],
        'max_active': max((b['active'] for b in brgy_rows), default=0),
        'with_birthdate': summary['with_birthdate'],
        'chart': {
            'brgy_labels': [b['barangay'] for b in brgy_rows[:10]],
            'brgy_total': [b['total'] for b in brgy_rows[:10]],
            'brgy_active': [b['active'] for b in brgy_rows[:10]],
            'status_labels': ['Active'] + list(hh.FLAGGED_STATUSES),
            'status_values': [meta['total'] - flagged] + [summary['status'].get(s, 0) for s in hh.FLAGGED_STATUSES],
            'age_labels': list(summary['ages']),
            'age_values': list(summary['ages'].values()),
            'sector_labels': [label for _, label in hh.MAIN_SECTORS],
            'sector_male': [by_sector.get(s, {}).get('Male', 0) for s in main],
            'sector_female': [by_sector.get(s, {}).get('Female', 0) for s in main],
        },
        'prev_url': f'?{base_qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{base_qs}&page={page + 1}' if page < pages else '',
    }
    return render(request, 'municipal/voters_list.html', ctx)


VOTER_FILTERS = ('q', 'kw', 'ln', 'bd', 'card', 'sector', 'religion', 'household', 'status', 'age', 'barangay', 'precinct')
# PHP ems.php's religion list; religions saved in Personal Details are added to it.
RELIGIONS = ('Aglipay', 'Christian', 'COC', 'Iglesia ni Cristo', 'Islam', "Jehovah's Witness", 'Jesus Is Lord',
             'LDS', 'Methodist', 'Mormons', 'Protestant', 'Roman Catholic', 'Seventh Day Adventist')


def voter_profile(request, voter_id):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')

    with _voter_cursor() as cur:
        cur.execute(
            f"SELECT id, fullname, address, district, municipality, barangay, precinct "
            f"FROM {scope['table']} WHERE id = %s AND municipality = %s",
            [voter_id, scope['municipality']],
        )
        rows = _dictfetchall(cur)
    if not rows:
        messages.error(request, 'Voter not found in this city.')
        return redirect('voters_list')
    r = rows[0]

    voter = {
        'id': r['id'],
        'name': _title(r['fullname']),
        'voter_id': f'STR-{r["id"]:07d}',
        'address': _title(r['address']),
        'barangay': _title(r['barangay']),
        'city': scope['municipality_pretty'],
        'district': _title(r['district']),
        'precinct': (r['precinct'] or '').strip(),
    }

    pol = mach.payload(scope['province'], voter_id)
    city_roles = [pol['roles'][c] for c in CITY_ROLE_CODES if c in pol['roles']]

    def in_city(b):
        return (b and b.get('province_slug') == scope['province']
                and b.get('municipality') == scope['municipality'])

    upline = pol['upline']
    if upline:
        upline['linkable'] = in_city(upline)
    for d in pol['downlines']:
        d['linkable'] = in_city(d)

    social_records = soc.for_voter(scope['province'], voter_id) if soc.table_exists() else []
    card = sc.card_for(scope['province'], voter_id)
    hh_ready = hh.tables_exist()
    members = hh.members_for(scope, voter_id) if hh_ready else []

    ctx = {
        'scope': scope,
        'voter': voter,
        'hh_ready': hh_ready,
        'pd': _personal_details(voter, hh.details_for(scope['province'], voter_id) if hh_ready else None,
                                card, social_records),
        'members': members,
        'member_of': hh.household_of(scope, voter_id) if hh_ready else None,
        'choices': {'genders': hh.GENDERS, 'civil': hh.CIVIL_STATUSES, 'education': hh.EDUCATION,
                    'income': hh.INCOME, 'status': hh.VOTER_STATUSES,
                    'relationships': hh.RELATIONSHIPS, 'scholar': hh.SCHOLAR, 'sectors': hh.SECTORS},
        'my_sectors': hh.sectors_for(scope['province'], voter_id) if hh_ready else [],
        # For the map + QR (place names only; the voter roll has no coordinates).
        'map_place': {
            'name': voter['name'], 'voter_id': voter['voter_id'], 'precinct': voter['precinct'],
            'barangay': voter['barangay'], 'province': scope['province_name'],
            'city': re.sub(r'\s+\d+(st|nd|rd|th)\s+district$', '', voter['city'], flags=re.I),
            'card_number': card['card_number'] if card else '',
        },
        'pol': pol,
        'city_roles': city_roles,
        'audit': mach.audit_for(scope['province'], voter_id),
        'social_records': social_records,
        'card': card,
        'card_statuses': sc.STATUS_LABELS,
        'social_statuses': soc.STATUSES,
    }
    return render(request, 'municipal/voter_profile.html', ctx)


def _personal_details(voter, saved, card, social_records):
    """Values for the Personal Details form.

    Saved details (muni_voter_details) win. Blank fields are pre-filled from what the EMS
    already knows about the voter — their smart card, then their newest social-service
    application — with a hint naming the source; saving the form stores them. The voter
    roll itself has none of these details.
    """
    saved = saved or {}
    values = {f: saved.get(f) for f, _, _ in hh.DETAIL_FIELDS}
    hints = {}

    def suggest(field, value, source):
        if value and not values.get(field):
            values[field], hints[field] = value, source

    if card:
        src = f'from smart card {card["card_number"]}'
        suggest('gender', card['gender_label'], src)
        suggest('civil_status', card['civil_status'], src)
    for s in social_records:
        src = f'from social service #SS{s["id"]:03d}'
        suggest('birthdate', s.get('birthdate'), src)
        suggest('gender', s.get('gender'), src)
        suggest('civil_status', s.get('civil_status'), src)
        suggest('mobile', s.get('contact_number'), src)
        street = ', '.join(p for p in (s.get('street'), s.get('purok')) if p)
        if street:
            suggest('home_address', f'{street}, Brgy. {voter["barangay"]}, {voter["city"]}', src)
    if voter['address']:
        suggest('home_address', f'{voter["address"]}, {voter["city"]}', 'from the voter roll')

    age = None
    bd = values.get('birthdate')
    if bd:
        today = timezone.localdate()
        age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
    return {'values': values, 'hints': hints, 'age': age,
            'updated_at': saved.get('updated_at'), 'updated_by': saved.get('updated_by')}


@require_POST
def voter_details(request, voter_id):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    voter = _city_voter(scope, voter_id)
    if not voter:
        messages.error(request, 'Voter not found in this city.')
        return redirect('voters_list')
    try:
        changed = hh.save_details(scope, voter, request.POST, _actor(request), _ip(request))
        messages.success(request, f'Personal details saved ({", ".join(changed).lower()}).' if changed
                         else 'No changes to save.')
    except hh.HouseholdError as e:
        messages.error(request, str(e))
    return redirect('voter_profile', voter_id=voter_id)


@require_POST
def household_add(request, voter_id):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    voter = _city_voter(scope, voter_id)
    if not voter:
        messages.error(request, 'Voter not found in this city.')
        return redirect('voters_list')
    try:
        name = hh.add_member(scope, voter, request.POST, _actor(request), _ip(request))
        messages.success(request, f'{name} added to the household.')
    except hh.HouseholdError as e:
        messages.error(request, str(e))
    return redirect('voter_profile', voter_id=voter_id)


@require_POST
def household_remove(request, voter_id, member_id):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    voter = _city_voter(scope, voter_id)
    if not voter:
        messages.error(request, 'Voter not found in this city.')
        return redirect('voters_list')
    try:
        name = hh.remove_member(scope, voter, member_id, _actor(request), _ip(request))
        messages.success(request, f'{name} removed from the household.')
    except hh.HouseholdError as e:
        messages.error(request, str(e))
    return redirect('voter_profile', voter_id=voter_id)


# ---------------------------------------------------------------------------
# Machinery: JSON endpoints (type-ahead) + one POST action endpoint
# ---------------------------------------------------------------------------
def api_search_voters(request):
    """Type-ahead over the current city's roll (surname-first prefix).

    With ?down_of=<id>, results are limited to the area that voter's downlines
    must come from: their barangay if they are a barangay coordinator (rank 4).
    """
    scope = _scope(request)
    if not scope:
        return JsonResponse({'ok': False, 'error': 'No city selected'}, status=400)
    q = request.GET.get('q', '').strip()
    exclude = request.GET.get('exclude', '')
    down_of = request.GET.get('down_of', '')
    if len(q) < 2:
        return JsonResponse({'ok': True, 'data': []})

    sql = (f"SELECT id, fullname, barangay FROM {scope['table']} "
           'WHERE municipality = %s AND fullname LIKE %s')
    params = [scope['municipality'], q + '%']
    if exclude.isdigit():
        sql += ' AND id <> %s'
        params.append(int(exclude))
    if down_of.isdigit():
        pol = mach.payload(scope['province'], int(down_of))
        me = _city_voter(scope, int(down_of))
        if pol['rank'] is not None and pol['rank'] >= 4 and me:
            sql += ' AND barangay = %s'
            params.append(me['barangay'])
    sql += ' ORDER BY fullname LIMIT 15'
    with _voter_cursor() as cur:
        cur.execute(sql, params)
        rows = _dictfetchall(cur)
    data = [{'id': r['id'], 'name': _title(r['fullname']), 'barangay': _title(r['barangay'])}
            for r in rows]
    return JsonResponse({'ok': True, 'data': data})


def api_superiors(request):
    """Voters holding `rank` that the given voter may report to (same province/city/barangay)."""
    scope = _scope(request)
    if not scope:
        return JsonResponse({'ok': False, 'error': 'No city selected'}, status=400)
    try:
        rank = int(request.GET.get('rank', 0))
        vid = int(request.GET.get('voter', 0))
    except (TypeError, ValueError):
        return JsonResponse({'ok': True, 'data': []})
    me = _city_voter(scope, vid)
    if not me:
        return JsonResponse({'ok': False, 'error': 'Voter not found'}, status=404)
    return JsonResponse({'ok': True, 'data': mach.superiors(scope, me, rank)})


@require_POST
def political(request, voter_id):
    """All machinery changes for one voter. POST action = assign | unassign |
    add_down | remove_down | set_upline (same actions as CVL-NATIONAL's API)."""
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    if not _city_voter(scope, voter_id):
        messages.error(request, 'Voter not found in this city.')
        return redirect('voters_list')

    prov, actor, ip = scope['province'], _actor(request), _ip(request)
    action = request.POST.get('action', '')
    notes = []

    try:
        if action == 'assign':
            code = request.POST.get('role', '')
            if code not in CITY_ROLE_CODES:
                raise mach.MachineryError('Choose a position.')
            role = mach.assign(prov, voter_id, code, actor, ip)
            messages.success(request, f'Assigned as {role["pretty"]}.')

            up = request.POST.get('upline_id', '')
            if up.isdigit():
                mach.set_upline(prov, voter_id, prov, int(up), actor, ip)
                messages.success(request, 'Superior set.')
            for sid in _ids(request.POST.get('subordinates', '')):
                notes.append(_add_one(scope, voter_id, sid, actor, ip))

        elif action == 'unassign':
            freed = mach.unassign(prov, voter_id, actor, ip)
            messages.success(request, 'Position removed' +
                             (f'; {freed} downline(s) detached.' if freed else '.'))

        elif action == 'add_down':
            for sid in _ids(request.POST.get('subordinates', '')):
                notes.append(_add_one(scope, voter_id, sid, actor, ip))

        elif action == 'remove_down':
            d_prov = (request.POST.get('down_province') or prov).lower()
            d_id = request.POST.get('down_id', '')
            if not d_id.isdigit():
                raise mach.MachineryError('Invalid voter.')
            if mach.remove_down(prov, voter_id, d_prov, int(d_id), actor, ip):
                messages.success(request, 'Downline detached.')

        elif action == 'set_upline':
            up = request.POST.get('upline_id', '')
            if not up.isdigit():
                raise mach.MachineryError('Pick a superior.')
            changed = mach.set_upline(prov, voter_id, prov, int(up), actor, ip)
            messages.success(request, 'Superior updated.' if changed else 'Superior unchanged.')

        else:
            raise mach.MachineryError('Unknown action.')
    except mach.MachineryError as e:
        messages.error(request, str(e))

    added = [n for n in notes if n[0]]
    failed = [n for n in notes if not n[0]]
    if added:
        messages.success(request, f'Added {len(added)} downline(s).')
    for _, msg in failed:
        messages.warning(request, msg)
    return redirect('voter_profile', voter_id=voter_id)


def _add_one(scope, voter_id, sid, actor, ip):
    """Attach one downline; returns (ok, message) so a batch can report partial success."""
    if sid == voter_id or not _city_voter(scope, sid):
        return False, f'Voter #{sid} is not in this city — skipped.'
    try:
        mach.add_down(scope['province'], voter_id, scope['province'], sid, actor, ip)
        return True, ''
    except mach.MachineryError as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Social services — Caloocan format, table generic_360_db.muni_social_services
# ---------------------------------------------------------------------------
SOCIAL_FORM_FIELDS = (
    'assistance_type', 'amount', 'status', 'date_requested', 'purpose', 'agency', 'program', 'remarks',
    'beneficiary', 'birthdate', 'gender', 'civil_status', 'contact_number',
    'region', 'province', 'city_municipality', 'barangay', 'purok', 'street',
    'claimed_by_other', 'claimant', 'claimant_relationship', 'claimant_contact',
)


def _safe_next(request, default):
    nxt = request.POST.get('next', '')
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect(default)


def social_list(request):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    if not soc.table_exists():
        messages.error(request, 'Social services table not set up yet — run: manage.py social_setup')
        return redirect('dashboard')
    f = {k: request.GET.get(k, '').strip() for k in ('q', 'type', 'status', 'from', 'to')}
    try:
        page = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    rows, total, total_amount, page, pages = soc.city_records(
        scope, f['q'], f['type'], f['status'], f['from'], f['to'], page)

    qs = urlencode({k: v for k, v in f.items() if v})
    ctx = {
        'scope': scope,
        'summary': soc.city_summary(scope),
        'types': list(soc.ASSISTANCE_TYPES),
        'statuses': soc.STATUSES,
        'rows': rows,
        'total': total,
        'total_amount': total_amount,
        'page': page,
        'pages': pages,
        'filters': f,
        'filtered': any(f.values()),
        'prev_url': f'?{qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{qs}&page={page + 1}' if page < pages else '',
    }
    return render(request, 'municipal/social_list.html', ctx)


def social_new(request):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    if not soc.table_exists():
        messages.error(request, 'Social services table not set up yet — run: manage.py social_setup')
        return redirect('dashboard')

    raw = request.POST.get('voter_id') or request.GET.get('voter') or ''
    voter = _city_voter(scope, int(raw)) if raw.isdigit() else None

    if request.method == 'POST':
        if not voter:
            messages.error(request, 'Pick the beneficiary from the suggestions so the record '
                                    'is linked to their voter profile.')
        else:
            try:
                new_id = soc.record(scope, voter, request.POST, _actor(request), _ip(request))
                messages.success(request, f'Social service #{new_id} recorded.')
                return redirect('social_list')
            except soc.SocialError as e:
                messages.error(request, str(e))
        values = {k: request.POST.get(k, '') for k in SOCIAL_FORM_FIELDS}
    else:
        values = {k: '' for k in SOCIAL_FORM_FIELDS}
        values.update({'status': soc.DEFAULT_STATUS,
                       'date_requested': timezone.localdate().isoformat()})
        if voter:   # pre-fill from the voter roll
            values.update({
                'beneficiary': voter['name'],
                'region': scope['region'] or '',
                'province': scope['province_name'],
                'city_municipality': scope['municipality_pretty'],
                'barangay': voter['barangay_pretty'],
                'street': _title(voter.get('address')),
            })

    return render(request, 'municipal/social_form.html', {
        'scope': scope, 'voter': voter, 'v': values,
        'types': soc.ASSISTANCE_TYPES, 'statuses': soc.STATUSES,
        'genders': soc.GENDERS, 'civil_statuses': soc.CIVIL_STATUSES,
        'agencies': soc.AGENCIES, 'programs': soc.PROGRAMS, 'relationships': soc.RELATIONSHIPS,
        'today': timezone.localdate().isoformat(),
    })


@require_POST
def social_status(request, record_id):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    status = request.POST.get('status', '')
    try:
        if soc.set_status(scope, record_id, status, _actor(request), _ip(request)):
            messages.success(request, f'Social service #{record_id} marked {status}.')
    except soc.SocialError as e:
        messages.error(request, str(e))
    return _safe_next(request, 'social_list')


# ---------------------------------------------------------------------------
# Smart cards — Caloocan format, table generic_360_db.muni_smart_cards
# ---------------------------------------------------------------------------
def cards_list(request):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    if not sc.table_exists():
        messages.error(request, 'Smart card table not set up yet — run: manage.py smartcard_setup')
        return redirect('dashboard')
    f = {k: request.GET.get(k, '').strip() for k in ('q', 'barangay', 'status', 'gender', 'service')}
    try:
        page = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    rows, total, page, pages = sc.city_cards(
        scope, f['q'], f['barangay'], f['status'], f['gender'], f['service'], page)

    # Barangay ranking (Caloocan's table) + coverage of registered voters.
    voters = _barangay_totals(scope)
    ranking = []
    for brgy, c in sc.barangay_counts(scope).items():
        v = voters.get(brgy, {}).get('total', 0)
        ranking.append({**c, 'barangay': brgy, 'voters': v,
                        'active_pct': (c['active'] / c['holders'] * 100) if c['holders'] else 0,
                        'coverage': (c['holders'] / v * 100) if v else 0})
    ranking.sort(key=lambda r: -r['holders'])

    qs = urlencode({k: v for k, v in f.items() if v})
    return render(request, 'municipal/cards_list.html', {
        'scope': scope,
        'summary': sc.city_summary(scope),
        'ranking': ranking,
        'rows': rows,
        'total': total,
        'page': page,
        'pages': pages,
        'filters': f,
        'filtered': any(f.values()),
        'barangays': sc.city_barangays(scope),
        'services': sc.SERVICES,
        'statuses': sc.STATUS_LABELS,
        'genders': sc.GENDERS,
        'prev_url': f'?{qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{qs}&page={page + 1}' if page < pages else '',
    })


def card_new(request):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    if not sc.table_exists():
        messages.error(request, 'Smart card table not set up yet — run: manage.py smartcard_setup')
        return redirect('dashboard')

    raw = request.POST.get('voter_id') or request.GET.get('voter') or ''
    voter = _city_voter(scope, int(raw)) if raw.isdigit() else None
    existing = sc.card_for(scope['province'], voter['id']) if voter else None

    if request.method == 'POST':
        if not voter:
            messages.error(request, 'Pick the cardholder from the suggestions.')
        else:
            try:
                number = sc.issue(scope, voter, request.POST, _actor(request), _ip(request))
                messages.success(request, f'Smart card {number} issued to {voter["name"]}.')
                return redirect('voter_profile', voter_id=voter['id'])
            except sc.CardError as e:
                messages.error(request, str(e))
        values = {k: request.POST.get(k, '') for k in ('service', 'gender', 'civil_status', 'status', 'issued_date')}
    else:
        values = {'service': '', 'gender': '', 'civil_status': '', 'status': 'active',
                  'issued_date': timezone.localdate().isoformat()}

    return render(request, 'municipal/card_form.html', {
        'scope': scope, 'voter': voter, 'existing': existing, 'v': values,
        'services': sc.SERVICES, 'genders': sc.GENDERS, 'civil_statuses': sc.CIVIL_STATUSES,
        'today': timezone.localdate().isoformat(),
    })


# ---------------------------------------------------------------------------
# Quick Count — real roll + cardholders, simulated attendance (see quickcount.py)
# ---------------------------------------------------------------------------
def quick_count(request):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')

    brgys = qc.barangays(_barangay_totals(scope))
    registered = sum(b['registered'] for b in brgys)
    checked = sum(b['checked'] for b in brgys)
    rate = (checked / registered * 100) if registered else 0
    hourly = qc.hourly(checked)
    so_far = hourly[:qc.SNAPSHOT_HOUR_IDX + 1]
    peak = max(range(len(so_far)), key=lambda i: so_far[i]) if checked else 0
    minutes = qc.SNAPSHOT_HOUR_IDX * 60
    cards = (qc.cardholders(scope, rate) if sc.table_exists()
             else {'holders': 0, 'checked': 0, 'flagged': 0, 'by_service': [], 'recent': [], 'pool': []})

    return render(request, 'municipal/quick_count.html', {
        'scope': scope,
        'brgys': brgys,
        'registered': registered,
        'checked': checked,
        'remaining': registered - checked,
        'rate': rate,
        'target': qc.TARGET_PCT,
        'precinct_count': sum(b['precincts'] for b in brgys),
        'last_hour': so_far[-1] if so_far else 0,
        'avg_hour': round(checked / len(so_far)) if so_far else 0,
        'avg_min': round(checked / minutes) if minutes else 0,
        'peak_label': qc.HOURS[peak],
        'peak_value': so_far[peak] if so_far else 0,
        'snapshot': qc.HOURS[qc.SNAPSHOT_HOUR_IDX],
        'top_precincts': qc.top_precincts(scope, _ckey('qcprec60', scope['province'], scope['municipality'])),
        'cards': cards,
        'chart': {
            'hours': qc.HOURS, 'hourly': hourly, 'snapshot_idx': qc.SNAPSHOT_HOUR_IDX,
            'checked': checked, 'registered': registered,
            'services': [s['service'] for s in cards['by_service']],
            'service_checked': [s['checked'] for s in cards['by_service']],
            'barangays': [b['barangay'] for b in brgys],
            'pool': cards['pool'],
        },
    })


# ---------------------------------------------------------------------------
# AI Analytics — PHP ai-analytics.php with Gemini; the city's aggregates only (see ai.py)
# ---------------------------------------------------------------------------
def ai_analytics(request):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    return render(request, 'municipal/ai_analytics.html', {
        'scope': scope,
        'configured': ai.configured(),
        'model': settings.GEMINI_MODEL,
        'places': sorted(_barangay_totals(scope)) + [scope['municipality_pretty']],
    })


@require_POST
def api_ai(request):
    scope = _scope(request)
    if not scope:
        return JsonResponse({'error': 'No city selected.'}, status=400)
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    query = str(body.get('query', '')).strip()[:ai.MAX_QUERY]
    intent = body.get('intent') if body.get('intent') in ai.INTENTS else 'general'
    if not query:
        return JsonResponse({'error': 'Type a question first.'}, status=400)
    try:
        answer = ai.ask(ai.snapshot(scope, _barangay_totals(scope)), query, intent)
    except ai.AIError as e:
        return JsonResponse({'error': str(e), 'detail': e.detail}, status=502)
    return JsonResponse({'ok': True, 'intent': intent, 'query': query, 'result': answer})


# ---------------------------------------------------------------------------
# User Profile — PHP profile.php layout, for the logged-in account
# ---------------------------------------------------------------------------
PROFILE_MODULES = [   # (label, Font Awesome icon, url name or None for Django admin)
    ('City Analytics', 'fa-chart-line', 'dashboard'), ('Barangay Heat Map', 'fa-map', 'heat_map'),
    ('Voters List', 'fa-user-tie', 'voters_list'), ('Smart Card Issuance', 'fa-id-card', 'cards_list'),
    ('Social Services', 'fa-hand-holding-heart', 'social_list'), ('Quick Count', 'fa-tower-broadcast', 'quick_count'),
    ('Transaction List', 'fa-list-check', 'transactions_list'), ('User Management (Admin)', 'fa-users-gear', None),
]


def user_profile(request):
    scope = _scope(request)
    user = request.user
    prof, _ = UserProfile.objects.get_or_create(user=user)
    if request.method == 'POST':
        data = {k: request.POST.get(k, '').strip() for k in ('first_name', 'last_name', 'email', 'phone')}
        errors = []
        if data['email']:
            try:
                validate_email(data['email'])
            except ValidationError:
                errors.append('Enter a valid email address.')
        if data['phone'] and not re.fullmatch(r'[0-9+()\-\s]{7,32}', data['phone']):
            errors.append('Phone: use digits, spaces, +, - or ( ) only.')
        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            user.first_name, user.last_name, user.email = data['first_name'][:150], data['last_name'][:150], data['email'][:254]
            user.save(update_fields=['first_name', 'last_name', 'email'])
            prof.phone = data['phone'][:32]
            prof.save()
            messages.success(request, 'Account details saved.')
            return redirect('user_profile')
    role = 'Superuser' if user.is_superuser else ('Staff' if user.is_staff else 'User')
    initials = ''.join(p[0] for p in (user.get_full_name() or user.get_username()).split()[:2]).upper()
    return render(request, 'municipal/profile.html', {
        'scope': scope,
        'prof': prof,
        'role': role,
        'initials': initials or '?',
        'password_form': PasswordChangeForm(user),
        'modules': [(label, icon, url, (user.is_staff if url is None else True)) for label, icon, url in PROFILE_MODULES],
        'activity': tx.actor_activity(user.get_username()),
    })


@require_POST
def user_password(request):
    form = PasswordChangeForm(request.user, request.POST)
    if form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)       # stay logged in on this device
        messages.success(request, 'Password changed.')
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
    return redirect('user_profile')


# ---------------------------------------------------------------------------
# Heat Map — PHP heat-map.php layout with real per-barangay numbers (see heatmap.py)
# ---------------------------------------------------------------------------
def heat_map(request):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    days = request.GET.get('period', '30')
    days = days if days in hm.PERIODS else '30'
    service = request.GET.get('service', '')
    service = service if service in soc.ASSISTANCE_TYPES else ''
    card_status = request.GET.get('cards', '')
    card_status = card_status if card_status in hm.CARD_STATUSES else ''

    totals = _barangay_totals(scope)
    raw_names = {}
    for raw in _city_meta(scope)['barangays']:
        raw_names.setdefault(_title(raw), raw)
    locs = geo.locations(scope)
    by_brgy, role_totals = mach.city_counts(scope)
    cards = sc.barangay_counts(scope) if sc.table_exists() else {}
    social = hm.social_by_barangay(scope, service)
    sectors, _ = hh.sector_counts(scope) if hh.tables_exist() else ({}, {})
    households = hm.households_by_barangay(scope)
    activity = hm.activity_by_barangay(scope, int(days))

    lgus = []
    for name, t in sorted(totals.items(), key=lambda kv: -kv[1]['total']):
        c, m, s = cards.get(name, {}), by_brgy.get(name, {}), social.get(name, {})
        holders = c.get(card_status, 0) if card_status else c.get('holders', 0)
        supporters = m.get('supporter', 0)
        sec = sectors.get(name, {})
        loc = locs.get(name)
        lgus.append({
            'name': name, 'raw': raw_names.get(name, name),
            'lat': loc['lat'] if loc else None, 'lng': loc['lng'] if loc else None,
            'approx': bool(loc and loc['source'] == 'approx'),
            'voters': t['total'], 'precincts': t['precincts'],
            'cards': holders, 'active_cards': c.get('active', 0), 'pending_cards': c.get('pending', 0),
            'card_pct': round(holders / t['total'] * 100, 2) if t['total'] else 0,
            'supporters': supporters, 'coverage': round(supporters / t['total'] * 100, 2) if t['total'] else 0,
            'coordinators': sum(m.get(code, 0) for code in COORDINATOR_CODES), 'opposition': m.get('opposition', 0),
            'beneficiaries': s.get('beneficiaries', 0), 'records': s.get('records', 0),
            'released': round(s.get('released', 0)), 'open': s.get('open', 0), 'open_amount': round(s.get('open_amount', 0)),
            'sector_members': sum(sec.values()),
            'top_sectors': sorted(sec.items(), key=lambda kv: -kv[1])[:5],
            'households': households.get(name, 0),
            'activity': activity.get(name, 0),
        })

    def top(key, n=5):
        return sorted(lgus, key=lambda l: -l[key])[:n]

    if request.GET.get('export') == 'csv':
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        slug = re.sub(r'[^a-z0-9]+', '-', scope['municipality_pretty'].lower()).strip('-')
        resp['Content-Disposition'] = f'attachment; filename="heatmap-{slug}-{timezone.localdate():%Y%m%d}.csv"'
        resp.write('﻿')
        w = csv.writer(resp)
        w.writerow(['Barangay', 'Latitude', 'Longitude', 'Location', 'Registered Voters', 'Precincts',
                    'Smart Cardholders', 'Active Cards', 'Pending Cards', 'Supporters', 'Supporter Coverage %',
                    'Coordinators', 'Opposition', 'Beneficiaries', 'Social Records', 'Released (PHP)',
                    'Sector Members', 'Households', f'Activity ({hm.PERIODS[days]})'])
        for l in lgus:
            w.writerow([l['name'], l['lat'] or '', l['lng'] or '',
                        'approximate' if l['approx'] else ('OpenStreetMap' if l['lat'] else 'not located'),
                        l['voters'], l['precincts'], l['cards'], l['active_cards'], l['pending_cards'], l['supporters'],
                        l['coverage'], l['coordinators'], l['opposition'], l['beneficiaries'], l['records'], l['released'],
                        l['sector_members'], l['households'], l['activity']])
        return resp

    mix = hm.services_mix(scope)
    month_labels, month_counts = hm.cards_per_month(scope)
    by_released = sorted(lgus, key=lambda l: -(l['released'] + l['open_amount']))[:8]
    feed = tx.page(scope, {}, 1)[0][:8] if lgus else []
    located = [l for l in lgus if l['lat'] is not None]
    qs = urlencode({k: v for k, v in (('period', days), ('service', service), ('cards', card_status)) if v})
    return render(request, 'municipal/heat_map.html', {
        'scope': scope,
        'lgus': lgus,
        'located': len(located),
        'approx': sum(1 for l in lgus if l['approx']),
        'missing': len(lgus) - len(located),
        'kpi': {
            'voters': sum(l['voters'] for l in lgus),
            'cards': sum(l['cards'] for l in lgus),
            'active_cards': sum(l['active_cards'] for l in lgus),
            'supporters': sum(l['supporters'] for l in lgus),
            'coordinators': sum(role_totals.get(code, 0) for code in COORDINATOR_CODES),
            'beneficiaries': sum(l['beneficiaries'] for l in lgus),
            'records': sum(l['records'] for l in lgus),
            'released': sum(l['released'] for l in lgus),
            'open': sum(l['open'] for l in lgus),
            'activity': sum(l['activity'] for l in lgus),
        },
        'filters': {'period': days, 'service': service, 'cards': card_status},
        'periods': hm.PERIODS, 'services': list(soc.ASSISTANCE_TYPES), 'card_statuses': hm.CARD_STATUSES,
        'period_label': hm.PERIODS[days],
        'export_url': f'?{qs}&export=csv' if qs else '?export=csv',
        'top_voters': top('voters'), 'top_cards': top('cards'), 'top_benef': top('beneficiaries'),
        'top_coverage': top('coverage'), 'top_sectors': top('sector_members'),
        'feed': feed,
        'chart': {
            'lgus': lgus,
            'month_labels': month_labels, 'month_counts': month_counts,
            'mix_labels': [t for t, _ in mix], 'mix_values': [n for _, n in mix],
            'rel_labels': [l['name'] for l in by_released],
            'rel_released': [l['released'] for l in by_released],
            'rel_open': [l['open_amount'] for l in by_released],
        },
    })


@require_POST
def heat_map_locate(request):
    """Staff: look up the city's not-yet-located barangays on OpenStreetMap (~2 s each)."""
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    if not request.user.is_staff:
        messages.error(request, 'Only staff can locate barangays.')
        return redirect('heat_map')
    names = list(_barangay_totals(scope))
    try:
        found, approx, left = geo.locate(scope, names, scope['province_name'], limit=25, log=lambda *_: None)
        msg = f'Located {found} barangay{"s" if found != 1 else ""} on OpenStreetMap'
        if approx:
            msg += f'; {approx} not found and placed near the town centre'
        if left:
            msg += f'; {left} more still to locate — click again'
        messages.success(request, msg + '.')
    except (OSError, RuntimeError) as e:
        messages.error(request, f'Could not reach OpenStreetMap: {e}')
    return redirect('heat_map')


# ---------------------------------------------------------------------------
# Transaction List — the real audit trail (ems_voter_audit) for the city
# ---------------------------------------------------------------------------
def transactions_list(request):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    f = {k: request.GET.get(k, '').strip() for k in ('q', 'action', 'actor', 'from', 'to')}

    if request.GET.get('export') == 'csv':
        rows = tx.export_rows(scope, f)
        stamp = timezone.localtime().strftime('%Y%m%d-%H%M')
        slug = re.sub(r'[^a-z0-9]+', '-', scope['municipality_pretty'].lower()).strip('-')
        resp = HttpResponse(content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="transactions-{slug}-{stamp}.csv"'
        resp.write('﻿')    # so Excel opens it as UTF-8 (Ñ, ₱)
        w = csv.writer(resp)
        w.writerow(['Tx ID', 'When (Manila)', 'Encoder', 'Category', 'Action', 'Voter ID', 'Voter',
                    'Barangay', 'Detail', 'Status', 'Mock'])
        for r in rows:
            w.writerow([r['tx_id'], timezone.localtime(r['created_at']).strftime('%Y-%m-%d %H:%M'),
                        r['actor'] or '', r['category'], r['label'], f'STR-{r["voter_id"]:07d}', r['voter_name'],
                        r['barangay'], r['description'], r['status_label'], 'yes' if r['mock'] else ''])
        return resp

    try:
        page = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    rows, total, page, pages = tx.page(scope, f, page)
    qs = urlencode({k: v for k, v in f.items() if v})
    return render(request, 'municipal/transactions.html', {
        'scope': scope,
        'summary': tx.summary(scope),
        'rows': rows,
        'total': total,
        'page': page,
        'pages': pages,
        'filters': f,
        'filtered': any(f.values()),
        'action_groups': tx.action_choices(),
        'encoders': tx.encoders(scope),
        'export_url': f'?{qs}&export=csv' if qs else '?export=csv',
        'prev_url': f'?{qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{qs}&page={page + 1}' if page < pages else '',
    })


@require_POST
def card_status(request, card_id):
    scope = _scope(request)
    if not scope:
        return redirect('select_city')
    status = request.POST.get('status', '')
    try:
        if sc.set_status(scope, card_id, status, _actor(request), _ip(request)):
            messages.success(request, f'Card updated: {sc.STATUS_LABELS.get(status, status)}.')
    except sc.CardError as e:
        messages.error(request, str(e))
    return _safe_next(request, 'cards_list')
