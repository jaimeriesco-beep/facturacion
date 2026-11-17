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
from django.db.models import F, Q

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
        return leer_pdf(contenido)
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


# ------------------------------------------------------------ Cartolas en PDF

_RE_FECHA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_RE_MONTO = re.compile(r"^-?[\d.]+(,\d+)?$")


def leer_pdf(contenido):
    """Cartola histórica en PDF de Santander (Office Banking / Banca Privada).

    Es un PDF con texto real y columnas fijas: FECHA, CARGO, ABONO, DESCRIPCIÓN (en una o
    varias líneas, centradas en torno a la fecha), SALDO, N° DOC y SUCURSAL. En ese PDF la
    columna SALDO trae el saldo ANTERIOR al movimiento, así que los saldos se recalculan
    desde el «Saldo inicial» y se comprueba que lleguen al «Saldo final».
    """
    try:
        import pdfplumber
        pdf = pdfplumber.open(io.BytesIO(contenido))
    except Exception:
        raise ErrorCartola("No pude abrir el PDF.")
    texto = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if not texto.strip():
        raise ErrorCartola("El PDF no tiene texto (parece escaneado). Descarga la cartola en Excel o en el PDF del banco.")
    if re.search(r"Consulta de movimientos", texto, re.I):
        return _leer_pdf_ultimos(pdf, texto)
    buscar = lambda patron: (re.search(patron, texto, re.I | re.M) or [None, ""])[1]  # noqa: E731
    inicial, final = buscar(r"Saldo inicial:\s*\$\s*([\d.]+)"), buscar(r"Saldo final:\s*\$\s*([\d.]+)")
    if not inicial or "FECHA" not in texto.upper():
        raise ErrorCartola("Este PDF no tiene el formato de cartola que la app reconoce (Santander). "
                           "Descarga la cartola en Excel o TXT, o mándame un ejemplo para agregar el formato.")
    datos = {"titular": (buscar(r"Empresa:\s*(.+?)\s*(?:RUT empresa|$)") or "").strip(),
             "rut": "", "cuenta": "", "banco": "Banco Santander", "movimientos": [],
             "saldo_inicial": _monto(inicial), "saldo_final": _monto(final) if final else None}
    if r := re.search(r"RUT empresa:\s*([\d.]+)-([\dkK])", texto, re.I):
        datos["rut"] = f"{int(r.group(1).replace('.', ''))}-{r.group(2).upper()}"
    if c := buscar(r"Cuenta:\s*([\d-]{6,})"):
        datos["cuenta"] = c.replace("-", "").lstrip("0") or c

    secciones = {"detalle": [], "comisiones": []}
    for pagina in pdf.pages:
        palabras = pagina.extract_words()
        # Límites verticales de cada sección según sus títulos.
        marcas = []
        for i, w in enumerate(palabras):
            siguiente = palabras[i + 1]["text"].lower() if i + 1 < len(palabras) else ""
            t = w["text"].lower()
            if t == "detalle" and siguiente.startswith("movimiento"):
                marcas.append(("detalle", w["top"]))
            elif t == "resumen" and siguiente.startswith("comision"):
                marcas.append(("comisiones", w["top"]))
            elif (t == "saldos" and siguiente.startswith("diario")) or t == "nota:":
                marcas.append(("fin", w["top"]))
        encabezados = [w for w in palabras if w["text"] == "FECHA"]
        for enc in encabezados:
            seccion = None
            for nombre, top in marcas:
                if top < enc["top"]:
                    seccion = nombre
            if seccion not in secciones:
                continue
            limite = min([top for _, top in marcas if top > enc["top"]] + [pagina.height])
            fila_enc = [w for w in palabras if abs(w["top"] - enc["top"]) < 3]
            col = {w["text"].upper(): w for w in fila_enc}
            if not {"CARGO", "ABONO", "SALDO"} <= set(col):
                continue
            x_desc = next((w["x0"] for w in fila_enc if w["text"].upper().startswith("DESCRIP")), None)
            x_doc = next((w["x0"] for w in fila_enc if w["text"].upper() == "N°"), col["SALDO"]["x1"] + 5)
            x_suc = next((w["x0"] for w in fila_enc if w["text"].upper().startswith("SUCURSAL")), x_doc + 40)
            cuerpo = [w for w in palabras if enc["top"] + 3 < w["top"] < limite - 1]
            anclas = [w for w in cuerpo if _RE_FECHA.match(w["text"]) and w["x0"] < col["CARGO"]["x0"]]
            filas = [{"fecha": _fecha(a["text"]), "top": a["top"], "cargo": 0, "abono": 0,
                      "desc": [], "doc": [], "suc": []} for a in anclas]
            if not filas:
                continue

            def cercana(w):
                return min(filas, key=lambda f: abs(f["top"] - w["top"]))
            for w in cuerpo:
                if w in anclas or w["text"] == "$":
                    continue
                x0, x1 = w["x0"], w["x1"]
                if _RE_MONTO.match(w["text"]) and x0 < (x_desc or col["ABONO"]["x1"] + 5):
                    destino = "cargo" if abs(x1 - col["CARGO"]["x1"]) < abs(x1 - col["ABONO"]["x1"]) else "abono"
                    cercana(w)[destino] = _monto(w["text"])
                elif x_desc and x_desc - 4 <= x0 < col["SALDO"]["x0"] - 15:
                    cercana(w)["desc"].append(w)
                elif x_doc - 4 <= x0 < x_suc - 4:
                    cercana(w)["doc"].append(w)
                elif x0 >= x_suc - 4:
                    cercana(w)["suc"].append(w)
            for f in filas:
                unir = lambda ws: " ".join(w["text"] for w in sorted(ws, key=lambda w: (round(w["top"]), w["x0"])))  # noqa: E731
                monto = f["abono"] - f["cargo"]
                if monto:
                    secciones[seccion].append({"fecha": f["fecha"], "monto": monto,
                                               "descripcion": unir(f["desc"])[:200] or "(sin descripción)",
                                               "documento": unir(f["doc"]).lstrip("0")[:30],
                                               "sucursal": unir(f["suc"])[:40]})
    movimientos = secciones["detalle"]
    if datos["saldo_final"] is not None:
        suma = datos["saldo_inicial"] + sum(m["monto"] for m in movimientos)
        con_comisiones = suma + sum(m["monto"] for m in secciones["comisiones"])
        if suma != datos["saldo_final"] and con_comisiones == datos["saldo_final"]:
            movimientos = movimientos + secciones["comisiones"]
    movimientos.sort(key=lambda m: m["fecha"])
    if not movimientos:
        raise ErrorCartola("La cartola no tiene movimientos.")
    saldo = datos["saldo_inicial"]
    vistos = {}
    for orden, m in enumerate(movimientos, start=1):
        saldo += m["monto"]
        m.update(orden=orden, saldo=saldo)
        clave = (m["fecha"], m["descripcion"].lower(), m["monto"], m["documento"])
        vistos[clave] = vistos.get(clave, 0) + 1
        m["huella"] = hashlib.sha256(repr(clave + (vistos[clave],)).encode()).hexdigest()
    datos["movimientos"] = movimientos
    return datos


