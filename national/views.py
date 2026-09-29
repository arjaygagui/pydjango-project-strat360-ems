"""Nationwide EMS pages (URLs under /national/)."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.db import connections
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from municipal import ai
from municipal import geo
from municipal import heatmap as hm
from municipal import household as hh
from municipal import quickcount as qc
from municipal import rollsummary
from municipal import smartcard as sc
from municipal import social as soc
from municipal.machinery import RDS, voter_brief
from municipal.views import (SOCIAL_FORM_FIELDS, _actor as actor, _ip as client_ip, _safe_next as safe_next,
                             heat_filters, heat_map_response, quick_count_context, transactions_response,
                             social_charts)
from municipal.regions import province_pretty, region_of, voter_table
from municipal.text import title
from provincial import voters
from provincial.data import extreme

from . import data


def dashboard(request):
    region = request.GET.get('region', '')
    region = region if region in data.regions() else ''
    rows, totals = data.dashboard(region)
    return render(request, 'national/dashboard.html', {
        'region': region,
        'regions': data.regions(),
        'rows': rows,
        't': totals,
        'by_supporters': sorted(rows, key=lambda p: (-p['supporters'], -p['voters'])),
        'by_cards': sorted(rows, key=lambda p: (-p['cards'], -p['voters'])),
        'most': extreme(rows, max, 'supporters', 'coverage'),
        'least': extreme(rows, min, 'supporters', 'coverage'),
        'most_cards': extreme(rows, max, 'cards', 'card_coverage'),
        'least_cards': extreme(rows, min, 'cards', 'card_coverage'),
        'sector_labels': [label for _, label in hh.MAIN_SECTORS],
        'chart': {
            'labels': [p['name'] for p in rows],
            'voters': [p['voters'] for p in rows],
            'supporters': [p['supporters'] for p in rows],
            'cards': [p['cards'] for p in rows],
        },
        'drill': {p['slug']: {'name': p['name'], 'region': p['region'],
                              'cities': [{k: c[k] for k in ('name', 'voters', 'barangays', 'supporters', 'cards')}
                                         for c in p['city_list']]} for p in rows},
    })


REGION_SEARCH_LIMIT = 50       # combined A–Z matches shown for a region-wide name search


def voters_list(request):
    """Nationwide Voters List: pick a region (provinces + region-wide name search) or a province
    (the full province Voters List, every filter). Browsing all 67.8M voters at once isn't offered."""
    province = (request.GET.get('province') or '').lower()
    province = province if voter_table(province) and rollsummary.built_at(province) else ''
    region = region_of(province) if province else request.GET.get('region', '')
    region = region if region in data.regions() else ''
    roll = data._roll()
    provinces = [{'slug': s, 'name': province_pretty(s), 'region': data.REGION_MAP[s],
                  'voters': sum(c['voters'] for c in roll.get(s, {}).values()), 'cities': len(roll.get(s, {}))}
                 for s in data.REGION_MAP if s in roll and (not region or data.REGION_MAP[s] == region)]
    common = {'region': region, 'regions': data.regions(), 'province': province,
              'region_provinces': sorted(provinces, key=lambda p: p['name'])}

    if province:
        ps = {'province': province, 'province_name': province_pretty(province), 'region': region,
              'table': voter_table(province)}
        ctx = voters.voters_page(ps, request.GET, level='nat')
        keep = f'?province={province}&'                  # the list's own links drop our province param
        ctx['prev_url'] = ctx['prev_url'].replace('?', keep, 1)
        ctx['next_url'] = ctx['next_url'].replace('?', keep, 1)
        return render(request, 'municipal/voters_list.html', {
            **ctx, **common, 'ps': ps, 'prov': True, 'nat': True, 'base_template': 'national/base.html'})

    q = (request.GET.get('q') or '').strip()
    results, per_province, note = [], [], ''
    if q and not region:
        note = 'Choose a region first — the name search runs across that region\'s provinces.'
    elif q and not any(voters._indexed(w) for w in voters._words(q)):
        note = 'Type at least 3 letters of a name.'
    elif q:
        def search(p):          # one province per thread, each on its own DB connection
            try:
                return voters.name_search(voter_table(p['slug']), q, REGION_SEARCH_LIMIT)
            finally:
                connections['rds'].close()
        with ThreadPoolExecutor(max_workers=8) as pool:
            found = list(pool.map(search, provinces))
        for p, (n, rows) in zip(provinces, found):
            if n:
                per_province.append({**p, 'matches': n})
                results += [{'slug': p['slug'], 'province': p['name'], 'id': vid, 'name': title(fn), 'city': title(m),
                             'barangay': title(b), 'precinct': (pr or '').strip(), 'sort': fn} for vid, fn, m, b, pr in rows]
        results = sorted(results, key=lambda r: r['sort'])[:REGION_SEARCH_LIMIT]
    return render(request, 'national/voters_pick.html', {
        **common, 'q': q, 'results': results, 'per_province': per_province, 'note': note,
        'total_matches': sum(p['matches'] for p in per_province), 'limit': REGION_SEARCH_LIMIT,
        'total_voters': sum(p['voters'] for p in provinces),
    })


