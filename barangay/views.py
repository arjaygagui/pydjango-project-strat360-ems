"""Barangay EMS pages (URLs under /barangay/). One barangay of one city — its own product, for
barangay-level clients: every page, profile and action stays in the Barangay EMS."""
import json
import math
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.db import connections
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from municipal import ai
from municipal import geo
from municipal import household as hh
from municipal import quickcount as qc
from municipal import machinery as mach
from municipal import smartcard as sc
from municipal import social as soc
from municipal.regions import regions_with_provinces, voter_table
from municipal.text import title
from municipal.views import (SOCIAL_FORM_FIELDS, _actor as actor, _city_voter, _ckey, _ip as client_ip,
                             _municipalities, _safe_next as safe_next, heat_filters, heat_map_response,
                             quick_count_context, social_charts, transactions_response)

from . import data

BASE = {'brgy': True, 'base_template': 'barangay/base.html'}
PER = 20


def _need_scope(request):
    scope = data.bscope(request)
    return scope, (None if scope else redirect('brgy_select'))


def _voter(scope, vid):
    """A voter's brief if they are on this barangay's roll, else None."""
    b = _city_voter(scope, vid)
    return b if b and b['barangay'] in scope['barangays'] else None


# ---------------------------------------------------------------------------
# Choose the barangay
# ---------------------------------------------------------------------------
def select(request):
    if request.method == 'POST':
        slug = (request.POST.get('province') or '').lower()
        city = request.POST.get('municipality', '')
        brgy = request.POST.get('barangay', '')
        if data.scope_for(slug, city, brgy):
            request.session['brgy'] = {'province': slug, 'municipality': city, 'barangay': brgy}
            return redirect('brgy_dashboard')
        messages.error(request, 'Please choose a valid province, city and barangay.')
    current = data.bscope(request)
    return render(request, 'barangay/select.html', {
        **BASE, 'bs': current, 'regions': regions_with_provinces(),
        'current': {'region': current['region'] if current else '', 'province': current['province'] if current else '',
                    'municipality': current['municipality'] if current else '',
                    'barangay': current['barangay'] if current else ''},
    })


def api_barangays(request):
    """JSON list of a city's barangays with their voter counts (cascading picker)."""
    slug = (request.GET.get('province') or '').lower()
    city = request.GET.get('municipality', '')
    if not voter_table(slug) or city not in {m['value'] for m in _municipalities(slug)}:
        return JsonResponse({'ok': False, 'error': 'Unknown city'}, status=400)
    return JsonResponse({'ok': True, 'data': [{'value': b['value'], 'name': b['value'], 'count': b['count']}
                                              for b in sorted(data.barangays(slug, city), key=lambda b: b['value'])]})


# ---------------------------------------------------------------------------
# Barangay Analytics (PHP BARANGAY-EMS index.php, real data)
# ---------------------------------------------------------------------------
def dashboard(request):
    scope, back = _need_scope(request)
    if back:
        return back
    purok_rows, precinct_rows, t = data.breakdown(scope)
    named = [p for p in purok_rows if p['name'] != data.UNSPECIFIED]
    ranked = sorted(named, key=lambda p: (-p['committed'], -p['voters']))
    sectors = data.sector_members(scope)
    main = [(s, label, len(sectors.get(s, ()))) for s, label in hh.MAIN_SECTORS]
    leaders = data.leaders(scope)
    return render(request, 'barangay/dashboard.html', {
        **BASE, 'bs': scope, 't': t,
        'puroks': purok_rows, 'named': named, 'ranked': ranked, 'precincts': precinct_rows,
        'most': ranked[0] if ranked and ranked[0]['committed'] else None,
        'least': ranked[-1] if len(ranked) > 1 and ranked[0]['committed'] else None,
        'leaders': leaders[:5], 'leader_count': len(leaders),
        'main_sectors': main, 'has_sectors': any(n for _, _, n in main),
        'chart': {
            'purok_labels': [p['name'] for p in purok_rows],
            'purok_voters': [p['voters'] for p in purok_rows],
            'purok_committed': [p['committed'] for p in purok_rows],
            'purok_opposition': [p['opposition'] for p in purok_rows],
            'purok_untagged': [p['untagged'] for p in purok_rows],
            'precinct_labels': [p['name'] for p in precinct_rows],
            'precinct_voters': [p['voters'] for p in precinct_rows],
            'precinct_committed': [p['committed'] for p in precinct_rows],
            'sentiment': [t['committed'], t['opposition'], t['untagged']],
        },
    })


