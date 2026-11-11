"""Envío de correos por la API web de Brevo.

El plan gratuito de Render bloquea los puertos de correo (SMTP), así que los
mensajes salen por HTTPS. Requiere las variables BREVO_API_KEY y
FACTURACION_CORREO_REMITENTE (un remitente verificado en Brevo).
"""
import base64
import logging
from email.utils import parseaddr

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

registro = logging.getLogger(__name__)


class BrevoBackend(BaseEmailBackend):
    @staticmethod
    def _cuerpo(mensaje):
        nombre, _ = parseaddr(mensaje.from_email or "")
        cuerpo = {"sender": {"name": nombre or "Facturación", "email": settings.DEFAULT_FROM_EMAIL},
                  "to": [{"email": d} for d in mensaje.to],
                  "subject": mensaje.subject, "textContent": mensaje.body}
        if mensaje.reply_to:
            cuerpo["replyTo"] = {"email": parseaddr(mensaje.reply_to[0])[1]}
        adjuntos = [{"name": nombre_archivo, "content": base64.b64encode(
                        contenido if isinstance(contenido, bytes) else contenido.encode()).decode()}
                    for nombre_archivo, contenido, *_ in mensaje.attachments]
        if adjuntos:
            cuerpo["attachment"] = adjuntos
        return cuerpo

    def send_messages(self, mensajes):
        enviados = 0
        for mensaje in mensajes:
            try:
                r = requests.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers={"api-key": settings.BREVO_API_KEY, "accept": "application/json"},
                    json=self._cuerpo(mensaje),
                    timeout=20)
                r.raise_for_status()
                enviados += 1
            except requests.RequestException as e:
                registro.error("No se pudo enviar el correo: %s", e)
                if not self.fail_silently:
                    raise
        return enviados
