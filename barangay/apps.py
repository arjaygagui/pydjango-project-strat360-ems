from django.apps import AppConfig


class BarangayConfig(AppConfig):
    """Barangay EMS — one barangay of one city, broken down by purok and precinct."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'barangay'
