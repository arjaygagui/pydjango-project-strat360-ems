from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include

from municipal.auth import ThrottledLoginView, healthz

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path('login/', ThrottledLoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('healthz', healthz, name='healthz'),
    path('national/', include('national.urls')),
    path('province/', include('provincial.urls')),
    path('', include('municipal.urls')),
]
