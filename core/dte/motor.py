"""Motor DTE: genera el XML del documento, lo timbra con el CAF y lo firma.

Todo ocurre en este computador: el certificado y los folios no salen de aquí.
Formato según el esquema oficial DTE_v10 / EnvioDTE_v10 del SII.
"""
import base64
import hashlib
import re
from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree

from ..utils import redondear, sin_ceros

NS = "http://www.sii.cl/SiiDte"
DSIG = "http://www.w3.org/2000/09/xmldsig#"
C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
DECLARACION = '<?xml version="1.0" encoding="ISO-8859-1"?>\n'
RUT_SII = "60803000-K"
ESQUEMAS = Path(__file__).parent / "esquemas"


class ErrorDTE(Exception):
    pass


# --- Escritura de XML ---------------------------------------------------------
def esc(valor):
    return escape(str(valor), {'"': "&quot;", "'": "&apos;"})


def tag(nombre, valor, largo=None):
    """<nombre>valor</nombre>, o nada si el valor está vacío."""
    if valor is None or valor == "":
        return ""
    texto = " ".join(str(valor).split())
    return f"<{nombre}>{esc(texto[:largo] if largo else texto)}</{nombre}>"


def bloque(nombre, *hijos, atributos=""):
    contenido = "".join(hijos)
    return f"<{nombre}{atributos}>{contenido}</{nombre}>" if contenido else ""


def a_bytes(texto):
    return texto.encode("latin-1", "xmlcharrefreplace")


def b64(datos):
    return base64.b64encode(datos).decode()


def _entero_b64(n):
    return b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


# --- Certificado y folios -----------------------------------------------------
class Firmante:
    """Certificado digital (.pfx) cargado en memoria."""

    def __init__(self, pfx: bytes, clave: str):
        try:
            self.llave, self.cert, _ = pkcs12.load_key_and_certificates(
                pfx, clave.encode() if clave else None)
        except Exception:
            raise ErrorDTE("No se pudo abrir el certificado: revisa el archivo y su clave.")
        if self.llave is None or self.cert is None:
            raise ErrorDTE("El archivo no contiene un certificado con su llave privada.")
        nombres = self.cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        self.titular = nombres[0].value if nombres else self.cert.subject.rfc4514_string()
        self.vence = self.cert.not_valid_after_utc.date()

    def firmar(self, datos: bytes) -> bytes:
        return self.llave.sign(datos, padding.PKCS1v15(), hashes.SHA1())

    def key_info(self):
        numeros = self.cert.public_key().public_numbers()
        der = self.cert.public_bytes(serialization.Encoding.DER)
        return (f"<KeyInfo><KeyValue><RSAKeyValue><Modulus>{_entero_b64(numeros.n)}</Modulus>"
                f"<Exponent>{_entero_b64(numeros.e)}</Exponent></RSAKeyValue></KeyValue>"
                f"<X509Data><X509Certificate>{b64(der)}</X509Certificate></X509Data></KeyInfo>")


class Folios:
    """Archivo CAF: rango de folios autorizado por el SII y su llave de timbraje."""

    def __init__(self, contenido: bytes):
        try:
            raiz = etree.fromstring(contenido)
            da = raiz.find("CAF/DA")
            self.rut = da.findtext("RE").strip().upper()
            self.tipo = int(da.findtext("TD"))
            self.desde = int(da.findtext("RNG/D"))
            self.hasta = int(da.findtext("RNG/H"))
            self.fecha = da.findtext("FA").strip()
            texto = contenido.decode(raiz.getroottree().docinfo.encoding or "latin-1")
            self.caf = re.sub(r">\s+<", "><", re.search(r"<CAF.*?</CAF>", texto, re.S).group(0))
            self.llave = serialization.load_pem_private_key(
                raiz.findtext("RSASK").strip().encode(), password=None)
            modulo = int.from_bytes(base64.b64decode(da.findtext("RSAPK/M")), "big")
        except Exception:
            raise ErrorDTE("El archivo no es un CAF válido del SII.")
        if self.llave.public_key().public_numbers().n != modulo:
            raise ErrorDTE("El CAF está dañado: su llave privada no corresponde a la pública.")

    def timbrar(self, datos: bytes) -> bytes:
        return self.llave.sign(datos, padding.PKCS1v15(), hashes.SHA1())


# --- Firma XML (XMLDSig, RSA-SHA1) --------------------------------------------
def c14n(nodo) -> bytes:
    """Forma canónica (C14N inclusivo) de un nodo, con los espacios de nombres heredados.

    No se usa etree.tostring(nodo, method="c14n") directamente sobre un nodo interior:
    en varias versiones de libxml2 agrega xmlns="" a los descendientes y la firma
    resultante no la puede verificar nadie más. Canonicalizar una copia cuya raíz es
    el propio nodo evita ese defecto.
    """
    copia = etree.fromstring(etree.tostring(nodo))
    prefijos = {k: v for k, v in nodo.nsmap.items() if k}
    etree.cleanup_namespaces(copia, top_nsmap=prefijos, keep_ns_prefixes=list(prefijos))
    return etree.tostring(copia, method="c14n")


