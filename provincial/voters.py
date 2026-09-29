"""
Province-wide Voters List: the city Voters List's filters over a whole province, plus a
City / Municipality filter (barangay and precinct open up once a city is chosen).

Big provinces (NCR has 7.5M voters) stay fast because:
- unfiltered and city/barangay totals come from the pre-computed roll summary, never a
  COUNT(*) over the province;
- names are matched with the roll's FULLTEXT index (start of each word): LIKE '%x%' has to
  read all 7.5M names (~4 s). When a name is too common to sort its matches quickly, the
  page walks the name index in A–Z order instead (REGEXP on the same word starts);
- EMS filters (card, sector, religion, …) are `id IN (SELECT voter_id FROM generic_360_db…)`
  so MySQL starts from the small EMS table. "Without card" / "Active" are NOT IN, and their
  counts come from the summary by inclusion–exclusion instead of scanning the province.
Read-only.
"""
import itertools
import math
import re
from urllib.parse import urlencode

from django.db import connections

from municipal import household as hh
from municipal import machinery as mach
from municipal import rollsummary
from municipal import smartcard as sc
from municipal.text import title
from municipal.views import RELIGIONS, _precinct_counts

FILTERS = ('city', 'q', 'kw', 'ln', 'bd', 'card', 'sector', 'religion', 'household', 'status', 'age', 'barangay',
           'precinct')
PER = 15
SORT_LIMIT = 20000   # up to this many matches are sorted A–Z directly; more walk the name index
# InnoDB's default full-text stopwords; with innodb_ft_min_token_size = 3 these words (and any
# word under 3 letters) are not in the index, so they are matched with LIKE instead.
FT_STOPWORDS = {'a', 'about', 'an', 'are', 'as', 'at', 'be', 'by', 'com', 'de', 'en', 'for', 'from', 'how', 'i',
                'in', 'is', 'it', 'la', 'of', 'on', 'or', 'that', 'the', 'this', 'to', 'was', 'what', 'when',
                'where', 'who', 'will', 'with', 'und', 'www'}


def _words(text):
    return re.findall(r'\w+', text.upper())


def _indexed(word):
    return len(word) >= 3 and word.lower() not in FT_STOPWORDS


def _name_conds(texts, walk, anywhere=False):
    """(sql, args) conditions matching every word of each text in fullname.

    Province-wide, indexed words match the start of a word: MATCH … AGAINST normally, or
    REGEXP '\\bWORD' when walking idx_fullname (MATCH would switch MySQL back to the full-text
    index). Other words — and every word once a city is chosen (`anywhere`: scanning one city
    via idx_muni_brgy beats checking each full-text hit's city) — match anywhere, like the city list.
    """
    conds = []
    for text in texts:
        words = _words(text)
        if anywhere:
            conds += [('fullname LIKE %s', [f'%{w}%']) for w in words]
            continue
        indexed = [w for w in words if _indexed(w)]
        if indexed and not walk:
            conds.append(('MATCH(fullname) AGAINST (%s IN BOOLEAN MODE)', [' '.join(f'+{w}*' for w in indexed)]))
        elif indexed:
            conds += [('fullname REGEXP %s', [r'\b' + w]) for w in indexed]
        conds += [('fullname LIKE %s', [f'%{w}%']) for w in words if not _indexed(w)]
    return conds


def _where(conds):
    return ' AND '.join(c for c, _ in conds) or '1', [a for _, args in conds for a in args]


def name_search(table, text, limit):
    """(match count, first `limit` rows A–Z) for a name in one province table — the same
    full-text / name-index strategy as the list. Rows: (id, fullname, municipality, barangay, precinct)."""
    if not any(_indexed(w) for w in _words(text)):
        return 0, []
    with connections[mach.RDS].cursor() as cur:
        wsql, params = _where(_name_conds([text], walk=False))
        cur.execute(f'SELECT COUNT(*) FROM {table} WHERE {wsql}', params)
        total = cur.fetchone()[0]
        if not total:
            return 0, []
        if total <= SORT_LIMIT:
            force = 'IGNORE INDEX FOR ORDER BY (idx_fullname)'
        else:
            force = 'FORCE INDEX (idx_fullname)'
            wsql, params = _where(_name_conds([text], walk=True))
        cur.execute(f'SELECT id, fullname, municipality, barangay, precinct FROM {table} {force} WHERE {wsql} '
                    'ORDER BY fullname LIMIT %s', params + [int(limit)])
        return total, cur.fetchall()


