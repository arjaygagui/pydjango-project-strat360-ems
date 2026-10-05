from django.urls import path

from municipal import views as city
from . import views

# A voter profile opened here stays in the Nationwide EMS: the city profile's views with level='nat'.
NAT = {'level': 'nat'}

urlpatterns = [
    path('', views.dashboard, name='nat_dashboard'),
    path('voters/', views.voters_list, name='nat_voters'),
    path('voters/<slug:slug>/<int:voter_id>/', city.voter_profile, NAT, name='nat_voter'),
    path('voters/<slug:slug>/<int:voter_id>/political/', city.political, NAT, name='nat_political'),
    path('voters/<slug:slug>/<int:voter_id>/details/', city.voter_details, NAT, name='nat_voter_details'),
    path('voters/<slug:slug>/<int:voter_id>/household/add/', city.household_add, NAT, name='nat_household_add'),
    path('voters/<slug:slug>/<int:voter_id>/household/<int:member_id>/remove/', city.household_remove, NAT,
         name='nat_household_remove'),
    path('voters/<slug:slug>/<int:voter_id>/search/', city.voter_search, NAT, name='nat_voter_search'),
    path('voters/<slug:slug>/<int:voter_id>/superiors/', city.voter_superiors, NAT, name='nat_voter_superiors'),
    path('cards/', views.cards_list, name='nat_cards'),
    path('cards/new/', views.card_new, name='nat_card_new'),
    path('cards/<int:card_id>/status/', views.card_status, name='nat_card_status'),
    path('social/', views.social_list, name='nat_social'),
    path('social/new/', views.social_new, name='nat_social_new'),
    path('social/<int:record_id>/status/', views.social_status, name='nat_social_status'),
    path('quick-count/', views.quick_count, name='nat_quick_count'),
    path('heat-map/', views.heat_map, name='nat_heat_map'),
    path('heat-map/locate/', views.heat_map_locate, name='nat_heat_map_locate'),
    path('transactions/', views.transactions_list, name='nat_transactions'),
    path('profile/', city.user_profile, NAT, name='nat_profile'),
    path('profile/password/', city.user_password, NAT, name='nat_password'),
    path('ai-analytics/', views.ai_analytics, name='nat_ai_analytics'),
    path('api/ai/', views.api_ai, name='nat_api_ai'),
    path('api/search-voters/', views.api_search_voters, name='nat_api_search_voters'),
]
