"""
Philippine province slug -> region (port of the live app's config/regions.php).

Slugs match the `cvl_<slug>` voter tables on the read-only 'rds' database.
REGION_MAP doubles as the whitelist: a table name is only ever built from a
slug that appears here, so user input can never inject an arbitrary table.
"""

REGION_MAP = {
    # Region I - Ilocos Region
    'ilocosnorte': 'Region I (Ilocos Region)',
    'ilocossur': 'Region I (Ilocos Region)',
    'launion': 'Region I (Ilocos Region)',
    'pangasinan': 'Region I (Ilocos Region)',
    # Region II - Cagayan Valley
    'batanes': 'Region II (Cagayan Valley)',
    'cagayan': 'Region II (Cagayan Valley)',
    'isabela': 'Region II (Cagayan Valley)',
    'nuevavizcaya': 'Region II (Cagayan Valley)',
    'quirino': 'Region II (Cagayan Valley)',
    # Region III - Central Luzon
    'aurora': 'Region III (Central Luzon)',
    'bataan': 'Region III (Central Luzon)',
    'bulacan': 'Region III (Central Luzon)',
    'nuevaecija': 'Region III (Central Luzon)',
    'pampanga': 'Region III (Central Luzon)',
    'tarlac': 'Region III (Central Luzon)',
    'zambales': 'Region III (Central Luzon)',
    # Region IV-A - CALABARZON
    'batangas': 'Region IV-A (CALABARZON)',
    'cavite': 'Region IV-A (CALABARZON)',
    'laguna': 'Region IV-A (CALABARZON)',
    'quezon': 'Region IV-A (CALABARZON)',
    'rizal': 'Region IV-A (CALABARZON)',
    # Region IV-B - MIMAROPA
    'marinduque': 'Region IV-B (MIMAROPA)',
    'mindorooccidental': 'Region IV-B (MIMAROPA)',
    'mindorooriental': 'Region IV-B (MIMAROPA)',
    'palawan': 'Region IV-B (MIMAROPA)',
    'romblon': 'Region IV-B (MIMAROPA)',
    # Region V - Bicol
    'albay': 'Region V (Bicol Region)',
    'camarinesnorte': 'Region V (Bicol Region)',
    'camarinessur': 'Region V (Bicol Region)',
    'catanduanes': 'Region V (Bicol Region)',
    'masbate': 'Region V (Bicol Region)',
    'sorsogon': 'Region V (Bicol Region)',
    # Region VI - Western Visayas
    'aklan': 'Region VI (Western Visayas)',
    'antique': 'Region VI (Western Visayas)',
    'capiz': 'Region VI (Western Visayas)',
    'guimaras': 'Region VI (Western Visayas)',
    'iloilo': 'Region VI (Western Visayas)',
    'negrosoccidental': 'Region VI (Western Visayas)',
    # Region VII - Central Visayas
    'bohol': 'Region VII (Central Visayas)',
    'cebu': 'Region VII (Central Visayas)',
    'negrosoriental': 'Region VII (Central Visayas)',
    'siquijor': 'Region VII (Central Visayas)',
    # Region VIII - Eastern Visayas
    'biliran': 'Region VIII (Eastern Visayas)',
    'easternsamar': 'Region VIII (Eastern Visayas)',
    'leyte': 'Region VIII (Eastern Visayas)',
    'northernsamar': 'Region VIII (Eastern Visayas)',
    'southernleyte': 'Region VIII (Eastern Visayas)',
    'westernsamar': 'Region VIII (Eastern Visayas)',
    # Region IX - Zamboanga Peninsula
    'zamboangadelnorte': 'Region IX (Zamboanga Peninsula)',
    'zamboangadelsur': 'Region IX (Zamboanga Peninsula)',
    'zamboangasibugay': 'Region IX (Zamboanga Peninsula)',
    # Region X - Northern Mindanao
    'bukidnon': 'Region X (Northern Mindanao)',
    'camiguin': 'Region X (Northern Mindanao)',
    'lanaodelnorte': 'Region X (Northern Mindanao)',
    'misamisoccidental': 'Region X (Northern Mindanao)',
    'misamisoriental': 'Region X (Northern Mindanao)',
    # Region XI - Davao
    'davaodelnorte': 'Region XI (Davao Region)',
    'davaodelsur': 'Region XI (Davao Region)',
    'davaodeoro': 'Region XI (Davao Region)',
    'davaooccidental': 'Region XI (Davao Region)',
    'davaooriental': 'Region XI (Davao Region)',
    # Region XII - SOCCSKSARGEN
    'northcotabato': 'Region XII (SOCCSKSARGEN)',
    'sarangani': 'Region XII (SOCCSKSARGEN)',
    'southcotabato': 'Region XII (SOCCSKSARGEN)',
    'sultankudarat': 'Region XII (SOCCSKSARGEN)',
    # Region XIII - Caraga
    'agusandelnorte': 'Region XIII (Caraga)',
    'agusandelsur': 'Region XIII (Caraga)',
    'dinagatislands': 'Region XIII (Caraga)',
    'surigaodelnorte': 'Region XIII (Caraga)',
    'surigaodelsur': 'Region XIII (Caraga)',
    # CAR
    'abra': 'CAR (Cordillera Administrative Region)',
    'apayao': 'CAR (Cordillera Administrative Region)',
    'benguet': 'CAR (Cordillera Administrative Region)',
    'ifugao': 'CAR (Cordillera Administrative Region)',
    'kalinga': 'CAR (Cordillera Administrative Region)',
    'mountainprovince': 'CAR (Cordillera Administrative Region)',
    # BARMM
    'basilan': 'BARMM (Bangsamoro)',
    'lanaodelsur': 'BARMM (Bangsamoro)',
    'maguindanaodelnorte': 'BARMM (Bangsamoro)',
    'maguindanaodelsur': 'BARMM (Bangsamoro)',
    'sulu': 'BARMM (Bangsamoro)',
    'tawitawi': 'BARMM (Bangsamoro)',
    'sga': 'BARMM (Bangsamoro)',
    # NCR
    'ncr': 'NCR (National Capital Region)',
}

