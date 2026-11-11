"""Configuración de la app de facturación.

Los datos (base de datos, clave secreta y, más adelante, certificados y folios)
se guardan FUERA de la carpeta del código, en ~/FacturacionDatos, para que no
queden dentro de Google Drive. Se puede cambiar con la variable de entorno
FACTURACION_DATOS.
"""
import os
from pathlib import Path

from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent

DATOS_DIR = Path(os.environ.get("FACTURACION_DATOS", Path.home() / "FacturacionDatos"))
DATOS_DIR.mkdir(parents=True, exist_ok=True)

# En un servidor (Render u otro) la configuración llega por variables de entorno.
PRODUCCION = bool(os.environ.get("DATABASE_URL")) or os.environ.get("FACTURACION_PRODUCCION") == "1"

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    _clave = DATOS_DIR / "clave_secreta.txt"
    if not _clave.exists():
        _clave.write_text(get_random_secret_key())
        _clave.chmod(0o600)
    SECRET_KEY = _clave.read_text().strip()

DEBUG = os.environ.get("FACTURACION_DEBUG", "0" if PRODUCCION else "1") == "1"
_hosts = [h.strip() for h in (os.environ.get("FACTURACION_HOSTS", "") + ","
                              + os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")).split(",") if h.strip()]
ALLOWED_HOSTS = ["127.0.0.1", "localhost"] + _hosts
CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in _hosts]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "core.middleware.DosPasosMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context.sociedades",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATOS_DIR / "facturacion.sqlite3",
    }
}

if os.environ.get("DATABASE_URL"):
    import dj_database_url
    DATABASES["default"] = dj_database_url.parse(
        os.environ["DATABASE_URL"], conn_max_age=600, conn_health_checks=True)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12 if PRODUCCION else 8}},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "es-cl"
TIME_ZONE = "America/Santiago"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Verificación en dos pasos (app de autenticación). Desactivada a pedido del 10-11-2026.
# Para volver a exigirla, definir la variable de entorno FACTURACION_DOS_PASOS=1.
EXIGIR_DOS_PASOS = os.environ.get("FACTURACION_DOS_PASOS", "0") == "1"

# Protección contra intentos de adivinar la clave: tras 5 fallos seguidos, el usuario
# queda bloqueado por 15 minutos.
INTENTOS_DE_INGRESO = 5
BLOQUEO_DE_INGRESO = 15 * 60

if PRODUCCION:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    }
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SESSION_COOKIE_AGE = 60 * 60 * 12  # la sesión dura 12 horas
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTHENTICATION_BACKENDS = ["core.auth.UsuarioOCorreo"]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "inicio"
LOGOUT_REDIRECT_URL = "login"

# Tasa de IVA vigente (19%).
TASA_IVA = 19
