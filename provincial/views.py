"""Province-Wide EMS pages (URLs under /province/)."""
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

import json
from urllib.parse import urlencode

from django.conf import settings
from django.db import connections
from django.http import JsonResponse
from django.utils import timezone

from municipal import rollsummary
from municipal import ai
from municipal import geo
from municipal import heatmap as hm
from municipal import quickcount as qc
from municipal import smartcard as sc
from municipal import social as soc
from municipal.machinery import RDS, voter_brief
from municipal.regions import regions_with_provinces, voter_table
from municipal.text import title
from municipal.views import (SOCIAL_FORM_FIELDS, _actor as actor, _ip as client_ip, _safe_next as safe_next,
                             heat_filters, heat_map_response, quick_count_context, social_charts, transactions_response)

from . import data, voters


def select_province(request):
    if request.method == 'POST':
        slug = (request.POST.get('province') or '').lower()
        if voter_table(slug):
            request.session['prov'] = {'province': slug}
            return redirect('prov_dashboard')
        messages.error(request, 'Please choose a valid province.')
    ps = data.pscope(request)
    return render(request, 'provincial/select_province.html', {
        'ps': ps,
        'regions': regions_with_provinces(),
        'current': {'region': ps['region'] if ps else '', 'province': ps['province'] if ps else ''},
    })


def dashboard(request):
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not rollsummary.built_at(ps['province']):
        return render(request, 'provincial/not_ready.html', {'ps': ps})
    rows, totals = data.dashboard(ps)
    return render(request, 'provincial/dashboard.html', {
        'ps': ps,
        'rows': rows,
        't': totals,
        'by_supporters': sorted(rows, key=lambda m: (-m['supporters'], -m['voters'])),
        'by_cards': sorted(rows, key=lambda m: (-m['cards'], -m['voters'])),
        'most': data.extreme(rows, max, 'supporters', 'coverage'),
        'least': data.extreme(rows, min, 'supporters', 'coverage'),
        'most_cards': data.extreme(rows, max, 'cards', 'card_coverage'),
        'least_cards': data.extreme(rows, min, 'cards', 'card_coverage'),
        'sector_labels': [label for _, label in data.hh.MAIN_SECTORS],
        'built_at': rollsummary.built_at(ps['province']),
        'chart': {
            'labels': [m['name'] for m in rows],
            'voters': [m['voters'] for m in rows],
            'supporters': [m['supporters'] for m in rows],
            'cards': [m['cards'] for m in rows],
        },
        'drill': {m['raw']: {'name': m['name'], 'barangays': m['barangay_list']} for m in rows},
    })


def voters_list(request):
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not rollsummary.built_at(ps['province']):
        return render(request, 'provincial/not_ready.html', {'ps': ps})
    return render(request, 'municipal/voters_list.html', {
        'ps': ps,
        'prov': True,
        'base_template': 'provincial/base.html',
        **voters.voters_page(ps, request.GET),
    })


