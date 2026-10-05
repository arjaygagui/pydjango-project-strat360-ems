"""
Barangay EMS data: one barangay of one city, broken down by purok and precinct.

  * Voters, precincts and addresses come from the city's voter roll (cvl_national, read-only).
  * Purok: the voter's saved Purok / sitio (muni_voter_details.purok), else one named in the
    roll address (household.purok_in_address), else "Unspecified". The roll has no purok field.
  * Machinery is the Barangay EMS's own (brgy_voter_political, see municipal/machinery.py).
    Sentiment is derived from it: Committed = supporters + coordinators, Opposition, and
    Untagged for everyone else.
  * Cards, social services, households and sectors are the shared EMS tables, limited to the
    barangay's voters by joining the roll.

Read-only.
"""
from django.core.cache import cache
from django.db import connections

from municipal import household as hh
from municipal import machinery as mach
from municipal import smartcard as sc
from municipal import social as soc
from municipal.regions import province_pretty, region_of, voter_table
from municipal.text import title
from municipal.views import _ckey, _city_meta, _municipalities

LEVEL = 'brgy'
UNSPECIFIED = 'Unspecified'
ROLL_TTL = 6 * 3600
COMMITTED_CODES = ('barangay_coordinator', 'supporter')
SENTIMENTS = (('committed', 'Committed'), ('opposition', 'Opposition'), ('untagged', 'Untagged'))


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
def barangays(slug, municipality):
    """[{'value': pretty name, 'raw': [roll spellings], 'count'}] for a city, biggest first."""
    if not voter_table(slug) or municipality not in {m['value'] for m in _municipalities(slug)}:
        return []
    key = _ckey('brgylist', slug, municipality)
    rows = cache.get(key)
    if rows is None:
        with connections[mach.RDS].cursor() as cur:
            cur.execute(f'SELECT barangay, COUNT(*) FROM {voter_table(slug)} WHERE municipality = %s GROUP BY barangay',
                        [municipality])
            merged = {}
            for raw, n in cur.fetchall():
                b = merged.setdefault(title(raw) or UNSPECIFIED, {'value': title(raw) or UNSPECIFIED, 'raw': [], 'count': 0})
                b['raw'].append(raw)
                b['count'] += n
        rows = sorted(merged.values(), key=lambda b: (-b['count'], b['value']))
        cache.set(key, rows, ROLL_TTL)
    return rows


def bscope(request):
    """The chosen barangay from the session, or None. A city scope (every municipal module
    accepts it) plus 'barangay' (pretty name) and 'barangays' (its raw roll spellings)."""
    b = request.session.get('brgy') or {}
    return scope_for((b.get('province') or '').lower(), b.get('municipality') or '', b.get('barangay') or '')


def scope_for(slug, municipality, barangay):
    match = next((b for b in barangays(slug, municipality) if b['value'] == barangay), None)
    if not match:
        return None
    return {'province': slug, 'province_name': province_pretty(slug), 'region': region_of(slug),
            'municipality': municipality, 'municipality_pretty': title(municipality), 'table': voter_table(slug),
            'barangay': barangay, 'barangays': list(match['raw']), 'voters': match['count'], 'level': LEVEL}


def in_barangay(scope, alias='v'):
    """(sql, params) limiting roll alias `alias` to the scoped barangay."""
    ph = ','.join(['%s'] * len(scope['barangays']))
    return f'{alias}.municipality = %s AND {alias}.barangay IN ({ph})', [scope['municipality'], *scope['barangays']]


def roll_table(scope):
    return mach.voter_table_qualified(scope)


# ---------------------------------------------------------------------------
# Voters, puroks, precincts
# ---------------------------------------------------------------------------
def roll(scope):
    """{voter_id: (purok named in the roll address or None, precinct)} for the barangay (cached)."""
    key = _ckey('brgyroll', scope['province'], scope['municipality'] + '|' + scope['barangay'])
    data = cache.get(key)
    if data is None:
        where, params = in_barangay(scope)
        with connections[mach.RDS].cursor() as cur:
            # Only addresses that may name a purok come back (a few % of rows), not 85k strings.
            cur.execute(f"SELECT v.id, IF(v.address {hh.SQL_HAS_PUROK}, v.address, NULL), v.precinct "
                        f"FROM {scope['table']} v WHERE {where}", params)
            data = {vid: (hh.purok_in_address(addr), (prec or '').strip()) for vid, addr, prec in cur.fetchall()}
        cache.set(key, data, ROLL_TTL)
    return data


