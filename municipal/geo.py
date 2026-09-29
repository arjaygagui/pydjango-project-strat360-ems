"""
Barangay locations for the Heat Map.

No table in cvl_national or generic_360_db has coordinates, so barangays are located once
per city by looking up their NAMES on OpenStreetMap Nominatim ("<barangay>, <city>,
<province>, Philippines") and stored in generic_360_db.muni_barangay_geo. Only place names
are ever sent — never voter data. Nominatim's usage policy allows one request per second,
so locating is done by `manage.py geocode_barangays` (or the Heat Map's "Locate barangays"
button for staff), never while rendering a page.

A result more than MAX_KM from the town centre is treated as a wrong match. Barangays that
can't be found are placed on a small ring around the town centre with source = 'approx', and
the page labels them as approximate.
"""
import json
import math
import re
import time
import urllib.parse
import urllib.request

from django.db import connections

from .machinery import EXT
from .text import title

TABLE = 'muni_barangay_geo'
MAX_KM = 25
USER_AGENT = 'Strat360-EMS-Django/1.0 (barangay heat map; municipal EMS prototype)'
NOMINATIM = 'https://nominatim.openstreetmap.org/search'

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  province_slug varchar(32) NOT NULL,
  municipality varchar(120) NOT NULL,
  barangay varchar(160) NOT NULL,
  lat decimal(9,6) NOT NULL,
  lng decimal(9,6) NOT NULL,
  source varchar(16) NOT NULL,
  display_name varchar(255) DEFAULT NULL,
  located_at datetime DEFAULT NULL,
  PRIMARY KEY (province_slug, municipality, barangay)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def ensure_table():
    with connections[EXT].cursor() as cur:
        cur.execute(DDL)


def locations(scope):
    """{barangay_pretty: {'lat', 'lng', 'source'}} already stored for the city."""
    with connections[EXT].cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s',
                    [TABLE])
        if not cur.fetchone()[0]:
            return {}
        cur.execute(f"SELECT barangay, lat, lng, source FROM {TABLE} WHERE province_slug = %s AND municipality = %s "
                    "AND barangay <> ''", [scope['province'], scope['municipality']])
        return {b: {'lat': float(lat), 'lng': float(lng), 'source': src} for b, lat, lng, src in cur.fetchall()}


def _search(q):
    url = NOMINATIM + '?' + urllib.parse.urlencode({'format': 'json', 'limit': 1, 'countrycodes': 'ph', 'q': q})
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept-Language': 'en'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        rows = json.loads(resp.read().decode('utf-8'))
    if not rows:
        return None
    return float(rows[0]['lat']), float(rows[0]['lon']), rows[0].get('display_name', '')[:255]


def _km(a, b):
    """Great-circle distance between two (lat, lng) points."""
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def city_label(scope):
    """'Quezon City 5th District' -> 'Quezon City' (Nominatim knows cities, not districts)."""
    return re.sub(r'\s+\d+(st|nd|rd|th)\s+district$', '', title(scope['municipality']), flags=re.I)


# ---------------------------------------------------------------------------
# City / municipality centres, for the Province-Wide heat map. Stored in the same table
# with barangay = '' (the town centre). Districts of one city (e.g. Quezon City 1st–6th)
# share its centre, so they are fanned out on a small ring and marked 'district'.
# ---------------------------------------------------------------------------
CITY_MAX_KM = 200            # a match farther than this from the province centre is wrong
SEARCH_NAMES = {'ncr': 'Metro Manila', 'sga': 'Cotabato'}   # pseudo-provinces on the roll
# Misspelt town names on the roll -> the name to look up (the roll itself is never changed).
TOWN_ALIASES = {'CALUMPT': 'Calumpit'}


def city_locations(province):
    """{municipality_raw: {'lat', 'lng', 'source'}} of the province's located town centres."""
    with connections[EXT].cursor() as cur:
        cur.execute('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s',
                    [TABLE])
        if not cur.fetchone()[0]:
            return {}
        cur.execute(f"SELECT municipality, lat, lng, source FROM {TABLE} WHERE province_slug = %s AND barangay = ''",
                    [province])
        return {m: {'lat': float(lat), 'lng': float(lng), 'source': src} for m, lat, lng, src in cur.fetchall()}


