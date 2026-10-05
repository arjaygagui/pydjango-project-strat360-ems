"""
AI Analytics — port of MUNICIPAL-EMS ai-analytics.php (Google Gemini, same answer schema).

Differences from PHP, on purpose:
  * The PHP city page posts to the PROVINCIAL backend (STRAT360-EMS/includes/ai_chat.php),
    so its answers aren't about the selected city. Here the snapshot is the chosen city's.
  * PHP sends a "top requesters" list (beneficiary NAMES) to Google. Here the snapshot is
    aggregates only — counts, sums and percentages per barangay / category. No voter names,
    ids, addresses, birthdates or any other personal record ever leaves the server.
  * The key comes from .env (settings.GEMINI_API_KEY) and is sent in a request header, not
    in the URL, so it can't end up in logs. PHP hard-codes it in includes/ai_config.php.
"""
import datetime
import hashlib
import json
import urllib.error
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.db import connections
from django.utils import timezone

from . import household as hh
from . import smartcard as sc
from . import social as soc
from .machinery import EXT, audit_level_sql, table as machinery_table, voter_table_qualified
from .text import title

ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
SNAPSHOT_TTL = 300          # seconds; the numbers are live-ish, the AI call is the slow part
# Zero values are left out of the rankings so an all-zero metric never names a "leader".
RANKINGS_NOTE = ('Each ranking lists only {unit} entries with a value above 0; any {unit} not listed has 0 for that '
                 'metric. An empty ranking means no {unit} has any yet.')
MAX_QUERY = 1000
INTENTS = ('general', 'overview', 'top_barangays', 'underperforming', 'sectoral', 'smart_card', 'social_services',
           'cross_domain', 'strategy')

SYSTEM_TEMPLATE = """You are the AI Analytics Assistant for the Strat360 EMS (Election Management System) of {area}.
You analyze, grounded ONLY in the provided DATA_SNAPSHOT:

  1. EMS: registered voters, political machinery (supporters, coordinators, opposition) and sectors
     (seniors, youth, PWD, OFW, TODA, single parents and others) per {unit}.
  2. Smart Card Holders: holders, active vs. pending, holders per {unit}, services.
  3. Social Services: records, amounts released vs. pending/approved, assistance types, {unit}
     breakdown, 14-day daily volume.
  4. Cross-domain: {units} strong in one domain but weak in another.

The snapshot contains aggregates only (no individual voters). For "which {unit} leads / ranks
N-th" statements use rankings_highest_first exactly as given; never re-rank numbers yourself, and never
claim one {unit} leads a metric unless it is rank 1 there. Speak in clear, executive-level English
for a campaign / government dashboard. Every number you state must come from DATA_SNAPSHOT; if the data does
not support a claim, say so plainly. Never invent numbers. If data_notes says some records are demo
(mock) data, mention that the figures include demo data."""

SYSTEM_SCHEMA = """
Respond with VALID JSON ONLY (no markdown) matching exactly:
{
  "summary": string,                       // 2-4 sentence executive summary
  "highlights": [string, ...],             // 3-5 insights, each <= 140 chars
  "recommendations": [string, ...],        // 3-5 concrete actions, each <= 160 chars
  "chart": {"type": "bar"|"doughnut"|"line"|"pie", "title": string, "labels": [string, ...],
            "datasets": [{"label": string, "data": [number, ...]}]} | null,
  "metric_cards": [{"label": string, "value": string, "trend": "up"|"down"|"flat"|null, "hint": string|null}]
}
Rules: keep labels and data aligned in length; prefer bar/doughnut unless a trend is clear; round large
numbers in value strings (e.g. "1.2K", "50,590", "2.8%"); use {unit} names exactly as in the snapshot."""

LEVELS = {   # snapshot level -> (area described, unit, units)
    'city': ('one Philippine city/municipality', 'barangay', 'barangays'),
    'province': ('one Philippine province, analysed across its cities and municipalities', 'city/municipality',
                 'cities/municipalities'),
    'national': ('the Philippines (or one region), analysed across its provinces', 'province', 'provinces'),
    'barangay': ('one Philippine barangay, analysed across its puroks (and its precincts)', 'purok', 'puroks'),
}


