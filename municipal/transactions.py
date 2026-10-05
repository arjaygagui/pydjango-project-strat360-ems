"""
Transaction List — the city's real activity log, read from generic_360_db.ems_voter_audit.

Replaces the PHP Transaction List pages, which generate mock rows. Every module writes
here: political machinery (shared with CVL-NATIONAL), social services, smart cards,
household and personal details. An entry belongs to the city when its voter is on the
city's roll, so the list joins cvl_national.cvl_<slug> (read-only) on the 'ext' connection.

Read-only: nothing here writes.
"""
import datetime
import json

from django.db import connections
from django.utils import timezone

from .machinery import EXT, _rows, audit_level_sql, voter_briefs, voter_table_qualified
from .regions import province_pretty
from .text import title

PAGE_SIZE = 25
EXPORT_LIMIT = 20000

CATEGORIES = {
    'political': 'Political Machinery',
    'social': 'Social Services',
    'card': 'Smart Cards',
    'household': 'Household',
    'details': 'Personal Details',
}

# action -> (label, Font Awesome icon)
ACTIONS = {
    'political.assign': ('Position Assigned', 'fa-sitemap'),
    'political.unassign': ('Position Removed', 'fa-user-minus'),
    'political.upline_set': ('Superior Set', 'fa-arrow-turn-up'),
    'political.upline_clear': ('Superior Cleared', 'fa-link-slash'),
    'political.downline_add': ('Downline Added', 'fa-user-plus'),
    'political.downline_remove': ('Downline Detached', 'fa-user-xmark'),
    'social.record': ('Social Service Recorded', 'fa-hand-holding-heart'),
    'social.status': ('Social Service Updated', 'fa-pen'),
    'social.request': ('Social Service Requested', 'fa-hand-holding-heart'),   # CVL-NATIONAL
    'social.approve': ('Social Service Approved', 'fa-circle-check'),         # CVL-NATIONAL
    'card.issue': ('Smart Card Issued', 'fa-id-card'),
    'card.status': ('Smart Card Updated', 'fa-id-card-clip'),
    'household.member_add': ('Household Member Added', 'fa-house-user'),
    'household.member_remove': ('Household Member Removed', 'fa-house-circle-xmark'),
    'household.add': ('Household Updated', 'fa-house-user'),                  # CVL-NATIONAL
    'household.head': ('Household Head Set', 'fa-house-user'),                # CVL-NATIONAL
    'household.head_transfer': ('Household Head Transferred', 'fa-house-user'),
    'details.update': ('Personal Details Updated', 'fa-user-pen'),
}

# Outcome shown in the Status column: value -> (label, css tone).
_OUTCOMES = {
    'active': ('Active', 'done'), 'pending': ('Pending', 'pending'), 'revoked': ('Revoked', 'fail'),
    'Released': ('Released', 'done'), 'Approved': ('Approved', 'live'),
    'Pending': ('Pending', 'pending'), 'Rejected': ('Rejected', 'fail'),
}
_REMOVALS = {'political.unassign', 'political.downline_remove', 'political.upline_clear',
             'household.member_remove'}


def action_choices():
    """[(category label, [(value, label), ...]), ...] for the filter's <optgroup>s."""
    groups = []
    for key, label in CATEGORIES.items():
        opts = [(key, f'All {label}')] + [(a, l) for a, (l, _) in ACTIONS.items() if a.startswith(key + '.')]
        groups.append((label, opts))
    return groups


def _outcome(action, meta):
    if action == 'card.issue':
        return _OUTCOMES.get(meta.get('status'), ('Active', 'done'))
    if action in ('card.status', 'social.status'):
        return _OUTCOMES.get(meta.get('to'), ('Updated', 'done'))
    if action == 'social.record':
        return _OUTCOMES.get(meta.get('status'), ('Recorded', 'done'))
    if action == 'social.approve':
        return _OUTCOMES['Approved']
    if action == 'social.request':      # CVL-NATIONAL: a request awaiting approval
        return ('Requested', 'pending')
    if action in _REMOVALS:
        return ('Removed', 'neutral')
    return ('Done', 'done')


