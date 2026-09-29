from django.apps import AppConfig


class ProvincialConfig(AppConfig):
    """Province-Wide EMS — reuses the municipal app's data modules one level up."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'provincial'