def _place(request, src=None):
    """(region, province, city) from the request, validated; a province implies its region."""
    src = src if src is not None else request.GET
    province = (src.get('province') or '').lower()
    province = province if voter_table(province) else ''
    region = region_of(province) if province else src.get('region', '')
    region = region if region in data.regions() else ''
    city = src.get('city', '') if province else ''
    if city and city not in data._roll().get(province, {}):
        city = ''
    return region, province, city


def _region_provinces(region):
    roll = data._roll()
    return [{'slug': s, 'name': province_pretty(s)} for s in data.REGION_MAP
            if s in roll and (not region or data.REGION_MAP[s] == region)]


def cards_list(request):
    """Smart Card Holders across the country: KPIs, province rankings with city drill-down, services,
    and the directory with Region → Province → City → Barangay filters."""
    if not sc.table_exists():
        messages.error(request, 'Smart card table not set up yet — run: manage.py smartcard_setup')
        return redirect('nat_dashboard')
    region, province, city = _place(request)
    f = {'region': region, 'province': province, 'city': city,
         **{k: request.GET.get(k, '').strip() for k in ('q', 'barangay', 'status', 'gender', 'service')}}
    if not city:
        f['barangay'] = ''
    try:
        page = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    provinces = _region_provinces(region)
    area = {'provinces': [p['slug'] for p in provinces]} if region else {}
    if province:
        rows, total, page, pages = sc.city_cards({'province': province, 'table': voter_table(province)}, f['q'],
                                                 f['barangay'], f['status'], f['gender'], f['service'], page, city=city)
        for r in rows:
            r['province_slug'] = province
    else:
        rows, total, page, pages = sc.national_cards(area, f['q'], f['status'], f['gender'], f['service'], page)
    for r in rows:
        r['province_name'] = province_pretty(r['province_slug'])

    roll = data._roll()
    ranking = []
    for slug, c in sc.province_counts(area).items():
        voters = sum(x['voters'] for x in roll.get(slug, {}).values())
        cities = sorted(({'name': title(m), 'holders': n, 'share': (n / c['holders'] * 100) if c['holders'] else 0}
                         for m, n in c['cities'].items()), key=lambda x: -x['holders'])
        ranking.append({**c, 'raw': slug, 'barangay': province_pretty(slug), 'voters': voters, 'barangay_list': cities,
                        'active_pct': (c['active'] / c['holders'] * 100) if c['holders'] else 0,
                        'coverage': (c['holders'] / voters * 100) if voters else 0})
    ranking = sorted((r for r in ranking if r['holders']), key=lambda r: -r['holders'])

    qs = urlencode({k: v for k, v in f.items() if v})
    return render(request, 'municipal/cards_list.html', {
        'prov': True, 'nat': True, 'base_template': 'national/base.html',
        'region': region, 'regions': data.regions(), 'region_provinces': provinces,
        'area_name': region or 'Philippines',
        'summary': sc.city_summary(area),
        'ranking': ranking,
        'area_chart': {'labels': [r['barangay'] for r in ranking], 'active': [r['active'] for r in ranking],
                       'pending': [r['pending'] for r in ranking]},
        'rows': rows, 'total': total, 'page': page, 'pages': pages,
        'filters': f, 'filtered': any(v for k, v in f.items() if k != 'region'),
        'cities': sorted(({'raw': m, 'pretty': c['name']} for m, c in roll.get(province, {}).items()),
                         key=lambda c: c['pretty']) if province else [],
        'barangays': sc.city_barangays({'province': province, 'municipality': city}) if city else [],
        'services': sc.SERVICES, 'statuses': sc.STATUS_LABELS, 'genders': sc.GENDERS,
        'prev_url': f'?{qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{qs}&page={page + 1}' if page < pages else '',
    })


