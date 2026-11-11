"""Correo de agradecimiento al cliente cuando se emite una factura."""
import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from .utils import formatear_clp

registro = logging.getLogger(__name__)


def redactar(sociedad, cliente, documento=None, total=None):
    """Asunto y texto del correo con los datos reemplazados."""
    contacto = cliente.contacto("comercial")["nombre"] or cliente.nombre_fantasia or cliente.razon_social
    datos = {
        "{contacto}": contacto, "{cliente}": cliente.nombre_fantasia or cliente.razon_social,
        "{sociedad}": sociedad.razon_social,
        "{documento}": documento or "factura",
        "{total}": formatear_clp(total) if total is not None else "",
    }
    asunto, mensaje = sociedad.gracias_asunto, sociedad.gracias_mensaje
    for clave, valor in datos.items():
        asunto, mensaje = asunto.replace(clave, valor), mensaje.replace(clave, valor)
    return asunto, mensaje


def enviar_correo(sociedad, destinatario, asunto, mensaje):
    correo = EmailMessage(
        asunto, mensaje, to=[destinatario],
        from_email=f"{sociedad.razon_social} <{settings.DEFAULT_FROM_EMAIL}>",
        reply_to=[sociedad.email] if sociedad.email else None)
    return correo.send() == 1


def motivo_para_no_enviar(doc):
    """None si corresponde enviar el agradecimiento; si no, la razón (para mostrarla)."""
    soc, cli = doc.sociedad, doc.cliente
    if not soc.gracias_activo:
        return "inactivo"
    if not doc.es_factura or doc.estado != doc.EMITIDO or doc.es_prueba:
        return "no aplica"
    if doc.gracias_enviado:
        return "ya enviado"
    if not cli.recibe_gracias:
        return "cliente excluido"
    if not cli.contacto("comercial")["email"]:
        return "El cliente no tiene correo de contacto comercial."
    if not settings.CORREO_CONFIGURADO:
        return "El envío de correos no está configurado."
    return None


def agradecer(doc):
    """Envía el agradecimiento si corresponde. Devuelve (enviado, mensaje para el usuario)."""
    motivo = motivo_para_no_enviar(doc)
    if motivo:
        avisos = {"inactivo", "no aplica", "ya enviado", "cliente excluido"}
        return False, None if motivo in avisos else f"No se envió el agradecimiento: {motivo}"
    destino = doc.cliente.contacto("comercial")["email"]
    asunto, mensaje = redactar(doc.sociedad, doc.cliente, str(doc), doc.total)
    try:
        enviar_correo(doc.sociedad, destino, asunto, mensaje)
    except Exception as e:  # un problema del correo nunca debe afectar la emisión
        registro.error("Agradecimiento no enviado (%s): %s", doc.pk, e)
        return False, "No se pudo enviar el correo de agradecimiento. La factura quedó emitida igual."
    doc.gracias_enviado = timezone.now()
    doc.save(update_fields=["gracias_enviado"])
    return True, f"Se envió el agradecimiento a {destino}."
