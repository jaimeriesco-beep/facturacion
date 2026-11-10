"""Representación impresa (PDF) de un DTE con su timbre electrónico PDF417."""
from io import BytesIO

import pdf417gen
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas

from ..utils import formatear_clp, formatear_rut
from .motor import extraer_ted

ROJO = colors.Color(0.78, 0.1, 0.1)
GRIS = colors.Color(0.4, 0.4, 0.4)
ANCHO, ALTO = letter
MARGEN = 15 * mm
NOMBRES = {33: "FACTURA ELECTRÓNICA", 34: "FACTURA NO AFECTA O EXENTA ELECTRÓNICA",
           52: "GUÍA DE DESPACHO ELECTRÓNICA", 56: "NOTA DE DÉBITO ELECTRÓNICA",
           61: "NOTA DE CRÉDITO ELECTRÓNICA"}


def _numero(valor):
    texto = f"{valor:,.3f}".rstrip("0").rstrip(".")
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _timbre(xml):
    codigos = pdf417gen.encode(extraer_ted(bytes(xml)), columns=14, security_level=5,
                               encoding="latin-1")
    return ImageReader(pdf417gen.render_image(codigos, scale=3, ratio=3, padding=0))


def _pagina(c, doc, lineas, cedible):
    soc, cli = doc.sociedad, doc.cliente
    y = ALTO - MARGEN

    # Recuadro rojo del SII.
    caja_x, caja_w, caja_h = ANCHO - MARGEN - 75 * mm, 75 * mm, 30 * mm
    c.setStrokeColor(ROJO); c.setFillColor(ROJO); c.setLineWidth(1.6)
    c.rect(caja_x, y - caja_h, caja_w, caja_h)
    centro = caja_x + caja_w / 2
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(centro, y - 8 * mm, f"R.U.T.: {formatear_rut(soc.rut)}")
    nombre = simpleSplit(NOMBRES[doc.tipo], "Helvetica-Bold", 11, caja_w - 6 * mm)
    for i, parte in enumerate(nombre):
        c.setFont("Helvetica-Bold", 11)
        c.drawCentredString(centro, y - 14.5 * mm - i * 4.6 * mm, parte)
    c.setFont("Helvetica-Bold", 12)
    c.drawCentredString(centro, y - 26 * mm, f"N° {doc.folio}")
    c.setFont("Helvetica-Bold", 9)
    c.drawCentredString(centro, y - caja_h - 5 * mm, f"S.I.I. - {(soc.oficina_sii or soc.comuna).upper()}")

    # Emisor (con logo si la sociedad tiene uno).
    c.setFillColor(colors.black)
    ey = y
    if soc.logo:
        logo = ImageReader(BytesIO(bytes(soc.logo)))
        lw, lh = logo.getSize()
        alto = 18 * mm
        ancho = min(alto * lw / lh, 80 * mm)
        alto = ancho * lh / lw
        c.drawImage(logo, MARGEN, y - alto, ancho, alto, mask="auto")
        ey = y - alto - 5 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGEN, ey - 3 * mm, soc.razon_social[:60])
    c.setFont("Helvetica", 8.5)
    datos = [f"Giro: {soc.giro}", ", ".join(x for x in (soc.direccion, soc.comuna, soc.ciudad) if x),
             " · ".join(x for x in (soc.telefono, soc.email) if x)]
    ey -= 7.5 * mm
    for texto in datos:
        if texto.strip(" :·Giro"):
            for parte in simpleSplit(texto, "Helvetica", 8.5, 105 * mm):
                c.drawString(MARGEN, ey, parte); ey -= 4 * mm
    y = min(ey, y - caja_h - 8 * mm) - 4 * mm

    # Receptor.
    filas = [("Señor(es)", cli.razon_social, "Fecha emisión", doc.fecha_emision.strftime("%d/%m/%Y")),
             ("R.U.T.", formatear_rut(cli.rut), "Vencimiento" if doc.es_factura else "",
              doc.fecha_vencimiento.strftime("%d/%m/%Y") if doc.es_factura and doc.fecha_vencimiento else ""),
             ("Giro", cli.giro, "Comuna", cli.comuna), ("Dirección", cli.direccion, "Ciudad", cli.ciudad)]
    if doc.es_guia:
        filas.append(("Traslado", doc.get_ind_traslado_display() or "", "Patente", doc.patente))
        if doc.direccion_destino:
            filas.append(("Destino", f"{doc.direccion_destino} {doc.comuna_destino}".strip(), "", ""))
    alto = len(filas) * 5 * mm + 4 * mm
    c.setStrokeColor(colors.black); c.setLineWidth(0.6)
    c.rect(MARGEN, y - alto, ANCHO - 2 * MARGEN, alto)
    fy = y - 6 * mm
    for r1, v1, r2, v2 in filas:
        c.setFont("Helvetica-Bold", 8.5); c.drawString(MARGEN + 3 * mm, fy, r1)
        c.setFont("Helvetica", 8.5); c.drawString(MARGEN + 24 * mm, fy, f": {v1}"[:70])
        if r2:
            c.setFont("Helvetica-Bold", 8.5); c.drawString(ANCHO - MARGEN - 62 * mm, fy, r2)
            c.setFont("Helvetica", 8.5); c.drawString(ANCHO - MARGEN - 38 * mm, fy, f": {v2}")
        fy -= 5 * mm
    y -= alto + 4 * mm

    # Referencias.
    referencias = []
    if doc.caso_set:
        referencias.append(f"SET · {doc.caso_set}")
    if doc.referencia_id:
        ref = doc.referencia
        motivo = f" · {doc.get_codigo_referencia_display()}" if doc.es_nota else ""
        referencias.append(f"{ref.get_tipo_display()} N° {ref.folio} del "
                           f"{ref.fecha_emision:%d/%m/%Y}{motivo} · {doc.razon_referencia}".rstrip(" ·"))
    if referencias:
        c.setFont("Helvetica-Bold", 8.5); c.drawString(MARGEN, y - 3 * mm, "Referencias")
        c.setFont("Helvetica", 8.5)
        for texto in referencias:
            y -= 4.5 * mm; c.drawString(MARGEN + 3 * mm, y - 3 * mm, texto[:120])
        y -= 8 * mm

    # Detalle.
    cols = [MARGEN, MARGEN + 100 * mm, MARGEN + 120 * mm, MARGEN + 146 * mm, MARGEN + 160 * mm]
    derecha = ANCHO - MARGEN
    c.setFillColor(colors.Color(0.92, 0.92, 0.92)); c.rect(MARGEN, y - 6 * mm, derecha - MARGEN, 6 * mm, fill=1, stroke=0)
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 8.5)
    c.drawString(cols[0] + 2 * mm, y - 4.2 * mm, "Descripción")
    for x, titulo in ((cols[2], "Cantidad"), (cols[3], "Precio"), (cols[4], "Desc."), (derecha, "Total")):
        c.drawRightString(x - 1 * mm, y - 4.2 * mm, titulo)
    y -= 10 * mm
    c.setFont("Helvetica", 8.5)
    for l in lineas:
        partes = simpleSplit(l.descripcion + ("  (exento)" if l.exento and doc.tipo != 34 else ""),
                             "Helvetica", 8.5, 96 * mm)
        c.drawString(cols[0] + 2 * mm, y, partes[0])
        if l.precio > 0:
            c.drawRightString(cols[2] - 1 * mm, y, _numero(l.cantidad))
            c.drawRightString(cols[3] - 1 * mm, y, "$" + _numero(l.precio))
        if l.descuento:
            c.drawRightString(cols[4] - 1 * mm, y, _numero(l.descuento) + "%")
        c.drawRightString(derecha - 1 * mm, y, formatear_clp(l.monto))
        for extra in partes[1:4]:
            y -= 4 * mm; c.drawString(cols[0] + 2 * mm, y, extra)
        y -= 5.5 * mm
    if doc.observaciones:
        c.setFillColor(GRIS); c.drawString(MARGEN + 2 * mm, y - 2 * mm, f"Observaciones: {doc.observaciones}"[:130])
        c.setFillColor(colors.black)

    # Pie: timbre a la izquierda, totales a la derecha.
    base = MARGEN + (38 * mm if cedible else 0)
    timbre_w, timbre_h = 62 * mm, 28 * mm
    c.drawImage(_timbre(doc.xml_firmado), MARGEN, base + 9 * mm, timbre_w, timbre_h)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawCentredString(MARGEN + timbre_w / 2, base + 5 * mm, "Timbre Electrónico SII")
    c.setFont("Helvetica", 7)
    anio = soc.resolucion_fecha.year if soc.resolucion_fecha else ""
    c.drawCentredString(MARGEN + timbre_w / 2, base + 1.5 * mm,
                        f"Res. {soc.resolucion_numero} de {anio} · Verifique documento: www.sii.cl")
    totales = [("Monto neto", doc.neto), ("Monto exento", doc.exento), ("IVA 19%", doc.iva)]
    ty = base + 30 * mm
    c.setFont("Helvetica", 9)
    for rotulo, valor in totales:
        if valor:
            c.drawString(derecha - 62 * mm, ty, rotulo); c.drawRightString(derecha - 1 * mm, ty, formatear_clp(valor))
            ty -= 5.5 * mm
    c.line(derecha - 62 * mm, ty + 3.5 * mm, derecha, ty + 3.5 * mm)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(derecha - 62 * mm, ty - 1.5 * mm, "TOTAL"); c.drawRightString(derecha - 1 * mm, ty - 1.5 * mm, formatear_clp(doc.total))

    if cedible:
        # Acuse de recibo exigido en la copia cedible (Ley 19.983).
        c.rect(MARGEN, MARGEN + 8 * mm, derecha - MARGEN, 26 * mm)
        c.setFont("Helvetica", 8)
        for i, texto in enumerate(("Nombre: ______________________________________   R.U.T.: __________________",
                                   "Fecha: ____________________   Recinto: ______________________________   Firma: __________________")):
            c.drawString(MARGEN + 3 * mm, MARGEN + 28 * mm - i * 7 * mm, texto)
        c.setFont("Helvetica", 6)
        aviso = ("El acuse de recibo que se declara en este acto, de acuerdo a lo dispuesto en la letra b) del Art. 4° y la letra c) "
                 "del Art. 5° de la Ley 19.983, acredita que la entrega de mercaderías o servicio(s) prestado(s) ha(n) sido recibido(s).")
        for i, parte in enumerate(simpleSplit(aviso, "Helvetica", 6, derecha - MARGEN - 6 * mm)):
            c.drawString(MARGEN + 3 * mm, MARGEN + 14.5 * mm - i * 2.6 * mm, parte)
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(derecha, MARGEN + 2 * mm, "CEDIBLE CON SU FACTURA" if doc.es_guia else "CEDIBLE")
    if doc.es_prueba:
        c.setFont("Helvetica", 7); c.setFillColor(GRIS)
        c.drawString(MARGEN, MARGEN - 6 * mm, "Documento de prueba emitido en ambiente de certificación del SII. Sin validez tributaria.")
        c.setFillColor(colors.black)


def generar_pdf(doc, cedible=False) -> bytes:
    """PDF del documento. Con cedible=True agrega la copia cedible como segunda página."""
    salida = BytesIO()
    c = canvas.Canvas(salida, pagesize=letter)
    c.setTitle(str(doc)); c.setAuthor(doc.sociedad.razon_social)
    lineas = list(doc.lineas.all())
    _pagina(c, doc, lineas, False)
    if cedible and (doc.es_factura or doc.es_guia):
        c.showPage()
        _pagina(c, doc, lineas, True)
    c.save()
    return salida.getvalue()
