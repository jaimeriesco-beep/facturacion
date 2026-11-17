"""Pantallas del módulo Banco: cartolas, categorías y conciliación."""
import re
from urllib.parse import quote
from datetime import date

from django import forms
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import banco
from .paginas import paginar, volver_seguro
from .models import (GRUPOS_BANCO, CategoriaBanco, Compra, CuentaBancaria, Documento, MovimientoBancario,
                     ReglaBanco, categorias_banco_de, hoy)
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
    mes = g.get("mes", "")
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
    elif g.get("categoria") == "sin":
        qs = qs.filter(categoria__isnull=True, ignorado=False)
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
    filas.reverse()                                    # lo más reciente primero
    pagina, ctx_paginas = paginar(request, filas)
    return render(request, "core/banco/movimientos.html", {
        "cuentas": cuentas, "cuenta": cuenta, "movimientos": list(pagina), **ctx_paginas, "categorias": categorias,
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
        self.fields["archivos"].widget.attrs["accept"] = ".xls,.xlsx,.csv,.txt,.pdf"


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
    volver = volver_seguro(request, reverse("banco"),
                           f"{reverse('banco')}?cuenta={m.cuenta_id}&mes={m.fecha:%Y-%m}")
    al_listado = f"{volver}#mov-{pk}"
    detalle = f"{reverse('banco_movimiento', args=[pk])}?volver={quote(volver)}"
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
                return redirect(al_listado)
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
                messages.success(request, f"Categoría guardada: {m.descripcion[:60]} → {m.categoria or 'sin categoría'}.")
            return redirect(al_listado)
        elif que == "ignorar":
            m.ignorado = not m.ignorado
            m.save(update_fields=["ignorado"])
            return redirect(al_listado)
        return redirect(detalle)

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
        "regla_sugerida": banco.texto_regla(m.descripcion), "siguiente": siguiente, "volver": volver})


# --------------------------------------------------- Categorías, reglas y cuentas

