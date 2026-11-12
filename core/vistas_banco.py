"""Pantallas del módulo Banco: cartolas, categorías y conciliación."""
from datetime import date

from django import forms
from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import banco
from .models import (CategoriaBanco, Compra, CuentaBancaria, Documento, MovimientoBancario,
                     ReglaBanco, categorias_banco_de)
from .utils import MESES, formatear_clp
from .views import con_sociedad
from .vistas_compras import VariosArchivosField
from .vistas_inventario import _excel


def _volver(request, defecto="banco"):
    destino = request.POST.get("volver", "")
    return destino if destino.startswith("/") and not destino.startswith("//") else reverse(defecto)


def _mes_nombre(clave):
    anio, mes = clave.split("-")
    return f"{MESES[int(mes) - 1].capitalize()} {anio}"


def _movimientos(sociedad):
    return (MovimientoBancario.objects.filter(cuenta__sociedad=sociedad)
            .select_related("cuenta", "categoria")
            .annotate(conciliado_a=Sum("conciliaciones__monto", default=0))
            .order_by("fecha", "orden", "id"))      # como en la cartola: el saldo se lee en orden


@con_sociedad
def movimientos(request):
    soc = request.sociedad
    cuentas = list(soc.cuentas.filter(activa=True))
    if not cuentas:
        return render(request, "core/banco/vacio.html")
    g = request.GET
    cuenta = next((c for c in cuentas if str(c.pk) == g.get("cuenta")), cuentas[0])
    qs = _movimientos(soc).filter(cuenta=cuenta)
    meses = sorted({f"{f:%Y-%m}" for f in cuenta.movimientos.values_list("fecha", flat=True)}, reverse=True)
    mes = g.get("mes", meses[0] if meses else "")
    if mes and mes != "todos":
        anio, m = mes.split("-")
        qs = qs.filter(fecha__year=int(anio), fecha__month=int(m))
    if q := g.get("q", "").strip():
        qs = qs.filter(Q(descripcion__icontains=q) | Q(nota__icontains=q) | Q(documento__icontains=q))
    if g.get("tipo") == "abono":
        qs = qs.filter(monto__gt=0)
    elif g.get("tipo") == "cargo":
        qs = qs.filter(monto__lt=0)
    if g.get("categoria", "").isdigit():
        qs = qs.filter(categoria_id=g["categoria"])
    filas = list(qs)
    del_periodo = list(filas)
    estado = g.get("estado", "")
    if estado == "por_revisar":
        filas = [m for m in filas if m.estado in ("pendiente", "parcial")]
    elif estado:
        filas = [m for m in filas if m.estado == estado]
    if g.get("formato") == "excel":
        return _excel(f"banco-{cuenta.numero}-{mes or 'todo'}",
                      ["Fecha", "Descripción", "Cargo", "Abono", "Saldo", "Documento", "Categoría", "Estado",
                       "Conciliado con", "Nota"],
                      [[m.fecha, m.descripcion, -m.monto if m.monto < 0 else None, m.monto if m.monto > 0 else None,
                        m.saldo, m.documento, str(m.categoria or ""), m.estado,
                        ", ".join(str(c.documento) for c in m.conciliaciones.all()), m.nota] for m in filas])
    ultimo = cuenta.ultimo
    categorias = list(categorias_banco_de(soc))
    return render(request, "core/banco/movimientos.html", {
        "cuentas": cuentas, "cuenta": cuenta, "movimientos": filas, "categorias": categorias,
        "meses": [(m, _mes_nombre(m)) for m in meses], "mes": mes, "g": g, "ultimo": ultimo,
        "abonos": sum(m.monto for m in del_periodo if m.monto > 0),
        "cargos": -sum(m.monto for m in del_periodo if m.monto < 0),
        "por_revisar": sum(1 for m in del_periodo if m.estado in ("pendiente", "parcial")),
        "conciliados": sum(1 for m in del_periodo if m.estado == "conciliado"),
        "total": len(del_periodo),
        "por_categoria": _por_categoria(del_periodo),
    })


def _por_categoria(filas):
    ingresos, egresos = {}, {}
    for m in filas:
        if m.ignorado:
            continue
        nombre = str(m.categoria) if m.categoria_id else "Sin categoría"
        destino = ingresos if m.monto > 0 else egresos
        destino[nombre] = destino.get(nombre, 0) + abs(m.monto)
    orden = lambda d: sorted(d.items(), key=lambda x: -x[1])  # noqa: E731
    return {"ingresos": orden(ingresos), "egresos": orden(egresos)}