def puroks(request):
    """Puroks & sitios (PHP puroks.php): each purok's voters, sentiment, cards, households and
    services, how its voters were placed (saved on the profile vs. the roll address), plus the
    same breakdown by precinct."""
    scope, back = _need_scope(request)
    if back:
        return back
    purok_rows, precinct_rows, t = data.breakdown(scope)
    return render(request, 'barangay/puroks.html', {**BASE, 'bs': scope, 't': t, 'puroks': purok_rows,
                                                    'precincts': precinct_rows})


def leaders(request):
    """Supporter leaders & coordinators (PHP leaders.php): the barangay's coordinators and the
    supporters they lead, from the Barangay EMS's own machinery."""
    scope, back = _need_scope(request)
    if back:
        return back
    rows = data.leaders(scope)
    pos = data.positions(scope)
    led = sum(r['downlines'] for r in rows)
    supporters = sum(1 for code, _ in pos.values() if code == 'supporter')
    return render(request, 'barangay/leaders.html', {
        **BASE, 'bs': scope, 'rows': rows,
        'k': {'leaders': len(rows), 'led': led, 'supporters': supporters,
              'unassigned': supporters - sum(r['supporters'] for r in rows),
              'avg': led / len(rows) if rows else 0},
        'chart': {'labels': [r['name'] for r in rows[:15]], 'supporters': [r['supporters'] for r in rows[:15]],
                  'cardholders': [r['cardholders'] for r in rows[:15]]},
    })


# ---------------------------------------------------------------------------
# Voters List — the PHP "Tagged Supporter Roster" over the real roll
# ---------------------------------------------------------------------------
ROSTER_FILTERS = ('q', 'purok', 'precinct', 'sentiment', 'leader', 'card', 'sector')