def saved_puroks(scope):
    """{voter_id: purok} saved on Personal Details for the barangay's voters (live)."""
    if not hh.tables_exist():
        return {}
    where, params = in_barangay(scope)
    with connections[mach.EXT].cursor() as cur:
        cur.execute(f"SELECT d.voter_id, d.purok FROM {hh.DETAILS} d JOIN {roll_table(scope)} v ON v.id = d.voter_id "
                    f"WHERE d.province_slug = %s AND d.purok IS NOT NULL AND d.purok <> '' AND {where}",
                    [scope['province'], *params])
        return dict(cur.fetchall())


def puroks(scope):
    """({voter_id: purok}, {voter_id: 'saved' | 'address'}) for every barangay voter; voters
    with neither get UNSPECIFIED (and no source)."""
    saved = saved_puroks(scope)
    eff, source = {}, {}
    for vid, (parsed, _) in roll(scope).items():
        if vid in saved:
            eff[vid], source[vid] = saved[vid], 'saved'
        elif parsed:
            eff[vid], source[vid] = parsed, 'address'
        else:
            eff[vid] = UNSPECIFIED
    return eff, source


def purok_sort_key(name):
    """Purok 2 before Purok 10; named puroks after numbered ones; Unspecified last."""
    if name == UNSPECIFIED:
        return (2, 0, '')
    tail = name.removeprefix('Purok ').strip()
    digits = ''.join(c for c in tail if c.isdigit())
    if tail[:1].isdigit():
        return (0, int(digits[:4] or 0), tail)
    return (1, 0, tail)


def purok_ids(scope, purok):
    """Voter ids of one purok (as grouped above)."""
    eff, _ = puroks(scope)
    return {vid for vid, p in eff.items() if p == purok}


# ---------------------------------------------------------------------------
# EMS numbers per voter (all limited to the barangay by joining the roll)
# ---------------------------------------------------------------------------
def positions(scope):
    """{voter_id: (role_code, upline_voter_id)} in the Barangay EMS's machinery."""
    where, params = in_barangay(scope)
    with connections[mach.EXT].cursor() as cur:
        cur.execute(f'SELECT p.voter_id, p.role_code, p.upline_voter_id FROM {mach.table(LEVEL)} p '
                    f'JOIN {roll_table(scope)} v ON v.id = p.voter_id WHERE p.province_slug = %s AND {where}',
                    [scope['province'], *params])
        return {vid: (code, up) for vid, code, up in cur.fetchall()}


def sentiment_of(code):
    if code in COMMITTED_CODES:
        return 'committed'
    if code == 'opposition':
        return 'opposition'
    return 'untagged'


def _ids(scope, sql_from, extra='', extra_params=()):
    where, params = in_barangay(scope)
    with connections[mach.EXT].cursor() as cur:
        cur.execute(f'SELECT DISTINCT x.voter_id FROM {sql_from} JOIN {roll_table(scope)} v ON v.id = x.voter_id '
                    f'WHERE x.province_slug = %s AND {where} {extra}', [scope['province'], *params, *extra_params])
        return {r[0] for r in cur.fetchall()}


def cardholders(scope):
    """{voter_id: status} of the barangay's active / pending cards."""
    if not sc.table_exists():
        return {}
    where, params = in_barangay(scope)
    with connections[mach.EXT].cursor() as cur:
        cur.execute(f"SELECT x.voter_id, x.status FROM {sc.TABLE} x JOIN {roll_table(scope)} v ON v.id = x.voter_id "
                    f"WHERE x.province_slug = %s AND x.status IN ('active', 'pending') AND {where}",
                    [scope['province'], *params])
        return dict(cur.fetchall())


def beneficiaries(scope):
    """{voter_id: (records, released ₱)} of the barangay's social-service beneficiaries."""
    if not soc.table_exists():
        return {}
    where, params = in_barangay(scope)
    with connections[mach.EXT].cursor() as cur:
        cur.execute(f"SELECT x.voter_id, COUNT(*), COALESCE(SUM(CASE WHEN x.status = 'Released' THEN x.amount END), 0) "
                    f"FROM {soc.TABLE} x JOIN {roll_table(scope)} v ON v.id = x.voter_id "
                    f"WHERE x.province_slug = %s AND {where} GROUP BY x.voter_id", [scope['province'], *params])
        return {vid: (n, float(rel)) for vid, n, rel in cur.fetchall()}


