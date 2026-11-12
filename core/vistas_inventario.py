"""Pantallas del módulo de inventario."""
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from openpyxl import Workbook
from openpyxl.styles import Font

from . import inventario
from .forms import (BodegaForm, InventarioConfigForm, MovimientoForm, MovimientoLineasFormSet,
                    NumeroCL, TrasladoForm)
from .inventario import CERO, numero
from .models import Bodega, Movimiento, Producto, hoy
from .views import con_sociedad


def _activo(vista):
    """Las pantallas de inventario exigen el módulo activo en la sociedad."""
    def envoltura(request, *args, **kwargs):
        if not request.sociedad.inventario_activo:
            messages.info(request, "El inventario está desactivado para esta sociedad. "
                                   "Actívalo aquí para usarlo.")
            return redirect("inventario_config")
        return vista(request, *args, **kwargs)
    envoltura.__name__ = vista.__name__
    return con_sociedad(envoltura)


def _productos(sociedad):
    return sociedad.productos.filter(controla_stock=True, activo=True)


def _excel(nombre, encabezados, filas):
    libro = Workbook()
    hoja = libro.active
    hoja.append(encabezados)
    for celda in hoja[1]:
        celda.font = Font(bold=True)
    for fila in filas:
        hoja.append([float(v) if isinstance(v, Decimal) else v for v in fila])
    for col in hoja.columns:
        hoja.column_dimensions[col[0].column_letter].width = max(
            12, min(45, max(len(str(c.value or "")) for c in col) + 2))
    salida = BytesIO()
    libro.save(salida)
    respuesta = HttpResponse(
        salida.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    respuesta["Content-Disposition"] = f'attachment; filename="{nombre}.xlsx"'
    return respuesta


# --------------------------------------------------------------------- Stock

@_activo
def stock(request):
    soc = request.sociedad
    bodegas = list(soc.bodegas.filter(activa=True))
    bodega = next((b for b in bodegas if str(b.pk) == request.GET.get("bodega")), None)
    filas = inventario.resumen(soc, bodega=bodega)
    q = request.GET.get("q", "").strip().lower()
    estado = request.GET.get("estado", "")
    todas = filas
    if q:
        filas = [f for f in filas if q in f["producto"].nombre.lower() or q in f["producto"].codigo.lower()]
    if estado:
        filas = [f for f in filas if f["estado"] == estado or (estado == "reponer" and f["estado"] == "negativo")]
    if request.GET.get("formato") == "excel":
        return _excel(f"stock-{hoy():%Y-%m-%d}",
                      ["Producto", "SKU", "Unidad", "Stock", "Stock mínimo", "Costo promedio", "Valor"],
                      [[f["producto"].nombre, f["producto"].codigo, f["producto"].unidad, f["stock"],
                        f["minimo"], f["costo"], f["valor"]] for f in filas])
    return render(request, "core/inventario/stock.html", {
        "filas": filas, "bodegas": bodegas, "bodega": bodega, "q": q, "estado": estado,
        "n_productos": len(todas),
        "n_reponer": sum(1 for f in todas if f["estado"] in ("reponer", "negativo")),
        "n_sin": sum(1 for f in todas if f["estado"] == "sin"),
        "valor": sum((f["valor"] or 0) for f in todas),
        "sin_costo": sum(1 for f in todas if f["costo"] is None and f["stock"] > 0),
        "ultimos": soc.movimientos.select_related("producto")[:6],
    })


@_activo
def kardex(request, pk):
    soc = request.sociedad
    producto = get_object_or_404(Producto, pk=pk, sociedad=soc)
    movs = list(producto.movimientos.select_related("bodega", "documento").order_by("fecha", "id"))
    saldo = CERO
    for m in movs:
        saldo += m.cantidad
        m.saldo = saldo
    movs.reverse()
    por_bodega = [(b, inventario.stock(soc, bodega=b).get(producto.pk, CERO))
                  for b in soc.bodegas.filter(activa=True)]
    costo = inventario.costos_promedio(soc).get(producto.pk)
    if request.GET.get("formato") == "excel":
        return _excel(f"kardex-{producto.codigo or producto.pk}",
                      ["Fecha", "Motivo", "Bodega", "Entrada", "Salida", "Saldo", "Costo unitario",
                       "Documento o nota", "Lote", "Usuario"],
                      [[m.fecha, m.get_motivo_display(), str(m.bodega),
                        m.cantidad if m.cantidad > 0 else None, -m.cantidad if m.cantidad < 0 else None,
                        m.saldo, m.costo_unitario, str(m.documento) if m.documento else m.nota,
                        m.lote, m.usuario] for m in reversed(movs)])
    return render(request, "core/inventario/kardex.html", {
        "producto": producto, "movimientos": movs, "stock": saldo, "por_bodega": por_bodega,
        "costo": costo, "varias_bodegas": len(por_bodega) > 1})


# --------------------------------------------------------------- Movimientos

@_activo
def movimientos(request):
    soc = request.sociedad
    qs = soc.movimientos.select_related("producto", "bodega", "documento")
    g = request.GET
    if g.get("producto", "").isdigit():
        qs = qs.filter(producto_id=g["producto"])
    if g.get("motivo"):
        qs = qs.filter(motivo=g["motivo"])
    if g.get("tipo") == "entrada":
        qs = qs.filter(cantidad__gt=0)
    elif g.get("tipo") == "salida":
        qs = qs.filter(cantidad__lt=0)
    for campo, filtro in (("desde", "fecha__gte"), ("hasta", "fecha__lte")):
        try:
            qs = qs.filter(**{filtro: date.fromisoformat(g.get(campo, ""))})
        except ValueError:
            pass
    if g.get("formato") == "excel":
        return _excel(f"movimientos-inventario-{hoy():%Y-%m-%d}",
                      ["Fecha", "Producto", "SKU", "Motivo", "Bodega", "Cantidad", "Unidad",
                       "Costo unitario", "Documento o nota", "Lote", "Usuario"],
                      [[m.fecha, m.producto.nombre, m.producto.codigo, m.get_motivo_display(),
                        str(m.bodega), m.cantidad, m.producto.unidad, m.costo_unitario,
                        str(m.documento) if m.documento else m.nota, m.lote, m.usuario] for m in qs])
    return render(request, "core/inventario/movimientos.html", {
        "movimientos": qs[:500], "total": qs.count(),
        "productos": soc.productos.filter(controla_stock=True),
        "motivos": Movimiento.ENTRADAS + Movimiento.SALIDAS, "g": g})


def _formulario_movimiento(request, entrada):
    soc = request.sociedad
    inventario.bodega_principal(soc)
    productos = _productos(soc)
    form = MovimientoForm(request.POST or None, sociedad=soc, entrada=entrada,
                          initial={"bodega": inventario.bodega_principal(soc),
                                   "motivo": request.GET.get("motivo")})
    inicial = []
    if request.GET.get("producto", "").isdigit():
        inicial = [{"producto": request.GET["producto"]}]
    lineas = MovimientoLineasFormSet(request.POST or None, prefix="lineas", initial=inicial or None,
                                     form_kwargs={"productos": productos})
    if not request.POST and not inicial:
        lineas = MovimientoLineasFormSet(prefix="lineas", initial=[{}, {}, {}],
                                         form_kwargs={"productos": productos})
    if request.method == "POST" and form.is_valid() and lineas.is_valid():
        filas = [f.cleaned_data for f in lineas if f.cleaned_data.get("producto")]
        if not filas:
            messages.error(request, "Agrega al menos un producto con su cantidad.")
        else:
            datos = form.cleaned_data
            errores = []
            if not entrada and not soc.inventario_permite_negativo:
                disponible = inventario.stock(soc, bodega=datos["bodega"])
                pedido = {}
                for f in filas:
                    pedido[f["producto"]] = pedido.get(f["producto"], CERO) + f["cantidad"]
                for p, c in pedido.items():
                    hay = disponible.get(p.pk, CERO)
                    if hay < c:
                        errores.append(f"Stock insuficiente de {p}: hay {numero(hay)} y quieres sacar {numero(c)}.")
            if errores:
                for e in errores:
                    messages.error(request, e)
            else:
                with transaction.atomic():
                    for f in filas:
                        Movimiento.objects.create(
                            sociedad=soc, producto=f["producto"], bodega=datos["bodega"],
                            fecha=datos["fecha"], motivo=datos["motivo"],
                            cantidad=f["cantidad"] if entrada else -f["cantidad"],
                            costo_unitario=f.get("costo_unitario") if entrada else None,
                            lote=datos.get("lote", ""), nota=datos["nota"],
                            usuario=request.user.get_username())
                texto = dict(Movimiento.ENTRADAS + Movimiento.SALIDAS)[datos["motivo"]].lower()
                messages.success(request, f"{'Entrada' if entrada else 'Salida'} registrada ({texto}): "
                                          f"{len(filas)} producto{'s' if len(filas) > 1 else ''}.")
                return redirect("inventario_stock")
    stock_actual = inventario.stock(soc)
    return render(request, "core/inventario/movimiento_form.html", {
        "form": form, "lineas": lineas, "entrada": entrada,
        "titulo": "Registrar entrada" if entrada else "Registrar salida",
        "productos_js": {p.pk: {"unidad": p.unidad, "stock": numero(stock_actual.get(p.pk, CERO))}
                         for p in productos}})


@_activo
def entrada(request):
    return _formulario_movimiento(request, entrada=True)


@_activo
def salida(request):
    return _formulario_movimiento(request, entrada=False)


@_activo
def toma(request):
    """Toma de inventario: se ingresa lo contado y se ajustan las diferencias."""
    soc = request.sociedad
    bodegas = list(soc.bodegas.filter(activa=True)) or [inventario.bodega_principal(soc)]
    bodega = next((b for b in bodegas if str(b.pk) == (request.POST.get("bodega") or request.GET.get("bodega"))),
                  inventario.bodega_principal(soc))
    actual = inventario.stock(soc, bodega=bodega)
    productos = list(_productos(soc))
    lector = NumeroCL(required=False, max_digits=14, decimal_places=3)
    errores = {}
    if request.method == "POST":
        fecha = hoy()
        try:
            fecha = min(date.fromisoformat(request.POST.get("fecha", "")), hoy())
        except ValueError:
            pass
        nota = request.POST.get("nota", "").strip()[:200] or "Toma de inventario"
        ajustes = []
        for p in productos:
            texto = request.POST.get(f"contado_{p.pk}", "").strip()
            if not texto:
                continue
            try:
                contado = lector.clean(texto)
            except Exception:
                errores[p.pk] = "Número no válido"
                continue
            if contado < 0:
                errores[p.pk] = "No puede ser negativo"
                continue
            diferencia = contado - actual.get(p.pk, CERO)
            if diferencia:
                ajustes.append((p, diferencia))
        if not errores:
            with transaction.atomic():
                for p, dif in ajustes:
                    Movimiento.objects.create(
                        sociedad=soc, producto=p, bodega=bodega, fecha=fecha,
                        motivo="ajuste_mas" if dif > 0 else "ajuste_menos", cantidad=dif,
                        nota=nota, usuario=request.user.get_username())
            if ajustes:
                messages.success(request, f"Toma de inventario guardada en {bodega}: "
                                          f"{len(ajustes)} ajuste{'s' if len(ajustes) > 1 else ''}.")
            else:
                messages.info(request, "Todo cuadra: lo contado coincide con el sistema; no hubo ajustes.")
            return redirect("inventario_stock")
        messages.error(request, "Revisa las cantidades marcadas.")
    filas = [{"producto": p, "sistema": actual.get(p.pk, CERO),
              "contado": request.POST.get(f"contado_{p.pk}", ""), "error": errores.get(p.pk)}
             for p in productos]
    return render(request, "core/inventario/toma.html", {
        "filas": filas, "bodegas": bodegas, "bodega": bodega, "hoy": hoy()})


@_activo
def traslado(request):
    soc = request.sociedad
    if soc.bodegas.filter(activa=True).count() < 2:
        messages.info(request, "Para trasladar necesitas al menos dos bodegas activas. Agrégalas aquí.")
        return redirect("inventario_config")
    productos = _productos(soc)
    form = TrasladoForm(request.POST or None, sociedad=soc)
    lineas = MovimientoLineasFormSet(request.POST or None, prefix="lineas",
                                     initial=None if request.POST else [{}, {}, {}],
                                     form_kwargs={"productos": productos})
    if request.method == "POST" and form.is_valid() and lineas.is_valid():
        filas = [f.cleaned_data for f in lineas if f.cleaned_data.get("producto")]
        d = form.cleaned_data
        if not filas:
            messages.error(request, "Agrega al menos un producto con su cantidad.")
        else:
            nota = d["nota"] or f"Traslado {d['origen']} → {d['destino']}"
            with transaction.atomic():
                for f in filas:
                    comun = dict(sociedad=soc, producto=f["producto"], fecha=d["fecha"], nota=nota,
                                 usuario=request.user.get_username())
                    Movimiento.objects.create(bodega=d["origen"], motivo="traspaso_out",
                                              cantidad=-f["cantidad"], **comun)
                    Movimiento.objects.create(bodega=d["destino"], motivo="traspaso_in",
                                              cantidad=f["cantidad"], **comun)
            messages.success(request, f"Traslado registrado de {d['origen']} a {d['destino']}.")
            return redirect("inventario_stock")
    return render(request, "core/inventario/movimiento_form.html", {
        "form": form, "lineas": lineas, "entrada": False, "traslado": True,
        "titulo": "Trasladar entre bodegas",
        "productos_js": {p.pk: {"unidad": p.unidad, "stock": ""} for p in productos}})


@require_POST
@_activo
def movimiento_eliminar(request, pk):
    mov = get_object_or_404(Movimiento, pk=pk, sociedad=request.sociedad)
    if mov.documento_id:
        messages.error(request, "Este movimiento viene de un documento: se corrige desde el documento.")
    else:
        mov.delete()
        messages.success(request, f"Movimiento eliminado: {mov.get_motivo_display().lower()} de "
                                  f"{numero(abs(mov.cantidad))} {mov.producto.unidad} de {mov.producto}.")
    volver = request.POST.get("volver", "")
    return redirect(volver if volver.startswith("/") and not volver.startswith("//") else "inventario_movimientos")


# ------------------------------------------------------------- Configuración

@con_sociedad
def config(request):
    soc = request.sociedad
    form = InventarioConfigForm(request.POST or None, instance=soc, prefix="cfg")
    if request.method == "POST" and form.is_valid():
        estaba = Bodega.objects.filter(sociedad=soc).exists()
        form.save()
        if soc.inventario_activo:
            inventario.bodega_principal(soc)
            messages.success(request, "Configuración de inventario guardada." if estaba else
                             "Inventario activado. Carga el stock inicial con «Registrar entrada» "
                             "y el motivo «Stock inicial».")
        else:
            messages.success(request, "Inventario desactivado. Los movimientos se conservan por si lo vuelves a activar.")
        return redirect("inventario_config")
    return render(request, "core/inventario/config.html", {
        "form": form, "bodegas": soc.bodegas.all(), "bodega_form": BodegaForm(prefix="bod"),
        "sin_control": soc.productos.filter(controla_stock=False, activo=True)})


@require_POST
@con_sociedad
def bodega_guardar(request, pk=None):
    soc = request.sociedad
    bodega = get_object_or_404(Bodega, pk=pk, sociedad=soc) if pk else Bodega(sociedad=soc)
    form = BodegaForm(request.POST, instance=bodega, prefix="bod")
    if form.is_valid():
        with transaction.atomic():
            b = form.save(commit=False)
            if b.principal and not b.activa:
                b.activa = True   # la principal recibe las ventas: no puede quedar inactiva
            b.save()
            if b.principal:
                soc.bodegas.exclude(pk=b.pk).update(principal=False)
            elif not soc.bodegas.filter(principal=True).exists():
                b.principal = True
                b.activa = True
                b.save()
        messages.success(request, f"Bodega «{b}» guardada.")
    else:
        messages.error(request, "Indica el nombre de la bodega.")
    return redirect("inventario_config")