def voters(request):
    scope, back = _need_scope(request)
    if back:
        return back
    f = {k: request.GET.get(k, '').strip() for k in ROSTER_FILTERS}
    try:
        page = max(1, int(request.GET.get('page', 1)))
    except (TypeError, ValueError):
        page = 1

    eff, source = data.puroks(scope)
    r = data.roll(scope)
    pos = data.positions(scope)
    cards = data.cardholders(scope)
    purok_names = sorted(set(eff.values()), key=data.purok_sort_key)
    precincts = sorted({p for _, p in r.values() if p})
    leader_rows = data.leaders(scope)

    where, params = data.in_barangay(scope)
    sql = f"SELECT v.id, v.fullname FROM {scope['table']} v WHERE {where}"
    if f['q']:
        for word in f['q'].split()[:4]:
            sql += ' AND v.fullname LIKE %s'
            params.append(f'%{word}%')
    if f['precinct'] in precincts:
        sql += ' AND TRIM(v.precinct) = %s'
        params.append(f['precinct'])
    if f['sector'] in hh.SECTORS:
        sql += (f' AND v.id IN (SELECT voter_id FROM {mach._schema("ext")}.{hh.SECTORS_TABLE} '
                'WHERE province_slug = %s AND sector = %s)')
        params += [scope['province'], f['sector']]
    with connections[mach.RDS].cursor() as cur:
        cur.execute(sql + ' ORDER BY v.fullname', params)
        rows = cur.fetchall()

    # Purok / sentiment / leader / card are known per voter here, so they filter in Python.
    if f['purok'] in purok_names:
        rows = [x for x in rows if eff.get(x[0]) == f['purok']]
    if f['sentiment'] in dict(data.SENTIMENTS):
        rows = [x for x in rows if data.sentiment_of(pos.get(x[0], (None,))[0]) == f['sentiment']]
    if f['leader'].isdigit():
        lid = int(f['leader'])
        rows = [x for x in rows if pos.get(x[0], (None, None))[1] == lid]
    if f['card'] in ('with', 'without'):
        rows = [x for x in rows if (x[0] in cards) == (f['card'] == 'with')]

    total = len(rows)
    pages = max(1, math.ceil(total / PER))
    page = min(page, pages)
    shown = rows[(page - 1) * PER: page * PER]
    ids = [x[0] for x in shown]
    roles = mach.positions_among(scope['province'], ids, data.LEVEL)
    info = hh.details_among(scope['province'], ids) if hh.tables_exist() else {}
    items = []
    for vid, fullname in shown:
        p = roles.get(vid) or {}
        d = info.get(vid, {})
        items.append({'id': vid, 'name': title(fullname), 'purok': eff.get(vid, data.UNSPECIFIED),
                      'purok_source': source.get(vid), 'precinct': r.get(vid, (None, ''))[1] or '—',
                      'sentiment': data.sentiment_of(pos.get(vid, (None,))[0]), 'role': p.get('role'),
                      'leader': p.get('leader_name'), 'card': cards.get(vid),
                      'sex': {'Male': 'M', 'Female': 'F'}.get(d.get('gender'), ''), 'age': d.get('age')})

    counts = {'committed': 0, 'opposition': 0, 'untagged': 0}
    for vid in r:
        counts[data.sentiment_of(pos.get(vid, (None,))[0])] += 1
    keep = {k: v for k, v in f.items() if v}
    qs = urlencode(keep)
    return render(request, 'barangay/voters.html', {
        **BASE, 'bs': scope, 'f': f, 'rows': items, 'total': total, 'page': page, 'pages': pages,
        'offset': (page - 1) * PER, 'filtered': bool(keep), 'counts': counts, 'registered': len(r),
        'card_total': len(cards), 'purok_names': purok_names, 'precinct_names': precincts,
        'leader_options': [(x['id'], x['name']) for x in leader_rows], 'sentiments': data.SENTIMENTS,
        'sectors': hh.SECTORS,
        'prev_url': f'?{qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{qs}&page={page + 1}' if page < pages else '',
    })


# ---------------------------------------------------------------------------
# Smart Card Holders + Social Services: the city pages limited to the barangay
# (smartcard._area / social._area add the barangay), ranked by purok.
# ---------------------------------------------------------------------------
def _purok_of_cards(scope):
    eff, _ = data.puroks(scope)
    with connections[mach.EXT].cursor() as cur:
        area, params = sc._area(scope)
        cur.execute(f'SELECT voter_id, status FROM {sc.TABLE} WHERE {area}', params)
        rows = cur.fetchall()
    out = {}
    for vid, status in rows:
        p = out.setdefault(eff.get(vid, data.UNSPECIFIED), {'holders': 0, 'active': 0, 'pending': 0, 'revoked': 0})
        p[status] = p.get(status, 0) + 1
        if status in sc.HOLDING:
            p['holders'] += 1
    return out


def cards_list(request):
    scope, back = _need_scope(request)
    if back:
        return back
    if not sc.table_exists():
        messages.error(request, 'Smart card table not set up yet — run: manage.py smartcard_setup')
        return redirect('brgy_dashboard')
    f = {k: request.GET.get(k, '').strip() for k in ('q', 'status', 'gender', 'service')}
    f['barangay'] = ''
    try:
        page = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    rows, total, page, pages = sc.city_cards(scope, f['q'], '', f['status'], f['gender'], f['service'], page)
    voters_by = {p['name']: p['voters'] for p in data.breakdown(scope)[0]}
    ranking = []
    for purok, c in _purok_of_cards(scope).items():
        v = voters_by.get(purok, 0)
        ranking.append({**c, 'barangay': purok, 'voters': v,
                        'active_pct': (c['active'] / c['holders'] * 100) if c['holders'] else 0,
                        'coverage': (c['holders'] / v * 100) if v else 0})
    ranking.sort(key=lambda r: -r['holders'])
    qs = urlencode({k: v for k, v in f.items() if v})
    return render(request, 'municipal/cards_list.html', {
        **BASE, 'bs': scope, 'scope': scope, 'unit': 'Purok',
        'summary': sc.city_summary(scope), 'ranking': ranking,
        'area_chart': {'labels': [r['barangay'] for r in ranking], 'active': [r['active'] for r in ranking],
                       'pending': [r['pending'] for r in ranking]},
        'rows': rows, 'total': total, 'page': page, 'pages': pages, 'filters': f,
        'filtered': any(f.values()), 'barangays': [], 'services': sc.SERVICES, 'statuses': sc.STATUS_LABELS,
        'genders': sc.GENDERS,
        'prev_url': f'?{qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{qs}&page={page + 1}' if page < pages else '',
    })


