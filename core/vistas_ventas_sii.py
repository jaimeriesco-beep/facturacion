"""Importar ventas desde el Registro de Compras y Ventas del SII (módulo opcional)."""
from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render

from . import ventas_sii
from .compras import ErrorRCV
from .views import con_sociedad
from .vistas_compras import VariosArchivosField, _nombre_periodo


class ImportarVentasForm(forms.Form):
    archivos = VariosArchivosField(label="Archivos del RCV de ventas (CSV)")


@con_sociedad
def importar(request):
    soc = request.sociedad
    if request.method == "POST" and request.POST.get("accion") in ("activar", "desactivar"):
        if not request.user.is_superuser:
            messages.error(request, "Solo un administrador puede activar o desactivar este módulo.")
        else:
            soc.ventas_sii_activo = request.POST["accion"] == "activar"
            soc.save(update_fields=["ventas_sii_activo"])
            messages.success(request, "Importación de ventas desde el SII " +
                             ("activada." if soc.ventas_sii_activo else "desactivada. Lo ya importado se mantiene."))
        return redirect("ventas_importar")
    if not soc.ventas_sii_activo:
        return render(request, "core/ventas_importar.html", {"inactivo": True})
    form = ImportarVentasForm(request.POST or None, request.FILES or None)
    resultados = []
    if request.method == "POST" and form.is_valid():
        for archivo in form.cleaned_data["archivos"]:
            if archivo.size > 5 * 1024 * 1024:
                resultados.append({"archivo": archivo.name, "error": "Pesa más de 5 MB."})
                continue
            try:
                r = ventas_sii.importar(soc, archivo.read(), archivo.name)
                r["archivo"] = archivo.name
                r["periodos"] = [_nombre_periodo(p) for p in r["periodos"]]
                r["repetidas"] = r["ya_existian"] + r["en_papelera"]
                resultados.append(r)
            except ErrorRCV as e:
                resultados.append({"archivo": archivo.name, "error": str(e)})
        nuevas = sum(r.get("nuevas", 0) for r in resultados)
        if nuevas:
            messages.success(request, f"Se importaron {nuevas} documento{'s' if nuevas > 1 else ''} de venta.")
    return render(request, "core/ventas_importar.html", {"form": form, "resultados": resultados})