def open_voter(request, voter_id):
    """A voter's profile lives in their city's EMS: switch the city scope to theirs and open it."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    brief = voter_brief(ps['province'], voter_id)
    if not brief or not brief['municipality']:
        messages.error(request, 'Voter not found in this province.')
        return redirect('prov_voters')
    request.session['muni'] = {'province': ps['province'], 'municipality': brief['municipality']}
    return redirect('voter_profile', voter_id=brief['id'])


def cards_list(request):
    """Smart Card Holders across the province: KPIs, per-city ranking with barangay drill-down,
    services, and the cardholder directory (City → Barangay filters)."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not sc.table_exists():
        messages.error(request, 'Smart card table not set up yet — run: manage.py smartcard_setup')
        return redirect('prov_dashboard')
    slug = ps['province']
    munis = rollsummary.municipalities(slug)
    f = {k: request.GET.get(k, '').strip() for k in ('city', 'q', 'barangay', 'status', 'gender', 'service')}
    if f['city'] not in munis:
        f.update(city='', barangay='')
    try:
        page = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    area = {'province': slug, 'table': ps['table']}
    rows, total, page, pages = sc.city_cards(area, f['q'], f['barangay'], f['status'], f['gender'], f['service'],
                                             page, city=f['city'])

    ranking = []
    for raw, c in sc.municipality_counts(slug).items():
        voters = munis.get(raw, {}).get('voters', 0)
        brgys = sorted(({'name': b, 'holders': n, 'share': (n / c['holders'] * 100) if c['holders'] else 0}
                        for b, n in c['barangays'].items()), key=lambda b: -b['holders'])
        ranking.append({**c, 'raw': raw, 'barangay': title(raw), 'voters': voters, 'barangay_list': brgys,
                        'active_pct': (c['active'] / c['holders'] * 100) if c['holders'] else 0,
                        'coverage': (c['holders'] / voters * 100) if voters else 0})
    ranking = [r for r in ranking if r['holders']]
    ranking.sort(key=lambda r: -r['holders'])

    qs = urlencode({k: v for k, v in f.items() if v})
    return render(request, 'municipal/cards_list.html', {
        'ps': ps,
        'prov': True,
        'base_template': 'provincial/base.html',
        'summary': sc.city_summary(area),
        'ranking': ranking,
        'area_chart': {'labels': [r['barangay'] for r in ranking], 'active': [r['active'] for r in ranking],
                       'pending': [r['pending'] for r in ranking]},
        'rows': rows,
        'total': total,
        'page': page,
        'pages': pages,
        'filters': f,
        'filtered': any(f.values()),
        'cities': sorted(({'raw': m['raw'], 'pretty': m['name']} for m in munis.values()), key=lambda c: c['pretty']),
        'barangays': sc.city_barangays({'province': slug, 'municipality': f['city']}) if f['city'] else [],
        'services': sc.SERVICES,
        'statuses': sc.STATUS_LABELS,
        'genders': sc.GENDERS,
        'prev_url': f'?{qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{qs}&page={page + 1}' if page < pages else '',
    })


def card_new(request):
    """The city's Issue Smart Card form, for any voter in the province. The card is saved under the
    cardholder's own city (same table, one-card rule, numbering and audit as the city EMS)."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not sc.table_exists():
        messages.error(request, 'Smart card table not set up yet — run: manage.py smartcard_setup')
        return redirect('prov_dashboard')

    raw = request.POST.get('voter_id') or request.GET.get('voter') or ''
    voter = voter_brief(ps['province'], int(raw)) if raw.isdigit() else None
    if voter and not voter['municipality']:
        voter = None
    existing = sc.card_for(ps['province'], voter['id']) if voter else None

    if request.method == 'POST':
        if not voter:
            messages.error(request, 'Pick the cardholder from the suggestions.')
        else:
            try:
                scope = {'province': ps['province'], 'municipality': voter['municipality']}
                number = sc.issue(scope, voter, request.POST, actor(request), client_ip(request))
                messages.success(request, f'Smart card {number} issued to {voter["name"]} ({voter["municipality_pretty"]}).')
                return redirect('prov_cards')
            except sc.CardError as e:
                messages.error(request, str(e))
        values = {k: request.POST.get(k, '') for k in ('service', 'gender', 'civil_status', 'status', 'issued_date')}
    else:
        values = {'service': '', 'gender': '', 'civil_status': '', 'status': 'active',
                  'issued_date': timezone.localdate().isoformat()}

    return render(request, 'municipal/card_form.html', {
        'ps': ps, 'prov': True, 'base_template': 'provincial/base.html',
        'voter': voter, 'existing': existing, 'v': values,
        'services': sc.SERVICES, 'genders': sc.GENDERS, 'civil_statuses': sc.CIVIL_STATUSES,
        'today': timezone.localdate().isoformat(),
    })


@require_POST
def card_status(request, card_id):
    """Activate / set pending / revoke from the province list — done in the card's own city scope,
    so the same checks and audit trail apply as in that city's EMS."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    status = request.POST.get('status', '')
    city = sc.card_city(ps['province'], card_id)
    try:
        if not city:
            raise sc.CardError('That card was not found in this province.')
        scope = {'province': ps['province'], 'municipality': city}
        if sc.set_status(scope, card_id, status, actor(request), client_ip(request)):
            messages.success(request, f'Card updated: {sc.STATUS_LABELS.get(status, status)}.')
    except sc.CardError as e:
        messages.error(request, str(e))
    return safe_next(request, 'prov_cards')