def firmar_xml(xml: bytes, referencia: str, firmante: Firmante) -> bytes:
    """Agrega <Signature> como último hijo de la raíz, firmando el nodo con ID=referencia.

    Con referencia vacía firma el documento completo (firma envuelta).
    """
    raiz = etree.fromstring(xml)
    nodo = raiz.xpath("//*[@ID=$i]", i=referencia)[0] if referencia else raiz
    digest = b64(hashlib.sha1(c14n(nodo)).digest())
    firma = (
        f'<Signature xmlns="{DSIG}"><SignedInfo>'
        f'<CanonicalizationMethod Algorithm="{C14N}"/>'
        f'<SignatureMethod Algorithm="{DSIG}rsa-sha1"/>'
        f'<Reference URI="{"#" + referencia if referencia else ""}"><Transforms>'
        f'<Transform Algorithm="{C14N if referencia else ENVELOPED}"/></Transforms>'
        f'<DigestMethod Algorithm="{DSIG}sha1"/><DigestValue>{digest}</DigestValue>'
        f"</Reference></SignedInfo><SignatureValue>@@FIRMA@@</SignatureValue>"
        f"{firmante.key_info()}</Signature>").encode()
    corte = xml.rindex(b"</")
    con_firma = xml[:corte] + firma + xml[corte:]
    info = etree.fromstring(con_firma).find(f"{{{DSIG}}}Signature/{{{DSIG}}}SignedInfo")
    valor = firmante.firmar(c14n(info))
    return con_firma.replace(b"@@FIRMA@@", b64(valor).encode())


def validar(xml: bytes, esquema: str):
    """Valida contra el esquema oficial del SII. Lanza ErrorDTE con el detalle."""
    xsd = etree.XMLSchema(etree.parse(str(ESQUEMAS / esquema)))
    if not xsd.validate(etree.fromstring(xml)):
        detalle = "; ".join(sorted({e.message for e in xsd.error_log})[:3])
        raise ErrorDTE(f"El documento no cumple el formato del SII: {detalle}")


# --- Documento ----------------------------------------------------------------
def _detalle(numero, linea, marcar_exento):
    nombre = " ".join(linea.descripcion.split())
    bruto = linea.cantidad * linea.precio
    descuento = redondear(bruto) - linea.monto if linea.descuento else 0
    con_precio = linea.precio > 0
    return bloque(
        "Detalle",
        tag("NroLinDet", numero),
        tag("IndExe", 1 if linea.exento and marcar_exento else ""),
        tag("NmbItem", nombre, 80),
        tag("DscItem", nombre if len(nombre) > 80 else "", 1000),
        tag("QtyItem", sin_ceros(linea.cantidad) if con_precio else ""),
        tag("UnmdItem", linea.producto.unidad if linea.producto_id and con_precio else "", 4),
        tag("PrcItem", sin_ceros(linea.precio) if con_precio else ""),
        tag("DescuentoPct", sin_ceros(linea.descuento) if descuento > 0 else ""),
        tag("DescuentoMonto", descuento if descuento > 0 else ""),
        f"<MontoItem>{linea.monto}</MontoItem>")


def _referencias(doc):
    refs = []
    if doc.caso_set:  # exigido por el SII en los documentos del set de pruebas
        refs.append(tag("TpoDocRef", "SET") + tag("FolioRef", doc.folio)
                    + tag("FchRef", doc.fecha_emision.isoformat())
                    + tag("RazonRef", doc.caso_set, 90))
    if doc.referencia_id:
        ref = doc.referencia
        refs.append(tag("TpoDocRef", ref.tipo) + tag("FolioRef", ref.folio)
                    + tag("FchRef", ref.fecha_emision.isoformat())
                    + tag("CodRef", doc.codigo_referencia if doc.es_nota else "")
                    + tag("RazonRef", doc.razon_referencia, 90))
    return "".join(bloque("Referencia", tag("NroLinRef", n), r) for n, r in enumerate(refs, 1))