def card_new(request):
    scope, back = _need_scope(request)
    if back:
        return back
    if not sc.table_exists():
        messages.error(request, 'Smart card table not set up yet — run: manage.py smartcard_setup')
        return redirect('brgy_dashboard')
    raw = request.POST.get('voter_id') or request.GET.get('voter') or ''
    voter = _voter(scope, int(raw)) if raw.isdigit() else None
    existing = sc.card_for(scope['province'], voter['id']) if voter else None
    if request.method == 'POST':
        if not voter:
            messages.error(request, 'Pick the cardholder from the suggestions.')
        else:
            try:
                number = sc.issue(scope, voter, request.POST, actor(request), client_ip(request))
                messages.success(request, f'Smart card {number} issued to {voter["name"]}.')
                return redirect('brgy_voter', voter_id=voter['id'])
            except sc.CardError as e:
                messages.error(request, str(e))
        values = {k: request.POST.get(k, '') for k in ('service', 'gender', 'civil_status', 'status', 'issued_date')}
    else:
        values = {'service': '', 'gender': '', 'civil_status': '', 'status': 'active',
                  'issued_date': timezone.localdate().isoformat()}
    return render(request, 'municipal/card_form.html', {
        **BASE, 'bs': scope, 'scope': scope, 'voter': voter, 'existing': existing, 'v': values,
        'services': sc.SERVICES, 'genders': sc.GENDERS, 'civil_statuses': sc.CIVIL_STATUSES,
        'today': timezone.localdate().isoformat(),
    })


@require_POST
def card_status(request, card_id):
    scope, back = _need_scope(request)
    if back:
        return back
    status = request.POST.get('status', '')
    try:
        area, params = sc._area(scope)
        with connections[mach.EXT].cursor() as cur:
            cur.execute(f'SELECT 1 FROM {sc.TABLE} WHERE id = %s AND {area}', [int(card_id), *params])
            if not cur.fetchone():
                raise sc.CardError('That card is not in this barangay.')
        if sc.set_status(scope, card_id, status, actor(request), client_ip(request)):
            messages.success(request, f'Card updated: {sc.STATUS_LABELS.get(status, status)}.')
    except sc.CardError as e:
        messages.error(request, str(e))
    return safe_next(request, 'brgy_cards')


def social_list(request):
    scope, back = _need_scope(request)
    if back:
        return back
    if not soc.table_exists():
        messages.error(request, 'Social services table not set up yet — run: manage.py social_setup')
        return redirect('brgy_dashboard')
    f = {k: request.GET.get(k, '').strip() for k in ('q', 'type', 'status', 'from', 'to')}
    f['barangay'] = ''
    try:
        page = int(request.GET.get('page', 1))
    except (TypeError, ValueError):
        page = 1
    rows, total, total_amount, page, pages = soc.city_records(scope, f['q'], f['type'], f['status'], f['from'],
                                                              f['to'], page)
    ranking = soc.area_breakdown(scope)            # puroks, drilling into assistance types
    for a in ranking:
        a['name'] = a['key'] or 'No purok'
    summary = soc.city_summary(scope)
    qs = urlencode({k: v for k, v in f.items() if v})
    return render(request, 'municipal/social_list.html', {
        **BASE, 'bs': scope, 'scope': scope, 'unit': 'Purok',
        'summary': summary, 'ranking': ranking, 'charts': social_charts(ranking, summary), 'barangays': [],
        'types': list(soc.ASSISTANCE_TYPES), 'statuses': soc.STATUSES, 'rows': rows, 'total': total,
        'total_amount': total_amount, 'page': page, 'pages': pages, 'filters': f, 'filtered': any(f.values()),
        'prev_url': f'?{qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{qs}&page={page + 1}' if page < pages else '',
    })