def _leer_pdf_ultimos(pdf, texto):
    """«Consulta de movimientos de Cuentas Corrientes» de Santander (últimos movimientos).

    Columnas: MONTO (con signo), DESCRIPCIÓN (varias líneas en torno a la fila), FECHA, SALDO
    (posterior al movimiento), N° DOCUMENTO, SUCURSAL y N° MOVIMIENTO. Viene del más reciente al
    más antiguo y el banco la marca como información provisoria.
    """
    buscar = lambda patron: (re.search(patron, texto, re.I | re.M) or [None, ""])[1]  # noqa: E731
    datos = {"titular": (buscar(r"Empresa:\s*(.+?)\s*(?:RUT empresa|$)") or "").strip(),
             "rut": "", "cuenta": "", "banco": "Banco Santander", "movimientos": [], "provisorio": True}
    if r := re.search(r"RUT empresa:\s*([\d.]+)-([\dkK])", texto, re.I):
        datos["rut"] = f"{int(r.group(1).replace('.', ''))}-{r.group(2).upper()}"
    if c := re.search(r"Corriente\s*N°?\s*:?\s*([\d-]{6,})", texto.replace("C uenta", "Cuenta"), re.I):
        datos["cuenta"] = c.group(1).replace("-", "").lstrip("0")
    filas = []
    for pagina in pdf.pages:
        palabras = pagina.extract_words()
        enc = next((w for w in palabras if w["text"].upper() == "MONTO"), None)
        if enc is None:
            continue
        fila_enc = [w for w in palabras if abs(w["top"] - enc["top"]) < 3]
        x = {}
        for w in fila_enc:
            t = w["text"].upper()
            if t.startswith("DESCRIP"):
                x.setdefault("desc", w["x0"])
            elif t in ("FECHA", "SALDO", "SUCURSAL"):
                x.setdefault(t.lower(), w["x0"])
            elif t == "N°":
                x["doc" if "doc" not in x else "nmov"] = w["x0"]
        if not {"desc", "fecha", "saldo"} <= set(x):
            continue
        x.setdefault("doc", x["saldo"] + 80)
        x.setdefault("sucursal", x["doc"] + 60)
        x.setdefault("nmov", x["sucursal"] + 70)
        fin = min([w["top"] for w in palabras if w["text"].lower() == "nota:"] + [pagina.height])
        cuerpo = [w for w in palabras if enc["top"] + 3 < w["top"] < fin - 1]
        anclas = [w for w in cuerpo if _RE_FECHA.match(w["text"]) and x["fecha"] - 6 <= w["x0"] < x["saldo"] - 4]
        propias = [{"fecha": _fecha(a["text"]), "top": a["top"], "monto": None, "saldo": None,
                    "desc": [], "doc": [], "suc": [], "nmov": ""} for a in anclas]
        if not propias:
            continue
        cercana = lambda w: min(propias, key=lambda f: abs(f["top"] - w["top"]))  # noqa: E731
        for w in cuerpo:
            if w in anclas or w["text"] == "$":
                continue
            x0, t = w["x0"], w["text"]
            if x0 < x["desc"] - 4 or x["saldo"] - 4 <= x0 < x["doc"] - 4:
                t = t.replace("$", "")            # «$-348.432» cuando el signo va pegado
            if x0 < x["desc"] - 4:
                if _RE_MONTO.match(t.lstrip("-")):
                    cercana(w)["monto"] = _monto(t)
            elif x0 < x["fecha"] - 6:
                cercana(w)["desc"].append(w)
            elif x["saldo"] - 4 <= x0 < x["doc"] - 4:
                if _RE_MONTO.match(t.lstrip("-")):
                    cercana(w)["saldo"] = _monto(t)
            elif x0 < x["sucursal"] - 4:
                cercana(w)["doc"].append(w)
            elif x0 < x["nmov"] - 4:
                cercana(w)["suc"].append(w)
            else:
                cercana(w)["nmov"] = t
        filas.extend(f for f in propias if f["monto"])
    if not filas:
        raise ErrorCartola("La consulta de movimientos no tiene movimientos.")
    unir = lambda ws: " ".join(w["text"] for w in sorted(ws, key=lambda w: (round(w["top"]), w["x0"])))  # noqa: E731
    # Del más antiguo al más reciente: por fecha y luego por N° de movimiento (o al revés del PDF).
    filas = list(reversed(filas))
    filas.sort(key=lambda f: (f["fecha"], int(f["nmov"]) if f["nmov"].isdigit() else 0))
    vistos = {}
    for orden, f in enumerate(filas, start=1):
        m = {"fecha": f["fecha"], "monto": f["monto"], "saldo": f["saldo"], "orden": orden,
             "descripcion": unir(f["desc"])[:200] or "(sin descripción)",
             "documento": unir(f["doc"]).lstrip("0")[:30], "sucursal": unir(f["suc"])[:40]}
        clave = (m["fecha"], m["descripcion"].lower(), m["monto"], m["documento"])
        vistos[clave] = vistos.get(clave, 0) + 1
        m["huella"] = hashlib.sha256(repr(clave + (vistos[clave],)).encode()).hexdigest()
        datos["movimientos"].append(m)
    return datos


