from django.urls import path

from . import views

urlpatterns = [
    path('', views.dashboard, name='prov_dashboard'),
    path('select/', views.select_province, name='prov_select'),
    path('build/', views.build_summary, name='prov_build'),
    path('open-city/', views.open_city, name='prov_open_city'),
    path('voters/', views.voters_list, name='prov_voters'),
    path('voters/<int:voter_id>/', views.open_voter, name='prov_voter'),
    path('cards/', views.cards_list, name='prov_cards'),
    path('cards/new/', views.card_new, name='prov_card_new'),
    path('cards/<int:card_id>/status/', views.card_status, name='prov_card_status'),
    path('social/', views.social_list, name='prov_social'),
    path('social/new/', views.social_new, name='prov_social_new'),
    path('quick-count/', views.quick_count, name='prov_quick_count'),
    path('heat-map/', views.heat_map, name='prov_heat_map'),
    path('ai-analytics/', views.ai_analytics, name='prov_ai_analytics'),
    path('transactions/', views.transactions_list, name='prov_transactions'),
    path('api/ai/', views.api_ai, name='prov_api_ai'),
    path('heat-map/locate/', views.heat_map_locate, name='prov_heat_map_locate'),
    path('api/search-voters/', views.api_search_voters, name='prov_api_search_voters'),
    path('social/<int:record_id>/status/', views.social_status, name='prov_social_status'),
]