def social_new(request):
    scope, back = _need_scope(request)
    if back:
        return back
    if not soc.table_exists():
        messages.error(request, 'Social services table not set up yet — run: manage.py social_setup')
        return redirect('brgy_dashboard')
    raw = request.POST.get('voter_id') or request.GET.get('voter') or ''
    voter = _voter(scope, int(raw)) if raw.isdigit() else None
    if request.method == 'POST':
        if not voter:
            messages.error(request, 'Pick the beneficiary from the suggestions so the record '
                                    'is linked to their voter profile.')
        else:
            try:
                new_id = soc.record(scope, voter, request.POST, actor(request), client_ip(request))
                messages.success(request, f'Social service #{new_id} recorded.')
                return safe_next(request, 'brgy_social')
            except soc.SocialError as e:
                messages.error(request, str(e))
        values = {k: request.POST.get(k, '') for k in SOCIAL_FORM_FIELDS}
    else:
        values = {k: '' for k in SOCIAL_FORM_FIELDS}
        values.update({'status': soc.DEFAULT_STATUS, 'date_requested': timezone.localdate().isoformat()})
        if voter:
            eff, _ = data.puroks(scope)
            values.update({'beneficiary': voter['name'], 'region': scope['region'] or '',
                           'province': scope['province_name'], 'city_municipality': scope['municipality_pretty'],
                           'barangay': voter['barangay_pretty'], 'street': title(voter.get('address')),
                           'purok': '' if eff.get(voter['id']) == data.UNSPECIFIED else eff.get(voter['id'], '')})
    return render(request, 'municipal/social_form.html', {
        **BASE, 'bs': scope, 'scope': scope, 'voter': voter, 'v': values,
        'types': soc.ASSISTANCE_TYPES, 'statuses': soc.STATUSES, 'genders': soc.GENDERS,
        'civil_statuses': soc.CIVIL_STATUSES, 'agencies': soc.AGENCIES, 'programs': soc.PROGRAMS,
        'relationships': soc.RELATIONSHIPS, 'today': timezone.localdate().isoformat(),
    })


@require_POST
def social_status(request, record_id):
    scope, back = _need_scope(request)
    if back:
        return back
    status = request.POST.get('status', '')
    try:
        area, params = soc._area(scope)
        with connections[mach.EXT].cursor() as cur:
            cur.execute(f'SELECT 1 FROM {soc.TABLE} WHERE id = %s AND {area}', [int(record_id), *params])
            if not cur.fetchone():
                raise soc.SocialError('That record is not in this barangay.')
        if soc.set_status(scope, record_id, status, actor(request), client_ip(request)):
            messages.success(request, f'Social service #{record_id} marked {status}.')
    except soc.SocialError as e:
        messages.error(request, str(e))
    return safe_next(request, 'brgy_social')


def api_search_voters(request):
    """Type-ahead over the barangay's roll (card / service forms)."""
    scope = data.bscope(request)
    if not scope:
        return JsonResponse({'ok': False, 'error': 'No barangay selected'}, status=400)
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse({'ok': True, 'data': []})
    where, params = data.in_barangay(scope)
    with connections[mach.RDS].cursor() as cur:
        cur.execute(f"SELECT v.id, v.fullname, v.barangay FROM {scope['table']} v WHERE {where} AND v.fullname LIKE %s "
                    'ORDER BY v.fullname LIMIT 15', [*params, q + '%'])
        rows = cur.fetchall()
    return JsonResponse({'ok': True, 'data': [{'id': vid, 'name': title(n), 'barangay': title(b)} for vid, n, b in rows]})


