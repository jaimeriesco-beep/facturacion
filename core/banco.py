"""Banco: lectura de cartolas, reglas de categorías y sugerencias de conciliación.

Formato principal: la cartola que el Banco de Chile entrega como «Excel» (.xls), que en
realidad es texto separado por punto y coma:

    NOMBRE DEL TITULAR      (096724880-0) cta:001760963808
    Fecha;Detalle Movimiento;Cheque o Cargo;Deposito o Abono;Saldo;Docto. Nro.;Trn;Caja;Sucursal
    01/10/2026;TRASPASO A:...;+0005000000;00000000000;+0000003885495;...

Para otros bancos se busca una fila de encabezado con «fecha» y columnas de cargo/abono
o monto, en CSV o en Excel (.xlsx).
"""
import csv
import hashlib
import io
import re
import unicodedata
from datetime import date, datetime

from django.db import transaction
from django.db.models import Q

from .models import (COBRO_CLIENTES, PAGO_PROVEEDORES, CategoriaBanco, Compra, Conciliacion,
                     CuentaBancaria, Documento, MovimientoBancario, Pago, PagoCompra,
                     categorias_banco_de)


class ErrorCartola(Exception):
    pass


def _clave(texto):
    texto = unicodedata.normalize("NFD", str(texto or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def _monto(valor):
    """'+0005000000' -> 5000000 ; '1.234.567' -> 1234567 ; '-15.000' -> -15000 ; 1234.0 -> 1234."""
    if valor is None:
        return 0
    if isinstance(valor, (int, float)):
        return round(valor)
    texto = str(valor).strip().replace("$", "").replace(" ", "")
    if not texto:
        return 0
    negativo = texto.startswith("-") or (texto.startswith("(") and texto.endswith(")"))
    texto = texto.strip("+-()")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    else:
        texto = texto.replace(".", "")
    numero = round(float(texto)) if texto else 0
    return -numero if negativo else numero


def _fecha(valor):
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor or "").strip()
    for formato in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    return None


COLUMNAS = {
    "fecha": ["fecha", "fecha movimiento", "fecha contable", "fecha operacion", "fecha transaccion",
              "fecha mov", "fecha de movimiento"],
    "descripcion": ["detalle movimiento", "descripcion", "detalle", "glosa", "descripcion movimiento",
                    "descripcion del movimiento", "concepto", "movimiento"],
    "cargo": ["cheque o cargo", "cargo", "cargos", "giros", "debito", "debitos", "monto cargo",
              "cargos y giros", "monto cargos", "cargo"],
    "abono": ["deposito o abono", "abono", "abonos", "depositos", "credito", "creditos", "monto abono",
              "depositos y abonos", "monto abonos"],
    "monto": ["monto", "importe", "monto movimiento"],
    "tipo": ["cargo abono", "tipo", "tipo movimiento", "c a", "d c"],
    "saldo": ["saldo", "saldo diario", "saldo contable", "saldo disponible"],
    "documento": ["docto nro", "n documento", "documento", "nro documento", "numero documento",
                  "n operacion", "nro operacion", "numero operacion", "n de documento"],
    "sucursal": ["sucursal", "oficina", "canal"],
}
# Para encabezados con agregados («Monto cargo ($)», «Fecha operación (dd/mm)»): se aceptan
# también los que EMPIEZAN con alguno de estos nombres.
PREFIJOS = {"fecha": ["fecha"], "descripcion": ["descripcion", "detalle", "glosa"],
            "cargo": ["cargo", "monto cargo", "debito", "giro"], "abono": ["abono", "monto abono", "deposito", "credito"],
            "saldo": ["saldo"]}


def _posiciones(encabezado):
    claves = [_clave(c) for c in encabezado]
    pos = {}
    for campo, nombres in COLUMNAS.items():
        for n in nombres:
            if n in claves and claves.index(n) not in pos.values():
                pos[campo] = claves.index(n)
                break
    for campo, prefijos in PREFIJOS.items():
        if campo in pos:
            continue
        for i, c in enumerate(claves):
            if i not in pos.values() and any(c.startswith(p) for p in prefijos):
                pos[campo] = i
                break
    return pos


def _con_signo(monto, tipo, encabezado):
    """Aplica el signo según una columna de tipo: «Cargo»/«Débito»/«Giro» salen; «Abono»/«Crédito»/«Depósito»
    entran. Una «C» suelta es «cargo», salvo que el encabezado sea «D/C» (débito/crédito)."""
    t = _clave(tipo)
    if not t:
        return monto
    if t.startswith(("cargo", "debito", "giro")) or t == "d" or (t == "c" and encabezado != "d c"):
        return -abs(monto)
    if t.startswith(("abono", "credito", "deposito")) or t == "a" or (t == "c" and encabezado == "d c"):
        return abs(monto)
    return monto


def _filas_texto(contenido):
    for codificacion in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            texto = contenido.decode(codificacion)
            break
        except UnicodeDecodeError:
            continue
    lineas = [l for l in texto.splitlines() if l.strip()]
    separador = ";" if sum(l.count(";") for l in lineas[:5]) >= sum(l.count(",") for l in lineas[:5]) else ","
    return lineas, [next(csv.reader([l], delimiter=separador)) for l in lineas]


def _filas_excel(contenido):
    from openpyxl import load_workbook
    libro = load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
    hoja = libro.active
    filas = [[c if c is not None else "" for c in fila] for fila in hoja.iter_rows(values_only=True)]
    return [" ".join(str(c) for c in f) for f in filas], filas


def _filas_xls(contenido):
    """Excel antiguo (.xls binario), habitual en descargas de bancos como Santander."""
    import xlrd
    try:
        libro = xlrd.open_workbook(file_contents=contenido)
    except Exception:
        raise ErrorCartola("No pude abrir el Excel (.xls). Ábrelo y guárdalo como .xlsx, o descarga la cartola en otro formato.")
    hoja = libro.sheet_by_index(0)
    filas = []
    for r in range(hoja.nrows):
        fila = []
        for c in range(hoja.ncols):
            celda = hoja.cell(r, c)
            if celda.ctype == xlrd.XL_CELL_DATE:
                fila.append(datetime(*xlrd.xldate_as_tuple(celda.value, libro.datemode)))
            elif celda.ctype == xlrd.XL_CELL_EMPTY:
                fila.append("")
            else:
                fila.append(celda.value)
        filas.append(fila)
    return [" ".join(str(c) for c in f) for f in filas], filas


def leer(contenido, nombre=""):
    """Devuelve {'titular', 'rut', 'cuenta', 'banco', 'movimientos': [...]}."""
    if contenido[:4] == b"PK\x03\x04":                     # .xlsx
        lineas, filas = _filas_excel(contenido)
    elif contenido[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # .xls binario antiguo
        lineas, filas = _filas_xls(contenido)
    elif contenido[:5] == b"%PDF-":
        raise ErrorCartola("Los PDF no se pueden leer con precisión. Descarga la cartola en formato Excel o TXT.")
    else:
        lineas, filas = _filas_texto(contenido)
    datos = {"titular": "", "rut": "", "cuenta": "", "banco": "", "movimientos": []}
    # Encabezado del Banco de Chile: «TITULAR (096724880-0) cta:001760963808»
    for linea in lineas[:5]:
        if m := re.search(r"cta\s*[:.]?\s*([0-9-]{6,})", linea, re.I):
            datos["cuenta"] = m.group(1).replace("-", "").lstrip("0") or m.group(1)
            datos["banco"] = "Banco de Chile"
            if r := re.search(r"\((\d{6,9})-?([\dkK])\)", linea):
                datos["rut"] = f"{int(r.group(1))}-{r.group(2).upper()}"
            datos["titular"] = re.split(r"\s{2,}|\(", linea)[0].strip()
            break
    # Otros bancos (Santander, etc.): banco, cuenta y RUT en las primeras líneas, si vienen.
    cabecera = " ".join(lineas[:15])
    if not datos["banco"]:
        for nombre_banco, clave in (("Banco Santander", "santander"), ("BCI", "bci"), ("Itaú", "itau"),
                                    ("BancoEstado", "bancoestado"), ("Scotiabank", "scotiabank"),
                                    ("Banco Security", "security"), ("Banco BICE", "bice")):
            if clave in _clave(cabecera).replace(" ", ""):
                datos["banco"] = nombre_banco
                break
    if not datos["cuenta"]:
        if m := re.search(r"(?:cuenta|cta)[^0-9\n]{0,30}?([0-9][0-9-]{5,})", cabecera, re.I):
            datos["cuenta"] = m.group(1).replace("-", "").lstrip("0") or m.group(1)
    if not datos["rut"]:
        if r := re.search(r"\b(\d{1,2}\.?\d{3}\.?\d{3})-([\dkK])\b", cabecera):
            datos["rut"] = f"{int(r.group(1).replace('.', ''))}-{r.group(2).upper()}"
    inicio = None
    for i, fila in enumerate(filas[:40]):
        pos = _posiciones(fila)
        if "fecha" in pos and ("monto" in pos or ("cargo" in pos and "abono" in pos)):
            inicio = i
            break
    if inicio is None:
        raise ErrorCartola("No encontré las columnas de la cartola (fecha, cargo, abono). "
                           "¿Es la cartola del banco en formato Excel o TXT?")
    encabezado_tipo = _clave(filas[inicio][pos["tipo"]]) if "tipo" in pos else ""
    vistos = {}
    for numero, fila in enumerate(filas[inicio + 1:], start=1):
        valor = lambda campo: fila[pos[campo]] if campo in pos and pos[campo] < len(fila) else ""  # noqa: E731
        fecha = _fecha(valor("fecha"))
        if not fecha:
            continue                                    # totales, líneas en blanco, notas
        if "cargo" in pos and "abono" in pos:
            monto = abs(_monto(valor("abono"))) - abs(_monto(valor("cargo")))
        else:
            monto = _monto(valor("monto"))
            if "tipo" in pos:
                monto = _con_signo(monto, valor("tipo"), encabezado_tipo)
        if not monto:
            continue
        descripcion = re.sub(r"\s+", " ", str(valor("descripcion"))).strip()[:200] or "(sin descripción)"
        saldo = _monto(valor("saldo")) if "saldo" in pos and str(valor("saldo")).strip() else None
        documento = str(valor("documento")).strip().lstrip("0")[:30]
        clave = (fecha, descripcion.lower(), monto, saldo, documento)
        vistos[clave] = vistos.get(clave, 0) + 1
        huella = hashlib.sha256(repr(clave + (vistos[clave],)).encode()).hexdigest()
        datos["movimientos"].append({
            "fecha": fecha, "orden": numero, "descripcion": descripcion, "monto": monto, "saldo": saldo,
            "documento": documento, "sucursal": str(valor("sucursal")).strip()[:40], "huella": huella})
    if not datos["movimientos"]:
        raise ErrorCartola("La cartola no tiene movimientos.")
    movs = datos["movimientos"]
    if movs[0]["fecha"] > movs[-1]["fecha"]:
        # Cartola del más reciente al más antiguo (habitual en Santander): se invierte el orden
        # dentro de cada día para que el saldo se lea en secuencia.
        total = len(movs)
        for m in movs:
            m["orden"] = total + 1 - m["orden"]
    return datos


def aplicar_reglas(sociedad, movimientos):
    reglas = list(sociedad.reglas_banco.select_related("categoria"))
    n = 0
    for m in movimientos:
        if m.categoria_id or m.conciliaciones.exists():
            continue
        for regla in reglas:
            if regla.aplica(m):
                m.categoria = regla.categoria
                m.save(update_fields=["categoria"])
                n += 1
                break
    return n


@transaction.atomic
def importar(sociedad, contenido, nombre="", cuenta=None, aceptar_otro_rut=False):
    datos = leer(contenido, nombre)
    if datos["rut"] and sociedad.rut and datos["rut"] != sociedad.rut and not aceptar_otro_rut:
        raise ErrorCartola(f"La cartola es de {datos['titular'] or 'otro titular'} (RUT {datos['rut']}), "
                           f"no de {sociedad} (RUT {sociedad.rut}). Revisa la sociedad elegida.")
    if cuenta is None:
        if not datos["cuenta"]:
            raise ErrorCartola("La cartola no indica el número de cuenta: elige la cuenta antes de importar.")
        cuenta, _ = CuentaBancaria.objects.get_or_create(
            sociedad=sociedad, banco=datos["banco"] or "Otro", numero=datos["cuenta"])
    categorias_banco_de(sociedad)
    existentes = set(cuenta.movimientos.values_list("huella", flat=True))
    nuevos = [MovimientoBancario(cuenta=cuenta, **m) for m in datos["movimientos"] if m["huella"] not in existentes]
    MovimientoBancario.objects.bulk_create(nuevos)
    creados = list(cuenta.movimientos.filter(huella__in=[m.huella for m in nuevos]))
    reglas = aplicar_reglas(sociedad, creados)
    fechas = [m["fecha"] for m in datos["movimientos"]]
    # Los saldos de la cartola deben encadenarse: si no, faltan movimientos en el archivo.
    descuadres, saldo = 0, None
    for m in sorted(datos["movimientos"], key=lambda x: (x["fecha"], x["orden"])):
        if m["saldo"] is None:
            continue
        if saldo is not None and saldo + m["monto"] != m["saldo"]:
            descuadres += 1
        saldo = m["saldo"]
    return {"descuadres": descuadres,"cuenta": cuenta, "nuevos": len(nuevos), "repetidos": len(datos["movimientos"]) - len(nuevos),
            "categorizados": reglas, "desde": min(fechas), "hasta": max(fechas)}


# ----------------------------------------------------------- Conciliación

def _palabras(texto):
    vacias = {"de", "la", "el", "los", "las", "y", "spa", "ltda", "limitada", "sa", "s", "a", "eirl",
              "sociedad", "comercial", "traspaso", "transferencia", "transf", "pago", "abono", "cargo", "dep"}
    return {p for p in _clave(texto).split() if len(p) > 2 and p not in vacias}


def sugerencias(movimiento, limite=6):
    """Documentos que probablemente corresponden al movimiento, con un puntaje."""
    soc = movimiento.cuenta.sociedad
    monto = movimiento.por_conciliar
    palabras = _palabras(movimiento.descripcion)
    candidatos = []
    if movimiento.es_abono:
        for d in soc.documentos.por_cobrar().select_related("cliente"):
            nombre = f"{d.cliente.razon_social} {d.cliente.nombre_fantasia}"
            candidatos.append((d, d.saldo, nombre, d.fecha_emision))
    else:
        for c in soc.compras.con_saldo().select_related("proveedor"):
            if c.saldo > 0:
                nombre = f"{c.proveedor.razon_social} {c.proveedor.nombre_fantasia}"
                candidatos.append((c, c.saldo, nombre, c.fecha_emision))
    resultado = []
    for doc, saldo, nombre, fecha in candidatos:
        puntos = 0
        if saldo == monto:
            puntos += 60
        elif 0 < saldo < monto:
            puntos += 10
        coincide = palabras & _palabras(nombre)
        if coincide:
            puntos += 25 + 5 * min(len(coincide), 3)
        if fecha and fecha <= movimiento.fecha:
            puntos += 5
        if puntos >= 30:
            resultado.append({"doc": doc, "saldo": saldo, "puntos": puntos, "exacto": saldo == monto,
                              "nombre": bool(coincide)})
    resultado.sort(key=lambda r: (-r["puntos"], r["doc"].fecha_emision))
    return resultado[:limite]


@transaction.atomic
def conciliar(movimiento, asignaciones):
    """asignaciones: [(documento_o_compra, monto)]. Crea los pagos y los une al movimiento."""
    restante = movimiento.por_conciliar
    hechos = 0
    for doc, monto in asignaciones:
        monto = min(monto, restante, doc.saldo)
        if monto <= 0:
            continue
        if isinstance(doc, Documento):
            pago = Pago.objects.create(documento=doc, fecha=movimiento.fecha, monto=monto, medio="transferencia",
                                       nota=f"Banco: {movimiento.descripcion}"[:120])
            Conciliacion.objects.create(movimiento=movimiento, pago=pago, monto=monto)
        else:
            pago = PagoCompra.objects.create(compra=doc, fecha=movimiento.fecha, monto=monto, medio="transferencia",
                                             nota=f"Banco: {movimiento.descripcion}"[:120])
            Conciliacion.objects.create(movimiento=movimiento, pago_compra=pago, monto=monto)
        restante -= monto
        hechos += 1
    if hechos and not movimiento.categoria_id:
        nombre = COBRO_CLIENTES if movimiento.es_abono else PAGO_PROVEEDORES
        movimiento.categoria = CategoriaBanco.objects.filter(
            sociedad=movimiento.cuenta.sociedad, nombre=nombre).first()
        movimiento.save(update_fields=["categoria"])
    return hechos


@transaction.atomic
def deshacer(movimiento):
    for c in movimiento.conciliaciones.select_related("pago", "pago_compra"):
        if c.creado_pago:
            (c.pago or c.pago_compra).delete()          # borra también la conciliación
        else:
            c.delete()
    if movimiento.categoria and movimiento.categoria.nombre in (COBRO_CLIENTES, PAGO_PROVEEDORES):
        movimiento.categoria = None
        movimiento.save(update_fields=["categoria"])
