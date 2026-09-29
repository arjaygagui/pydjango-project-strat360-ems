"""Province-Wide EMS pages (URLs under /province/)."""
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from municipal import rollsummary
from municipal.regions import regions_with_provinces, voter_table

from . import data


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
