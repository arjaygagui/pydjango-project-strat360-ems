"""
Sign-in brute-force protection + a health check for the hosting platform.

After settings.LOGIN_MAX_FAILURES failed sign-ins for one username (or 4x that from one IP
address), further attempts are refused for settings.LOGIN_LOCKOUT_MINUTES — without even
checking the password, so guessing can't continue in the background. A successful sign-in
clears the username's counter. Counters live in the app cache.

The client IP is REMOTE_ADDR; X-Forwarded-For is trusted only when DJANGO_BEHIND_PROXY is on
(otherwise anyone could fake it to dodge the IP limit).
"""
import hashlib
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.cache import never_cache

log = logging.getLogger('municipal.auth')


def client_ip(request):
    if getattr(settings, 'SECURE_PROXY_SSL_HEADER', None):
        fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if fwd:
            return fwd.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or 'unknown'


def _key(kind, value):
    return f'login-fail:{kind}:{hashlib.sha256(value.lower().encode("utf-8")).hexdigest()[:32]}'


# The sign-in page is shared by every EMS level; its badge names the level being signed in to
# (from ?next=), or none when the user will choose a level on the landing page next.
LOGIN_BADGES = (('/barangay/', 'fa-house-flag', 'Barangay'), ('/province/', 'fa-landmark', 'Provincial'),
                ('/national/', 'fa-flag', 'National'))
CITY_PATHS = ('/dashboard/', '/voters/', '/cards/', '/social/', '/quick-count/', '/heat-map/', '/ai-analytics/',
              '/transactions/', '/profile/', '/select-city/')


def login_badge(next_url):
    """(icon, label) for the login page badge."""
    path = (next_url or '').split('?')[0]
    for prefix, icon, label in LOGIN_BADGES:
        if path.startswith(prefix):
            return icon, label
    if path.startswith(CITY_PATHS):
        return 'fa-city', 'City / Municipal'
    return 'fa-check-to-slot', 'Election Management System'


class ThrottledLoginView(LoginView):
    template_name = 'municipal/login.html'
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['badge_icon'], ctx['badge_label'] = login_badge(ctx.get('next') or self.request.GET.get('next', ''))
        return ctx

    def _keys(self):
        username = (self.request.POST.get('username') or '').strip()
        return _key('user', username), _key('ip', client_ip(self.request))

    def _locked(self):
        user_key, ip_key = self._keys()
        limit = settings.LOGIN_MAX_FAILURES
        return cache.get(user_key, 0) >= limit or cache.get(ip_key, 0) >= limit * 4

    def post(self, request, *args, **kwargs):
        if self._locked():
            log.warning('Sign-in refused (locked out) for %r from %s', request.POST.get('username', ''), client_ip(request))
            form = self.get_form()
            return self.render_to_response(self.get_context_data(form=form, locked=True))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        timeout = settings.LOGIN_LOCKOUT_MINUTES * 60
        for key in self._keys():
            cache.set(key, cache.get(key, 0) + 1, timeout)
        log.warning('Failed sign-in for %r from %s', self.request.POST.get('username', ''), client_ip(self.request))
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(self._keys()[0])
        return super().form_valid(form)


@login_not_required
@never_cache
def healthz(request):
    """For load balancers / uptime checks: no data, no database access."""
    return JsonResponse({'ok': True})