def voters_page(ps, get, level='prov'):
    """Everything the province Voters List template needs for this request."""
    slug, table = ps['province'], ps['table']
    ext = mach._schema('ext')
    f = {k: (get.get(k) or '').strip() for k in FILTERS}
    try:
        page = max(1, int(get.get('page', 1)))
    except (TypeError, ValueError):
        page = 1

    roll = rollsummary.province_rows(slug)                  # [(municipality, barangay, voters, precincts)]
    munis = rollsummary.municipalities(slug)
    hh_ready = hh.tables_exist()

    loc, sel, neg = [], [], []     # location (summary-countable) / selective / NOT IN (positive form)
    names = []                     # name texts, turned into conditions once the sort mode is known
    active, notes = [], []

    def ems(tbl, cond, *args):
        return (f'id IN (SELECT voter_id FROM {ext}.{tbl} WHERE province_slug = %s AND {cond})', [slug, *args])

    # --- where ---------------------------------------------------------------------------
    city = f['city'] if f['city'] in munis else ''
    f['city'] = city
    if city:
        loc.append(('municipality = %s', [city]))
        active.append(('City / Municipality', munis[city]['name']))
    brgy_raws = sorted({b for m, b, _, _ in roll if m == city and (title(b) or 'Unspecified') == f['barangay']}) \
        if city and f['barangay'] else []
    if brgy_raws:
        ph = ','.join(['%s'] * len(brgy_raws))
        sql = f'barangay IN ({ph})' + (' OR barangay IS NULL' if '' in brgy_raws else '')
        loc.append((f'({sql})', brgy_raws))
        active.append(('Barangay', f['barangay']))
    else:
        f['barangay'] = ''

    precinct_options = []
    if brgy_raws:
        precinct_options = sorted({p['precinct'] for p in _precinct_counts({'province': slug, 'municipality': city,
                                                                              'table': table})
                                   if p['barangay'] == f['barangay'] and p['precinct']})
    if f['precinct'] in precinct_options:
        sel.append(('TRIM(precinct) = %s', [f['precinct']]))
        active.append(('Precinct', f['precinct']))
    else:
        f['precinct'] = ''

    if f['q']:
        if city or any(_indexed(w) for w in _words(f['q'])):
            names.append(f['q'])
            active.append(('Voter', f['q']))
        else:
            notes.append('Type at least 3 letters of a name to search the whole province, or choose a city first.')
            f['q'] = ''
    if f['kw']:
        if f['ln']:
            sel.append(('fullname LIKE %s', [f['kw'] + '%']))        # idx_fullname range, already A–Z
            active.append(('Lastname', f['kw']))
        elif city:
            like = f'%{f["kw"]}%'
            sel.append(('(fullname LIKE %s OR address LIKE %s OR precinct LIKE %s)', [like, like, like]))
            active.append(('Keyword', f['kw']))
        elif any(_indexed(w) for w in _words(f['kw'])):
            names.append(f['kw'])
            active.append(('Keyword (names)', f['kw']))
            notes.append('Without a city, Keyword searches names only. Choose a city to also search addresses and precincts.')
        else:
            notes.append('Type at least 3 letters to search the whole province, or choose a city first.')
            f['kw'] = ''
    f['ln'] = 'on' if f['ln'] else ''

    if f['card'] in ('with', 'without') and sc.table_exists():
        cond = ems(sc.TABLE, "status IN ('active', 'pending')")
        (sel if f['card'] == 'with' else neg).append(cond)
        active.append(('Card', 'With card' if f['card'] == 'with' else 'Without card'))
    else:
        f['card'] = ''
    if hh_ready:
        bd = hh._valid_date(f['bd'])
        if bd:
            sel.append(ems(hh.DETAILS, 'birthdate = %s', bd))
            active.append(('Birthdate', bd.strftime('%b %d, %Y')))
        else:
            f['bd'] = ''
        if f['religion']:
            sel.append(ems(hh.DETAILS, 'religion = %s', f['religion']))
            active.append(('Religion', f['religion']))
        if f['sector'] in hh.SECTORS:
            sel.append(ems(hh.SECTORS_TABLE, 'sector = %s', f['sector']))
            active.append(('Sector', f['sector']))
        else:
            f['sector'] = ''
        if f['household'] == 'leader':
            sel.append(ems(hh.MEMBERS, '1'))
            active.append(('Household', 'Household leader'))
        else:
            f['household'] = ''
        flagged_ph = ','.join(['%s'] * len(hh.FLAGGED_STATUSES))
        if f['status'] == 'Active':
            neg.append(ems(hh.DETAILS, f'status IN ({flagged_ph})', *hh.FLAGGED_STATUSES))
            active.append(('Voting status', 'Active'))
        elif f['status'] in hh.FLAGGED_STATUSES:
            sel.append(ems(hh.DETAILS, 'status = %s', f['status']))
            active.append(('Voting status', f['status']))
        else:
            f['status'] = ''
        bucket = next((b for b in hh.AGE_BUCKETS if b[0] == f['age']), None)
        if bucket:
            sel.append(ems(hh.DETAILS, 'birthdate IS NOT NULL AND TIMESTAMPDIFF(YEAR, birthdate, CURDATE()) '
                                       'BETWEEN %s AND %s', bucket[1], bucket[2]))
            active.append(('Age', bucket[0]))
        else:
            f['age'] = ''
    else:
        f.update(bd='', religion='', household='', status='', age='', sector='')

    # --- count + order -------------------------------------------------------------------
    negated = [(f'NOT {c}', a) for c, a in neg]
    force, order, order_note = '', 'municipality, barangay, id', ''
    with connections[mach.RDS].cursor() as cur:
        if not sel and not names:
            # Location + NOT IN only: the summary total minus the matches, by inclusion–exclusion.
            total = sum(n for m, b, n, _ in roll if (not city or m == city) and (not brgy_raws or b in brgy_raws))
            for k in range(1, len(neg) + 1):
                for combo in itertools.combinations(neg, k):
                    wsql, params = _where(loc + list(combo))
                    cur.execute(f'SELECT COUNT(*) FROM {table} WHERE {wsql}', params)
                    total += (-1) ** k * cur.fetchone()[0]
            conds = loc + negated
        else:
            conds = loc + sel + _name_conds(names, walk=False, anywhere=bool(city)) + negated
            wsql, params = _where(conds)
            cur.execute(f'SELECT COUNT(*) FROM {table} WHERE {wsql}', params)
            total = cur.fetchone()[0]
            if total <= SORT_LIMIT:
                # Few matches: sort them. Left alone, MySQL often "optimises" ORDER BY … LIMIT 15 by
                # walking the whole province's idx_fullname (2.6 s for one precinct vs 0.06 s).
                force, order = 'IGNORE INDEX FOR ORDER BY (idx_fullname)', 'fullname'
            elif names:
                conds = loc + sel + _name_conds(names, walk=True, anywhere=bool(city)) + negated
                force, order = 'FORCE INDEX (idx_fullname)', 'fullname'
            elif f['kw'] and f['ln']:        # a lastname prefix is an idx_fullname range, already A–Z
                order = 'fullname'
            else:
                order_note = (f'{total:,} matches: listed by city and barangay. '
                              'Narrow the filters to sort them A–Z.')

        pages = max(1, math.ceil(total / PER)) if total else 1
        page = min(page, pages)
        wsql, params = _where(conds)
        cur.execute(f'SELECT id, fullname, municipality, barangay, precinct FROM {table} {force} WHERE {wsql} '
                    f'ORDER BY {order} LIMIT %s OFFSET %s', params + [PER, (page - 1) * PER])
        rows = cur.fetchall()

    # --- decorate the page -----------------------------------------------------------------
    ids = [r[0] for r in rows]
    holders = sc.holders_among({'province': slug}, ids)
    positions = mach.positions_among(slug, ids, level)      # this EMS level's own machinery
    info = hh.details_among(slug, ids) if hh_ready else {}
    sectors = hh.sectors_among(slug, ids) if hh_ready else {}
    voters = []
    for vid, fullname, muni, brgy, precinct in rows:
        d = info.get(vid, {})
        voters.append({
            'id': vid, 'name': title(fullname), 'city': title(muni), 'barangay': title(brgy),
            'precinct': (precinct or '').strip(), 'card': holders.get(vid), 'pos': positions.get(vid),
            'sex': {'Male': 'M', 'Female': 'F'}.get(d.get('gender'), ''),
            'birthdate': d.get('birthdate'), 'age': d.get('age'),
            'org': d.get('org') or '', 'status': d.get('status') or '',
            'sectors': sectors.get(vid, []),
        })

    keep = {k: v for k, v in f.items() if v}
    base_qs = urlencode(keep)
    return {
        'f': f, 'voters': voters, 'total': total, 'page': page, 'pages': pages,
        'offset': (page - 1) * PER,
        'showing_from': (page - 1) * PER + 1 if total else 0,
        'showing_to': min(page * PER, total),
        'active_filters': active, 'filtered': bool(active), 'notes': notes, 'order_note': order_note,
        'cities': sorted(({'raw': m['raw'], 'pretty': m['name']} for m in munis.values()), key=lambda c: c['pretty']),
        'barangays': [{'raw': b, 'pretty': b} for b in sorted(munis[city]['barangays'])] if city else [],
        'precinct_options': precinct_options,
        'prev_url': f'?{base_qs}&page={page - 1}' if page > 1 else '',
        'next_url': f'?{base_qs}&page={page + 1}' if page < pages else '',
        'hh_ready': hh_ready,
        **_overview(ps, munis, hh_ready),
    }


