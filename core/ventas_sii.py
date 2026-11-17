"""Ventas: importación del Registro de Ventas (RCV) del SII.

Sirve para traer las facturas emitidas fuera de la app (por ejemplo, en el sistema
gratuito del SII). Un documento que ya existe en la plataforma —emitido aquí o
registrado antes— se reconoce por tipo y folio y NO se vuelve a crear.
"""
import csv
import io
import re
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction

from .compras import ErrorRCV, _clave, _decodificar, _fecha, _monto, datos_del_nombre
from .models import FACTURAS, NOTA_CREDITO, NOTAS, TIPOS, Cliente, Documento
from .utils import normalizar_rut

TIPOS_IMPORTABLES = {t for t, _ in TIPOS if t in FACTURAS or t in NOTAS}

COLUMNAS = {
    "tipo": ["tipo doc", "tipo documento", "tipo dte"],
    "rut": ["rut cliente", "rut receptor", "rut"],
    "razon": ["razon social", "razon social cliente"],
    "folio": ["folio"],
    "fecha": ["fecha docto", "fecha documento", "fecha emision"],
    "exento": ["monto exento"],
    "neto": ["monto neto"],
    "iva": ["monto iva", "monto iva recuperable"],
    "total": ["monto total"],
    "ref_tipo": ["tipo docto referencia", "tipo doc referencia"],
    "ref_folio": ["folio docto referencia", "folio doc referencia"],
}
OBLIGATORIAS = ["tipo", "rut", "folio", "fecha", "total"]


def leer(contenido):
    texto = _decodificar(contenido)
    lineas = [l for l in texto.splitlines() if l.strip()]
    if not lineas:
        raise ErrorRCV("El archivo está vacío.")
    separador = ";" if lineas[0].count(";") >= lineas[0].count(",") else ","
    lector = csv.reader(io.StringIO("\n".join(lineas)), delimiter=separador)
    encabezado = [_clave(c) for c in next(lector)]
    if "rut proveedor" in encabezado:
        raise ErrorRCV("Este archivo es del registro de COMPRAS. Súbelo en Compras → Importar desde el SII.")
    posicion = {}
    for campo, nombres in COLUMNAS.items():
        for n in nombres:
            if n in encabezado:
                posicion[campo] = encabezado.index(n)
                break
    faltan = [c for c in OBLIGATORIAS if c not in posicion]
    if faltan:
        raise ErrorRCV("El archivo no parece un detalle de ventas del RCV del SII "
                       "(faltan las columnas: " + ", ".join(faltan) + ").")
    filas = []
    for numero, fila in enumerate(lector, start=2):
        if not any(c.strip() for c in fila):
            continue
        valor = {campo: (fila[i].strip() if i < len(fila) else "") for campo, i in posicion.items()}
        valor["_linea"] = numero
        filas.append(valor)
    return filas


def _entero(texto):
    return int(re.sub(r"\D", "", texto or "") or 0)


def _cliente(sociedad, rut, razon, cache):
    if rut in cache:
        return cache[rut], False
    cliente = (sociedad.clientes.filter(rut=rut).order_by("-activo", "pk").first())
    creado = False
    if cliente is None:
        cliente = Cliente.objects.create(sociedad=sociedad, rut=rut, razon_social=(razon or rut)[:100])
        creado = True
    cache[rut] = cliente
    return cliente, creado


@transaction.atomic
def importar(sociedad, contenido, nombre_archivo=""):
    """Crea las ventas que no existen en la plataforma. Devuelve un resumen."""
    _, periodo_nombre = datos_del_nombre(nombre_archivo)
    r = {"nuevas": 0, "ya_existian": 0, "en_papelera": 0, "otros_tipos": 0, "clientes_nuevos": 0,
         "diferencias": [], "errores": [], "periodos": set()}
    clientes, creados, refs = {}, [], []
    vistos = set()
    for f in leer(contenido):
        try:
            tipo = _entero(f["tipo"])
            if tipo not in TIPOS_IMPORTABLES:
                r["otros_tipos"] += 1      # boletas, exportación, etc.: no se manejan en la app
                continue
            try:
                rut = normalizar_rut(f["rut"])
            except ValidationError:
                raise ValueError(f"RUT «{f['rut']}» no válido")
            folio = _entero(f["folio"])
            fecha = _fecha(f["fecha"])
            if not folio or not fecha:
                raise ValueError("falta el folio o la fecha")
            montos = {"exento": _monto(f.get("exento")), "neto": _monto(f.get("neto")),
                      "iva": _monto(f.get("iva")), "total": _monto(f["total"])}
        except (ValueError, TypeError) as e:
            r["errores"].append(f"Línea {f['_linea']}: {e}.")
            continue
        r["periodos"].add(periodo_nombre or f"{fecha:%Y-%m}")
        if (tipo, folio) in vistos:            # fila repetida dentro del mismo archivo
            continue
        vistos.add((tipo, folio))
        existente = Documento.todos.filter(sociedad=sociedad, tipo=tipo, folio=folio, es_prueba=False).first()
        if existente:
            if existente.eliminado_el:
                r["en_papelera"] += 1
            else:
                r["ya_existian"] += 1
            if existente.total != montos["total"]:
                r["diferencias"].append(
                    f"{existente}: en la plataforma suma {existente.total:,} y en el SII {montos['total']:,}."
                    .replace(",", "."))
            continue
        cliente, nuevo = _cliente(sociedad, rut, f.get("razon"), clientes)
        r["clientes_nuevos"] += nuevo
        doc = Documento.objects.create(
            sociedad=sociedad, cliente=cliente, tipo=tipo, folio=folio, estado=Documento.EMITIDO,
            estado_sii="portal", importado_sii=True, fecha_emision=fecha,
            fecha_vencimiento=fecha + timedelta(days=cliente.dias_pago) if tipo in FACTURAS else None,
            **montos)
        creados.append(doc)
        if tipo in NOTAS and _entero(f.get("ref_folio")):
            refs.append((doc, _entero(f.get("ref_tipo")), _entero(f.get("ref_folio"))))
        r["nuevas"] += 1
    for nota, ref_tipo, ref_folio in refs:   # notas de crédito/débito → la factura que corrigen
        filtro = {"tipo": ref_tipo} if ref_tipo else {"tipo__in": FACTURAS}
        original = Documento.objects.filter(sociedad=sociedad, folio=ref_folio, es_prueba=False,
                                            **filtro).first()
        if original:
            nota.referencia = original
            if nota.total == original.total and nota.tipo == NOTA_CREDITO:
                nota.codigo_referencia = 1
            nota.save(update_fields=["referencia", "codigo_referencia"])
    r["periodos"] = sorted(r["periodos"])
    return r