def system_prompt(level):
    area, unit, units = LEVELS[level]
    text = SYSTEM_TEMPLATE + '\n' + SYSTEM_SCHEMA      # plain replace: the schema has literal JSON braces
    return text.replace('{area}', area).replace('{units}', units).replace('{unit}', unit)


class AIError(Exception):
    def __init__(self, message, detail=''):
        super().__init__(message)
        self.detail = detail


def configured():
    return bool(settings.GEMINI_API_KEY)


def _q(cur, sql, params):
    cur.execute(sql, params)
    return cur.fetchall()


def snapshot(scope, brgy_totals):
    """City aggregates for the prompt (cached a few minutes). Aggregates only — no personal data."""
    key = f'ai-snap:{scope["province"]}:{hashlib.md5(scope["municipality"].encode("utf-8")).hexdigest()}'
    snap = cache.get(key)
    if snap is not None:
        return snap
    prov, city = scope['province'], scope['municipality']
    roll = voter_table_qualified(scope)
    brgys = {name: {'registered_voters': t['total'], 'precincts': t['precincts']} for name, t in brgy_totals.items()}

    def put(name, field, value):
        brgys.setdefault(title(name or ''), {})[field] = value

    today = timezone.localdate()
    since30 = (timezone.localtime() - datetime.timedelta(days=30)).astimezone(datetime.timezone.utc).replace(tzinfo=None)
    with connections[EXT].cursor() as cur:
        machinery = {}
        for brgy, code, n in _q(cur, f'SELECT v.barangay, p.role_code, COUNT(*) FROM {machinery_table("city")} p JOIN {roll} v '
                                     'ON v.id = p.voter_id WHERE p.province_slug = %s AND v.municipality = %s GROUP BY 1, 2',
                                [prov, city]):
            b = machinery.setdefault(title(brgy), {})
            b[code] = b.get(code, 0) + n
        for name, roles in machinery.items():
            put(name, 'supporters', roles.get('supporter', 0))
            put(name, 'coordinators', roles.get('municipal_coordinator', 0) + roles.get('barangay_coordinator', 0))
            put(name, 'opposition', roles.get('opposition', 0))

        cards = {'by_status': {}, 'by_service': {}}
        if sc.table_exists():
            for brgy, status, service, n in _q(cur, f'SELECT barangay, status, service, COUNT(*) FROM {sc.TABLE} '
                                                  'WHERE province_slug = %s AND municipality = %s GROUP BY 1, 2, 3', [prov, city]):
                b = brgys.setdefault(title(brgy or ''), {})
                b[f'cards_{status}'] = b.get(f'cards_{status}', 0) + n
                cards['by_status'][status] = cards['by_status'].get(status, 0) + n
                if status in sc.HOLDING:
                    cards['by_service'][service or 'Unspecified'] = cards['by_service'].get(service or 'Unspecified', 0) + n
            cards['mock_cards'] = _q(cur, f'SELECT COUNT(*) FROM {sc.TABLE} WHERE province_slug = %s AND municipality = %s '
                                          'AND is_mock = 1', [prov, city])[0][0]

        social = {'by_type': {}, 'by_status': {}, 'last_14_days': []}
        if soc.table_exists():
            where = 'province_slug = %s AND municipality = %s'
            for brgy, n, people, released in _q(cur, f"SELECT barangay, COUNT(*), COUNT(DISTINCT voter_id), "
                                                     f"COALESCE(SUM(CASE WHEN status = 'Released' THEN amount END), 0) "
                                                     f'FROM {soc.TABLE} WHERE {where} GROUP BY 1', [prov, city]):
                b = brgys.setdefault(title(brgy or ''), {})
                b['social_records'] = b.get('social_records', 0) + n
                b['beneficiaries'] = b.get('beneficiaries', 0) + people
                b['released_php'] = round(b.get('released_php', 0) + float(released or 0))
            for atype, n, amount in _q(cur, f'SELECT assistance_type, COUNT(*), COALESCE(SUM(amount), 0) FROM {soc.TABLE} '
                                            f'WHERE {where} GROUP BY 1', [prov, city]):
                social['by_type'][atype or 'Unspecified'] = {'records': n, 'amount_php': round(float(amount))}
            for status, n, amount in _q(cur, f'SELECT status, COUNT(*), COALESCE(SUM(amount), 0) FROM {soc.TABLE} '
                                             f'WHERE {where} GROUP BY 1', [prov, city]):
                social['by_status'][status] = {'records': n, 'amount_php': round(float(amount))}
            daily = dict(_q(cur, f'SELECT {soc.REQ_DATE}, COUNT(*) FROM {soc.TABLE} WHERE {where} '
                                 f'AND {soc.REQ_DATE} >= %s GROUP BY 1', [prov, city, today - datetime.timedelta(days=13)]))
            social['last_14_days'] = [{'date': (today - datetime.timedelta(days=d)).isoformat(),
                                       'records': daily.get(today - datetime.timedelta(days=d), 0)} for d in range(13, -1, -1)]
            social['mock_records'] = _q(cur, f'SELECT COUNT(*) FROM {soc.TABLE} WHERE {where} AND is_mock = 1', [prov, city])[0][0]

        sectors = {}
        if hh.tables_exist():
            for brgy, sector, n in _q(cur, f'SELECT v.barangay, s.sector, COUNT(*) FROM {hh.SECTORS_TABLE} s JOIN {roll} v '
                                           'ON v.id = s.voter_id WHERE s.province_slug = %s AND s.municipality = %s GROUP BY 1, 2',
                                      [prov, city]):
                sectors[sector] = sectors.get(sector, 0) + n
                b = brgys.setdefault(title(brgy), {})
                b.setdefault('sectors', {})[sector] = b.get('sectors', {}).get(sector, 0) + n
            for brgy, n in _q(cur, f'SELECT v.barangay, COUNT(DISTINCT m.voter_id) FROM {hh.MEMBERS} m JOIN {roll} v '
                                   'ON v.id = m.voter_id WHERE m.province_slug = %s AND m.municipality = %s GROUP BY 1', [prov, city]):
                put(brgy, 'households', n)
        hide, hide_params = audit_level_sql('city')       # as the city's Transaction List
        for brgy, n in _q(cur, f'SELECT v.barangay, COUNT(*) FROM ems_voter_audit a JOIN {roll} v ON v.id = a.voter_id '
                               f'WHERE a.province_slug = %s AND v.municipality = %s AND {hide} AND a.created_at >= %s GROUP BY 1',
                          [prov, city, *hide_params, since30]):
            put(brgy, 'ems_activity_30d', n)
        mock_people = _q(cur, f"SELECT COUNT(*) FROM {machinery_table('city')} p JOIN " + roll + " v ON v.id = p.voter_id "
                              "WHERE p.province_slug = %s AND v.municipality = %s AND p.assigned_by = 'mock-seed'", [prov, city])[0][0]

    total_voters = sum(b.get('registered_voters', 0) for b in brgys.values())
    totals = {k: sum(b.get(k, 0) for b in brgys.values())
              for k in ('supporters', 'coordinators', 'opposition', 'social_records', 'beneficiaries', 'released_php', 'households')}
    notes = []
    if mock_people or cards.get('mock_cards') or social.get('mock_records'):
        notes.append(f'Includes demo (mock) data seeded for testing: {mock_people} machinery positions, '
                     f'{cards.get("mock_cards", 0)} smart cards, {social.get("mock_records", 0)} social-service records.')
    notes.append('The voter roll has no ages, votes cast or turnout; sector and age data exist only for voters '
                 'whose Personal Details were recorded.')
    # Pre-computed rankings: models mis-order numbers they have to sort themselves, so the
    # snapshot states who leads each metric (highest first, with the value).
    ranked_metrics = {
        'supporters': 'supporters', 'supporter_coverage_pct': None, 'active_smart_cards': 'cards_active',
        'smart_cardholders': None, 'social_beneficiaries': 'beneficiaries', 'released_php': 'released_php',
        'sector_members': None, 'ems_activity_30d': 'ems_activity_30d',
    }

    def metric(b, name):
        if name == 'supporter_coverage_pct':
            return round(b.get('supporters', 0) / b['registered_voters'] * 100, 2) if b.get('registered_voters') else 0
        if name == 'smart_cardholders':
            return b.get('cards_active', 0) + b.get('cards_pending', 0)
        if name == 'sector_members':
            return sum(b.get('sectors', {}).values())
        return b.get(ranked_metrics[name], 0)

    rankings = {}
    for name in ranked_metrics:
        order = sorted(((metric(b, name), n) for n, b in brgys.items() if n and metric(b, name)), key=lambda x: (-x[0], x[1]))
        rankings[name] = [{'rank': i + 1, 'barangay': n, 'value': v} for i, (v, n) in enumerate(order)]
    snap = {
        'city': scope['municipality_pretty'], 'province': scope['province_name'],
        'rankings_highest_first': rankings, 'rankings_note': RANKINGS_NOTE.format(unit='barangay'),
        'as_of': timezone.localtime().strftime('%Y-%m-%d %H:%M'),
        'totals': {'registered_voters': total_voters, 'barangays': len(brgy_totals),
                   'precincts': sum(t['precincts'] for t in brgy_totals.values()), **totals,
                   'supporter_coverage_pct': round(totals['supporters'] / total_voters * 100, 2) if total_voters else 0},
        'smart_cards': cards, 'social_services': social, 'sectors': sectors,
        'barangays': [{'barangay': name, **vals} for name, vals in sorted(brgys.items(), key=lambda kv: -kv[1].get('registered_voters', 0))],
        'data_notes': notes,
    }
    cache.set(key, snap, SNAPSHOT_TTL)
    return snap


