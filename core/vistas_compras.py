"""Pantallas del módulo de Compras."""
from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import compras as rcv
from .forms import Fecha, FormBase
from .models import (TIPOS_COMPRA, CategoriaGasto, Compra, Pago, PagoCompra, Proveedor,
                     categorias_de, hoy)
from .utils import MESES, formatear_clp, normalizar_rut
from .views import con_sociedad
from .vistas_inventario import _excel


def _volver(request, defecto="compras"):
    destino = request.POST.get("volver", "")
    return destino if destino.startswith("/") and not destino.startswith("//") else reverse(defecto)


def _nombre_periodo(periodo):
    try:
        anio, mes = periodo.split("-")
        return f"{MESES[int(mes) - 1].capitalize()} {anio}"
    except (ValueError, IndexError):
        return periodo


# ------------------------------------------------------------------- Listado

@con_sociedad
def compras(request):
    soc = request.sociedad
    categorias = list(categorias_de(soc))
    qs = soc.compras.select_related("proveedor", "categoria").con_saldo()
    periodos = sorted(soc.compras.values_list("periodo", flat=True).distinct(), reverse=True)
    g = request.GET
    periodo = g.get("periodo", periodos[0] if periodos else "")
    if periodo and periodo != "todos":
        qs = qs.filter(periodo=periodo)
    if q := g.get("q", "").strip():
        filtro = Q(proveedor__razon_social__icontains=q) | Q(proveedor__nombre_fantasia__icontains=q)
        if q.isdigit():
            filtro |= Q(folio=int(q))
        filtro |= Q(proveedor__rut__startswith=q.replace(".", "").split("-")[0]) if q[:2].isdigit() else Q()
        qs = qs.filter(filtro)
    categoria = g.get("categoria", "")
    if categoria == "sin":
        qs = qs.filter(categoria__isnull=True)
    elif categoria.isdigit():
        qs = qs.filter(categoria_id=categoria)
    if g.get("tipo") == "nc":
        qs = qs.filter(tipo__in=(60, 61))
    elif g.get("tipo") == "facturas":
        qs = qs.exclude(tipo__in=(60, 61))
    filas = list(qs)
    estado = g.get("estado", "")
    if estado:
        filas = [c for c in filas if c.estado_pago == estado or (estado == "por_pagar" and c.saldo > 0)]
    if g.get("formato") == "excel":
        return _excel(f"compras-{periodo or 'todas'}",
                      ["Período", "Fecha", "Tipo", "Folio", "RUT proveedor", "Proveedor", "Categoría",
                       "Exento", "Neto", "IVA recuperable", "IVA no recuperable", "Otros impuestos",
                       "Total", "Pagado", "Saldo", "Vence", "Estado SII"],
                      [[c.periodo, c.fecha_emision, c.get_tipo_display(), c.folio, c.proveedor.rut,
                        c.proveedor.razon_social, str(c.categoria or ""), c.signo * c.exento,
                        c.signo * c.neto, c.signo * c.iva, c.signo * c.iva_no_recuperable,
                        c.signo * c.otros_impuestos, c.signo * c.total, c.pagado, c.saldo,
                        c.fecha_vencimiento, c.get_estado_rcv_display()] for c in filas])
    return render(request, "core/compras/compras.html", {
        "compras": filas, "categorias": categorias, "periodos": [(p, _nombre_periodo(p)) for p in periodos],
        "periodo": periodo, "g": g, "medios": Pago.MEDIOS,
        "gasto": sum(c.gasto for c in filas), "iva": sum(c.signo * c.iva for c in filas),
        "por_pagar": sum(c.saldo for c in filas),
        "vencido": sum(c.saldo for c in filas if c.estado_pago == "vencida"),
        "sin_categoria": sum(1 for c in filas if not c.categoria_id),
        "por_categoria": _por_categoria(filas),
    })


def _por_categoria(filas):
    totales = {}
    for c in filas:
        nombre = str(c.categoria) if c.categoria_id else "Sin categoría"
        totales[nombre] = totales.get(nombre, 0) + c.gasto
    return sorted(((n, v) for n, v in totales.items() if v), key=lambda x: -x[1])


