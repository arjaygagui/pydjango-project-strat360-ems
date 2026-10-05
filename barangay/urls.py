from django.urls import path

from municipal import views as city
from . import views

# A voter profile opened here stays in the Barangay EMS: the city profile's views with level='brgy'.
BRGY = {'level': 'brgy'}

urlpatterns = [
    path('', views.dashboard, name='brgy_dashboard'),
    path('select/', views.select, name='brgy_select'),
    path('api/barangays/', views.api_barangays, name='brgy_api_barangays'),
    path('api/search-voters/', views.api_search_voters, name='brgy_api_search_voters'),
    path('voters/', views.voters, name='brgy_voters'),
    path('voters/<int:voter_id>/', city.voter_profile, BRGY, name='brgy_voter'),
    path('voters/<int:voter_id>/political/', city.political, BRGY, name='brgy_political'),
    path('voters/<int:voter_id>/details/', city.voter_details, BRGY, name='brgy_voter_details'),
    path('voters/<int:voter_id>/household/add/', city.household_add, BRGY, name='brgy_household_add'),
    path('voters/<int:voter_id>/household/<int:member_id>/remove/', city.household_remove, BRGY,
         name='brgy_household_remove'),
    path('voters/<int:voter_id>/search/', city.voter_search, BRGY, name='brgy_voter_search'),
    path('voters/<int:voter_id>/superiors/', city.voter_superiors, BRGY, name='brgy_voter_superiors'),
    path('puroks/', views.puroks, name='brgy_puroks'),
    path('leaders/', views.leaders, name='brgy_leaders'),
    path('cards/', views.cards_list, name='brgy_cards'),
    path('cards/new/', views.card_new, name='brgy_card_new'),
    path('cards/<int:card_id>/status/', views.card_status, name='brgy_card_status'),
    path('social/', views.social_list, name='brgy_social'),
    path('social/new/', views.social_new, name='brgy_social_new'),
    path('social/<int:record_id>/status/', views.social_status, name='brgy_social_status'),
    path('quick-count/', views.quick_count, name='brgy_quick_count'),
    path('heat-map/', views.heat_map, name='brgy_heat_map'),
    path('heat-map/locate/', views.heat_map_locate, name='brgy_heat_map_locate'),
    path('ai-analytics/', views.ai_analytics, name='brgy_ai_analytics'),
    path('api/ai/', views.api_ai, name='brgy_api_ai'),
    path('transactions/', views.transactions_list, name='brgy_transactions'),
    path('profile/', city.user_profile, BRGY, name='brgy_profile'),
    path('profile/password/', city.user_password, BRGY, name='brgy_password'),
]
