"""Inventario: stock calculado desde los movimientos y su vínculo con los documentos.

Modelo inspirado en los ERP habituales (Odoo, Defontana): el stock no se edita, se
deriva de entradas y salidas; cada movimiento tiene un motivo, una bodega y una fecha;
las ventas rebajan solas al emitir el documento y las correcciones se hacen con
ajustes o con la toma de inventario.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.db.models.signals import post_save
from django.dispatch import receiver

from .utils import redondear
from .models import (FACTURAS, GUIA, NOTA_CREDITO, Bodega, Documento, Movimiento, Producto)

CERO = Decimal(0)


def bodega_principal(sociedad):
    bodega = sociedad.bodegas.filter(activa=True, principal=True).first() or \
        sociedad.bodegas.filter(activa=True).first()
    if bodega is None:
        bodega = Bodega.objects.create(sociedad=sociedad, nombre="Bodega principal", principal=True)
    return bodega


def stock(sociedad, bodega=None, hasta=None):
    """{producto_id: cantidad} con la suma de movimientos."""
    qs = Movimiento.objects.filter(sociedad=sociedad)
    if bodega is not None:
        qs = qs.filter(bodega=bodega)
    if hasta is not None:
        qs = qs.filter(fecha__lte=hasta)
    return {f["producto"]: f["s"] for f in qs.values("producto").annotate(s=Sum("cantidad"))}


def stock_por_bodega(sociedad):
    """{(producto_id, bodega_id): cantidad}."""
    filas = (Movimiento.objects.filter(sociedad=sociedad)
             .values("producto", "bodega").annotate(s=Sum("cantidad")))
    return {(f["producto"], f["bodega"]): f["s"] for f in filas}


def costos_promedio(sociedad):
    """Costo promedio ponderado de las entradas con costo: {producto_id: costo}."""
    filas = (Movimiento.objects.filter(sociedad=sociedad, cantidad__gt=0, costo_unitario__isnull=False)
             .exclude(motivo__in=["traspaso_in", "nota_credito"])
             .values("producto")
             .annotate(cant=Sum("cantidad"),
                       valor=Sum(F("cantidad") * F("costo_unitario"),
                                 output_field=DecimalField(max_digits=30, decimal_places=4))))
    return {f["producto"]: (f["valor"] / f["cant"]) for f in filas if f["cant"]}


def signo_documento(doc):
    """-1 si el documento rebaja stock, +1 si lo devuelve, 0 si no lo mueve."""
    soc = doc.sociedad
    if (not soc.inventario_activo or doc.estado != Documento.EMITIDO or doc.es_prueba
            or doc.eliminado_el or doc.estado_sii == "rechazado"):
        return 0
    if soc.inventario_desde and doc.fecha_emision < soc.inventario_desde:
        return 0
    if doc.tipo in FACTURAS and soc.inventario_rebaja == "factura":
        return -1
    if doc.tipo == GUIA and soc.inventario_rebaja == "guia" and doc.ind_traslado != 5:
        return -1   # 5 = traslado interno: el producto no sale de la empresa
    if (doc.tipo == NOTA_CREDITO and soc.inventario_nc_devuelve and doc.codigo_referencia == 1
            and doc.referencia_id and signo_documento(doc.referencia) == -1):
        return 1
    return 0


def _cantidades(doc):
    """{producto: cantidad} de las líneas con productos que controlan stock."""
    total = {}
    for linea in doc.lineas.select_related("producto"):
        if linea.producto_id and linea.producto.controla_stock:
            total[linea.producto] = total.get(linea.producto, CERO) + linea.cantidad
    return total


@transaction.atomic
def sincronizar_documento(doc):
    """Deja los movimientos del documento como corresponde a su estado actual.

    Es idempotente: se puede llamar cada vez que el documento cambia.
    """
    signo = signo_documento(doc)
    actuales = list(doc.movimientos.all())
    if not signo:
        if actuales:
            doc.movimientos.all().delete()
        return
    bodega = actuales[0].bodega if actuales else bodega_principal(doc.sociedad)
    deseados = {p.pk: c for p, c in _cantidades(doc).items()}
    vigentes = {m.producto_id: m for m in actuales}
    motivo = "venta" if signo < 0 else "nota_credito"
    for pid, mov in vigentes.items():
        if pid not in deseados:
            mov.delete()
    for pid, cantidad in deseados.items():
        valor = cantidad * signo
        mov = vigentes.get(pid)
        if mov is None:
            Movimiento.objects.create(sociedad=doc.sociedad, producto_id=pid, bodega=bodega,
                                      fecha=doc.fecha_emision, motivo=motivo, cantidad=valor,
                                      documento=doc, nota=str(doc))
        elif (mov.cantidad, mov.fecha, mov.motivo) != (valor, doc.fecha_emision, motivo):
            mov.cantidad, mov.fecha, mov.motivo, mov.nota = valor, doc.fecha_emision, motivo, str(doc)
            mov.save()


@receiver(post_save, sender=Documento)
def _al_guardar_documento(sender, instance, raw=False, **kwargs):
    if raw:
        return
    if instance.sociedad.inventario_activo or instance.movimientos.exists():
        sincronizar_documento(instance)


def faltantes(doc):
    """Productos sin stock suficiente para emitir el documento, si la sociedad no permite
    stock negativo. Devuelve una lista de textos (vacía si se puede emitir)."""
    soc = doc.sociedad
    if not soc.inventario_activo or soc.inventario_permite_negativo:
        return []
    # Se evalúa como si ya estuviera emitido.
    estado = doc.estado
    doc.estado = Documento.EMITIDO
    try:
        signo = signo_documento(doc)
    finally:
        doc.estado = estado
    if signo >= 0:
        return []
    bodega = bodega_principal(soc)
    disponible = stock(soc, bodega=bodega)
    errores = []
    for producto, cantidad in _cantidades(doc).items():
        hay = disponible.get(producto.pk, CERO)
        if hay < cantidad:
            errores.append(f"Stock insuficiente de {producto} en {bodega}: hay {numero(hay)} "
                           f"{producto.unidad or ''} y el documento lleva {numero(cantidad)}.".replace("  ", " "))
    return errores


def numero(valor):
    """Cantidad en formato chileno, sin ceros sobrantes: 1.234,5"""
    if valor is None:
        return ""
    valor = Decimal(valor).quantize(Decimal("0.001")).normalize()
    entero, _, dec = f"{abs(valor):f}".partition(".")
    entero = f"{int(entero):,}".replace(",", ".")
    texto = f"{entero},{dec}" if dec else entero
    return f"-{texto}" if valor < 0 else texto


def resumen(sociedad, bodega=None):
    """Filas para la pantalla de stock: producto, stock, mínimo, costo y valor."""
    cantidades = stock(sociedad, bodega=bodega)
    costos = costos_promedio(sociedad)
    filas = []
    productos = sociedad.productos.filter(controla_stock=True).filter(
        Q(activo=True) | Q(pk__in=[p for p, c in cantidades.items() if c]))
    for p in productos:
        cant = cantidades.get(p.pk, CERO)
        costo = costos.get(p.pk)
        if cant < 0:
            estado = "negativo"
        elif p.stock_minimo is not None and cant <= p.stock_minimo:
            estado = "reponer"
        elif cant == 0:
            estado = "sin"
        else:
            estado = "ok"
        filas.append({"producto": p, "stock": cant, "minimo": p.stock_minimo, "costo": costo,
                      "valor": redondear(cant * costo) if costo is not None and cant > 0 else None,
                      "estado": estado})
    return filas