@require_POST
@con_sociedad
def compras_accion(request):
    """Acciones sobre varias compras marcadas: categorizar o marcar pagadas."""
    soc = request.sociedad
    ids = [int(i) for i in request.POST.getlist("compras") if i.isdigit()]
    seleccion = list(soc.compras.filter(pk__in=ids).con_saldo())
    if not seleccion:
        messages.error(request, "Marca al menos una compra.")
        return redirect(_volver(request))
    accion = request.POST.get("accion")
    if accion == "categoria":
        valor = request.POST.get("categoria", "")
        categoria = get_object_or_404(CategoriaGasto, pk=valor, sociedad=soc) if valor.isdigit() else None
        soc.compras.filter(pk__in=[c.pk for c in seleccion]).update(categoria=categoria)
        if categoria and request.POST.get("recordar"):
            Proveedor.objects.filter(pk__in={c.proveedor_id for c in seleccion}).update(categoria=categoria)
        messages.success(request, f"{len(seleccion)} compra{'s' if len(seleccion) > 1 else ''} "
                                  f"{'en' if categoria else 'sin'} categoría"
                                  f"{' «' + str(categoria) + '»' if categoria else ''}.")
    elif accion == "pagar":
        try:
            fecha = min(date.fromisoformat(request.POST.get("fecha", "")), hoy())
        except ValueError:
            fecha = hoy()
        medio = request.POST.get("medio") if request.POST.get("medio") in dict(Pago.MEDIOS) else "transferencia"
        pagadas, total = 0, 0
        with transaction.atomic():
            for c in seleccion:
                if c.saldo > 0:
                    PagoCompra.objects.create(compra=c, fecha=fecha, monto=c.saldo, medio=medio,
                                              nota=request.POST.get("nota", "")[:120])
                    pagadas += 1
                    total += c.saldo
        if pagadas:
            messages.success(request, f"{pagadas} compra{'s' if pagadas > 1 else ''} pagada"
                                      f"{'s' if pagadas > 1 else ''} el {fecha:%d/%m/%Y} por {formatear_clp(total)}.")
        else:
            messages.info(request, "Las compras marcadas no tenían saldo por pagar.")
    return redirect(_volver(request))


# ------------------------------------------------------------------ Importar

class VariosArchivos(forms.ClearableFileInput):
    allow_multiple_selected = True


class VariosArchivosField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", VariosArchivos(attrs={"accept": ".csv,text/csv"}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        uno = super().clean
        if isinstance(data, (list, tuple)):
            if not data:
                return uno(None, initial)
            return [uno(d, initial) for d in data]
        return [uno(data, initial)]


class ImportarRCVForm(forms.Form):
    archivos = VariosArchivosField(label="Archivos del RCV (CSV)")
    estado = forms.ChoiceField(
        label="Estado en el SII", required=False,
        choices=[("", "Según el nombre del archivo")] + [c for c in Compra.ESTADOS_RCV if c[0] != "manual"])


@con_sociedad
def importar(request):
    soc = request.sociedad
    form = ImportarRCVForm(request.POST or None, request.FILES or None)
    resultados = []
    if request.method == "POST" and form.is_valid():
        for archivo in form.cleaned_data["archivos"]:
            if archivo.size > 5 * 1024 * 1024:
                resultados.append({"archivo": archivo.name, "error": "Pesa más de 5 MB."})
                continue
            try:
                r = rcv.importar(soc, archivo.read(), archivo.name, estado=form.cleaned_data["estado"] or None)
                r["archivo"] = archivo.name
                r["periodos"] = [_nombre_periodo(p) for p in r["periodos"]]
                resultados.append(r)
            except rcv.ErrorRCV as e:
                resultados.append({"archivo": archivo.name, "error": str(e)})
        nuevas = sum(r.get("nuevas", 0) for r in resultados)
        if nuevas:
            messages.success(request, f"Se importaron {nuevas} compra{'s' if nuevas > 1 else ''} nueva{'s' if nuevas > 1 else ''}.")
    return render(request, "core/compras/importar.html", {
        "form": form, "resultados": resultados,
        "rut_sin_dv": soc.rut.split("-")[0] if soc.rut else ""})


# ------------------------------------------------------------- Una compra

