"""Descarga en Excel de cada gráfico del Inicio: el gráfico (nativo de Excel), su tabla
y el detalle de los documentos que se consideraron para calcularlo."""
from io import BytesIO

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import resumen
from .models import NOTA_CREDITO, hoy

VERDE, AZUL, NARANJO = "2F8A55", "2A78D6", "C2410C"
PESOS = '"$"#,##0;[Red]-"$"#,##0'
TITULOS = {
    "ventas": "Ventas netas",
    "cobros": "Facturado y cobrado",
    "clientes": "Principales clientes",
    "antiguedad": "Antigüedad de la cobranza",
    "categorias": "En qué se gasta",
    "gastos": "Ventas y gastos",
}


def _hoja_tabla(hoja, titulo, nota, encabezados, filas, formatos=None):
    hoja["A1"] = titulo
    hoja["A1"].font = Font(bold=True, size=14)
    hoja["A2"] = nota
    hoja["A2"].font = Font(italic=True, color="66736B")
    hoja.append([])
    hoja.append(encabezados)
    fila_enc = hoja.max_row
    for c in hoja[fila_enc]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F6B45")
        c.alignment = Alignment(vertical="center")
    for f in filas:
        hoja.append(list(f))
    formatos = formatos or {}
    for i, enc in enumerate(encabezados, start=1):
        letra = get_column_letter(i)
        largo = max([len(str(enc))] + [len(str(f[i - 1])) for f in filas if i - 1 < len(f)] or [10])
        hoja.column_dimensions[letra].width = max(11, min(48, largo + 3))
        formato = formatos.get(i)
        if formato:
            for celda in hoja[letra][fila_enc:]:
                celda.number_format = formato
    hoja.freeze_panes = hoja.cell(row=fila_enc + 1, column=1)
    return fila_enc, fila_enc + len(filas)


def _grafico(hoja, titulo, desde_fila, hasta_fila, n_series, colores, horizontal=False, ancla="F4"):
    if hasta_fila <= desde_fila:
        return
    g = BarChart()
    g.type = "bar" if horizontal else "col"
    g.title = titulo
    g.style = 10
    g.height, g.width = (max(7, 0.9 * (hasta_fila - desde_fila) + 3) if horizontal else 9), 22
    datos = Reference(hoja, min_col=2, max_col=1 + n_series, min_row=desde_fila, max_row=hasta_fila)
    categorias = Reference(hoja, min_col=1, min_row=desde_fila + 1, max_row=hasta_fila)
    g.add_data(datos, titles_from_data=True)
    g.set_categories(categorias)
    g.y_axis.numFmt = '"$"#,##0'
    g.y_axis.majorGridlines = None if horizontal else g.y_axis.majorGridlines
    g.x_axis.delete = False
    g.y_axis.delete = False
    if horizontal:
        g.x_axis.scaling.orientation = "maxMin"     # el mayor arriba, como en la app
    if n_series == 1:
        g.legend = None
    else:
        g.legend.position = "b"
    for serie, color in zip(g.series, colores):
        serie.graphicalProperties.solidFill = color
        serie.graphicalProperties.line.solidFill = color
    g.gapWidth = 60
    hoja.add_chart(g, ancla)


def _doc_fila(d):
    signo = -1 if d.tipo == NOTA_CREDITO else 1
    return [d.fecha_emision, d.get_tipo_display(), d.folio, d.cliente.rut, d.cliente.razon_social,
            signo * d.neto, signo * d.exento, signo * (d.neto + d.exento), signo * d.iva, signo * d.total,
            "Emitido en el SII" if d.importado_sii or d.estado_sii == "portal" else "Emitido en la app"]


DOC_ENC = ["Fecha", "Tipo", "Folio", "RUT cliente", "Cliente", "Neto", "Exento", "Venta neta (sin IVA)",
           "IVA", "Total", "Origen"]
DOC_FMT = {1: "dd/mm/yyyy", 6: PESOS, 7: PESOS, 8: PESOS, 9: PESOS, 10: PESOS}


def _detalle_documentos(libro, datos, sociedad):
    hoja = libro.create_sheet("Documentos considerados")
    docs = sorted(resumen.emitidos(sociedad, datos["desde"]), key=lambda d: (d.fecha_emision, d.tipo, d.folio or 0))
    _hoja_tabla(hoja, "Documentos de venta considerados",
                "Emitidos (no borradores, cotizaciones, guías ni documentos de prueba). Las notas de crédito restan.",
                DOC_ENC, [_doc_fila(d) for d in docs], DOC_FMT)


def _detalle_pagos(libro, datos, sociedad):
    hoja = libro.create_sheet("Pagos considerados")
    filas = [[p.fecha, str(p.documento), p.documento.cliente.razon_social, p.get_medio_display(), p.monto]
             for p in sorted(resumen.pagos(sociedad, datos["desde"]), key=lambda p: (p.fecha, p.pk))]
    _hoja_tabla(hoja, "Pagos recibidos considerados", "Cobros registrados en el período (por fecha de pago).",
                ["Fecha de pago", "Documento", "Cliente", "Medio", "Monto"], filas, {1: "dd/mm/yyyy", 5: PESOS})