def social_list(request):
    """Social Services across the province: KPIs, per-city chart + ranking with barangay
    drill-down, assistance-type charts, and the request directory (City → Barangay filters)."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not soc.table_exists():
        messages.error(request, 'Social services table not set up yet — run: manage.py social_setup')
        return redirect('prov_dashboard')
    slug = ps['province']
    munis = rollsummary.municipalities(slug)
    f = {k: request.GET.get(k, '').strip() for k in ('city', 'q', 'type', 'status', 'from', 'to', 'barangay')}
    if f['city'] not in munis:
        f.update(city='', barangay='')
    try:
        page = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    area = {'province': slug}
    rows, total, total_amount, page, pages = soc.city_records(
        area, f['q'], f['type'], f['status'], f['from'], f['to'], page, city=f['city'], barangay=f['barangay'])
    for r in rows:
        r['city'] = title(r['municipality'])

    ranking = soc.area_breakdown(area)             # cities, drilling into barangays
    for a in ranking:
        a['name'] = title(a['key'])
    summary = soc.city_summary(area)
    qs = urlencode({k: v for k, v in f.items() if v})
    return render(request, 'municipal/social_list.html', {
        'ps': ps,
        'prov': True,
        'base_template': 'provincial/base.html',
        'summary': summary,
        'ranking': ranking,
        'charts': social_charts(ranking, summary),
        'cities': sorted(({'raw': m['raw'], 'pretty': m['name']} for m in munis.values()), key=lambda c: c['pretty']),
        'barangays': soc.city_barangays(area, f['city']) if f['city'] else [],
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
    })


def social_new(request):
    """The city's Social Services application form, for any voter in the province. The record is
    saved under the beneficiary's own city (same table, checks and audit as the city EMS)."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not soc.table_exists():
        messages.error(request, 'Social services table not set up yet — run: manage.py social_setup')
        return redirect('prov_dashboard')

    raw = request.POST.get('voter_id') or request.GET.get('voter') or ''
    voter = voter_brief(ps['province'], int(raw)) if raw.isdigit() else None
    if voter and not voter['municipality']:
        voter = None
    scope = {'province': ps['province'], 'municipality': voter['municipality']} if voter else None

    if request.method == 'POST':
        if not voter:
            messages.error(request, 'Pick the beneficiary from the suggestions so the record '
                                    'is linked to their voter profile.')
        else:
            try:
                new_id = soc.record(scope, voter, request.POST, actor(request), client_ip(request))
                messages.success(request, f'Social service #{new_id} recorded for {voter["name"]} '
                                          f'({voter["municipality_pretty"]}).')
                return redirect('prov_social')
            except soc.SocialError as e:
                messages.error(request, str(e))
        values = {k: request.POST.get(k, '') for k in SOCIAL_FORM_FIELDS}
    else:
        values = {k: '' for k in SOCIAL_FORM_FIELDS}
        values.update({'status': soc.DEFAULT_STATUS, 'date_requested': timezone.localdate().isoformat()})
        if voter:   # pre-fill from the voter roll
            values.update({
                'beneficiary': voter['name'],
                'region': ps['region'] or '',
                'province': ps['province_name'],
                'city_municipality': voter['municipality_pretty'],
                'barangay': voter['barangay_pretty'],
                'street': title(voter.get('address')),
            })

    return render(request, 'municipal/social_form.html', {
        'ps': ps, 'prov': True, 'base_template': 'provincial/base.html',
        'voter': voter, 'v': values,
        'types': soc.ASSISTANCE_TYPES, 'statuses': soc.STATUSES,
        'genders': soc.GENDERS, 'civil_statuses': soc.CIVIL_STATUSES,
        'agencies': soc.AGENCIES, 'programs': soc.PROGRAMS, 'relationships': soc.RELATIONSHIPS,
        'today': timezone.localdate().isoformat(),
    })


