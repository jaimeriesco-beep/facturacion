"""Comunicación con el SII: autenticación, envío de documentos y consulta de estado.

Ambiente de certificación: maullin.sii.cl — Producción: palena.sii.cl
"""
import html
import re

import requests

from .motor import ErrorDTE, Firmante, firmar_xml

HOSTS = {"certificacion": "maullin.sii.cl", "produccion": "palena.sii.cl"}
AGENTE = "Mozilla/4.0 (compatible; PROG 1.0; Windows NT 5.0; YComp 5.0.2.4)"
ESPERA = 40
ERRORES_ENVIO = {
    "1": "El RUT que envía no tiene permiso para enviar por esta sociedad.",
    "2": "Error en el tamaño del archivo.",
    "3": "El archivo llegó cortado.",
    "5": "No está autenticado.",
    "6": "La sociedad no está autorizada a enviar archivos.",
    "7": "El esquema del archivo es inválido.",
    "8": "La firma del documento es inválida.",
    "9": "El sistema del SII está bloqueado.",
}
EN_PROCESO = {"REC", "SOK", "FOK", "PRD", "CRT", "PDR", "-11"}


def _soap(ambiente, servicio, metodo, **parametros):
    """Llama a un servicio web del SII y devuelve el texto de la respuesta."""
    cuerpo = "".join(f'<{n} xsi:type="xsd:string">{html.escape(str(v))}</{n}>'
                     for n, v in parametros.items())
    sobre = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:def="http://DefaultNamespace"><soapenv:Body>'
        f'<def:{metodo} soapenv:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"{cuerpo}</def:{metodo}></soapenv:Body></soapenv:Envelope>")
    try:
        r = requests.post(
            f"https://{HOSTS[ambiente]}/DTEWS/{servicio}.jws", data=sobre.encode(),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""},
            timeout=ESPERA)
        r.raise_for_status()
    except requests.RequestException as e:
        raise ErrorDTE(f"No se pudo conectar con el SII ({servicio}): {e}")
    return html.unescape(r.text)


def _dato(texto, etiqueta):
    m = re.search(rf"<(?:\w+:)?{etiqueta}>\s*([^<]*?)\s*</(?:\w+:)?{etiqueta}>", texto)
    return m.group(1) if m else ""


def obtener_token(ambiente, firmante: Firmante) -> str:
    semilla = _dato(_soap(ambiente, "CrSeed", "getSeed"), "SEMILLA")
    if not semilla:
        raise ErrorDTE("El SII no entregó la semilla de autenticación.")
    solicitud = firmar_xml(
        f"<getToken><item><Semilla>{semilla}</Semilla></item></getToken>".encode(), "", firmante)
    respuesta = _soap(ambiente, "GetTokenFromSeed", "getToken", pszXml=solicitud.decode())
    token = _dato(respuesta, "TOKEN")
    if not token:
        raise ErrorDTE("El SII rechazó la autenticación con el certificado: "
                       + (_dato(respuesta, "GLOSA") or "sin detalle"))
    return token


def _partes(rut):
    numero, dv = rut.split("-")
    return numero, dv


def subir(ambiente, firmante, rut_envia, rut_empresa, xml: bytes, nombre="envio.xml") -> str:
    """Sube un EnvioDTE y devuelve el Track ID."""
    token = obtener_token(ambiente, firmante)
    (rs, ds), (rc, dc) = _partes(rut_envia), _partes(rut_empresa)
    try:
        r = requests.post(
            f"https://{HOSTS[ambiente]}/cgi_dte/UPL/DTEUpload",
            data={"rutSender": rs, "dvSender": ds, "rutCompany": rc, "dvCompany": dc},
            files={"archivo": (nombre, xml, "text/xml")},
            headers={"User-Agent": AGENTE, "Cookie": f"TOKEN={token}"}, timeout=ESPERA)
        r.raise_for_status()
    except requests.RequestException as e:
        raise ErrorDTE(f"No se pudo subir el envío al SII: {e}")
    estado, track = _dato(r.text, "STATUS"), _dato(r.text, "TRACKID")
    if estado != "0" or not track:
        raise ErrorDTE("El SII no recibió el envío: "
                       + ERRORES_ENVIO.get(estado, f"respuesta inesperada ({estado or 'vacía'})."))
    return track


def consultar(ambiente, firmante, rut_empresa, track_id):
    """Estado de un envío. Devuelve (estado_interno, código SII, detalle legible)."""
    token = obtener_token(ambiente, firmante)
    rc, dc = _partes(rut_empresa)
    r = _soap(ambiente, "QueryEstUp", "getEstUp",
              RutCompania=rc, DvCompania=dc, TrackId=track_id, Token=token)
    codigo, glosa = _dato(r, "ESTADO"), _dato(r, "GLOSA")
    aceptados, rechazados, reparos = (int(_dato(r, e) or 0)
                                      for e in ("ACEPTADOS", "RECHAZADOS", "REPAROS"))
    detalle = f"{codigo} {glosa}".strip()
    if codigo == "EPR":
        detalle += f" · aceptados {aceptados}, con reparos {reparos}, rechazados {rechazados}"
        estado = "rechazado" if rechazados else "reparo" if reparos else "aceptado"
    elif codigo in EN_PROCESO or not codigo:
        estado = "enviado"
    else:
        estado = "rechazado"
    return estado, codigo, detalle