class CompraForm(FormBase):
    class Meta:
        model = Compra
        fields = ["categoria", "fecha_vencimiento", "nota"]
        widgets = {"fecha_vencimiento": Fecha()}
        labels = {"categoria": "Categoría"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categoria"].queryset = categorias_de(self.instance.sociedad)
        self.fields["categoria"].empty_label = "Sin categoría"


class CompraManualForm(FormBase):
    rut_proveedor = forms.CharField(label="RUT del proveedor", max_length=12)
    razon_social = forms.CharField(label="Razón social del proveedor", max_length=100, required=False,
                                   help_text="Solo si es un proveedor nuevo.")

    class Meta:
        model = Compra
        fields = ["tipo", "folio", "fecha_emision", "categoria", "exento", "neto", "iva", "total",
                  "fecha_vencimiento", "nota"]
        widgets = {"fecha_emision": Fecha(), "fecha_vencimiento": Fecha()}
        labels = {"categoria": "Categoría", "tipo": "Tipo de documento", "iva": "IVA"}
        help_texts = {"total": "Si lo dejas en 0 se calcula como exento + neto + IVA."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categoria"].queryset = categorias_de(self.instance.sociedad)
        self.fields["categoria"].empty_label = "Sin categoría"
        for campo in ("exento", "neto", "iva", "total"):
            self.fields[campo].required = False
        self.order_fields(["rut_proveedor", "razon_social"] + list(self.Meta.fields))

    def clean_rut_proveedor(self):
        try:
            return normalizar_rut(self.cleaned_data["rut_proveedor"])
        except forms.ValidationError as e:
            raise forms.ValidationError(e.messages)

    def clean(self):
        datos = super().clean()
        for campo in ("exento", "neto", "iva", "total"):
            datos[campo] = datos.get(campo) or 0
            if datos[campo] < 0:
                self.add_error(campo, "No puede ser negativo.")
        if not datos["total"]:
            datos["total"] = datos["exento"] + datos["neto"] + datos["iva"]
        if not datos["total"]:
            self.add_error("total", "Indica los montos del documento.")
        rut = datos.get("rut_proveedor")
        if rut and not self.instance.sociedad.proveedores.filter(rut=rut).exists() and not datos.get("razon_social"):
            self.add_error("razon_social", "Es un proveedor nuevo: indica su razón social.")
        return datos


@con_sociedad
def compra_nueva(request):
    soc = request.sociedad
    compra = Compra(sociedad=soc, estado_rcv="manual", fecha_emision=hoy())
    form = CompraManualForm(request.POST or None, instance=compra)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        proveedor, _ = Proveedor.objects.get_or_create(
            sociedad=soc, rut=d["rut_proveedor"], defaults={"razon_social": d["razon_social"]})
        compra = form.save(commit=False)
        compra.proveedor = proveedor
        compra.origen = "manual"
        compra.periodo = f"{compra.fecha_emision:%Y-%m}"
        compra.exento, compra.neto, compra.iva, compra.total = d["exento"], d["neto"], d["iva"], d["total"]
        compra.categoria = compra.categoria or proveedor.categoria
        compra.fecha_vencimiento = compra.fecha_vencimiento or compra.fecha_emision + timedelta(days=proveedor.dias_pago)
        try:
            compra.save()
        except IntegrityError:
            form.add_error("folio", "Ya existe ese documento de este proveedor.")
        else:
            messages.success(request, f"{compra} de {proveedor} registrada.")
            return redirect("compra", pk=compra.pk)
    return render(request, "core/form.html", {"form": form, "titulo": "Registrar compra manual",
                                              "volver": reverse("compras")})


@con_sociedad
def compra(request, pk):
    soc = request.sociedad
    c = get_object_or_404(Compra.objects.select_related("proveedor", "categoria"), pk=pk, sociedad=soc)
    form = CompraForm(request.POST if request.POST.get("accion") == "datos" else None, instance=c)
    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "datos" and form.is_valid():
            form.save()
            if request.POST.get("recordar") and c.categoria:
                c.proveedor.categoria = c.categoria
                c.proveedor.save(update_fields=["categoria"])
            messages.success(request, "Compra actualizada.")
            return redirect("compra", pk=pk)
        if accion == "pago":
            try:
                monto = int(request.POST.get("monto", "").replace(".", "").replace("$", "").strip())
                fecha = min(date.fromisoformat(request.POST.get("fecha", "")), hoy()) if request.POST.get("fecha") else hoy()
            except ValueError:
                messages.error(request, "Revisa el monto y la fecha del pago.")
            else:
                if monto <= 0 or monto > c.saldo:
                    messages.error(request, f"El monto debe estar entre $1 y el saldo ({formatear_clp(c.saldo)}).")
                else:
                    medio = request.POST.get("medio") if request.POST.get("medio") in dict(Pago.MEDIOS) else "transferencia"
                    PagoCompra.objects.create(compra=c, monto=monto, fecha=fecha, medio=medio,
                                              nota=request.POST.get("nota", "")[:120])
                    messages.success(request, f"Pago de {formatear_clp(monto)} registrado.")
            return redirect("compra", pk=pk)
    return render(request, "core/compras/compra.html", {
        "c": c, "form": form, "pagos": c.pagos.all(), "medios": Pago.MEDIOS, "hoy": hoy(),
        "otras": soc.compras.filter(proveedor=c.proveedor).exclude(pk=c.pk)[:8]})


@require_POST
@con_sociedad
def compra_pago_eliminar(request, pk):
    pago = get_object_or_404(PagoCompra, pk=pk, compra__sociedad=request.sociedad)
    compra_id = pago.compra_id
    pago.delete()
    messages.success(request, "Pago eliminado.")
    return redirect("compra", pk=compra_id)


@require_POST
@con_sociedad
def compra_eliminar(request, pk):
    c = get_object_or_404(Compra, pk=pk, sociedad=request.sociedad)
    if c.origen != "manual":
        messages.error(request, "Las compras traídas del SII no se eliminan: si el documento no corresponde, "
                                "recházalo o reclámalo en el Registro de Compras del SII.")
        return redirect("compra", pk=pk)
    c.delete()
    messages.success(request, f"{c} eliminada.")
    return redirect("compras")


# --------------------------------------------------------------- Proveedores

class ProveedorForm(FormBase):
    class Meta:
        model = Proveedor
        fields = ["razon_social", "nombre_fantasia", "categoria", "dias_pago", "email", "telefono", "banco"]
        labels = {"categoria": "Categoría habitual"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categoria"].queryset = categorias_de(self.instance.sociedad)
        self.fields["categoria"].empty_label = "Sin categoría habitual"


@con_sociedad
def proveedores(request):
    soc = request.sociedad
    lista = list(soc.proveedores.select_related("categoria").annotate(
        comprado=Sum("compras__total", filter=~Q(compras__tipo__in=(60, 61))),
        n=Count("compras")))
    saldos = {}
    for c in soc.compras.con_saldo().select_related(None):
        saldos[c.proveedor_id] = saldos.get(c.proveedor_id, 0) + c.saldo
    for p in lista:
        p.saldo = saldos.get(p.pk, 0)
    q = request.GET.get("q", "").strip().lower()
    if q:
        lista = [p for p in lista if q in p.razon_social.lower() or q in p.nombre_fantasia.lower() or q in p.rut]
    return render(request, "core/compras/proveedores.html", {"proveedores": lista, "q": q})


@con_sociedad
def proveedor(request, pk):
    soc = request.sociedad
    p = get_object_or_404(Proveedor, pk=pk, sociedad=soc)
    form = ProveedorForm(request.POST or None, instance=p)
    if request.method == "POST" and form.is_valid():
        form.save()
        if request.POST.get("aplicar") and p.categoria:
            n = p.compras.filter(categoria__isnull=True).update(categoria=p.categoria)
            messages.success(request, f"Proveedor guardado. {n} compra{'s' if n != 1 else ''} sin categoría "
                                      f"quedaron en «{p.categoria}».")
        else:
            messages.success(request, "Proveedor guardado.")
        return redirect("proveedor", pk=pk)
    compras_p = p.compras.select_related("categoria").con_saldo()[:50]
    return render(request, "core/compras/proveedor.html", {"p": p, "form": form, "compras": compras_p})


# ---------------------------------------------------------------- Categorías

@con_sociedad
def categorias(request):
    soc = request.sociedad
    categorias_de(soc)
    if request.method == "POST":
        nombre = request.POST.get("nombre", "").strip()[:60]
        pk = request.POST.get("pk", "")
        if not nombre:
            messages.error(request, "Indica el nombre de la categoría.")
        elif soc.categorias_gasto.filter(nombre__iexact=nombre).exclude(pk=pk or None).exists():
            messages.error(request, f"Ya existe la categoría «{nombre}».")
        elif pk.isdigit():
            cat = get_object_or_404(CategoriaGasto, pk=pk, sociedad=soc)
            cat.nombre, cat.activa = nombre, bool(request.POST.get("activa"))
            cat.save()
            messages.success(request, f"Categoría «{cat}» guardada.")
        else:
            CategoriaGasto.objects.create(sociedad=soc, nombre=nombre)
            messages.success(request, f"Categoría «{nombre}» creada.")
        return redirect("compras_categorias")
    lista = soc.categorias_gasto.annotate(n=Count("compras"))
    return render(request, "core/compras/categorias.html", {"categorias": lista})