def _detalle_compras(libro, datos, sociedad):
    hoja = libro.create_sheet("Compras consideradas")
    filas = [[c.fecha_emision, c.get_tipo_display(), c.folio, c.proveedor.rut, c.proveedor.razon_social,
              str(c.categoria or "Sin categoría"), c.gasto, c.signo * c.iva, c.signo * c.total]
             for c in sorted(resumen.compras(sociedad, datos["desde"]), key=lambda c: (c.fecha_emision, c.pk))]
    _hoja_tabla(hoja, "Compras consideradas", "Gasto = neto + exento (sin IVA recuperable); las notas de crédito restan.",
                ["Fecha", "Tipo", "Folio", "RUT proveedor", "Proveedor", "Categoría", "Gasto (sin IVA)",
                 "IVA", "Total"], filas, {1: "dd/mm/yyyy", 7: PESOS, 8: PESOS, 9: PESOS})


def generar(sociedad, grafico, vista):
    """Devuelve (nombre_archivo, bytes) o None si el gráfico no existe."""
    if grafico not in TITULOS:
        return None
    datos = resumen.calcular(sociedad, vista)
    s = datos["series"]
    titulo = TITULOS[grafico]
    nota = f"{sociedad} · {datos['rango']} ({datos['vistas'][datos['vista']][0].lower()}) · generado el {hoy():%d/%m/%Y}"
    libro = Workbook()
    hoja = libro.active
    hoja.title = "Gráfico"
    if grafico in ("ventas", "cobros", "gastos"):
        series = {"ventas": [("Ventas netas", s["ventas"], VERDE)],
                  "cobros": [("Facturado", s["facturado"], VERDE), ("Cobrado", s["cobrado"], AZUL)],
                  "gastos": [("Ventas netas", s["ventas"], VERDE), ("Gastos", s["gastos"], NARANJO)]}[grafico]
        filas = [[s["rangos"][i]] + [v[i] for _, v, _ in series] for i in range(len(s["rangos"]))]
        filas.append(["Total"] + [sum(v) for _, v, _ in series])
        if grafico == "gastos":
            hoja_enc = ["Período", "Ventas netas", "Gastos", "Margen"]
            filas = [f + [f[1] - f[2]] for f in filas]
        else:
            hoja_enc = ["Período"] + [n for n, _, _ in series]
        ini, fin = _hoja_tabla(hoja, titulo, nota, hoja_enc, filas, {i: PESOS for i in range(2, len(hoja_enc) + 1)})
        for c in hoja[fin]:
            c.font = Font(bold=True)
        _grafico(hoja, titulo, ini, fin - 1, len(series), [c for _, _, c in series])
        if grafico in ("ventas", "gastos"):
            _detalle_documentos(libro, datos, sociedad)
        if grafico == "cobros":
            _detalle_documentos(libro, datos, sociedad)
            _detalle_pagos(libro, datos, sociedad)
        if grafico == "gastos":
            _detalle_compras(libro, datos, sociedad)
    elif grafico == "clientes":
        todos = datos["todos_clientes"]
        total = sum(v for _, v in todos) or 1
        filas = [[c.nombre_fantasia or c.razon_social, v, c.rut, v / total] for c, v in todos]
        ini, fin = _hoja_tabla(hoja, titulo, nota + " · ventas netas sin IVA",
                               ["Cliente", "Ventas netas", "RUT", "% del total"], filas, {2: PESOS, 4: "0.0%"})
        _grafico(hoja, titulo + " (10 mayores)", ini, min(fin, ini + 10), 1, [VERDE], horizontal=True)
        _detalle_documentos(libro, datos, sociedad)
    elif grafico == "categorias":
        todas = datos["todas_categorias"]
        total = sum(v for _, v in todas) or 1
        ini, fin = _hoja_tabla(hoja, titulo, nota + " · compras sin IVA",
                               ["Categoría", "Gasto neto", "% del total"],
                               [[n, v, v / total] for n, v in todas], {2: PESOS, 3: "0.0%"})
        _grafico(hoja, titulo, ini, min(fin, ini + 12), 1, [NARANJO], horizontal=True)
        _detalle_compras(libro, datos, sociedad)
    else:   # antiguedad
        a = resumen.antiguedad(sociedad)
        ini, fin = _hoja_tabla(hoja, titulo, f"{sociedad} · saldo por cobrar al {hoy():%d/%m/%Y}",
                               ["Tramo", "Saldo por cobrar"], list(zip(a["etiquetas"], a["valores"])), {2: PESOS})
        _grafico(hoja, titulo, ini, fin, 1, [AZUL], horizontal=True)
        det = libro.create_sheet("Documentos por cobrar")
        docs = sorted(sociedad.documentos.por_cobrar().select_related("cliente"),
                      key=lambda d: (-d.dias_vencido, d.fecha_emision))
        _hoja_tabla(det, "Documentos con saldo por cobrar", "Días de atraso contados desde el vencimiento.",
                    ["Documento", "Cliente", "RUT", "Emisión", "Vencimiento", "Días de atraso", "Tramo", "Total", "Saldo"],
                    [[str(d), d.cliente.razon_social, d.cliente.rut, d.fecha_emision, d.fecha_vencimiento,
                      max(d.dias_vencido, 0), resumen.TRAMOS[resumen.tramo(d.dias_vencido)], d.total, d.saldo]
                     for d in docs], {4: "dd/mm/yyyy", 5: "dd/mm/yyyy", 8: PESOS, 9: PESOS})
    salida = BytesIO()
    libro.save(salida)
    nombre = f"{titulo.lower().replace(' ', '-')}-{datos['vista']}-{hoy():%Y-%m-%d}"
    return nombre, salida.getvalue()
