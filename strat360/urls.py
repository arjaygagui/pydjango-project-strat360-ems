from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include

from municipal.auth import ThrottledLoginView, healthz

routes = [
    path(settings.ADMIN_URL, admin.site.urls),
    path('login/', ThrottledLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('healthz', healthz, name='healthz'),
    path('national/', include('national.urls')),
    path('barangay/', include('barangay.urls')),
    path('province/', include('provincial.urls')),
    path('', include('municipal.urls')),
]

# DJANGO_URL_PREFIX (e.g. 'strat360') puts the whole app under /strat360/ on a shared server;
# empty locally. Nginx passes the full path through, so nothing else needs to know about it.
urlpatterns = [path(f'{settings.URL_PREFIX}/', include(routes))] if settings.URL_PREFIX else routes