@require_POST
def card_status(request, card_id):
    """Activate / set pending / revoke from the national list — in the card's own city scope."""
    status = request.POST.get('status', '')
    province, city = sc.card_scope(card_id)
    try:
        if not province:
            raise sc.CardError('That card was not found.')
        if sc.set_status({'province': province, 'municipality': city}, card_id, status, actor(request), client_ip(request)):
            messages.success(request, f'Card updated: {sc.STATUS_LABELS.get(status, status)}.')
    except sc.CardError as e:
        messages.error(request, str(e))
    return safe_next(request, 'nat_cards')


def card_new(request):
    """The city's Issue Smart Card form for any voter in the country: pick the province, then the
    voter. The card is saved under the cardholder's own city (same rules and audit)."""
    if not sc.table_exists():
        messages.error(request, 'Smart card table not set up yet — run: manage.py smartcard_setup')
        return redirect('nat_dashboard')
    src = request.POST if request.method == 'POST' else request.GET
    region, province, _ = _place(request, src)
    raw = src.get('voter_id') or src.get('voter') or ''
    voter = voter_brief(province, int(raw)) if province and raw.isdigit() else None
    if voter and not voter['municipality']:
        voter = None
    existing = sc.card_for(province, voter['id']) if voter else None

    if request.method == 'POST':
        if not voter:
            messages.error(request, 'Pick the province and the cardholder from the suggestions.')
        else:
            try:
                number = sc.issue({'province': province, 'municipality': voter['municipality']}, voter, request.POST,
                                  actor(request), client_ip(request))
                messages.success(request, f'Smart card {number} issued to {voter["name"]} '
                                          f'({voter["municipality_pretty"]}, {province_pretty(province)}).')
                return safe_next(request, 'nat_cards')
            except sc.CardError as e:
                messages.error(request, str(e))
        values = {k: request.POST.get(k, '') for k in ('service', 'gender', 'civil_status', 'status', 'issued_date')}
    else:
        values = {'service': '', 'gender': '', 'civil_status': '', 'status': 'active',
                  'issued_date': timezone.localdate().isoformat()}
    return render(request, 'municipal/card_form.html', {
        'prov': True, 'nat': True, 'base_template': 'national/base.html',
        'region': region, 'regions': data.regions(), 'region_provinces': _region_provinces(region),
        'ps': {'province': province, 'province_name': province_pretty(province)} if province else None,
        'voter': voter, 'existing': existing, 'v': values,
        'services': sc.SERVICES, 'genders': sc.GENDERS, 'civil_statuses': sc.CIVIL_STATUSES,
        'today': timezone.localdate().isoformat(),
    })