def api_search_voters(request):
    """Province-wide beneficiary type-ahead: surname prefix (idx_fullname range — fast even on NCR),
    like the city form's search."""
    ps = data.pscope(request)
    if not ps:
        return JsonResponse({'ok': False, 'error': 'No province selected'}, status=400)
    q = (request.GET.get('q') or '').strip()
    if len(q) < 2:
        return JsonResponse({'ok': True, 'data': []})
    with connections[RDS].cursor() as cur:
        cur.execute(f"SELECT id, fullname, municipality, barangay FROM {ps['table']} "
                    'WHERE fullname LIKE %s ORDER BY fullname LIMIT 15', [q + '%'])
        rows = cur.fetchall()
    return JsonResponse({'ok': True, 'data': [
        {'id': vid, 'name': title(name), 'city': title(muni), 'barangay': title(brgy)} for vid, name, muni, brgy in rows]})


@require_POST
def social_status(request, record_id):
    """Change a record's status from the province list — in the record's own city scope, so the
    same checks and audit trail apply as in that city's EMS."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    status = request.POST.get('status', '')
    city = soc.record_city(ps['province'], record_id)
    try:
        if not city:
            raise soc.SocialError('That record was not found in this province.')
        if soc.set_status({'province': ps['province'], 'municipality': city}, record_id, status,
                          actor(request), client_ip(request)):
            messages.success(request, f'Social service #{record_id} marked {status}.')
    except soc.SocialError as e:
        messages.error(request, str(e))
    return safe_next(request, 'prov_social')


def quick_count(request):
    """Voting-day attendance across the province (simulated like the city page, see quickcount.py):
    per-city turnout, province KPIs, cardholder scans and the province's top precincts."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not rollsummary.built_at(ps['province']):
        return render(request, 'provincial/not_ready.html', {'ps': ps})
    area = {'province': ps['province'], 'table': ps['table']}
    cities = qc.city_areas(rollsummary.municipalities(ps['province']))
    candidates = rollsummary.largest_precincts(ps['province'], qc.PRECINCT_CANDIDATES, qc.MAX_PRECINCT)
    return render(request, 'municipal/quick_count.html', quick_count_context(
        area, cities, qc.rank_precincts(area, candidates),
        ps=ps, prov=True, base_template='provincial/base.html',
        precincts_pending=not rollsummary.precincts_built(ps['province'])))


def heat_map(request):
    """Geo-analytics per city / municipality: the city heat map's page with one pin per town."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not rollsummary.built_at(ps['province']):
        return render(request, 'provincial/not_ready.html', {'ps': ps})
    days, service, card_status = heat_filters(request)
    area = {'province': ps['province'], 'table': ps['table']}
    rows, _ = data.dashboard(ps)                     # voters, machinery, cards, sectors per city
    locs = geo.city_locations(ps['province'])
    social = hm.social_by_barangay(area, service)
    households = hm.households_by_barangay(area)
    activity = hm.activity_by_barangay(area, int(days))

    lgus = []
    for m in rows:
        raw, loc, s = m['raw'], locs.get(m['raw']), social.get(m['raw'], {})
        pending = m['cards'] - m['active_cards']
        holders = {'active': m['active_cards'], 'pending': pending}.get(card_status, m['cards'])
        lgus.append({
            'name': m['name'], 'raw': raw,
            'lat': loc['lat'] if loc else None, 'lng': loc['lng'] if loc else None,
            'approx': bool(loc and loc['source'] in ('approx', 'district')),
            'voters': m['voters'], 'precincts': m['precincts'],
            'cards': holders, 'active_cards': m['active_cards'], 'pending_cards': pending,
            'card_pct': round(holders / m['voters'] * 100, 2) if m['voters'] else 0,
            'supporters': m['supporters'], 'coverage': round(m['coverage'], 2),
            'coordinators': m['coordinators'], 'opposition': m['opposition'],
            'beneficiaries': s.get('beneficiaries', 0), 'records': s.get('records', 0),
            'released': round(s.get('released', 0)), 'open': s.get('open', 0), 'open_amount': round(s.get('open_amount', 0)),
            'sector_members': sum(m['sectors'].values()),
            'top_sectors': sorted(m['sectors'].items(), key=lambda kv: -kv[1])[:5],
            'households': households.get(raw, 0),
            'activity': activity.get(raw, 0),
        })
    return heat_map_response(request, area, lgus, sum(m['coordinators'] for m in rows), (days, service, card_status),
                             ps['province_name'], ps=ps, prov=True, base_template='provincial/base.html')


@require_POST
def heat_map_locate(request):
    """Staff: look up the province's not-yet-located cities on OpenStreetMap (~2 s each)."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not request.user.is_staff:
        messages.error(request, 'Only staff can locate cities.')
        return redirect('prov_heat_map')
    try:
        found, approx, left = geo.locate_cities(ps['province'], sorted(rollsummary.municipalities(ps['province'])),
                                                ps['province_name'], limit=20, log=lambda *_: None)
        msg = f'Located {found} cit{"y" if found == 1 else "ies"} / municipalit{"y" if found == 1 else "ies"} on OpenStreetMap'
        if approx:
            msg += f'; {approx} not found and placed near the province centre'
        if left:
            msg += f'; {left} more still to locate — click again'
        messages.success(request, msg + '.')
    except (OSError, RuntimeError) as e:
        messages.error(request, f'Could not reach OpenStreetMap: {e}')
    return redirect('prov_heat_map')