def aplicar_reglas(sociedad, movimientos, reglas=None):
    """Categoriza los movimientos SIN categoría ni conciliación con la primera regla que coincide
    (las reglas se revisan por prioridad). Devuelve cuántos categorizó."""
    if reglas is None:
        reglas = list(sociedad.reglas_banco.filter(activa=True).select_related("categoria"))
    usadas, n = {}, 0
    for m in movimientos:
        if m.categoria_id or m.ignorado or m.conciliaciones.exists():
            continue
        for regla in reglas:
            if regla.aplica(m):
                m.categoria = regla.categoria
                m.save(update_fields=["categoria"])
                usadas[regla.pk] = usadas.get(regla.pk, 0) + 1
                n += 1
                break
    for pk, veces in usadas.items():
        type(reglas[0]).objects.filter(pk=pk).update(veces=F("veces") + veces)
    return n


def coincidencias(regla, sociedad):
    """Movimientos de la sociedad que cumplen la regla (para la vista previa)."""
    candidatos = MovimientoBancario.objects.filter(cuenta__sociedad=sociedad).select_related("cuenta", "categoria")
    if regla.solo == "cargo":
        candidatos = candidatos.filter(monto__lt=0)
    elif regla.solo == "abono":
        candidatos = candidatos.filter(monto__gt=0)
    if regla.cuenta_id:
        candidatos = candidatos.filter(cuenta_id=regla.cuenta_id)
    activa = regla.activa
    regla.activa = True
    try:
        return [m for m in candidatos.order_by("-fecha", "-orden") if regla.aplica(m)]
    finally:
        regla.activa = activa


