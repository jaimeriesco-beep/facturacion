"""Paginación de listados (50 por página)."""
from django.core.paginator import Paginator

POR_PAGINA = 50


def paginar(request, filas, por_pagina=POR_PAGINA):
    """Devuelve (página, contexto). La plantilla core/_paginas.html dibuja la navegación."""
    pagina = Paginator(filas, por_pagina).get_page(request.GET.get("pagina"))
    resto = request.GET.copy()
    resto.pop("pagina", None)
    return pagina, {"pagina": pagina, "qs_sin_pagina": resto.urlencode()}


def volver_seguro(request, prefijo, defecto):
    """Dirección del listado desde el que se abrió un detalle (con sus filtros y página).

    Solo acepta rutas internas que empiezan con `prefijo`, para no redirigir fuera de la app.
    """
    destino = request.POST.get("volver") or request.GET.get("volver") or ""
    if destino.startswith(prefijo) and not destino.startswith("//") and "\\" not in destino:
        return destino
    return defecto
