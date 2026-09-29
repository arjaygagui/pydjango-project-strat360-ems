"""
Django settings for the STRAT360 municipal EMS.

Secrets (Django secret key, RDS credentials, Gemini key) live in `.env` next to manage.py —
never in this file; see `.env.example` for every setting. Every page requires login
(LoginRequiredMiddleware). With DJANGO_DEBUG=False the production block at the bottom turns
on HTTPS-only cookies, HSTS and the HTTPS redirect (see DEPLOY.md).
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env(path):
    """Minimal .env loader: KEY=VALUE per line, '#' lines are comments."""
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())
    except FileNotFoundError:
        pass


def _env(name, default=None):
    value = os.environ.get(name, default)
    if value is None:
        raise ImproperlyConfigured(f'Missing {name} — add it to the .env file next to manage.py.')
    return value


def _flag(name, default):
    return _env(name, 'True' if default else 'False').lower() in ('1', 'true', 'yes')


def _list(name, default=''):
    return [x.strip() for x in _env(name, default).split(',') if x.strip()]


_load_env(BASE_DIR / '.env')

SECRET_KEY = _env('DJANGO_SECRET_KEY')
DEBUG = _flag('DJANGO_DEBUG', False)
ALLOWED_HOSTS = _list('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost')
# Full origins (scheme + host) allowed to POST forms, e.g. https://ems.example.gov.ph
CSRF_TRUSTED_ORIGINS = _list('DJANGO_CSRF_TRUSTED_ORIGINS')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'municipal',
    'provincial',
    'national',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # Serves /static/ from STATIC_ROOT in production (no separate web server needed).
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    # Every view requires a logged-in user unless marked @login_not_required.
    'django.contrib.auth.middleware.LoginRequiredMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'strat360.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'strat360.wsgi.application'

# 'default' (local SQLite) holds only Django's own tables: logins, sessions, UserProfile.
# 'rds' is the national voter roll (read-only); 'ext' is where every EMS change is written.
# Each can use its own MySQL account (least privilege — see DEPLOY.md); EXT_DB_USER /
# EXT_DB_PASSWORD fall back to the RDS_* account when not set.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'strat360.db',
    },
    # cvl_national — the national voter roll. READ-ONLY: the session is opened in
    # read-only transaction mode, so MySQL itself rejects any write on this connection.
    'rds': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': _env('RDS_NAME', 'cvl_national'),
        'USER': _env('RDS_USER'),
        'PASSWORD': _env('RDS_PASSWORD'),
        'HOST': _env('RDS_HOST'),
        'PORT': _env('RDS_PORT', '3306'),
        # Reuse DB connections across requests instead of reconnecting to RDS every time.
        'CONN_MAX_AGE': 60,
        'OPTIONS': {
            'init_command': "SET SESSION sql_mode='STRICT_TRANS_TABLES', "
                            "SESSION transaction_read_only = ON",
        },
    },
    # generic_360_db — where ALL EMS transactions are written (political machinery +
    # audit trail), shared with CVL-NATIONAL. Its tables already exist and are never
    # created or migrated by Django (see strat360/routers.py).
    'ext': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': _env('EXT_DB_NAME', 'generic_360_db'),
        'USER': _env('EXT_DB_USER', _env('RDS_USER')),
        'PASSWORD': _env('EXT_DB_PASSWORD', _env('RDS_PASSWORD')),
        'HOST': _env('EXT_DB_HOST', _env('RDS_HOST')),
        'PORT': _env('EXT_DB_PORT', _env('RDS_PORT', '3306')),
        'CONN_MAX_AGE': 60,
        'OPTIONS': {
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    },
}

# Only 'default' (SQLite) is ever migrated; 'rds' and 'ext' are used via raw SQL only.
DATABASE_ROUTERS = ['strat360.routers.VoterRouter']

# --- Authentication ---
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'landing'           # choose the EMS level after signing in
LOGOUT_REDIRECT_URL = 'login'
SESSION_COOKIE_AGE = 8 * 3600          # sign-ins last one working day
SESSION_COOKIE_HTTPONLY = True

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Manila'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'          # filled by `manage.py collectstatic`
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    # Gzip/Brotli copies, no hashed names (the Sneat CSS references files we don't ship).
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage'},
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# File cache for the heavy RDS aggregates (city lists, dashboard/list totals), like the
# PHP app's cache/*.json. Survives server restarts; delete the folder to force a refresh.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': BASE_DIR / 'cache',
    }
}

# AI Analytics (Google Gemini, like the PHP app). Optional: without a key the page shows
# a "not configured" notice. Put GEMINI_API_KEY=... in .env — never in code.
GEMINI_API_KEY = _env('GEMINI_API_KEY', '')
GEMINI_MODEL = _env('GEMINI_MODEL', 'gemini-2.5-flash')

# Login brute-force protection (municipal/auth.py): after this many failed sign-ins for one
# username or one IP address, further attempts are refused for LOGIN_LOCKOUT_MINUTES.
LOGIN_MAX_FAILURES = int(_env('LOGIN_MAX_FAILURES', '5'))
LOGIN_LOCKOUT_MINUTES = int(_env('LOGIN_LOCKOUT_MINUTES', '15'))

# Django admin lives here (change it in production to keep it off obvious scanners).
ADMIN_URL = _env('DJANGO_ADMIN_URL', 'admin/').strip('/') + '/'

# Errors (and WARNING+ from Django) go to the console, where the host's service manager or
# platform log collects them. Tracebacks never reach users when DEBUG is off.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {'plain': {'format': '{asctime} {levelname} {name}: {message}', 'style': '{'}},
    'handlers': {'console': {'class': 'logging.StreamHandler', 'formatter': 'plain'}},
    'root': {'handlers': ['console'], 'level': _env('DJANGO_LOG_LEVEL', 'WARNING')},
}

# ---------------------------------------------------------------------------
# Production (DJANGO_DEBUG=False): HTTPS everywhere.
# ---------------------------------------------------------------------------
if not DEBUG:
    SECURE_SSL_REDIRECT = _flag('DJANGO_SSL_REDIRECT', True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Start small; raise to 31536000 (1 year) once HTTPS is confirmed working for the domain.
    SECURE_HSTS_SECONDS = int(_env('DJANGO_HSTS_SECONDS', '3600'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = _flag('DJANGO_HSTS_INCLUDE_SUBDOMAINS', False)
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    # Behind nginx / a load balancer / a PaaS that terminates HTTPS and sets X-Forwarded-Proto.
    if _flag('DJANGO_BEHIND_PROXY', False):
        SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
