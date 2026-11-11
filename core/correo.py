"""Envío de correos por la API web de Brevo.

El plan gratuito de Render bloquea los puertos de correo (SMTP), así que los
mensajes salen por HTTPS. Requiere las variables BREVO_API_KEY y
FACTURACION_CORREO_REMITENTE (un remitente verificado en Brevo).
"""
import logging

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

registro = logging.getLogger(__name__)


class BrevoBackend(BaseEmailBackend):
    def send_messages(self, mensajes):
        enviados = 0
        for mensaje in mensajes:
            try:
                r = requests.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers={"api-key": settings.BREVO_API_KEY, "accept": "application/json"},
                    json={"sender": {"name": "Facturación", "email": settings.DEFAULT_FROM_EMAIL},
                          "to": [{"email": d} for d in mensaje.to],
                          "subject": mensaje.subject, "textContent": mensaje.body},
                    timeout=20)
                r.raise_for_status()
                enviados += 1
            except requests.RequestException as e:
                registro.error("No se pudo enviar el correo: %s", e)
                if not self.fail_silently:
                    raise
        return enviados
