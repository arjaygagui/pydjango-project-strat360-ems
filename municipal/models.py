# EMS data has no Django models: the political machinery lives in the national tables
# (generic_360_db.ems_political_role / ems_voter_political / ems_voter_audit), shared with
# CVL-NATIONAL and accessed with raw SQL in municipal/machinery.py, and the other modules
# use raw SQL on generic_360_db too.
#
# The one model here belongs to the app's own login database (local SQLite, 'default'):
# extra account details for the User Profile page that Django's User has no field for.
from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ems_profile')
    phone = models.CharField(max_length=32, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Profile of {self.user}'