# ---------------------------------------------------------------------------
# Quick Count — attendance per precinct (simulated like the other levels, see quickcount.py)
# ---------------------------------------------------------------------------
def quick_count(request):
    scope, back = _need_scope(request)
    if back:
        return back
    areas = qc.precinct_areas(scope['barangay'], data.precinct_registered(scope))
    top = qc.rank_precincts(scope, data.precinct_candidates(scope, qc.PRECINCT_CANDIDATES, qc.MAX_PRECINCT))
    return render(request, 'municipal/quick_count.html', quick_count_context(
        scope, areas, top, 'brgy', **BASE, bs=scope, scope=scope, area_name=f'Brgy. {scope["barangay"]}'))


# ---------------------------------------------------------------------------
# Heat Map — the barangay on the map (one pin: its centre) and its puroks as a ranked heat list.
# Puroks and precinct venues have no coordinates anywhere, so they are not pinned.
# ---------------------------------------------------------------------------
def heat_map(request):
    scope, back = _need_scope(request)
    if back:
        return back
    days, service, card_status = heat_filters(request)
    purok_rows, _, t = data.breakdown(scope)
    social = data.social_by_purok(scope, service)
    activity = data.activity_by_purok(scope, int(days))
    sectors = data.sectors_by_purok(scope)
    status = data.card_status_by_purok(scope)
    lgus = []
    for p in purok_rows:
        name, s, st = p['name'], social.get(p['name'], {}), status.get(p['name'], {'active': 0, 'pending': 0})
        holders = st.get(card_status, 0) if card_status else st['active'] + st['pending']
        sec = sectors.get(name, {})
        lgus.append({
            'name': name, 'raw': name, 'lat': None, 'lng': None, 'approx': False,
            'voters': p['voters'], 'precincts': p['precinct_count'],
            'cards': holders, 'active_cards': st['active'], 'pending_cards': st['pending'],
            'card_pct': round(holders / p['voters'] * 100, 2) if p['voters'] else 0,
            'supporters': p['committed'], 'coverage': round(p['reach'], 2),
            'coordinators': p['coordinators'], 'opposition': p['opposition'],
            'beneficiaries': s.get('beneficiaries', 0), 'records': s.get('records', 0),
            'released': round(s.get('released', 0)), 'open': s.get('open', 0), 'open_amount': round(s.get('open_amount', 0)),
            'sector_members': sum(sec.values()),
            'top_sectors': sorted(sec.items(), key=lambda kv: -kv[1])[:5],
            'households': p['households'], 'activity': activity.get(name, 0),
        })
    loc = geo.locations(scope).get(scope['barangay'])
    center = {'lat': loc['lat'], 'lng': loc['lng'], 'approx': loc['source'] == 'approx'} if loc else None
    return heat_map_response(request, scope, lgus, t['coordinators'], (days, service, card_status),
                             f'Brgy. {scope["barangay"]}', 'brgy', **BASE, bs=scope, scope=scope, center=center)


@require_POST
def heat_map_locate(request):
    """Staff: look up the barangay's centre on OpenStreetMap (its name only)."""
    scope, back = _need_scope(request)
    if back:
        return back
    if not request.user.is_staff:
        messages.error(request, 'Only staff can locate the barangay.')
        return redirect('brgy_heat_map')
    try:
        found, approx, _ = geo.locate(scope, [scope['barangay']], scope['province_name'], limit=1, log=lambda *_: None)
        messages.success(request, 'Barangay located on OpenStreetMap.' if found else
                         'Not found on OpenStreetMap; it is shown near the town centre.' if approx else 'Already located.')
    except (OSError, RuntimeError) as e:
        messages.error(request, f'Could not reach OpenStreetMap: {e}')
    return redirect('brgy_heat_map')


