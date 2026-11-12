"""Operaciones de emisión electrónica sobre los modelos de la app."""
from datetime import timedelta
from io import BytesIO

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from PIL import Image, ImageChops

from ..models import Caf, CertificadoDigital, Documento, Envio, Sociedad
from ..utils import normalizar_rut
from . import sii
from .motor import ErrorDTE, Firmante, Folios, generar_dte, generar_envio, validar
from .secretos import cifrar, descifrar


def firmante_de(sociedad) -> Firmante:
    cert = CertificadoDigital.objects.filter(sociedad=sociedad).first()
    if not cert:
        raise ErrorDTE("La sociedad no tiene certificado digital cargado (Conexión SII).")
    if not cert.vigente:
        raise ErrorDTE(f"El certificado digital venció el {cert.vence:%d/%m/%Y}.")
    return Firmante(descifrar(cert.pfx), descifrar(cert.clave).decode())


def guardar_certificado(sociedad, pfx: bytes, clave: str, rut_titular: str):
    firmante = Firmante(pfx, clave)
    CertificadoDigital.objects.update_or_create(sociedad=sociedad, defaults={
        "titular": firmante.titular[:120], "rut_titular": normalizar_rut(rut_titular),
        "vence": firmante.vence, "pfx": cifrar(pfx), "clave": cifrar(clave.encode())})
    return firmante


def guardar_caf(sociedad, contenido: bytes):
    folios = Folios(contenido)
    if folios.rut != sociedad.rut.upper():
        raise ErrorDTE(f"El CAF es de otro RUT ({folios.rut}), no de {sociedad}.")
    if folios.tipo not in dict(Caf._meta.get_field("tipo").choices):
        raise ErrorDTE(f"La app aún no emite documentos tipo {folios.tipo}.")
    if sociedad.cafs.filter(tipo=folios.tipo, ambiente=sociedad.ambiente, desde=folios.desde).exists():
        raise ErrorDTE("Ese CAF ya estaba cargado.")
    return Caf.objects.create(
        sociedad=sociedad, tipo=folios.tipo, ambiente=sociedad.ambiente, desde=folios.desde,
        hasta=folios.hasta, siguiente=folios.desde, fecha_autorizacion=folios.fecha,
        xml=cifrar(contenido))


def caf_disponible(sociedad, tipo):
    return (sociedad.cafs.filter(tipo=tipo, ambiente=sociedad.ambiente, siguiente__lte=F("hasta"))
            .order_by("desde").first())


def faltantes(doc):
    """Qué falta para que la app pueda timbrar y firmar este documento."""
    soc, cli = doc.sociedad, doc.cliente
    falta = []
    if not CertificadoDigital.objects.filter(sociedad=soc).exists():
        falta.append("Cargar el certificado digital en Conexión SII.")
    if not caf_disponible(soc, doc.tipo):
        falta.append(f"Cargar folios (CAF) de {doc.tipo_corto.lower()} en Conexión SII.")
    datos_soc = [n for n, v in (("giro", soc.giro), ("código de actividad económica", soc.acteco),
                                ("dirección", soc.direccion), ("comuna", soc.comuna),
                                ("fecha de resolución SII", soc.resolucion_fecha)) if not v]
    if datos_soc:
        falta.append("Completar en la sociedad: " + ", ".join(datos_soc) + ".")
    datos_cli = [n for n, v in (("giro", cli.giro), ("dirección", cli.direccion),
                                ("comuna", cli.comuna)) if not v]
    if datos_cli:
        falta.append("Completar en el cliente: " + ", ".join(datos_cli) + ".")
    return falta


@transaction.atomic
def emitir(doc):
    """Asigna folio del CAF, genera el XML timbrado y firmado, y deja el documento emitido."""
    if doc.es_cotizacion:
        raise ErrorDTE("Las cotizaciones no se timbran ni se envían al SII.")
    errores = doc.errores_para_emitir() + faltantes(doc)
    if doc.estado != Documento.BORRADOR:
        errores = ["El documento ya está emitido."]
    if errores:
        raise ErrorDTE(" ".join(errores))
    soc = doc.sociedad = Sociedad.objects.select_for_update().get(pk=doc.sociedad_id)
    firmante = firmante_de(soc)
    caf = caf_disponible(soc, doc.tipo)
    doc.folio, caf.siguiente = caf.siguiente, caf.siguiente + 1
    caf.save(update_fields=["siguiente"])
    ahora = timezone.localtime()
    doc.estado, doc.estado_sii, doc.timbrado = Documento.EMITIDO, "", ahora
    doc.es_prueba = soc.ambiente == Sociedad.CERTIFICACION
    if doc.es_factura and not doc.fecha_vencimiento:
        doc.fecha_vencimiento = doc.fecha_emision + timedelta(days=doc.cliente.dias_pago)
    xml = generar_dte(doc, Folios(descifrar(caf.xml)), firmante, ahora)
    validar(xml, "DTE_v10.xsd")  # si falla, se deshace todo y el folio no se consume
    doc.xml_firmado = xml
    doc.save()
    return doc


def enviar(sociedad, documentos):
    """Arma un sobre con los documentos, lo sube al SII y registra el Track ID."""
    documentos = [d for d in documentos if d.es_propio and not d.envio_id]
    if not documentos:
        raise ErrorDTE("No hay documentos timbrados pendientes de envío.")
    firmante = firmante_de(sociedad)
    cert = sociedad.certificado
    xml = generar_envio(sociedad, [(d.tipo, d.xml_firmado) for d in documentos],
                        cert.rut_titular, firmante, timezone.localtime())
    validar(xml, "EnvioDTE_v10.xsd")
    track = sii.subir(sociedad.ambiente, firmante, cert.rut_titular, sociedad.rut, xml)
    with transaction.atomic():
        envio = Envio.objects.create(sociedad=sociedad, ambiente=sociedad.ambiente, xml=xml,
                                     track_id=track, estado="enviado")
        Documento.objects.filter(pk__in=[d.pk for d in documentos]).update(
            envio=envio, estado_sii="enviado")
    return envio


def consultar(envio):
    estado, _, detalle = sii.consultar(
        envio.ambiente, firmante_de(envio.sociedad), envio.sociedad.rut, envio.track_id)
    envio.estado, envio.detalle = estado, detalle
    envio.save(update_fields=["estado", "detalle"])
    envio.documentos.update(estado_sii=estado)
    if estado == "rechazado":   # un DTE rechazado no es venta: devuelve el stock
        from ..inventario import sincronizar_documento
        for doc in envio.documentos.all():
            sincronizar_documento(doc)
    return envio


def preparar_logo(contenido: bytes) -> bytes:
    """Recorta los márgenes en blanco y devuelve el logo como PNG de tamaño razonable."""
    try:
        imagen = Image.open(BytesIO(contenido)).convert("RGBA")
    except Exception:
        raise ErrorDTE("El archivo del logo no es una imagen válida (usa PNG o JPG).")
    fondo = Image.new("RGBA", imagen.size, (255, 255, 255, 255))
    plano = Image.alpha_composite(fondo, imagen).convert("RGB")
    caja = ImageChops.difference(plano, Image.new("RGB", plano.size, (255, 255, 255))).getbbox()
    if caja:
        plano = plano.crop(caja)
    plano.thumbnail((900, 400))
    salida = BytesIO()
    plano.save(salida, "PNG", optimize=True)
    return salida.getvalue()