def generar_dte(doc, folios: Folios, firmante: Firmante, ahora) -> bytes:
    """XML del DTE timbrado y firmado. `doc` debe tener folio asignado."""
    from ..models import FACTURA_EXENTA  # evita importación circular
    soc, cli = doc.sociedad, doc.cliente
    lineas = list(doc.lineas.select_related("producto"))
    if not folios.desde <= doc.folio <= folios.hasta or folios.tipo != doc.tipo:
        raise ErrorDTE("El folio no pertenece al CAF indicado.")
    if folios.rut != soc.rut.upper():
        raise ErrorDTE("El CAF pertenece a otro RUT.")
    marca = ahora.strftime("%Y-%m-%dT%H:%M:%S")
    afecto = any(not l.exento for l in lineas) and doc.tipo != FACTURA_EXENTA

    encabezado = bloque(
        "Encabezado",
        bloque("IdDoc",
               tag("TipoDTE", doc.tipo), tag("Folio", doc.folio),
               tag("FchEmis", doc.fecha_emision.isoformat()),
               tag("IndTraslado", doc.ind_traslado if doc.es_guia else ""),
               tag("FchVenc", doc.fecha_vencimiento.isoformat()
                   if doc.es_factura and doc.fecha_vencimiento else "")),
        bloque("Emisor",
               tag("RUTEmisor", soc.rut), tag("RznSoc", soc.razon_social, 100),
               tag("GiroEmis", soc.giro, 80), tag("Telefono", soc.telefono, 20),
               tag("CorreoEmisor", soc.email, 80), tag("Acteco", soc.acteco),
               tag("DirOrigen", soc.direccion, 70), tag("CmnaOrigen", soc.comuna, 20),
               tag("CiudadOrigen", soc.ciudad, 20)),
        bloque("Receptor",
               tag("RUTRecep", cli.rut), tag("RznSocRecep", cli.razon_social, 100),
               tag("GiroRecep", cli.giro, 40),
               tag("Contacto", cli.contacto("facturacion")["nombre"], 80),
               tag("CorreoRecep", cli.contacto("facturacion")["email"], 80),
               tag("DirRecep", cli.direccion, 70),
               tag("CmnaRecep", cli.comuna, 20), tag("CiudadRecep", cli.ciudad, 20)),
        bloque("Transporte",
               tag("Patente", doc.patente, 8), tag("DirDest", doc.direccion_destino, 70),
               tag("CmnaDest", doc.comuna_destino, 20)) if doc.es_guia else "",
        bloque("Totales",
               tag("MntNeto", doc.neto if afecto else ""),
               tag("MntExe", doc.exento if doc.exento else ""),
               tag("TasaIVA", 19 if afecto else ""),
               tag("IVA", doc.iva if afecto else ""),
               f"<MntTotal>{doc.total}</MntTotal>"))

    dd = bloque(
        "DD",
        tag("RE", soc.rut), tag("TD", doc.tipo), tag("F", doc.folio),
        tag("FE", doc.fecha_emision.isoformat()), tag("RR", cli.rut),
        tag("RSR", cli.razon_social, 40), f"<MNT>{doc.total}</MNT>",
        tag("IT1", lineas[0].descripcion, 40), folios.caf, tag("TSTED", marca))
    ted = (f'<TED version="1.0">{dd}<FRMT algoritmo="SHA1withRSA">'
           f"{b64(folios.timbrar(a_bytes(dd)))}</FRMT></TED>")

    identificador = f"F{doc.folio}T{doc.tipo}"
    xml = (f'{DECLARACION}<DTE xmlns="{NS}" version="1.0"><Documento ID="{identificador}">'
           f"{encabezado}"
           f"{''.join(_detalle(n, l, afecto) for n, l in enumerate(lineas, 1))}"
           f"{_referencias(doc)}{ted}{tag('TmstFirma', marca)}</Documento></DTE>")
    return firmar_xml(a_bytes(xml), identificador, firmante)


def extraer_ted(xml: bytes) -> str:
    """Texto del timbre (TED) tal como va en el código de barras PDF417."""
    return re.search(rb"<TED .*?</TED>", xml, re.S).group(0).decode("latin-1")


# --- Sobre de envío -----------------------------------------------------------
def generar_envio(sociedad, dtes, rut_envia, firmante: Firmante, ahora) -> bytes:
    """EnvioDTE firmado con los DTE indicados (lista de pares tipo, xml)."""
    if not sociedad.resolucion_fecha:
        raise ErrorDTE("Falta la fecha de resolución del SII en los datos de la sociedad.")
    cuenta = Counter(tipo for tipo, _ in dtes)
    caratula = bloque(
        "Caratula",
        tag("RutEmisor", sociedad.rut), tag("RutEnvia", rut_envia), tag("RutReceptor", RUT_SII),
        tag("FchResol", sociedad.resolucion_fecha.isoformat()),
        f"<NroResol>{sociedad.resolucion_numero}</NroResol>",
        tag("TmstFirmaEnv", ahora.strftime("%Y-%m-%dT%H:%M:%S")),
        *(bloque("SubTotDTE", tag("TpoDTE", t), tag("NroDTE", n))
          for t, n in sorted(cuenta.items())),
        atributos=' version="1.0"')
    cabecera = a_bytes(
        f'{DECLARACION}<EnvioDTE xmlns="{NS}" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="{NS} EnvioDTE_v10.xsd" version="1.0">'
        f'<SetDTE ID="SetDoc">{caratula}')
    # Los DTE se insertan tal cual, sin reescribirlos, para no invalidar su firma.
    cuerpo = b"".join(bytes(x).split(b"?>", 1)[1].strip() for _, x in dtes)
    return firmar_xml(cabecera + cuerpo + b"</SetDTE></EnvioDTE>", "SetDoc", firmante)