def province_snapshot(ps, rows, activity, households):
    """Province aggregates for the prompt, per city / municipality (cached a few minutes).

    `rows` is provincial.data.dashboard()'s per-city rows (voters, machinery, cards, social,
    sectors); `activity` / `households` are heatmap's per-city counts. Aggregates only — no
    personal data, same rule as the city snapshot.
    """
    units = {m['name']: {
        'registered_voters': m['voters'], 'barangays': len(m['barangays']), 'precincts': m['precincts'],
        'supporters': m['supporters'], 'coordinators': m['coordinators'], 'opposition': m['opposition'],
        'cards_active': m['active_cards'], 'cards_pending': m['cards'] - m['active_cards'],
        'social_records': m['records'], 'beneficiaries': m['beneficiaries'], 'released_php': round(m['released']),
        'households': households.get(m['raw'], 0), 'ems_activity_30d': activity.get(m['raw'], 0),
        'sectors': {s: n for s, n in m['sectors'].items() if n},
    } for m in rows}
    return _rollup_snapshot(f'ai-snap-prov:{ps["province"]}', {'province': ps['province_name'], 'region': ps['region']},
                            units, 'city_municipality', 'cities_municipalities', 'city/municipality',
                            'province_slug = %s', [ps['province']], 'prov')


def national_snapshot(region, rows, activity, households):
    """Nationwide (or one region's) aggregates for the prompt, per province (cached a few minutes).

    `rows` is national.data.dashboard()'s per-province rows; `activity` / `households` are
    heatmap's per-province counts. Aggregates only — no personal data.
    """
    units = {p['name']: {
        'region': p['region'], 'registered_voters': p['voters'], 'cities_municipalities': len(p['cities']),
        'barangays': p['barangays'], 'precincts': p['precincts'],
        'supporters': p['supporters'], 'coordinators': p['coordinators'], 'opposition': p['opposition'],
        'cards_active': p['active_cards'], 'cards_pending': p['cards'] - p['active_cards'],
        'social_records': p['records'], 'beneficiaries': p['beneficiaries'], 'released_php': round(p['released']),
        'households': households.get(p['slug'], 0), 'ems_activity_30d': activity.get(p['slug'], 0),
        'sectors': {s: n for s, n in p['sectors'].items() if n},
    } for p in rows}
    slugs = [p['slug'] for p in rows]
    area, params = (f"province_slug IN ({','.join(['%s'] * len(slugs))})", slugs) if region else ('1 = 1', [])
    key = 'ai-snap-nat:' + hashlib.sha1((region or 'all').encode('utf-8')).hexdigest()[:12]
    return _rollup_snapshot(key, {'country': 'Philippines', 'region': region or 'All regions'}, units,
                            'province', 'provinces', 'province', area, params, 'nat')


