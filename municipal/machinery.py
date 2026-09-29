"""
Political machinery on the national tables in generic_360_db.

A faithful port of CVL-NATIONAL's api/political.php, so this app and the national
app apply the same rules to the same shared data:

  * One assignment per voter (PK province_slug + voter_id), plus an optional upline.
  * Downlines are simply the rows whose upline points at a voter — the chain is
    stored once, so it can never disagree with itself.
  * Ranks run 1 (regional) .. 5 (supporter). A downline sits at a strictly higher
    rank number than its upline. OPPOSITION has no rank: it is a tag, not a rung,
    so it takes neither an upline nor downlines.
  * An upline may live in another province table, so every reference carries its
    own province slug.
  * Every change appends ems_voter_audit rows in the same transaction.

Writes go ONLY through the 'ext' connection (generic_360_db). Voter names and
locations are read from cvl_national through the read-only 'rds' connection.
"""
import datetime
import json
import re

from django.conf import settings
from django.db import connections, transaction

from .regions import province_pretty, voter_table
from .text import title

EXT = 'ext'
RDS = 'rds'
MAX_HOPS = 20           # deeper than any real hierarchy: treated as a loop
SUPPORTER_RANK = 5

_UPSERT = (
    'INSERT INTO ems_voter_political '
    '(province_slug, voter_id, role_code, upline_province_slug, upline_voter_id, assigned_by) '
    'VALUES (%s, %s, %s, %s, %s, %s) '
    'ON DUPLICATE KEY UPDATE role_code = VALUES(role_code), '
    'upline_province_slug = VALUES(upline_province_slug), '
    'upline_voter_id = VALUES(upline_voter_id), '
    'assigned_by = VALUES(assigned_by), assigned_at = NOW()'
)


class MachineryError(Exception):
    """A rule violation to show the user (not a server fault)."""


def _schema(alias):
    name = settings.DATABASES[alias]['NAME']
    if not re.fullmatch(r'[A-Za-z0-9_]+', name):
        raise RuntimeError(f'Invalid database name for {alias!r}')
    return name


def voter_table_qualified(scope):
    """`cvl_national.cvl_<slug>` — for joins run on the 'ext' connection.

    Reads that join machinery to the voter roll run on 'ext' so they see the
    machinery exactly as this connection last wrote it; they only ever SELECT
    from cvl_national.
    """
    return f'{_schema(RDS)}.{scope["table"]}'


