from django import template

from ..utils import formatear_clp, formatear_rut

register = template.Library()
register.filter("clp", formatear_clp)
register.filter("rut", formatear_rut)


@register.filter
def numero(valor):
    """Cantidades y precios sin ceros sobrantes: 1.000 -> 1 ; 1250.50 -> 1.250,5"""
    try:
        texto = f"{valor:,.3f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return valor
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")