def _overview(ps, munis, hh_ready):
    """Province KPIs + charts (unfiltered), like the city list's but per city/municipality."""
    slug = ps['province']
    summary = hh.city_details_summary({'province': slug})
    flagged_by_city, by_sector = {}, {}
    if hh_ready:
        ph = ','.join(['%s'] * len(hh.FLAGGED_STATUSES))
        with connections[mach.EXT].cursor() as cur:
            cur.execute(f'SELECT municipality, COUNT(*) FROM {hh.DETAILS} WHERE province_slug = %s '
                        f'AND status IN ({ph}) GROUP BY municipality', [slug, *hh.FLAGGED_STATUSES])
            flagged_by_city = dict(cur.fetchall())
            cur.execute(f'SELECT s.sector, d.gender, COUNT(*) FROM {hh.SECTORS_TABLE} s '
                        f'LEFT JOIN {hh.DETAILS} d ON d.province_slug = s.province_slug AND d.voter_id = s.voter_id '
                        'WHERE s.province_slug = %s GROUP BY s.sector, d.gender', [slug])
            for sector, gender, n in cur.fetchall():
                s = by_sector.setdefault(sector, {'Male': 0, 'Female': 0, 'total': 0})
                s['total'] += n
                if gender in s:
                    s[gender] += n

    total = sum(m['voters'] for m in munis.values())
    brgys = sum(len(m['barangays']) for m in munis.values())
    city_rows = sorted(({'barangay': m['name'], 'total': m['voters'],
                         'active': m['voters'] - flagged_by_city.get(m['raw'], 0)} for m in munis.values()),
                       key=lambda r: -r['total'])
    flagged = summary['flagged']
    main = [s for s, _ in hh.MAIN_SECTORS]
    return {
        'religions': sorted(set(RELIGIONS) | set(summary['religions'])),
        'sector_options': hh.SECTORS,
        'sector_members': sum(s['total'] for s in by_sector.values()),
        'statuses': ('Active',) + hh.FLAGGED_STATUSES,
        'age_buckets': [b[0] for b in hh.AGE_BUCKETS],
        'kpi': {
            'total': total, 'brgys': brgys, 'cities': len(munis),
            'active': total - flagged, 'active_pct': ((total - flagged) / total * 100) if total else 0,
            'deactivated': flagged, 'saved': summary['saved'],
        },
        'top_active': sorted(city_rows, key=lambda r: -r['active'])[:8],
        'max_active': max((r['active'] for r in city_rows), default=0),
        'with_birthdate': summary['with_birthdate'],
        'chart': {
            'brgy_labels': [r['barangay'] for r in city_rows[:10]],
            'brgy_total': [r['total'] for r in city_rows[:10]],
            'brgy_active': [r['active'] for r in city_rows[:10]],
            'status_labels': ['Active'] + list(hh.FLAGGED_STATUSES),
            'status_values': [total - flagged] + [summary['status'].get(s, 0) for s in hh.FLAGGED_STATUSES],
            'age_labels': list(summary['ages']),
            'age_values': list(summary['ages'].values()),
            'sector_labels': [label for _, label in hh.MAIN_SECTORS],
            'sector_male': [by_sector.get(s, {}).get('Male', 0) for s in main],
            'sector_female': [by_sector.get(s, {}).get('Female', 0) for s in main],
        },
    }