def _rollup_snapshot(key, header, units, unit_key, units_key, unit_label, area, params, level,
                     totals_extra=None, machinery_area=None, extra=None, notes_extra=()):
    """Shared by the barangay (units = puroks), province (units = cities) and national (units =
    provinces) snapshots: the per-unit rows plus area-wide smart cards, social services, sectors,
    totals and rankings. `area` / `params` limit the card and social tables (e.g. province_slug = %s);
    `machinery_area` (sql, params) the machinery table when that differs; `totals_extra` overrides
    totals that don't add up across units; `extra` adds keys to the snapshot."""
    snap = cache.get(key)
    if snap is not None:
        return snap
    today = timezone.localdate()
    with connections[EXT].cursor() as cur:
        cards = {'by_status': {}, 'by_service': {}}
        if sc.table_exists():
            for status, service, n in _q(cur, f'SELECT status, service, COUNT(*) FROM {sc.TABLE} WHERE {area} '
                                              'GROUP BY 1, 2', params):
                cards['by_status'][status] = cards['by_status'].get(status, 0) + n
                if status in sc.HOLDING:
                    cards['by_service'][service or 'Unspecified'] = cards['by_service'].get(service or 'Unspecified', 0) + n
            cards['mock_cards'] = _q(cur, f'SELECT COUNT(*) FROM {sc.TABLE} WHERE {area} AND is_mock = 1', params)[0][0]
        social = {'by_type': {}, 'by_status': {}, 'last_14_days': []}
        if soc.table_exists():
            for atype, n, amount in _q(cur, f'SELECT assistance_type, COUNT(*), COALESCE(SUM(amount), 0) FROM {soc.TABLE} '
                                            f'WHERE {area} GROUP BY 1', params):
                social['by_type'][atype or 'Unspecified'] = {'records': n, 'amount_php': round(float(amount))}
            for status, n, amount in _q(cur, f'SELECT status, COUNT(*), COALESCE(SUM(amount), 0) FROM {soc.TABLE} '
                                             f'WHERE {area} GROUP BY 1', params):
                social['by_status'][status] = {'records': n, 'amount_php': round(float(amount))}
            daily = dict(_q(cur, f'SELECT {soc.REQ_DATE}, COUNT(*) FROM {soc.TABLE} WHERE {area} '
                                 f'AND {soc.REQ_DATE} >= %s GROUP BY 1', params + [today - datetime.timedelta(days=13)]))
            social['last_14_days'] = [{'date': (today - datetime.timedelta(days=d)).isoformat(),
                                       'records': daily.get(today - datetime.timedelta(days=d), 0)} for d in range(13, -1, -1)]
            social['mock_records'] = _q(cur, f'SELECT COUNT(*) FROM {soc.TABLE} WHERE {area} AND is_mock = 1', params)[0][0]
        # This level's own machinery (machinery.TABLES) — only its seeded rows count as demo data.
        m_area, m_params = machinery_area or (area, params)
        mock_people = _q(cur, f"SELECT COUNT(*) FROM {machinery_table(level)} WHERE {m_area} AND assigned_by = 'mock-seed'",
                         m_params)[0][0]

    sectors = {}
    for c in units.values():
        for s, n in c['sectors'].items():
            sectors[s] = sectors.get(s, 0) + n
    total_voters = sum(c['registered_voters'] for c in units.values())
    totals = {k: sum(c[k] for c in units.values())
              for k in ('supporters', 'coordinators', 'opposition', 'social_records', 'beneficiaries', 'released_php',
                        'households', 'barangays', 'precincts')}
    totals.update(totals_extra or {})
    notes = []
    if mock_people or cards.get('mock_cards') or social.get('mock_records'):
        notes.append(f'Includes demo (mock) data seeded for testing: {mock_people} machinery positions, '
                     f'{cards.get("mock_cards", 0)} smart cards, {social.get("mock_records", 0)} social-service records.')
    notes.append('The voter roll has no ages, votes cast or turnout; sector and age data exist only for voters '
                 'whose Personal Details were recorded.')
    notes += list(notes_extra)

    def metric(c, name):
        return {'supporters': c['supporters'], 'registered_voters': c['registered_voters'],
                'supporter_coverage_pct': round(c['supporters'] / c['registered_voters'] * 100, 2) if c['registered_voters'] else 0,
                'active_smart_cards': c['cards_active'], 'smart_cardholders': c['cards_active'] + c['cards_pending'],
                'social_beneficiaries': c['beneficiaries'], 'released_php': c['released_php'],
                'sector_members': sum(c['sectors'].values()), 'ems_activity_30d': c['ems_activity_30d']}[name]

    rankings = {}
    for name in ('registered_voters', 'supporters', 'supporter_coverage_pct', 'active_smart_cards', 'smart_cardholders',
                 'social_beneficiaries', 'released_php', 'sector_members', 'ems_activity_30d'):
        order = sorted(((metric(c, name), n) for n, c in units.items() if metric(c, name)), key=lambda x: (-x[0], x[1]))
        rankings[name] = [{'rank': i + 1, unit_key: n, 'value': v} for i, (v, n) in enumerate(order)]
    snap = {
        **header,
        'rankings_highest_first': rankings, 'rankings_note': RANKINGS_NOTE.format(unit=unit_label),
        'as_of': timezone.localtime().strftime('%Y-%m-%d %H:%M'),
        'totals': {'registered_voters': total_voters, units_key: len(units), **totals,
                   'supporter_coverage_pct': round(totals['supporters'] / total_voters * 100, 2) if total_voters else 0},
        'smart_cards': cards, 'social_services': social, 'sectors': sectors,
        units_key: [{unit_key: n, **vals} for n, vals in sorted(units.items(), key=lambda kv: -kv[1]['registered_voters'])],
        'data_notes': notes,
        **(extra or {}),
    }
    cache.set(key, snap, SNAPSHOT_TTL)
    return snap


