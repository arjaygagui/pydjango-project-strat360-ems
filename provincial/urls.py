from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='prov_dashboard'),
    path('select/', views.select_province, name='prov_select'),
    path('build/', views.build_summary, name='prov_build'),
    path('open-city/', views.open_city, name='prov_open_city'),
]