def sugerir_reglas(sociedad, minimo=2, limite=8):
    """Descripciones que se repiten entre los movimientos sin categoría: buenas candidatas a regla."""
    pendientes = MovimientoBancario.objects.filter(
        cuenta__sociedad=sociedad, categoria__isnull=True, ignorado=False, conciliaciones__isnull=True)
    grupos = {}
    for m in pendientes:
        texto = texto_regla(m.descripcion)
        if len(texto) < 4:
            continue
        g = grupos.setdefault(texto.lower(), {"texto": texto, "n": 0, "total": 0, "cargos": 0})
        g["n"] += 1
        g["total"] += abs(m.monto)
        g["cargos"] += m.monto < 0
    sugeridas = [g for g in grupos.values() if g["n"] >= minimo]
    for g in sugeridas:
        g["solo"] = "cargo" if g["cargos"] == g["n"] else "abono" if g["cargos"] == 0 else ""
    return sorted(sugeridas, key=lambda g: (-g["n"], -g["total"]))[:limite]


def texto_regla(descripcion):
    """Parte estable de una descripción (sin números de operación, RUT ni fechas)."""
    texto = re.sub(r"\b\d[\d./-]*\b", " ", descripcion)       # números, RUT, fechas
    texto = re.sub(r"\s+", " ", texto).strip(" -:.,")
    return texto[:60]


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
    provisorio = bool(datos.get("provisorio"))
    candidatos = [m for m in datos["movimientos"] if m["huella"] not in existentes]
    # La consulta de «últimos movimientos» y la cartola definitiva describen distinto el mismo
    # movimiento: se reconocen por fecha y monto para no duplicarlos.
    desde = min((m["fecha"] for m in candidatos), default=None)
    previos = {}
    if desde:
        for e in cuenta.movimientos.filter(fecha__gte=desde).exclude(huella__in=[m["huella"] for m in candidatos]):
            if provisorio or e.provisorio:
                previos.setdefault((e.fecha, e.monto), []).append(e)
    nuevos, confirmados = [], 0
    for m in candidatos:
        pares = previos.get((m["fecha"], m["monto"]))
        if pares:
            previo = pares.pop(0)
            if not provisorio:          # la cartola definitiva reemplaza los datos provisorios
                for campo in ("descripcion", "saldo", "orden", "documento", "sucursal", "huella"):
                    setattr(previo, campo, m[campo])
                previo.provisorio = False
                previo.save(update_fields=["descripcion", "saldo", "orden", "documento", "sucursal", "huella",
                                           "provisorio"])
                confirmados += 1
            continue
        nuevos.append(MovimientoBancario(cuenta=cuenta, provisorio=provisorio, **m))
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
    if datos.get("saldo_final") is not None and datos["movimientos"]:
        if datos["movimientos"][-1]["saldo"] != datos["saldo_final"]:
            descuadres += 1        # el PDF no llega al saldo final que informa el banco
    return {"descuadres": descuadres, "cuenta": cuenta, "nuevos": len(nuevos), "confirmados": confirmados,
            "provisorio": provisorio, "repetidos": len(datos["movimientos"]) - len(nuevos) - confirmados,
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