class ReglaForm(forms.ModelForm):
    class Meta:
        model = ReglaBanco
        fields = ["texto", "categoria", "solo", "monto_min", "monto_max", "cuenta", "prioridad", "nota", "activa"]
        labels = {"categoria": "Asignar la categoría", "nota": "Nota interna", "activa": "Regla activa"}
        widgets = {"texto": forms.TextInput(attrs={"placeholder": "Ej.: TESORERIA, TGR"})}

    def __init__(self, *args, sociedad, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["categoria"].queryset = categorias_banco_de(sociedad)
        self.fields["categoria"].empty_label = None
        self.fields["cuenta"].queryset = sociedad.cuentas.all()
        self.fields["cuenta"].empty_label = "Cualquier cuenta"
        self.fields["monto_min"].help_text = "Opcional. En pesos, sin puntos."
        self.fields["monto_max"].help_text = "Opcional."

    def clean_texto(self):
        palabras = [p.strip() for p in self.cleaned_data["texto"].split(",") if p.strip()]
        if not palabras or any(len(p) < 3 for p in palabras):
            raise forms.ValidationError("Cada palabra debe tener al menos 3 letras.")
        return ", ".join(palabras)

    def clean(self):
        datos = super().clean()
        a, b = datos.get("monto_min"), datos.get("monto_max")
        if a is not None and b is not None and a > b:
            self.add_error("monto_max", "Debe ser mayor que el monto desde.")
        return datos


def _pestanas(activa):
    return [{"clave": c, "titulo": t, "url": reverse(u), "activa": c == activa} for c, t, u in (
        ("categorias", "Categorías", "banco_configuracion"), ("reglas", "Reglas automáticas", "banco_reglas"),
        ("resumen", "Resumen por categoría", "banco_resumen"), ("cuentas", "Cuentas", "banco_cuentas"))]


@con_sociedad
def configuracion(request):
    """Categorías: crear, editar, agrupar, eliminar o fusionar."""
    soc = request.sociedad
    categorias_banco_de(soc)
    if request.method == "POST":
        nombre = request.POST.get("nombre", "").strip()[:60]
        pk = request.POST.get("pk", "")
        tipo = request.POST.get("tipo") if request.POST.get("tipo") in dict(CategoriaBanco.TIPOS) else "egreso"
        grupo = request.POST.get("grupo", "").strip()[:60]
        if not nombre:
            messages.error(request, "Indica el nombre.")
        elif soc.categorias_banco.filter(nombre__iexact=nombre).exclude(pk=pk or None).exists():
            messages.error(request, f"Ya existe una categoría «{nombre}».")
        elif pk.isdigit():
            c = get_object_or_404(CategoriaBanco, pk=pk, sociedad=soc)
            c.nombre, c.tipo, c.grupo, c.activa = nombre, tipo, grupo, bool(request.POST.get("activa"))
            c.save()
            messages.success(request, f"Categoría «{c}» guardada.")
        else:
            CategoriaBanco.objects.create(sociedad=soc, nombre=nombre, tipo=tipo, grupo=grupo)
            messages.success(request, f"Categoría «{nombre}» creada.")
        return redirect("banco_configuracion")
    anio = hoy().year
    categorias = list(soc.categorias_banco.annotate(
        n=Count("movimientos"),
        total_anio=Sum("movimientos__monto", filter=Q(movimientos__fecha__year=anio), default=0)))
    n_reglas = dict(soc.reglas_banco.values_list("categoria").annotate(n=Count("id")))
    for c in categorias:
        c.n_reglas = n_reglas.get(c.pk, 0)
        c.total_anio = abs(c.total_anio)
    grupos = {}
    for c in categorias:
        grupos.setdefault(c.grupo or "Sin grupo", []).append(c)
    orden = {g: i for i, g in enumerate(GRUPOS_BANCO)}
    return render(request, "core/banco/categorias.html", {
        "pestanas": _pestanas("categorias"), "anio": anio,
        "grupos": sorted(grupos.items(), key=lambda g: (orden.get(g[0], 98), g[0])),
        "tipos": CategoriaBanco.TIPOS, "grupos_sugeridos": GRUPOS_BANCO,
        "categorias": [c for c in categorias if c.activa]})


@require_POST
@con_sociedad
def categoria_eliminar(request, pk):
    """Elimina una categoría. Si tiene movimientos o reglas, primero los pasa a otra (fusionar)."""
    soc = request.sociedad
    c = get_object_or_404(CategoriaBanco, pk=pk, sociedad=soc)
    destino_id = request.POST.get("destino", "")
    destino = soc.categorias_banco.filter(pk=destino_id).exclude(pk=c.pk).first() if destino_id.isdigit() else None
    usados = c.movimientos.count() + c.reglas.count()
    if usados and not destino:
        messages.error(request, f"«{c}» tiene movimientos o reglas: elige a qué categoría pasarlos.")
        return redirect("banco_configuracion")
    with transaction.atomic():
        n = c.movimientos.update(categoria=destino) if destino else 0
        if destino:
            c.reglas.update(categoria=destino)
        c.delete()
    messages.success(request, f"Categoría «{c}» eliminada" + (f"; {n} movimiento{'s' if n != 1 else ''} pasaron a «{destino}»." if destino else "."))
    return redirect("banco_configuracion")


@con_sociedad
def reglas(request):
    soc = request.sociedad
    categorias_banco_de(soc)
    if request.method == "POST":
        que = request.POST.get("accion")
        if que == "aplicar":
            pendientes = MovimientoBancario.objects.filter(cuenta__sociedad=soc, categoria__isnull=True)
            n = banco.aplicar_reglas(soc, list(pendientes))
            messages.success(request, f"Reglas aplicadas: {n} movimiento{'s' if n != 1 else ''} categorizado{'s' if n != 1 else ''}.")
        elif que in ("activar", "borrar"):
            regla = get_object_or_404(ReglaBanco, pk=request.POST.get("pk"), sociedad=soc)
            if que == "borrar":
                regla.delete()
                messages.success(request, "Regla eliminada. Los movimientos ya categorizados no cambian.")
            else:
                regla.activa = not regla.activa
                regla.save(update_fields=["activa"])
        return redirect("banco_reglas")
    lista = list(soc.reglas_banco.select_related("categoria", "cuenta"))
    sin_categoria = MovimientoBancario.objects.filter(cuenta__sociedad=soc, categoria__isnull=True,
                                                      ignorado=False, conciliaciones__isnull=True).count()
    return render(request, "core/banco/reglas.html", {
        "pestanas": _pestanas("reglas"), "reglas": lista, "sin_categoria": sin_categoria,
        "sugeridas": banco.sugerir_reglas(soc)})


@con_sociedad
def regla(request, pk=None):
    """Crear o editar una regla, con vista previa de los movimientos a los que se aplica."""
    soc = request.sociedad
    instancia = get_object_or_404(ReglaBanco, pk=pk, sociedad=soc) if pk else ReglaBanco(sociedad=soc)
    inicial = {}
    if not pk:
        inicial = {"texto": request.GET.get("texto", ""), "solo": request.GET.get("solo", "")}
        if request.GET.get("categoria", "").isdigit():
            inicial["categoria"] = request.GET["categoria"]
    form = ReglaForm(request.POST or None, instance=instancia, sociedad=soc, initial=inicial)
    vista = None
    if request.method == "POST" and form.is_valid():
        regla_obj = form.save(commit=False)
        if request.POST.get("accion") == "probar":
            vista = banco.coincidencias(regla_obj, soc)
        else:
            regla_obj.save()
            n = 0
            if request.POST.get("aplicar"):
                pendientes = [m for m in banco.coincidencias(regla_obj, soc) if not m.categoria_id]
                n = banco.aplicar_reglas(soc, pendientes, reglas=[regla_obj])
            if request.POST.get("recategorizar"):
                ids = [m.pk for m in banco.coincidencias(regla_obj, soc)
                       if m.categoria_id and m.categoria_id != regla_obj.categoria_id and not m.conciliaciones.exists()]
                n += MovimientoBancario.objects.filter(pk__in=ids).update(categoria=regla_obj.categoria)
            messages.success(request, f"Regla guardada. Se aplicó a {n} movimiento{'s' if n != 1 else ''}.")
            return redirect("banco_reglas")
    elif pk and request.method == "GET":
        vista = banco.coincidencias(instancia, soc)
    resumen_vista = None
    if vista is not None:
        cat_id = form.cleaned_data["categoria"].pk if form.is_bound and form.is_valid() else instancia.categoria_id
        resumen_vista = {"total": len(vista), "sin": sum(1 for m in vista if not m.categoria_id),
                         "otra": sum(1 for m in vista if m.categoria_id and m.categoria_id != cat_id),
                         "suma": sum(abs(m.monto) for m in vista)}
    return render(request, "core/banco/regla.html", {
        "pestanas": _pestanas("reglas"), "form": form, "regla": instancia if pk else None,
        "vista": vista[:30] if vista is not None else None, "resumen_vista": resumen_vista})


@con_sociedad
def resumen(request):
    """Ingresos y egresos por categoría en los últimos meses (flujo de caja por categoría)."""
    soc = request.sociedad
    try:
        n_meses = max(3, min(int(request.GET.get("meses", 6)), 24))
    except ValueError:
        n_meses = 6
    hoy_ = hoy()
    meses = []
    anio, mes = hoy_.year, hoy_.month
    for _ in range(n_meses):
        meses.append((anio, mes))
        anio, mes = (anio - 1, 12) if mes == 1 else (anio, mes - 1)
    meses.reverse()
    desde = date(*meses[0], 1)
    cuenta = request.GET.get("cuenta", "")
    qs = MovimientoBancario.objects.filter(cuenta__sociedad=soc, fecha__gte=desde, ignorado=False)
    if cuenta.isdigit():
        qs = qs.filter(cuenta_id=cuenta)
    tabla = {}
    for m in qs.select_related("categoria").only("fecha", "monto", "categoria__nombre", "categoria__grupo"):
        tipo = "ingresos" if m.monto > 0 else "egresos"
        cat = m.categoria
        clave = (tipo, cat.grupo if cat and cat.grupo else ("Sin categoría" if not cat else "Sin grupo"),
                 cat.nombre if cat else "Sin categoría", cat.pk if cat else None)
        fila = tabla.setdefault(clave, [0] * n_meses)
        fila[meses.index((m.fecha.year, m.fecha.month))] += abs(m.monto)
    secciones = []
    for tipo in ("ingresos", "egresos"):
        grupos = {}
        for (t, grupo, nombre, pk), valores in tabla.items():
            if t == tipo:
                grupos.setdefault(grupo, []).append({"nombre": nombre, "pk": pk, "valores": valores, "total": sum(valores)})
        orden = {g: i for i, g in enumerate(GRUPOS_BANCO)}
        lista = []
        for grupo, filas in sorted(grupos.items(), key=lambda g: (g[0] == "Sin categoría", orden.get(g[0], 98), g[0])):
            filas.sort(key=lambda f: -f["total"])
            sub = [sum(f["valores"][i] for f in filas) for i in range(n_meses)]
            lista.append({"grupo": grupo, "filas": filas, "subtotal": sub, "total": sum(sub)})
        totales = [sum(g["subtotal"][i] for g in lista) for i in range(n_meses)]
        secciones.append({"tipo": tipo, "grupos": lista, "totales": totales, "total": sum(totales)})
    neto = [secciones[0]["totales"][i] - secciones[1]["totales"][i] for i in range(n_meses)]
    claves = [f"{a}-{m:02d}" for a, m in meses]
    for sec in secciones:
        for g in sec["grupos"]:
            for f in g["filas"]:
                f["celdas"] = list(zip(f["valores"], [{"clave": c} for c in claves]))
    if request.GET.get("formato") == "excel":
        filas = []
        for sec in secciones:
            for g in sec["grupos"]:
                for f in g["filas"]:
                    filas.append([sec["tipo"].capitalize(), g["grupo"], f["nombre"], *f["valores"], f["total"]])
        filas.append(["Neto", "", "", *neto, sum(neto)])
        return _excel(f"banco-por-categoria-{hoy_:%Y-%m}", ["Tipo", "Grupo", "Categoría"]
                      + [f"{MESES[m - 1][:3].capitalize()} {a}" for a, m in meses] + ["Total"], filas)
    return render(request, "core/banco/resumen.html", {
        "pestanas": _pestanas("resumen"), "secciones": secciones, "neto": neto, "neto_total": sum(neto),
        "meses": [{"clave": f"{a}-{m:02d}", "nombre": f"{MESES[m - 1][:3].capitalize()} {str(a)[2:]}"} for a, m in meses],
        "n_meses": n_meses, "opciones_meses": (3, 6, 12, 24), "cuentas": soc.cuentas.all(), "cuenta": cuenta})


@con_sociedad
def cuentas(request):
    soc = request.sociedad
    if request.method == "POST":
        que = request.POST.get("accion")
        if que == "cuenta_nueva":
            numero = re.sub(r"[^0-9]", "", request.POST.get("numero", "")).lstrip("0")
            banco_nombre = request.POST.get("banco", "")
            if not numero or banco_nombre not in CuentaBancaria.BANCOS:
                messages.error(request, "Indica el banco y el número de cuenta.")
            else:
                c, creada = CuentaBancaria.objects.get_or_create(
                    sociedad=soc, banco=banco_nombre, numero=numero,
                    defaults={"alias": request.POST.get("alias", "").strip()[:60]})
                messages.success(request, f"Cuenta «{c}» {'creada' if creada else 'ya existía'}.")
        elif que == "cuenta":
            c = get_object_or_404(CuentaBancaria, pk=request.POST.get("pk"), sociedad=soc)
            c.alias = request.POST.get("alias", "").strip()[:60]
            c.activa = bool(request.POST.get("activa"))
            c.save(update_fields=["alias", "activa"])
            messages.success(request, "Cuenta guardada.")
        return redirect("banco_cuentas")
    return render(request, "core/banco/cuentas.html", {
        "pestanas": _pestanas("cuentas"), "cuentas": soc.cuentas.annotate(n=Count("movimientos")),
        "bancos": CuentaBancaria.BANCOS})