# Slugs with a voter table that are NOT actual provinces (region / special area).
PSEUDO_PROVINCE_SLUGS = {'ncr', 'sga'}

_PRETTY = {
    'ncr': 'NCR', 'sga': 'SGA',
    'launion': 'La Union', 'davaodeoro': 'Davao de Oro',
    'davaodelnorte': 'Davao del Norte', 'davaodelsur': 'Davao del Sur',
    'davaooccidental': 'Davao Occidental', 'davaooriental': 'Davao Oriental',
    'agusandelnorte': 'Agusan del Norte', 'agusandelsur': 'Agusan del Sur',
    'camarinesnorte': 'Camarines Norte', 'camarinessur': 'Camarines Sur',
    'easternsamar': 'Eastern Samar', 'westernsamar': 'Western Samar',
    'northernsamar': 'Northern Samar', 'southernleyte': 'Southern Leyte',
    'ilocosnorte': 'Ilocos Norte', 'ilocossur': 'Ilocos Sur',
    'lanaodelnorte': 'Lanao del Norte', 'lanaodelsur': 'Lanao del Sur',
    'maguindanaodelnorte': 'Maguindanao del Norte', 'maguindanaodelsur': 'Maguindanao del Sur',
    'mindorooccidental': 'Occidental Mindoro', 'mindorooriental': 'Oriental Mindoro',
    'misamisoccidental': 'Misamis Occidental', 'misamisoriental': 'Misamis Oriental',
    'mountainprovince': 'Mountain Province', 'negrosoccidental': 'Negros Occidental',
    'negrosoriental': 'Negros Oriental', 'northcotabato': 'North Cotabato',
    'nuevaecija': 'Nueva Ecija', 'nuevavizcaya': 'Nueva Vizcaya',
    'southcotabato': 'South Cotabato', 'sultankudarat': 'Sultan Kudarat',
    'surigaodelnorte': 'Surigao del Norte', 'surigaodelsur': 'Surigao del Sur',
    'tawitawi': 'Tawi-Tawi', 'zamboangadelnorte': 'Zamboanga del Norte',
    'zamboangadelsur': 'Zamboanga del Sur', 'zamboangasibugay': 'Zamboanga Sibugay',
    'dinagatislands': 'Dinagat Islands',
}


def province_pretty(slug):
    return _PRETTY.get(slug, slug.title())


def region_of(slug):
    return REGION_MAP.get(slug)


def voter_table(slug):
    """`cvl_<slug>` for a whitelisted province slug, else None."""
    slug = (slug or '').strip().lower()
    return f'cvl_{slug}' if slug in REGION_MAP else None


def regions_with_provinces():
    """[{'region': str, 'provinces': [{'slug', 'name'}...]}...] sorted like the PHP app."""
    grouped = {}
    for slug, region in REGION_MAP.items():
        grouped.setdefault(region, []).append(slug)
    return [
        {'region': region,
         'provinces': [{'slug': s, 'name': province_pretty(s)} for s in sorted(grouped[region])]}
        for region in sorted(grouped)
    ]