def social_list(request):
    """Social Services across the country: KPIs, province rankings with city drill-down, assistance
    charts, and the request directory (Region → Province → City → Barangay)."""
    if not soc.table_exists():
        messages.error(request, 'Social services table not set up yet — run: manage.py social_setup')
        return redirect('nat_dashboard')
    region, province, city = _place(request)
    f = {'region': region, 'province': province, 'city': city,
         **{k: request.GET.get(k, '').strip() for k in ('q', 'type', 'status', 'from', 'to', 'barangay')}}
    if not city:
        f['barangay'] = ''
    try:
        page = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    provinces = _region_provinces(region)
    area = {'provinces': [p['slug'] for p in provinces]} if region else {}
    list_area = {'province': province} if province else area
    rows, total, total_amount, page, pages = soc.city_records(
        list_area, f['q'], f['type'], f['status'], f['from'], f['to'], page, city=city, barangay=f['barangay'])
    for r in rows:
        r['city'] = title(r['municipality'])
        r['province_name'] = province_pretty(r['province_slug'])

    ranking = soc.area_breakdown(area)            # provinces, drilling into cities
    for a in ranking:
        a['name'] = province_pretty(a['key'])
        for ch in a['children']:
            ch['name'] = title(ch['name'])
    summary = soc.city_summary(area)
    qs = urlencode({k: v for k, v in f.items() if v})
    roll = data._roll()
    return render(request, 'municipal/social_list.html', {
        'prov': True, 'nat': True, 'base_template': 'national/base.html',
        'region': region, 'regions': data.regions(), 'region_provinces': provinces,
        'area_name': region or 'Philippines',
        'summary': summary,
        'ranking': ranking,
        'charts': social_charts(ranking, summary),
        'cities': sorted(({'raw': m, 'pretty': c['name']} for m, c in roll.get(province, {}).items()),
                         key=lambda c: c['pretty']) if province else [],
        'barangays': soc.city_barangays({'province': province}, city) if city else [],
        'types': list(soc.ASSISTANCE_TYPES),
        'statuses': soc.STATUSES,
        'rows': rows, 'total': total, 'total_amount': total_amount, 'page': page, 'pages': pages,
        'filters': f, 'filtered': any(v for k, v in f.items() if k != 'region'),
        'prev_url': f'?{qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{qs}&page={page + 1}' if page < pages else '',
    })


def social_new(request):
    """The city's Social Services application for any voter in the country: pick the province,
    then the beneficiary. Saved under the beneficiary's own city (same checks and audit)."""
    if not soc.table_exists():
        messages.error(request, 'Social services table not set up yet — run: manage.py social_setup')
        return redirect('nat_dashboard')
    src = request.POST if request.method == 'POST' else request.GET
    region, province, _ = _place(request, src)
    raw = src.get('voter_id') or src.get('voter') or ''
    voter = voter_brief(province, int(raw)) if province and raw.isdigit() else None
    if voter and not voter['municipality']:
        voter = None

    if request.method == 'POST':
        if not voter:
            messages.error(request, 'Pick the province and the beneficiary from the suggestions.')
        else:
            try:
                new_id = soc.record({'province': province, 'municipality': voter['municipality']}, voter, request.POST,
                                    actor(request), client_ip(request))
                messages.success(request, f'Social service #{new_id} recorded for {voter["name"]} '
                                          f'({voter["municipality_pretty"]}, {province_pretty(province)}).')
                return safe_next(request, 'nat_social')      # back to the profile when opened from one
            except soc.SocialError as e:
                messages.error(request, str(e))
        values = {k: request.POST.get(k, '') for k in SOCIAL_FORM_FIELDS}
    else:
        values = {k: '' for k in SOCIAL_FORM_FIELDS}
        values.update({'status': soc.DEFAULT_STATUS, 'date_requested': timezone.localdate().isoformat()})
        if voter:
            values.update({'beneficiary': voter['name'], 'region': region_of(province) or '',
                           'province': province_pretty(province), 'city_municipality': voter['municipality_pretty'],
                           'barangay': voter['barangay_pretty'], 'street': title(voter.get('address'))})
    return render(request, 'municipal/social_form.html', {
        'prov': True, 'nat': True, 'base_template': 'national/base.html',
        'region': region, 'regions': data.regions(), 'region_provinces': _region_provinces(region),
        'ps': {'province': province, 'province_name': province_pretty(province)} if province else None,
        'voter': voter, 'v': values,
        'types': soc.ASSISTANCE_TYPES, 'statuses': soc.STATUSES,
        'genders': soc.GENDERS, 'civil_statuses': soc.CIVIL_STATUSES,
        'agencies': soc.AGENCIES, 'programs': soc.PROGRAMS, 'relationships': soc.RELATIONSHIPS,
        'today': timezone.localdate().isoformat(),
    })