@require_POST
@con_sociedad
def accion(request):
    """Varios movimientos a la vez: categorizar o ignorar."""
    soc = request.sociedad
    ids = [int(i) for i in request.POST.getlist("movimientos") if i.isdigit()]
    qs = MovimientoBancario.objects.filter(cuenta__sociedad=soc, pk__in=ids)
    if not qs.exists():
        messages.error(request, "Marca al menos un movimiento.")
        return redirect(_volver(request))
    que = request.POST.get("accion")
    if que == "categoria":
        valor = request.POST.get("categoria", "")
        categoria = get_object_or_404(CategoriaBanco, pk=valor, sociedad=soc) if valor.isdigit() else None
        n = qs.update(categoria=categoria, ignorado=False)
        messages.success(request, f"{n} movimiento{'s' if n > 1 else ''} "
                                  f"{'en «' + str(categoria) + '»' if categoria else 'sin categoría'}.")
    elif que == "ignorar":
        n = qs.update(ignorado=True)
        messages.success(request, f"{n} movimiento{'s' if n > 1 else ''} marcado{'s' if n > 1 else ''} como «no requiere revisión».")
    return redirect(_volver(request))


# ------------------------------------------------------------------ Importar

class ImportarCartolaForm(forms.Form):
    archivos = VariosArchivosField(label="Cartolas")
    cuenta = forms.ModelChoiceField(label="Cuenta", queryset=CuentaBancaria.objects.none(), required=False,
                                    empty_label="Reconocerla desde la cartola")
    otro_rut = forms.BooleanField(label="Importar aunque la cartola sea de otro RUT", required=False)

    def __init__(self, *args, sociedad, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cuenta"].queryset = sociedad.cuentas.all()
        self.fields["archivos"].widget.attrs["accept"] = ".xls,.xlsx,.csv,.txt"


@con_sociedad
def importar(request):
    soc = request.sociedad
    form = ImportarCartolaForm(request.POST or None, request.FILES or None, sociedad=soc)
    resultados = []
    if request.method == "POST" and form.is_valid():
        for archivo in form.cleaned_data["archivos"]:
            if archivo.size > 10 * 1024 * 1024:
                resultados.append({"archivo": archivo.name, "error": "Pesa más de 10 MB."})
                continue
            try:
                r = banco.importar(soc, archivo.read(), archivo.name, cuenta=form.cleaned_data["cuenta"],
                                   aceptar_otro_rut=form.cleaned_data["otro_rut"])
                r["archivo"] = archivo.name
                resultados.append(r)
            except banco.ErrorCartola as e:
                resultados.append({"archivo": archivo.name, "error": str(e)})
    return render(request, "core/banco/importar.html", {"form": form, "resultados": resultados})


# ------------------------------------------------------------ Un movimiento

@con_sociedad
def movimiento(request, pk):
    soc = request.sociedad
    m = get_object_or_404(_movimientos(soc), pk=pk)
    if request.method == "POST":
        que = request.POST.get("accion")
        if que == "conciliar":
            asignaciones = []
            for clave in request.POST.getlist("doc"):
                tipo, _, ident = clave.partition("-")
                modelo = Documento if tipo == "v" else Compra
                base = soc.documentos.con_saldo() if tipo == "v" else soc.compras.con_saldo()
                doc = base.filter(pk=ident).first() if ident.isdigit() else None
                if doc is None:
                    continue
                try:
                    monto = int(request.POST.get(f"monto-{clave}", "").replace(".", "").replace("$", "").strip())
                except ValueError:
                    monto = doc.saldo
                asignaciones.append((doc, monto))
            if not asignaciones:
                messages.error(request, "Marca al menos un documento para conciliar.")
            else:
                n = banco.conciliar(m, asignaciones)
                messages.success(request, f"Movimiento conciliado con {n} documento{'s' if n > 1 else ''}; "
                                          f"{'los cobros' if m.es_abono else 'los pagos'} quedaron registrados.")
                return redirect(request.POST.get("siguiente") or reverse("banco_movimiento", args=[pk]))
        elif que == "deshacer":
            banco.deshacer(m)
            messages.success(request, "Conciliación deshecha: se eliminaron los pagos que había creado.")
        elif que == "categoria":
            valor = request.POST.get("categoria", "")
            m.categoria = get_object_or_404(CategoriaBanco, pk=valor, sociedad=soc) if valor.isdigit() else None
            m.nota = request.POST.get("nota", "")[:200]
            m.ignorado = False
            m.save(update_fields=["categoria", "nota", "ignorado"])
            texto = request.POST.get("regla", "").strip()[:80]
            if texto and m.categoria:
                ReglaBanco.objects.get_or_create(sociedad=soc, texto=texto, defaults={
                    "categoria": m.categoria, "solo": "abono" if m.es_abono else "cargo"})
                otros = MovimientoBancario.objects.filter(cuenta__sociedad=soc, categoria__isnull=True,
                                                          descripcion__icontains=texto)
                otros = otros.filter(monto__gt=0) if m.es_abono else otros.filter(monto__lt=0)
                n = otros.update(categoria=m.categoria)
                messages.success(request, f"Categoría guardada y regla creada: lo que contenga «{texto}» irá a "
                                          f"«{m.categoria}». Se aplicó a {n} movimiento{'s' if n != 1 else ''} más.")
            else:
                messages.success(request, "Categoría guardada.")
        elif que == "ignorar":
            m.ignorado = not m.ignorado
            m.save(update_fields=["ignorado"])
        return redirect("banco_movimiento", pk=pk)

    sugeridas = banco.sugerencias(m) if m.por_conciliar else []
    sugeridos_ids = {(isinstance(s["doc"], Documento), s["doc"].pk) for s in sugeridas}
    if m.es_abono:
        otros = [d for d in soc.documentos.por_cobrar().select_related("cliente").order_by("fecha_emision")
                 if (True, d.pk) not in sugeridos_ids]
    else:
        otros = [c for c in soc.compras.con_saldo().select_related("proveedor").order_by("fecha_emision")
                 if c.saldo > 0 and (False, c.pk) not in sugeridos_ids]
    siguiente = (_movimientos(soc).filter(cuenta=m.cuenta, categoria__isnull=True, ignorado=False)
                 .exclude(pk=m.pk).filter(conciliado_a=0).order_by("-fecha", "-orden").first())
    return render(request, "core/banco/movimiento.html", {
        "m": m, "sugeridas": sugeridas, "otros": otros, "categorias": categorias_banco_de(soc),
        "conciliaciones": m.conciliaciones.select_related("pago__documento", "pago_compra__compra"),
        "regla_sugerida": _texto_regla(m.descripcion), "siguiente": siguiente})


def _texto_regla(descripcion):
    """Parte estable de la descripción para crear una regla (sin números de operación)."""
    import re
    texto = re.sub(r"\s+\d[\d\s/-]*$", "", descripcion).strip()
    return texto[:60]


# --------------------------------------------------- Categorías, reglas y cuentas

@con_sociedad
def configuracion(request):
    soc = request.sociedad
    categorias_banco_de(soc)
    if request.method == "POST":
        que = request.POST.get("accion")
        if que == "categoria":
            nombre = request.POST.get("nombre", "").strip()[:60]
            pk = request.POST.get("pk", "")
            if not nombre:
                messages.error(request, "Indica el nombre.")
            elif soc.categorias_banco.filter(nombre__iexact=nombre).exclude(pk=pk or None).exists():
                messages.error(request, f"Ya existe «{nombre}».")
            elif pk.isdigit():
                c = get_object_or_404(CategoriaBanco, pk=pk, sociedad=soc)
                c.nombre, c.activa = nombre, bool(request.POST.get("activa"))
                c.tipo = request.POST.get("tipo") if request.POST.get("tipo") in dict(CategoriaBanco.TIPOS) else c.tipo
                c.save()
                messages.success(request, f"Categoría «{c}» guardada.")
            else:
                CategoriaBanco.objects.create(sociedad=soc, nombre=nombre, tipo=request.POST.get("tipo") or "ambos")
                messages.success(request, f"Categoría «{nombre}» creada.")
        elif que == "regla":
            texto = request.POST.get("texto", "").strip()[:80]
            cat = request.POST.get("categoria", "")
            if texto and cat.isdigit():
                categoria = get_object_or_404(CategoriaBanco, pk=cat, sociedad=soc)
                regla = ReglaBanco.objects.create(sociedad=soc, texto=texto, categoria=categoria,
                                                  solo=request.POST.get("solo", "")[:6])
                pendientes = MovimientoBancario.objects.filter(cuenta__sociedad=soc, categoria__isnull=True)
                n = banco.aplicar_reglas(soc, [mv for mv in pendientes if regla.aplica(mv)])
                messages.success(request, f"Regla creada. Se aplicó a {n} movimiento{'s' if n != 1 else ''}.")
            else:
                messages.error(request, "Indica el texto y la categoría de la regla.")
        elif que == "regla_borrar":
            ReglaBanco.objects.filter(sociedad=soc, pk=request.POST.get("pk")).delete()
            messages.success(request, "Regla eliminada.")
        elif que == "cuenta":
            c = get_object_or_404(CuentaBancaria, pk=request.POST.get("pk"), sociedad=soc)
            c.alias = request.POST.get("alias", "").strip()[:60]
            c.activa = bool(request.POST.get("activa"))
            c.save(update_fields=["alias", "activa"])
            messages.success(request, "Cuenta guardada.")
        return redirect("banco_configuracion")
    return render(request, "core/banco/configuracion.html", {
        "categorias": soc.categorias_banco.annotate(n=Count("movimientos")),
        "reglas": soc.reglas_banco.select_related("categoria"),
        "cuentas": soc.cuentas.annotate(n=Count("movimientos")), "tipos": CategoriaBanco.TIPOS})
