from django.apps import AppConfig


class NationalConfig(AppConfig):
    """Nationwide EMS — the province pages one level up: all 84 provinces, region as a filter."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'national'