def locate_cities(province, municipalities, province_name, limit=None, log=print):
    """Locate the province's not-yet-stored town centres. Returns (found, approx, left)."""
    ensure_table()
    have = city_locations(province)
    todo = [m for m in municipalities if m not in have]
    left = max(0, len(todo) - limit) if limit else 0
    if limit:
        todo = todo[:limit]
    if not todo:
        return 0, 0, 0
    pname = SEARCH_NAMES.get(province, province_name)
    centre = _search(f'{pname}, Philippines')
    time.sleep(1.1)
    def label_of(m):
        return TOWN_ALIASES.get(m, city_label({'municipality': m}))

    # Districts are fanned out only when several share one city name (Caloocan City 1st–3rd);
    # a lone "Binondo 3rd District" keeps Binondo's own spot.
    shared = {}
    for m in municipalities:
        shared[label_of(m)] = shared.get(label_of(m), 0) + 1
    labels = {}              # one lookup per city name (its districts reuse it)
    for m, v in have.items():
        if v['source'] == 'nominatim':       # district rows are offset copies, not the centre
            labels.setdefault(label_of(m), (v['lat'], v['lng']))
    found = approx = 0
    rows = []
    for i, m in enumerate(todo):
        label = label_of(m)
        district = shared.get(label, 0) > 1
        hit = labels.get(label)
        if hit is None:
            for q in (f'{label}, {pname}, Philippines', f'{label}, Philippines'):
                try:
                    res = _search(q)
                except OSError as e:
                    log(f'  error  {m}: {e}')
                    res = None
                time.sleep(1.1)       # Nominatim policy: at most 1 request per second
                if res and (not centre or _km(centre[:2], res[:2]) <= CITY_MAX_KM):
                    hit = res[:2]
                    break
            if hit:
                labels[label] = hit
        if hit:
            if district:
                num = re.search(r'(\d+)', title(m))
                angle = 2 * math.pi * ((int(num.group(1)) if num else i) / 6)
                rows.append([m, hit[0] + 0.02 * math.sin(angle), hit[1] + 0.02 * math.cos(angle), 'district', label])
            else:
                rows.append([m, hit[0], hit[1], 'nominatim', label])
            found += 1
            log(f'  found  {m}')
        elif centre:
            angle = 2 * math.pi * (i / max(1, len(todo)))
            rows.append([m, centre[0] + 0.08 * math.sin(angle), centre[1] + 0.08 * math.cos(angle), 'approx', None])
            approx += 1
            log(f'  approx {m}: not found, placed near the province centre')
    with connections[EXT].cursor() as cur:
        cur.executemany(
            f'INSERT INTO {TABLE} (province_slug, municipality, barangay, lat, lng, source, display_name, located_at) '
            "VALUES (%s, %s, '', %s, %s, %s, %s, UTC_TIMESTAMP()) "
            'ON DUPLICATE KEY UPDATE lat = VALUES(lat), lng = VALUES(lng), source = VALUES(source), '
            'display_name = VALUES(display_name), located_at = VALUES(located_at)',
            [[province, *r] for r in rows])
    return found, approx, left


def locate(scope, barangays, province_name, limit=None, log=print):
    """Locate the given barangays that aren't stored yet. Returns (found, approx, skipped)."""
    ensure_table()
    have = locations(scope)
    todo = [b for b in barangays if b not in have]
    if limit:
        todo = todo[:limit]
    if not todo:
        return 0, 0, 0
    city = city_label(scope)
    centre = _search(f'{city}, {province_name}, Philippines') or _search(f'{city}, Philippines')
    time.sleep(1.1)
    if not centre:
        raise RuntimeError(f'Could not find {city} on OpenStreetMap.')
    found = approx = 0
    rows = []
    for i, b in enumerate(todo):
        hit = None
        for q in (f'{b}, {city}, {province_name}, Philippines', f'Barangay {b}, {city}, Philippines'):
            try:
                hit = _search(q)
            except OSError as e:           # network hiccup: leave it for the next run
                log(f'  error  {b}: {e}')
                hit = None
            time.sleep(1.1)                 # Nominatim policy: at most 1 request per second
            if hit and _km(centre[:2], hit[:2]) <= MAX_KM:
                break
            hit = None
        if hit:
            rows.append([b, hit[0], hit[1], 'nominatim', hit[2]])
            found += 1
            log(f'  found  {b}: {hit[0]:.5f}, {hit[1]:.5f}')
        else:
            # Not found: a small ring around the town centre, clearly marked as approximate.
            angle = 2 * math.pi * (i / max(1, len(todo)))
            rows.append([b, centre[0] + 0.012 * math.sin(angle), centre[1] + 0.012 * math.cos(angle), 'approx', None])
            approx += 1
            log(f'  approx {b}: not found, placed near the town centre')
    with connections[EXT].cursor() as cur:
        cur.executemany(
            f'INSERT INTO {TABLE} (province_slug, municipality, barangay, lat, lng, source, display_name, located_at) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP()) '
            'ON DUPLICATE KEY UPDATE lat = VALUES(lat), lng = VALUES(lng), source = VALUES(source), '
            'display_name = VALUES(display_name), located_at = VALUES(located_at)',
            [[scope['province'], scope['municipality'], *r] for r in rows])
    return found, approx, len([b for b in barangays if b not in have]) - len(todo)