def household_heads(scope):
    return _ids(scope, f'{hh.MEMBERS} x') if hh.tables_exist() else set()


def sector_members(scope):
    """{sector: set(voter_id)} for the barangay."""
    if not hh.tables_exist():
        return {}
    where, params = in_barangay(scope)
    with connections[mach.EXT].cursor() as cur:
        cur.execute(f'SELECT x.sector, x.voter_id FROM {hh.SECTORS_TABLE} x JOIN {roll_table(scope)} v ON v.id = x.voter_id '
                    f'WHERE x.province_slug = %s AND {where}', [scope['province'], *params])
        out = {}
        for sector, vid in cur.fetchall():
            out.setdefault(sector, set()).add(vid)
        return out


# ---------------------------------------------------------------------------
# Breakdown rows
# ---------------------------------------------------------------------------
def _blank(name):
    return {'name': name, 'voters': 0, 'committed': 0, 'supporters': 0, 'coordinators': 0, 'opposition': 0,
            'untagged': 0, 'cards': 0, 'beneficiaries': 0, 'released': 0.0, 'households': 0, 'saved': 0,
            'precincts': set(), 'puroks': set()}


def breakdown(scope):
    """Everything the dashboard / Puroks page needs: per-purok and per-precinct rows plus totals."""
    eff, source = puroks(scope)
    r = roll(scope)
    pos = positions(scope)
    cards = cardholders(scope)
    benef = beneficiaries(scope)
    heads = household_heads(scope)

    by_purok, by_precinct, tot = {}, {}, _blank('Total')
    for vid, purok in eff.items():
        precinct = r[vid][1] or '—'
        code = pos.get(vid, (None, None))[0]
        for row in (by_purok.setdefault(purok, _blank(purok)), by_precinct.setdefault(precinct, _blank(precinct)), tot):
            row['voters'] += 1
            row['precincts'].add(precinct)
            row['puroks'].add(purok)
            s = sentiment_of(code)
            row[s] += 1
            row['supporters'] += code == 'supporter'
            row['coordinators'] += code == 'barangay_coordinator'
            row['cards'] += vid in cards
            row['households'] += vid in heads
            row['saved'] += source.get(vid) == 'saved'
            if vid in benef:
                row['beneficiaries'] += 1
                row['released'] += benef[vid][1]

    def finish(row):
        row['reach'] = row['committed'] / row['voters'] * 100 if row['voters'] else 0
        row['card_pct'] = row['cards'] / row['voters'] * 100 if row['voters'] else 0
        row['precinct_count'] = len(row.pop('precincts'))
        row['purok_count'] = len(row.pop('puroks'))
        return row

    purok_rows = [finish(by_purok[p]) for p in sorted(by_purok, key=purok_sort_key)]
    precinct_rows = [finish(by_precinct[p]) for p in sorted(by_precinct)]
    finish(tot)
    tot['named_puroks'] = sum(1 for p in purok_rows if p['name'] != UNSPECIFIED)
    tot['from_address'] = sum(1 for v in source.values() if v == 'address')
    tot['unspecified'] = sum(1 for p in eff.values() if p == UNSPECIFIED)
    tot['leaders'] = tot['coordinators']
    return purok_rows, precinct_rows, tot


def leaders(scope):
    """The barangay's coordinators with what they lead: direct downlines by sentiment,
    cardholders among them, and their purok / precinct."""
    pos = positions(scope)
    eff, _ = puroks(scope)
    r = roll(scope)
    cards = cardholders(scope)
    coords = [vid for vid, (code, _) in pos.items() if code == 'barangay_coordinator']
    briefs = mach.voter_briefs([(scope['province'], vid) for vid in coords])
    out = []
    for vid in coords:
        downs = [d for d, (code, up) in pos.items() if up == vid]
        b = briefs.get((scope['province'], vid)) or {'name': f'Voter #{vid}'}
        out.append({
            'id': vid, 'name': b['name'], 'purok': eff.get(vid, UNSPECIFIED), 'precinct': r.get(vid, (None, ''))[1] or '—',
            'downlines': len(downs),
            'supporters': sum(1 for d in downs if pos[d][0] == 'supporter'),
            'cardholders': sum(1 for d in downs if d in cards),
            'puroks': len({eff.get(d, UNSPECIFIED) for d in downs}),
            'card': cards.get(vid),
        })
    return sorted(out, key=lambda x: (-x['supporters'], x['name']))


