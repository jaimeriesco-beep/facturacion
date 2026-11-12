"""Compras: importación del Registro de Compras y Ventas (RCV) del SII.

El RCV se descarga desde sii.cl → Servicios online → Factura electrónica → Registro de
compras y ventas → Compras → «Descargar detalles». Es un CSV separado por punto y coma
con una fila por documento. Como el SII cambia a veces el orden o el nombre de las
columnas, se buscan por nombre (sin tildes ni mayúsculas) y no por posición.
"""
import csv
import io
import re
import unicodedata
from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import TIPOS_COMPRA, Compra, Proveedor, categorias_de
from .utils import normalizar_rut

ESTADOS_ARCHIVO = {"REGISTRO": "registro", "PENDIENTE": "pendiente", "NO_INCLUIR": "no_incluir",
                   "RECLAMADO": "reclamado"}


class ErrorRCV(Exception):
    pass


def _clave(texto):
    texto = unicodedata.normalize("NFD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


COLUMNAS = {
    "tipo": ["tipo doc", "tipo documento", "tipo dte"],
    "tipo_compra": ["tipo compra"],
    "rut": ["rut proveedor", "rut emisor"],
    "razon": ["razon social", "razon social proveedor"],
    "folio": ["folio"],
    "fecha": ["fecha docto", "fecha documento", "fecha emision"],
    "recepcion": ["fecha recepcion"],
    "exento": ["monto exento"],
    "neto": ["monto neto"],
    "iva": ["monto iva recuperable", "monto iva"],
    "iva_no_rec": ["monto iva no recuperable"],
    "iva_comun": ["iva uso comun"],
    "sin_credito": ["impto sin derecho a credito"],
    "otro": ["valor otro impuesto"],
    "total": ["monto total"],
}
OBLIGATORIAS = ["tipo", "rut", "folio", "fecha", "total"]


def _decodificar(contenido):
    for codificacion in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return contenido.decode(codificacion)
        except UnicodeDecodeError:
            continue
    raise ErrorRCV("No se pudo leer el archivo.")


def _fecha(texto):
    texto = (texto or "").strip()
    if not texto:
        return None
    for formato in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                    "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise ValueError(f"fecha no válida «{texto}»")


def _monto(texto):
    texto = (texto or "").strip().replace("$", "").replace(" ", "")
    if not texto:
        return 0
    if "," in texto:                     # 1.234,56 (formato chileno)
        texto = texto.replace(".", "").replace(",", ".")
    elif texto.count(".") > 1 or re.fullmatch(r"-?\d{1,3}(\.\d{3})+", texto):
        texto = texto.replace(".", "")   # 1.234.567
    return abs(round(float(texto)))


def datos_del_nombre(nombre):
    """Del nombre de archivo del SII (RCV_COMPRA_REGISTRO_77552628-9_202611.csv) saca
    el estado y el período."""
    nombre = (nombre or "").upper()
    estado = next((v for k, v in ESTADOS_ARCHIVO.items() if f"_{k}_" in nombre or f"_{k}." in nombre), None)
    periodo = None
    if m := re.search(r"_(20\d{2})(0[1-9]|1[0-2])(?:\D|$)", nombre):
        periodo = f"{m.group(1)}-{m.group(2)}"
    return estado, periodo


def leer(contenido):
    """Filas del CSV como diccionarios con las claves de COLUMNAS."""
    texto = _decodificar(contenido)
    lineas = [l for l in texto.splitlines() if l.strip()]
    if not lineas:
        raise ErrorRCV("El archivo está vacío.")
    separador = ";" if lineas[0].count(";") >= lineas[0].count(",") else ","
    lector = csv.reader(io.StringIO("\n".join(lineas)), delimiter=separador)
    encabezado = [_clave(c) for c in next(lector)]
    posicion = {}
    for campo, nombres in COLUMNAS.items():
        for n in nombres:
            if n in encabezado:
                posicion[campo] = encabezado.index(n)
                break
    faltan = [c for c in OBLIGATORIAS if c not in posicion]
    if faltan:
        raise ErrorRCV("El archivo no parece un detalle de compras del RCV del SII "
                       "(faltan las columnas: " + ", ".join(faltan) + ").")
    filas = []
    for numero, fila in enumerate(lector, start=2):
        if not any(c.strip() for c in fila):
            continue
        valor = {campo: (fila[i].strip() if i < len(fila) else "") for campo, i in posicion.items()}
        valor["_linea"] = numero
        filas.append(valor)
    return filas


@transaction.atomic
def importar(sociedad, contenido, nombre_archivo="", estado=None, periodo=None):
    """Crea o actualiza compras desde un CSV del RCV. Devuelve un resumen."""
    estado_nombre, periodo_nombre = datos_del_nombre(nombre_archivo)
    estado = estado or estado_nombre or "registro"
    periodo = periodo or periodo_nombre
    categorias_de(sociedad)
    resumen = {"nuevas": 0, "actualizadas": 0, "sin_cambios": 0, "proveedores_nuevos": 0,
               "errores": [], "periodos": set()}
    for f in leer(contenido):
        try:
            tipo = int(re.sub(r"\D", "", f["tipo"]) or 0)
            if tipo not in TIPOS_COMPRA:
                raise ValueError(f"tipo de documento {f['tipo']} no reconocido")
            try:
                rut = normalizar_rut(f["rut"])
            except ValidationError:
                raise ValueError(f"RUT «{f['rut']}» no válido")
            folio = int(re.sub(r"\D", "", f["folio"]) or 0)
            fecha = _fecha(f["fecha"])
            if not folio or not fecha:
                raise ValueError("falta el folio o la fecha")
            recepcion = _fecha(f.get("recepcion", ""))
            montos = {
                "exento": _monto(f.get("exento")), "neto": _monto(f.get("neto")),
                "iva": _monto(f.get("iva")) + _monto(f.get("iva_comun")),
                "iva_no_recuperable": _monto(f.get("iva_no_rec")),
                "otros_impuestos": _monto(f.get("otro")) + _monto(f.get("sin_credito")),
                "total": _monto(f["total"]),
            }
        except (ValueError, TypeError) as e:
            resumen["errores"].append(f"Línea {f['_linea']}: {e}.")
            continue
        per = periodo or f"{(recepcion or fecha):%Y-%m}"
        resumen["periodos"].add(per)
        proveedor, creado = Proveedor.objects.get_or_create(
            sociedad=sociedad, rut=rut, defaults={"razon_social": (f.get("razon") or rut)[:100]})
        if creado:
            resumen["proveedores_nuevos"] += 1
        compra = Compra.objects.filter(sociedad=sociedad, proveedor=proveedor, tipo=tipo, folio=folio).first()
        campos = dict(fecha_emision=fecha, fecha_recepcion=recepcion, periodo=per, estado_rcv=estado,
                      tipo_compra=(f.get("tipo_compra") or "")[:40], **montos)
        if compra is None:
            Compra.objects.create(
                sociedad=sociedad, proveedor=proveedor, tipo=tipo, folio=folio, origen="rcv",
                categoria=proveedor.categoria,
                fecha_vencimiento=fecha + timedelta(days=proveedor.dias_pago), **campos)
            resumen["nuevas"] += 1
        else:
            cambios = {k: v for k, v in campos.items() if getattr(compra, k) != v}
            if cambios:
                for k, v in cambios.items():
                    setattr(compra, k, v)
                compra.save(update_fields=list(cambios))
                resumen["actualizadas"] += 1
            else:
                resumen["sin_cambios"] += 1
    resumen["periodos"] = sorted(resumen["periodos"])
    return resumen