# ---------------------------------------------------------------------------
# AI Analytics — the barangay's per-purok (and per-precinct) aggregates only
# ---------------------------------------------------------------------------
def ai_analytics(request):
    scope, back = _need_scope(request)
    if back:
        return back
    names = [p['name'] for p in data.breakdown(scope)[0] if p['name'] != data.UNSPECIFIED]
    return render(request, 'municipal/ai_analytics.html', {
        **BASE, 'bs': scope, 'scope': scope, 'configured': ai.configured(), 'model': settings.GEMINI_MODEL,
        'places': names + [scope['barangay']], 'examples': names[:2],
    })


def snapshot(scope):
    """Aggregates for the prompt: per purok (+ per precinct), never names or records."""
    purok_rows, precinct_rows, t = data.breakdown(scope)
    social = data.social_by_purok(scope)
    activity = data.activity_by_purok(scope, 30)
    sectors = data.sectors_by_purok(scope)
    status = data.card_status_by_purok(scope)
    units = {}
    for p in purok_rows:
        st, s = status.get(p['name'], {'active': 0, 'pending': 0}), social.get(p['name'], {})
        units[p['name']] = {
            'registered_voters': p['voters'], 'precincts': p['precinct_count'], 'barangays': 0,
            'supporters': p['supporters'], 'coordinators': p['coordinators'], 'opposition': p['opposition'],
            'committed': p['committed'], 'untagged': p['untagged'],
            'cards_active': st['active'], 'cards_pending': st['pending'],
            'social_records': s.get('records', 0), 'beneficiaries': s.get('beneficiaries', 0),
            'released_php': round(s.get('released', 0)), 'households': p['households'],
            'ems_activity_30d': activity.get(p['name'], 0), 'sectors': sectors.get(p['name'], {}),
        }
    where = 'province_slug = %s AND municipality = %s AND barangay = %s'
    note = (f'Puroks: the roll has no purok field. {t["saved"]} voters have a saved purok, {t["from_address"]} were '
            f'placed from their roll address, and {t["unspecified"]} are "Unspecified"; purok figures cover only '
            'placed voters, so use precincts_breakdown for complete coverage.')
    return ai._rollup_snapshot(
        _ckey('ai-snap-brgy', scope['province'], scope['municipality'] + '|' + scope['barangay']),
        {'barangay': scope['barangay'], 'city': scope['municipality_pretty'], 'province': scope['province_name']},
        units, 'purok', 'puroks', 'purok', where, [scope['province'], scope['municipality'], scope['barangay']], 'brgy',
        totals_extra={'precincts': t['precinct_count'], 'barangays': 1, 'committed': t['committed'],
                      'untagged': t['untagged']},
        machinery_area=('province_slug = %s', [scope['province']]),
        extra={'precincts_breakdown': [{'precinct': p['name'], 'registered_voters': p['voters'],
                                        'committed': p['committed'], 'opposition': p['opposition'],
                                        'cardholders': p['cards']} for p in precinct_rows],
               'sentiment': {'committed': t['committed'], 'opposition': t['opposition'], 'untagged': t['untagged'],
                             'definition': 'committed = supporters + coordinators in this EMS; untagged = no position'}},
        notes_extra=[note])


@require_POST
def api_ai(request):
    scope = data.bscope(request)
    if not scope:
        return JsonResponse({'error': 'No barangay selected.'}, status=400)
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    query = str(body.get('query', '')).strip()[:ai.MAX_QUERY]
    intent = body.get('intent') if body.get('intent') in ai.INTENTS else 'general'
    if not query:
        return JsonResponse({'error': 'Type a question first.'}, status=400)
    try:
        answer = ai.ask(snapshot(scope), query, intent, level='barangay')
    except ai.AIError as e:
        return JsonResponse({'error': str(e), 'detail': e.detail}, status=502)
    return JsonResponse({'ok': True, 'intent': intent, 'query': query, 'result': answer})


# ---------------------------------------------------------------------------
# Transaction List — the barangay's voters, its own machinery entries
# ---------------------------------------------------------------------------
def transactions_list(request):
    scope, back = _need_scope(request)
    if back:
        return back
    return transactions_response(request, scope, f'Brgy. {scope["barangay"]}', level='brgy', **BASE, bs=scope,
                                 scope=scope)