@require_POST
def social_status(request, record_id):
    """Change a record's status from the national list — in the record's own city scope."""
    status = request.POST.get('status', '')
    province, city = soc.record_scope(record_id)
    try:
        if not province:
            raise soc.SocialError('That record was not found.')
        if soc.set_status({'province': province, 'municipality': city}, record_id, status, actor(request), client_ip(request)):
            messages.success(request, f'Social service #{record_id} marked {status}.')
    except soc.SocialError as e:
        messages.error(request, str(e))
    return safe_next(request, 'nat_social')


def api_search_voters(request):
    """Surname type-ahead in one province (?province=slug), for the national forms."""
    province = (request.GET.get('province') or '').lower()
    table = voter_table(province)
    q = (request.GET.get('q') or '').strip()
    if not table or len(q) < 2:
        return JsonResponse({'ok': True, 'data': []})
    with connections[RDS].cursor() as cur:
        cur.execute(f'SELECT id, fullname, municipality, barangay FROM {table} WHERE fullname LIKE %s ORDER BY fullname LIMIT 15',
                    [q + '%'])
        rows = cur.fetchall()
    return JsonResponse({'ok': True, 'data': [
        {'id': vid, 'name': title(name), 'city': title(muni), 'barangay': title(brgy)} for vid, name, muni, brgy in rows]})


def quick_count(request):
    """Voting-day attendance across the country, or one region (simulated like the city page, see
    quickcount.py): per-province turnout — each province summed from its cities exactly as its own
    Quick Count page — national KPIs, cardholder scans and the country's top precincts."""
    region = request.GET.get('region', '')
    region = region if region in data.regions() else ''
    slugs = [p['slug'] for p in _region_provinces(region)]
    names = {s: province_pretty(s) for s in slugs}
    key = 'nat-qc-prec:' + (re.sub(r'\W+', '-', region) if region else 'all')
    candidates = cache.get(key)
    if candidates is None:
        candidates = rollsummary.largest_precincts_in(slugs, qc.PRECINCT_CANDIDATES, qc.MAX_PRECINCT)
        cache.set(key, candidates, qc.PRECINCT_TTL)
    area = {'provinces': slugs} if region else {}
    return render(request, 'municipal/quick_count.html', quick_count_context(
        area, qc.province_areas(data._roll(), slugs, names), qc.rank_precincts({'province': None}, candidates), 'nat',
        prov=True, nat=True, base_template='national/base.html', region=region, regions=data.regions(),
        area_name=region or 'Nationwide'))


def heat_map(request):
    """Geo-analytics per province (the city heat map's page with one pin per province), for the
    country or one region."""
    region = request.GET.get('region', '')
    region = region if region in data.regions() else ''
    days, service, card_status = heat_filters(request)
    rows, totals = data.dashboard(region)
    area = {'provinces': [p['slug'] for p in rows]} if region else {}
    locs = geo.province_locations()
    social = hm.social_by_barangay(area, service)
    households = hm.households_by_barangay(area)
    activity = hm.activity_by_barangay(area, int(days))

    lgus = []
    for p in rows:
        slug, loc, soc_ = p['slug'], locs.get(p['slug']), social.get(p['slug'], {})
        pending = p['cards'] - p['active_cards']
        holders = {'active': p['active_cards'], 'pending': pending}.get(card_status, p['cards'])
        lgus.append({
            'name': p['name'], 'raw': slug,
            'lat': loc['lat'] if loc else None, 'lng': loc['lng'] if loc else None,
            'approx': bool(loc and loc['source'] == 'approx'),
            'voters': p['voters'], 'precincts': p['precincts'],
            'cards': holders, 'active_cards': p['active_cards'], 'pending_cards': pending,
            'card_pct': round(holders / p['voters'] * 100, 2) if p['voters'] else 0,
            'supporters': p['supporters'], 'coverage': round(p['coverage'], 2),
            'coordinators': p['coordinators'], 'opposition': p['opposition'],
            'beneficiaries': soc_.get('beneficiaries', 0), 'records': soc_.get('records', 0),
            'released': round(soc_.get('released', 0)), 'open': soc_.get('open', 0),
            'open_amount': round(soc_.get('open_amount', 0)),
            'sector_members': sum(p['sectors'].values()),
            'top_sectors': sorted(p['sectors'].items(), key=lambda kv: -kv[1])[:5],
            'households': households.get(slug, 0),
            'activity': activity.get(slug, 0),
        })
    return heat_map_response(request, area, lgus, totals['coordinators'], (days, service, card_status),
                             region or 'Philippines', 'nat', keep={'region': region},
                             prov=True, nat=True, base_template='national/base.html', region=region, regions=data.regions())