# ---------------------------------------------------------------------------
# Per-purok EMS numbers for the heat map and AI snapshot (voters grouped by their purok)
# ---------------------------------------------------------------------------
def social_by_purok(scope, service=''):
    """{purok: {'records', 'beneficiaries', 'released', 'open', 'open_amount'}} — records grouped by
    their beneficiary's purok (the voter's, as everywhere in this EMS)."""
    if not soc.table_exists():
        return {}
    eff, _ = puroks(scope)
    where, params = in_barangay(scope)
    extra, extra_params = (' AND x.assistance_type = %s', [service]) if service else ('', [])
    with connections[mach.EXT].cursor() as cur:
        cur.execute(f"SELECT x.voter_id, x.status, x.amount FROM {soc.TABLE} x JOIN {roll_table(scope)} v ON v.id = x.voter_id "
                    f"WHERE x.province_slug = %s AND {where}{extra}", [scope['province'], *params, *extra_params])
        rows = cur.fetchall()
    out, people = {}, {}
    for vid, status, amount in rows:
        p = eff.get(vid, UNSPECIFIED)
        o = out.setdefault(p, {'records': 0, 'beneficiaries': 0, 'released': 0.0, 'open': 0, 'open_amount': 0.0})
        o['records'] += 1
        people.setdefault(p, set()).add(vid)
        if status == 'Released':
            o['released'] += float(amount or 0)
        elif status in ('Pending', 'Approved'):
            o['open'] += 1
            o['open_amount'] += float(amount or 0)
    for p, ids in people.items():
        out[p]['beneficiaries'] = len(ids)
    return out


def activity_by_purok(scope, days):
    """{purok: entries} in the activity log over the last `days` — the Barangay EMS's own machinery
    entries plus the shared ones (cards, services, details, households), as on its Transaction List."""
    from municipal.heatmap import _since
    eff, _ = puroks(scope)
    where, params = in_barangay(scope)
    keep, keep_params = mach.audit_level_sql(LEVEL)
    with connections[mach.EXT].cursor() as cur:
        cur.execute(f'SELECT a.voter_id, COUNT(*) FROM ems_voter_audit a JOIN {roll_table(scope)} v ON v.id = a.voter_id '
                    f'WHERE a.province_slug = %s AND {where} AND {keep} AND a.created_at >= %s GROUP BY a.voter_id',
                    [scope['province'], *params, *keep_params, _since(days)])
        rows = cur.fetchall()
    out = {}
    for vid, n in rows:
        p = eff.get(vid, UNSPECIFIED)
        out[p] = out.get(p, 0) + n
    return out


def sectors_by_purok(scope):
    """{purok: {sector: members}}."""
    eff, _ = puroks(scope)
    out = {}
    for sector, ids in sector_members(scope).items():
        for vid in ids:
            p = out.setdefault(eff.get(vid, UNSPECIFIED), {})
            p[sector] = p.get(sector, 0) + 1
    return out


def card_status_by_purok(scope):
    """{purok: {'active', 'pending'}} of the barangay's cards."""
    eff, _ = puroks(scope)
    out = {}
    for vid, status in cardholders(scope).items():
        p = out.setdefault(eff.get(vid, UNSPECIFIED), {'active': 0, 'pending': 0})
        p[status] = p.get(status, 0) + 1
    return out


def precinct_registered(scope):
    """{precinct code: registered voters} for the barangay."""
    out = {}
    for _, prec in roll(scope).values():
        out[prec] = out.get(prec, 0) + 1
    return out


def precinct_candidates(scope, limit, max_voters):
    """Quick Count candidates: the barangay's biggest precincts (with a sample voter id)."""
    firsts, regs = {}, {}
    for vid, (_, prec) in roll(scope).items():
        if not prec or prec.startswith('#'):          # '#REF!' and the like are not precincts
            continue
        regs[prec] = regs.get(prec, 0) + 1
        firsts[prec] = min(firsts.get(prec, vid), vid)
    rows = [{'municipality': scope['municipality'], 'barangay': scope['barangays'][0], 'precinct': p, 'reg': n,
             'first_id': firsts[p], 'province_slug': scope['province']} for p, n in regs.items() if n <= max_voters]
    return sorted(rows, key=lambda r: -r['reg'])[:limit]