def ai_analytics(request):
    """The city AI Analytics page, asking about the whole province (per-city aggregates only)."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not rollsummary.built_at(ps['province']):
        return render(request, 'provincial/not_ready.html', {'ps': ps})
    names = sorted(m['name'] for m in rollsummary.municipalities(ps['province']).values())
    return render(request, 'municipal/ai_analytics.html', {
        'ps': ps, 'prov': True, 'base_template': 'provincial/base.html',
        'configured': ai.configured(),
        'model': settings.GEMINI_MODEL,
        'places': names + [ps['province_name']],
        'examples': names[:2],
    })


@require_POST
def api_ai(request):
    ps = data.pscope(request)
    if not ps:
        return JsonResponse({'error': 'No province selected.'}, status=400)
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    query = str(body.get('query', '')).strip()[:ai.MAX_QUERY]
    intent = body.get('intent') if body.get('intent') in ai.INTENTS else 'general'
    if not query:
        return JsonResponse({'error': 'Type a question first.'}, status=400)
    area = {'province': ps['province'], 'table': ps['table']}
    try:
        rows, _ = data.dashboard(ps)
        snap = ai.province_snapshot(ps, rows, hm.activity_by_barangay(area, 30), hm.households_by_barangay(area))
        answer = ai.ask(snap, query, intent, level='province')
    except ai.AIError as e:
        return JsonResponse({'error': str(e), 'detail': e.detail}, status=502)
    return JsonResponse({'ok': True, 'intent': intent, 'query': query, 'result': answer})


def transactions_list(request):
    """The audit trail (ems_voter_audit) for every voter in the province, with a City filter."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    cities = sorted(({'raw': m['raw'], 'pretty': m['name']} for m in rollsummary.municipalities(ps['province']).values()),
                    key=lambda c: c['pretty'])
    return transactions_response(request, {'province': ps['province'], 'table': ps['table']}, ps['province_name'],
                                 cities=cities, ps=ps, prov=True, base_template='provincial/base.html')


@require_POST
def build_summary(request):
    """Staff: build this province's voter totals now (a few seconds; up to ~30 s for NCR)."""
    ps = data.pscope(request)
    if not ps:
        return redirect('prov_select')
    if not request.user.is_staff:
        messages.error(request, 'Only staff can prepare province data.')
        return redirect('prov_dashboard')
    rows, voters, secs = rollsummary.build(ps['province'])
    messages.success(request, f'{ps["province_name"]} is ready: {voters:,} voters in {rows:,} barangay rows ({secs:.0f} s).')
    return redirect('prov_dashboard')


@require_POST
def open_city(request):
    """Drill from the province into one city's EMS (sets the city scope, opens its dashboard)."""
    ps = data.pscope(request)
    muni = request.POST.get('municipality', '')
    if ps and muni in {m for m, _, _, _ in rollsummary.province_rows(ps['province'])}:
        request.session['muni'] = {'province': ps['province'], 'municipality': muni}
        return redirect('dashboard')
    messages.error(request, 'That city is not in this province.')
    return redirect('prov_dashboard')