@require_POST
def heat_map_locate(request):
    """Staff: look up the not-yet-located province centres on OpenStreetMap (1–3 s each)."""
    back = redirect('nat_heat_map')
    if not request.user.is_staff:
        messages.error(request, 'Only staff can locate provinces.')
        return back
    try:
        found, approx, left = geo.locate_provinces({s: province_pretty(s) for s in data.REGION_MAP}, limit=20,
                                                   log=lambda *_: None)
        msg = f'Located {found} province{"s" if found != 1 else ""} on OpenStreetMap'
        if approx:
            msg += f'; {approx} placed at an approximate spot'
        if left:
            msg += f'; {left} more still to locate — click again'
        messages.success(request, msg + '.')
    except (OSError, RuntimeError) as e:
        messages.error(request, f'Could not reach OpenStreetMap: {e}')
    return back


def transactions_list(request):
    """The audit trail (ems_voter_audit) across the country, with Region → Province → City filters.
    Without a province it lists every province's entries (names from each entry's own roll, search by
    TX ID / detail); with one it is that province's list (surname search, City filter) — always with
    the Nationwide EMS's own machinery entries only."""
    region, province, _ = _place(request)
    if province:
        roll = data._roll().get(province, {})
        cities = sorted(({'raw': raw, 'pretty': c['name']} for raw, c in roll.items()), key=lambda c: c['pretty'])
        area = {'province': province, 'table': voter_table(province), 'level': 'nat'}
        name = province_pretty(province)
    else:
        cities = None
        area = {'provinces': [p['slug'] for p in _region_provinces(region)]} if region else {}
        name = region or 'Philippines'
    return transactions_response(request, area, name, cities=cities, level='nat',
                                 keep={'region': region, 'province': province},
                                 prov=True, nat=True, base_template='national/base.html', region=region,
                                 regions=data.regions(), province=province,
                                 region_provinces=_region_provinces(region))


def ai_analytics(request):
    """The city AI Analytics page, asking about the country or one region (per-province aggregates only)."""
    region = request.GET.get('region', '')
    region = region if region in data.regions() else ''
    names = [p['name'] for p in _region_provinces(region)]
    return render(request, 'municipal/ai_analytics.html', {
        'prov': True, 'nat': True, 'base_template': 'national/base.html',
        'region': region, 'regions': data.regions(),
        'configured': ai.configured(),
        'model': settings.GEMINI_MODEL,
        'places': names + data.regions(),
        'examples': sorted(names)[:2],
    })


@require_POST
def api_ai(request):
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    query = str(body.get('query', '')).strip()[:ai.MAX_QUERY]
    intent = body.get('intent') if body.get('intent') in ai.INTENTS else 'general'
    region = body.get('region') if body.get('region') in data.regions() else ''
    if not query:
        return JsonResponse({'error': 'Type a question first.'}, status=400)
    try:
        rows, _ = data.dashboard(region)
        area = {'provinces': [p['slug'] for p in rows]} if region else {}
        snap = ai.national_snapshot(region, rows, hm.activity_by_barangay(area, 30), hm.households_by_barangay(area))
        answer = ai.ask(snap, query, intent, level='national')
    except ai.AIError as e:
        return JsonResponse({'error': str(e), 'detail': e.detail}, status=502)
    return JsonResponse({'ok': True, 'intent': intent, 'query': query, 'result': answer})