def _manila_day_start_utc(day):
    """UTC (naive, as stored) for 00:00 Manila time on `day`."""
    local = timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))
    return local.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _date(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _national(scope):
    """Nationwide scope: no province ({} = the country, {'provinces': [...]} = a region)."""
    return not scope.get('province')


def _area(scope):
    """The city — the whole province when the scope has no municipality (Province-Wide EMS), or the
    country / a region when it has no province (Nationwide EMS) — with only that EMS level's own
    machinery entries (each level keeps its own machinery)."""
    if scope.get('barangays'):       # Barangay EMS: the barangay's voters, its own machinery
        ph = ','.join(['%s'] * len(scope['barangays']))
        sql, params, level = (f'a.province_slug = %s AND v.municipality = %s AND v.barangay IN ({ph})',
                              [scope['province'], scope['municipality'], *scope['barangays']], 'brgy')
    elif scope.get('municipality'):
        sql, params, level = 'a.province_slug = %s AND v.municipality = %s', [scope['province'], scope['municipality']], 'city'
    elif scope.get('province'):
        # A Province-Wide list — or a Nationwide one narrowed to a province ({'level': 'nat'}).
        sql, params, level = 'a.province_slug = %s', [scope['province']], scope.get('level', 'prov')
    elif scope.get('provinces'):
        slugs = list(scope['provinces'])
        sql, params, level = f"a.province_slug IN ({','.join(['%s'] * len(slugs))})", slugs, 'nat'
    else:
        sql, params, level = '1 = 1', [], 'nat'
    hide, hide_params = audit_level_sql(level)
    return f'{sql} AND {hide}', params + hide_params


def _where(scope, f):
    """WHERE clause + params for the area and the filters (`city` narrows a province-wide list)."""
    area, params = _area(scope)
    where = [area]
    if f.get('province') and _national(scope):
        where.append('a.province_slug = %s')
        params.append(f['province'])
    if f.get('city') and not scope.get('municipality') and not _national(scope):
        where.append('v.municipality = %s')
        params.append(f['city'])
    action = f.get('action', '')
    if action in CATEGORIES:
        where.append('a.action LIKE %s')
        params.append(action + '.%')
    elif action in ACTIONS:
        where.append('a.action = %s')
        params.append(action)
    if f.get('actor'):
        where.append('a.actor = %s')
        params.append(f['actor'])
    start, end = _date(f.get('from')), _date(f.get('to'))
    if start:
        where.append('a.created_at >= %s')
        params.append(_manila_day_start_utc(start))
    if end:
        where.append('a.created_at < %s')
        params.append(_manila_day_start_utc(end + datetime.timedelta(days=1)))
    q = (f.get('q') or '').strip()
    if q:
        tx = q.upper().removeprefix('TX-').lstrip('0')
        if tx.isdigit():
            where.append('a.id = %s')
            params.append(int(tx))
        else:
            if _national(scope):         # entries of many provinces: no roll to search names in
                where.append('a.description LIKE %s')
                params.append(f'%{q}%')
            else:
                where.append('(a.description LIKE %s OR v.fullname LIKE %s)')
                params += [f'%{q}%', f'{q}%']
    return ' AND '.join(where), params


def _from(scope):
    if _national(scope):
        return 'ems_voter_audit a'
    return f'ems_voter_audit a JOIN {voter_table_qualified(scope)} v ON v.id = a.voter_id'


def summary(scope):
    """KPI counts for the whole city / province (unfiltered)."""
    today = timezone.localdate()
    area, args = _area(scope)
    with connections[EXT].cursor() as cur:
        cur.execute(
            f'SELECT COUNT(*), SUM(a.created_at >= %s), SUM(a.created_at >= %s), COUNT(DISTINCT a.actor) '
            f'FROM {_from(scope)} WHERE {area}',
            [_manila_day_start_utc(today), _manila_day_start_utc(today - datetime.timedelta(days=6)), *args])
        total, today_n, week_n, actors = cur.fetchone()
        cur.execute(
            f"SELECT SUBSTRING_INDEX(a.action, '.', 1) cat, COUNT(*) FROM {_from(scope)} WHERE {area} GROUP BY cat", args)
        by_cat = dict(cur.fetchall())
    return {
        'total': total or 0, 'today': int(today_n or 0), 'week': int(week_n or 0), 'actors': actors or 0,
        'by_category': [(CATEGORIES.get(k, title(k)), n) for k, n in sorted(by_cat.items(), key=lambda kv: -kv[1])],
    }


def encoders(scope):
    area, args = _area(scope)
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT DISTINCT a.actor FROM {_from(scope)} WHERE {area} AND a.actor IS NOT NULL ORDER BY a.actor', args)
        return [r[0] for r in cur.fetchall()]


def _decorate(row):
    try:
        meta = json.loads(row.pop('meta') or '{}')
    except (TypeError, ValueError):
        meta = {}
    label, icon = ACTIONS.get(row['action'], (title(row['action'].replace('.', ' ').replace('_', ' ')), 'fa-circle-info'))
    row['tx_id'] = f'TX-{row["id"]:06d}'
    row['label'], row['icon'] = label, icon
    row['category'] = CATEGORIES.get(row['action'].split('.', 1)[0], 'Other')
    row['status_label'], row['tone'] = _outcome(row['action'], meta)
    row['mock'] = bool(meta.get('mock'))
    row['voter_name'] = title(row.pop('fullname'))
    row['barangay'] = title(row['barangay'])
    row['city'] = title(row.get('municipality'))
    row['province'] = province_pretty(row['province_slug'])
    return row


_COLS = 'a.id, a.province_slug, a.voter_id, a.action, a.description, a.actor, a.meta, a.created_at'


def _select(scope):
    return _COLS if _national(scope) else f'{_COLS}, v.fullname, v.barangay, v.municipality'


def _fetch(scope, cur):
    """Rows of the last query, decorated. Nationwide rows get the voter's name and place from
    their own province's roll (voter_briefs)."""
    rows = _rows(cur)
    if _national(scope):
        briefs = voter_briefs([(r['province_slug'], r['voter_id']) for r in rows])
        for r in rows:
            b = briefs.get((r['province_slug'], r['voter_id'])) or {}
            r.update(fullname=b.get('fullname', ''), barangay=b.get('barangay'), municipality=b.get('municipality'))
    return [_decorate(r) for r in rows]


def page(scope, f, page_no=1):
    wsql, params = _where(scope, f)
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM {_from(scope)} WHERE {wsql}', params)
        total = cur.fetchone()[0]
        pages = max(1, -(-total // PAGE_SIZE))
        page_no = min(max(1, page_no), pages)
        cur.execute(
            f'SELECT {_select(scope)} FROM {_from(scope)} WHERE {wsql} ORDER BY a.created_at DESC, a.id DESC '
            f'LIMIT {PAGE_SIZE} OFFSET {(page_no - 1) * PAGE_SIZE}', params)
        rows = _fetch(scope, cur)
    return rows, total, page_no, pages


def actor_activity(actor, limit=10):
    """One user's own entries, across every city: totals per module + the latest `limit`."""
    with connections[EXT].cursor() as cur:
        cur.execute("SELECT SUBSTRING_INDEX(action, '.', 1), COUNT(*), SUM(created_at >= %s) FROM ems_voter_audit "
                    'WHERE actor = %s GROUP BY 1', [_manila_day_start_utc(timezone.localdate() - datetime.timedelta(days=29)), actor])
        by_cat = cur.fetchall()
        cur.execute('SELECT id, province_slug, voter_id, action, description, meta, created_at FROM ems_voter_audit '
                    f'WHERE actor = %s ORDER BY created_at DESC, id DESC LIMIT {int(limit)}', [actor])
        rows = _rows(cur)
    latest = []
    for r in rows:
        try:
            meta = json.loads(r.pop('meta') or '{}')
        except (TypeError, ValueError):
            meta = {}
        label, icon = ACTIONS.get(r['action'], (title(r['action'].replace('.', ' ').replace('_', ' ')), 'fa-circle-info'))
        latest.append({**r, 'tx_id': f'TX-{r["id"]:06d}', 'label': label, 'icon': icon, 'mock': bool(meta.get('mock'))})
    return {
        'total': sum(n for _, n, _ in by_cat),
        'last30': sum(int(m or 0) for _, _, m in by_cat),
        'by_category': sorted(((CATEGORIES.get(k, title(k)), n) for k, n, _ in by_cat), key=lambda kv: -kv[1]),
        'latest': latest,
    }


def export_rows(scope, f):
    """All matching entries (capped) for CSV export, newest first."""
    wsql, params = _where(scope, f)
    with connections[EXT].cursor() as cur:
        cur.execute(
            f'SELECT {_select(scope)} FROM {_from(scope)} WHERE {wsql} ORDER BY a.created_at DESC, a.id DESC '
            f'LIMIT {EXPORT_LIMIT}', params)
        return _fetch(scope, cur)