def _aware(value):
    """generic_360_db runs in UTC and raw SQL returns naive datetimes: mark them UTC so
    templates show Manila time (USE_TZ)."""
    if settings.USE_TZ and isinstance(value, datetime.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _rows(cur):
    cols = [c[0] for c in cur.description]
    return [{c: _aware(v) for c, v in zip(cols, r)} for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def roles():
    """Active positions keyed by code, in display order."""
    with connections[EXT].cursor() as cur:
        cur.execute(
            'SELECT code, label, hierarchy_rank, scope_level, is_opposition, sort_order '
            'FROM ems_political_role WHERE active = 1 ORDER BY sort_order'
        )
        out = {}
        for r in _rows(cur):
            r['hierarchy_rank'] = None if r['hierarchy_rank'] is None else int(r['hierarchy_rank'])
            r['is_opposition'] = bool(r['is_opposition'])
            r['pretty'] = title(r['label'])
            out[r['code']] = r
    return out


def positions_among(province, voter_ids):
    """{voter_id: {'role', 'is_opposition', 'leader_id', 'leader_province', 'leader_name'}} for a page
    of voters — the voters list's Leader column (a voter's leader is their upline)."""
    ids = [int(i) for i in voter_ids]
    if not ids:
        return {}
    ph = ','.join(['%s'] * len(ids))
    with connections[EXT].cursor() as cur:
        cur.execute(f'SELECT voter_id, role_code, upline_province_slug, upline_voter_id FROM ems_voter_political '
                    f'WHERE province_slug = %s AND voter_id IN ({ph})', [province, *ids])
        rows = _rows(cur)
    if not rows:
        return {}
    rs = roles()
    briefs = voter_briefs([(r['upline_province_slug'], r['upline_voter_id'])
                           for r in rows if r['upline_voter_id'] is not None])
    out = {}
    for r in rows:
        role = rs.get(r['role_code'])
        up = briefs.get((r['upline_province_slug'], int(r['upline_voter_id']))) if r['upline_voter_id'] is not None else None
        out[r['voter_id']] = {
            'role': role['pretty'] if role else title(r['role_code']),
            'is_opposition': bool(role and role['is_opposition']),
            'leader_id': up['id'] if up else None,
            'leader_province': r['upline_province_slug'],
            'leader_name': up['name'] if up else None,
        }
    return out


def child_role(rs, rank):
    """The position one level below `rank`, or None."""
    if rank is None:
        return None
    return next((r for r in rs.values() if r['hierarchy_rank'] == rank + 1), None)


def political_of(cur, province, vid):
    """One voter's political row (dict) or None. `cur` must be an 'ext' cursor."""
    if not province or vid is None:
        return None
    cur.execute(
        'SELECT * FROM ems_voter_political WHERE province_slug = %s AND voter_id = %s LIMIT 1',
        [province, int(vid)],
    )
    rows = _rows(cur)
    return rows[0] if rows else None


def voter_briefs(pairs):
    """{(province, id): brief} for (province, id) pairs, batched per province table."""
    by_prov = {}
    for prov, vid in pairs:
        if prov and vid is not None and voter_table(prov):
            by_prov.setdefault(prov, set()).add(int(vid))
    out = {}
    with connections[RDS].cursor() as cur:
        for prov, ids in by_prov.items():
            ids = sorted(ids)
            for i in range(0, len(ids), 1000):
                chunk = ids[i:i + 1000]
                ph = ','.join(['%s'] * len(chunk))
                cur.execute(
                    f'SELECT id, fullname, address, municipality, barangay, precinct '
                    f'FROM {voter_table(prov)} WHERE id IN ({ph})',
                    chunk,
                )
                for r in _rows(cur):
                    out[(prov, r['id'])] = {
                        'province_slug': prov,
                        'province_pretty': province_pretty(prov),
                        'id': r['id'],
                        'fullname': (r['fullname'] or '').strip(),   # raw, as the audit trail stores it
                        'name': title(r['fullname']),
                        'address': r['address'],
                        'municipality': r['municipality'],
                        'barangay': r['barangay'],
                        'municipality_pretty': title(r['municipality']),
                        'barangay_pretty': title(r['barangay']),
                    }
    return out


def voter_brief(province, vid):
    if not province or vid is None:
        return None
    return voter_briefs([(province, vid)]).get((province, int(vid)))


def would_cycle(cur, cand_prov, cand_id, up_prov, up_id):
    """Would making cand report to up create a loop? Walk up from `up`."""
    if cand_prov == up_prov and int(cand_id) == int(up_id):
        return True
    prov, vid = up_prov, int(up_id)
    for _ in range(MAX_HOPS):
        row = political_of(cur, prov, vid)
        if not row or row['upline_voter_id'] is None:
            return False
        prov, vid = row['upline_province_slug'], int(row['upline_voter_id'])
        if prov == cand_prov and vid == int(cand_id):
            return True
    return True


def payload(province, vid):
    """Everything the profile card needs: role, upline, downlines, child role."""
    rs = roles()
    with connections[EXT].cursor() as cur:
        row = political_of(cur, province, vid)
        downs = []
        up_row = None
        if row:
            cur.execute(
                'SELECT province_slug, voter_id, role_code FROM ems_voter_political '
                'WHERE upline_province_slug = %s AND upline_voter_id = %s',
                [province, int(vid)],
            )
            downs = _rows(cur)
            if row['upline_voter_id'] is not None:
                up_row = political_of(cur, row['upline_province_slug'], row['upline_voter_id'])

    pairs = [(d['province_slug'], d['voter_id']) for d in downs]
    if row and row['upline_voter_id'] is not None:
        pairs.append((row['upline_province_slug'], int(row['upline_voter_id'])))
    briefs = voter_briefs(pairs)

    role = rs.get(row['role_code']) if row else None
    rank = role['hierarchy_rank'] if role else None

    upline = None
    if row and row['upline_voter_id'] is not None:
        key = (row['upline_province_slug'], int(row['upline_voter_id']))
        upline = dict(briefs.get(key) or {
            'province_slug': key[0], 'province_pretty': province_pretty(key[0]),
            'id': key[1], 'name': f'Voter #{key[1]}', 'fullname': f'voter #{key[1]}',
            'municipality': None, 'barangay': None,
            'municipality_pretty': '', 'barangay_pretty': '',
        })
        upline['role_pretty'] = rs[up_row['role_code']]['pretty'] if up_row and up_row['role_code'] in rs else ''

    downlines = []
    for d in downs:
        key = (d['province_slug'], int(d['voter_id']))
        b = briefs.get(key) or {'province_slug': key[0], 'id': key[1], 'name': f'Voter #{key[1]}',
                                'municipality': None, 'barangay': None,
                                'municipality_pretty': '', 'barangay_pretty': ''}
        downlines.append({**b, 'role_code': d['role_code'],
                          'role_pretty': rs[d['role_code']]['pretty'] if d['role_code'] in rs else d['role_code']})
    downlines.sort(key=lambda x: x['name'])

    return {
        'roles': rs,
        'assigned': bool(row),
        'role': role,
        'rank': rank,
        'is_opposition': bool(role and role['is_opposition']),
        'assigned_at': row['assigned_at'] if row else None,
        'assigned_by': row['assigned_by'] if row else None,
        'upline': upline,
        'downlines': downlines,
        'can_have_downlines': rank is not None and rank < SUPPORTER_RANK,
        'child_role': child_role(rs, rank),
    }


def audit_for(province, vid, limit=15):
    """Most recent audit entries for one voter, newest first."""
    limit = max(1, min(200, int(limit)))
    with connections[EXT].cursor() as cur:
        cur.execute(
            'SELECT id, action, description, actor, created_at FROM ems_voter_audit '
            'WHERE province_slug = %s AND voter_id = %s '
            f'ORDER BY created_at DESC, id DESC LIMIT {limit}',
            [province, int(vid)],
        )
        return _rows(cur)


def superiors(scope, voter, rank):
    """Existing holders of `rank` a voter may report to, limited to the area that
    rank covers (province for provincial, city for municipal, barangay for barangay)."""
    if rank not in (2, 3, 4):
        return []
    where = ['p.province_slug = %s', 'r.hierarchy_rank = %s', 'v.id <> %s']
    params = [scope['province'], rank, int(voter['id'])]
    if rank >= 3:
        where.append('v.municipality = %s')
        params.append(voter['municipality'])
    if rank >= 4:
        where.append('v.barangay = %s')
        params.append(voter['barangay'])
    with connections[EXT].cursor() as cur:
        cur.execute(
            f'SELECT v.id, v.fullname, v.municipality, v.barangay '
            f'FROM ems_voter_political p '
            f'JOIN ems_political_role r ON r.code = p.role_code '
            f'JOIN {voter_table_qualified(scope)} v ON v.id = p.voter_id '
            f'WHERE {" AND ".join(where)} ORDER BY v.fullname LIMIT 200',
            params,
        )
        return [{'id': r['id'], 'name': title(r['fullname']),
                 'barangay': title(r['barangay']), 'municipality': title(r['municipality'])}
                for r in _rows(cur)]


def city_counts(scope):
    """{barangay_pretty: {role_code: count}} and {role_code: count} for one city.

    One cross-schema query: the national assignments joined to this province's
    voter table to learn each assigned voter's city and barangay.
    """
    with connections[EXT].cursor() as cur:
        cur.execute(
            f'SELECT v.barangay, p.role_code, COUNT(*) '
            f'FROM ems_voter_political p '
            f'JOIN {voter_table_qualified(scope)} v ON v.id = p.voter_id '
            f'WHERE p.province_slug = %s AND v.municipality = %s '
            f'GROUP BY v.barangay, p.role_code',
            [scope['province'], scope['municipality']],
        )
        rows = cur.fetchall()
    by_brgy, totals = {}, {}
    for brgy, code, n in rows:
        b = by_brgy.setdefault(title(brgy), {})
        b[code] = b.get(code, 0) + n
        totals[code] = totals.get(code, 0) + n
    return by_brgy, totals


# ---------------------------------------------------------------------------
# Writes (all through 'ext', each in one transaction with its audit rows)
# ---------------------------------------------------------------------------
def _audit(cur, province, vid, action, description, meta, actor, ip):
    cur.execute(
        'INSERT INTO ems_voter_audit '
        '(province_slug, voter_id, action, description, actor, actor_ip, meta) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s)',
        [province, int(vid), action[:48], description[:255], actor, ip,
         json.dumps(meta, ensure_ascii=False) if meta else None],
    )


def assign(province, vid, code, actor, ip):
    """Set or change a voter's own position (national 'assign')."""
    rs = roles()
    if code not in rs:
        raise MachineryError('Choose a position.')
    if not voter_brief(province, vid):
        raise MachineryError('Voter not found.')
    new_rank = rs[code]['hierarchy_rank']

    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        prev = political_of(cur, province, vid)

        # Changing role can invalidate links: an upline no longer above this voter,
        # or downlines no longer below them.
        keep_upline = False
        if prev and prev['upline_voter_id'] is not None and new_rank is not None:
            up = political_of(cur, prev['upline_province_slug'], prev['upline_voter_id'])
            up_rank = rs[up['role_code']]['hierarchy_rank'] if up and up['role_code'] in rs else None
            keep_upline = up_rank is not None and up_rank < new_rank

        cur.execute(_UPSERT, [
            province, int(vid), code,
            prev['upline_province_slug'] if keep_upline else None,
            prev['upline_voter_id'] if keep_upline else None,
            actor,
        ])

        if new_rank is None:
            cur.execute(
                'UPDATE ems_voter_political SET upline_province_slug = NULL, upline_voter_id = NULL '
                'WHERE upline_province_slug = %s AND upline_voter_id = %s',
                [province, int(vid)],
            )
        else:
            cur.execute(
                'UPDATE ems_voter_political p JOIN ems_political_role r ON r.code = p.role_code '
                'SET p.upline_province_slug = NULL, p.upline_voter_id = NULL '
                'WHERE p.upline_province_slug = %s AND p.upline_voter_id = %s '
                'AND (r.hierarchy_rank IS NULL OR r.hierarchy_rank <= %s)',
                [province, int(vid), new_rank],
            )
        detached = cur.rowcount

        was = (rs[prev['role_code']]['label'] if prev['role_code'] in rs else prev['role_code']) if prev else None
        _audit(cur, province, vid, 'political.assign',
               f'Political position changed from {was} to {rs[code]["label"]}' if was
               else f'Tagged as {rs[code]["label"]}',
               {'role': code, 'detached_downlines': detached}, actor, ip)
    return rs[code]


def unassign(province, vid, actor, ip):
    """Clear the position and detach anyone reporting to this voter."""
    rs = roles()
    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        prev = political_of(cur, province, vid)
        if not prev:
            raise MachineryError('This voter has no political position.')
        cur.execute(
            'UPDATE ems_voter_political SET upline_province_slug = NULL, upline_voter_id = NULL '
            'WHERE upline_province_slug = %s AND upline_voter_id = %s',
            [province, int(vid)],
        )
        freed = cur.rowcount
        cur.execute('DELETE FROM ems_voter_political WHERE province_slug = %s AND voter_id = %s',
                    [province, int(vid)])
        label = rs[prev['role_code']]['label'] if prev['role_code'] in rs else prev['role_code']
        _audit(cur, province, vid, 'political.unassign',
               f'Removed political position ({label})', {'freed_downlines': freed}, actor, ip)
    return freed


def add_down(province, vid, d_prov, d_id, actor, ip):
    """Attach (d_prov, d_id) as a downline of (province, vid)."""
    rs = roles()
    if not voter_table(d_prov):
        raise MachineryError('Invalid province for that voter.')
    me = voter_brief(province, vid)
    down = voter_brief(d_prov, d_id)
    if not me:
        raise MachineryError('Voter not found.')
    if not down:
        raise MachineryError('That voter was not found.')

    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        row = political_of(cur, province, vid)
        if not row:
            raise MachineryError('Assign a position to this voter first.')
        rank = rs[row['role_code']]['hierarchy_rank'] if row['role_code'] in rs else None
        if rank is None or rank >= SUPPORTER_RANK:
            raise MachineryError('This position does not take downlines.')
        if would_cycle(cur, d_prov, d_id, province, vid):
            raise MachineryError('That would create a loop in the hierarchy.')

        existing = political_of(cur, d_prov, d_id)
        if (existing and existing['upline_voter_id'] is not None
                and not (int(existing['upline_voter_id']) == int(vid)
                         and existing['upline_province_slug'] == province)):
            raise MachineryError(f'"{down["name"]}" already reports to someone else. Detach them there first.')

        child = child_role(rs, rank)
        if not child:
            raise MachineryError('No position exists below this one.')
        # A downline takes the position one level below their upline unless they
        # already hold a lower-ranked one worth keeping.
        existing_rank = (rs[existing['role_code']]['hierarchy_rank']
                         if existing and existing['role_code'] in rs else None)
        code = existing['role_code'] if existing_rank is not None and existing_rank > rank else child['code']

        cur.execute(_UPSERT, [d_prov, int(d_id), code, province, int(vid), actor])
        _audit(cur, province, vid, 'political.downline_add',
               f'Added {down["fullname"]} as a downline ({rs[code]["label"]})',
               {'down_province': d_prov, 'down_id': int(d_id), 'role': code}, actor, ip)
        _audit(cur, d_prov, d_id, 'political.upline_set',
               f'Now reports to {me["fullname"]} ({rs[row["role_code"]]["label"]})',
               {'upline_province': province, 'upline_id': int(vid), 'role': code}, actor, ip)
    return rs[code]


def remove_down(province, vid, d_prov, d_id, actor, ip):
    """Detach a downline from (province, vid). Returns True if a link was removed."""
    with transaction.atomic(using=EXT), connections[EXT].cursor() as cur:
        cur.execute(
            'UPDATE ems_voter_political SET upline_province_slug = NULL, upline_voter_id = NULL '
            'WHERE province_slug = %s AND voter_id = %s '
            'AND upline_province_slug = %s AND upline_voter_id = %s',
            [d_prov, int(d_id), province, int(vid)],
        )
        if cur.rowcount <= 0:
            return False
        me = voter_brief(province, vid)
        down = voter_brief(d_prov, d_id)
        name = down['fullname'] if down else f'voter #{d_id}'
        _audit(cur, province, vid, 'political.downline_remove',
               f'Detached {name} from this network',
               {'down_province': d_prov, 'down_id': int(d_id)}, actor, ip)
        _audit(cur, d_prov, d_id, 'political.upline_clear',
               f'No longer reports to {me["fullname"] if me else f"voter #{vid}"}',
               {'upline_province': province, 'upline_id': int(vid)}, actor, ip)
    return True


def set_upline(province, vid, up_prov, up_id, actor, ip):
    """Make (province, vid) report to (up_prov, up_id) — 'Change superior'.

    Implemented exactly as the national app would do it by hand: detach from the
    current upline (remove_down), then attach under the new one (add_down), in one
    transaction.
    """
    rs = roles()
    with transaction.atomic(using=EXT):
        with connections[EXT].cursor() as cur:
            row = political_of(cur, province, vid)
            up = political_of(cur, up_prov, up_id)
        if not row:
            raise MachineryError('Assign a position to this voter first.')
        my_rank = rs[row['role_code']]['hierarchy_rank'] if row['role_code'] in rs else None
        up_rank = rs[up['role_code']]['hierarchy_rank'] if up and up['role_code'] in rs else None
        if my_rank is None:
            raise MachineryError('Opposition does not report to anyone.')
        if up_rank is None or up_rank >= my_rank:
            raise MachineryError('A superior must hold a higher position than this voter.')
        if (row['upline_voter_id'] is not None and row['upline_province_slug'] == up_prov
                and int(row['upline_voter_id']) == int(up_id)):
            return False   # unchanged
        if row['upline_voter_id'] is not None:
            remove_down(row['upline_province_slug'], int(row['upline_voter_id']), province, vid, actor, ip)
        add_down(up_prov, up_id, province, vid, actor, ip)
    return True