def ask(snap, query, intent, level='city'):
    """Call Gemini; returns the parsed answer dict (PHP's schema). Raises AIError."""
    if not configured():
        raise AIError('AI Analytics is not configured — add GEMINI_API_KEY to .env and restart the server.')
    intent = intent.replace('barangays', LEVELS[level][2])      # province: top_cities/municipalities
    payload = {
        'systemInstruction': {'role': 'user', 'parts': [{'text': system_prompt(level)}]},
        'contents': [{'role': 'user', 'parts': [{'text': f'INTENT: {intent}\nUSER_QUESTION: {query}\n\n'
                                                         f'DATA_SNAPSHOT (authoritative):\n{json.dumps(snap, default=str)}'}]}],
        'generationConfig': {'temperature': 0.35, 'topP': 0.9, 'maxOutputTokens': 8192,
                             'response_mime_type': 'application/json', 'thinkingConfig': {'thinkingBudget': 0}},
    }
    req = urllib.request.Request(ENDPOINT.format(model=settings.GEMINI_MODEL), data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', 'x-goog-api-key': settings.GEMINI_API_KEY},
                                 method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode('utf-8')).get('error', {}).get('message', '')
        except (ValueError, AttributeError):
            detail = ''
        raise AIError(f'Gemini API error ({e.code}).', detail)
    except (urllib.error.URLError, TimeoutError) as e:
        raise AIError('Network error contacting Gemini.', str(getattr(e, 'reason', e)))

    try:
        cand = body['candidates'][0]
        text = cand['content']['parts'][0]['text']
    except (KeyError, IndexError, TypeError):
        raise AIError('Gemini returned no answer.', json.dumps(body.get('promptFeedback', ''))[:300])
    for attempt in (text, text.strip().removeprefix('```json').removeprefix('```').removesuffix('```')):
        try:
            answer = json.loads(attempt)
            if isinstance(answer, dict):
                return answer
        except ValueError:
            continue
    reason = cand.get('finishReason', 'unknown')
    hint = {'MAX_TOKENS': 'The answer was cut off — try a more focused question.',
            'SAFETY': 'The answer was blocked by safety filters.'}.get(reason, '')
    raise AIError('Gemini returned an answer that isn\'t valid JSON.', hint or text[:600])
