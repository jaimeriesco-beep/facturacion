"""Utilidades: RUT, redondeo y nombres de meses."""
import re
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def redondear(valor):
    """Redondea a pesos enteros (medio hacia arriba, como el SII)."""
    return int(Decimal(valor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def digito_verificador(numero):
    suma, factor = 0, 2
    for d in reversed(str(numero)):
        suma += int(d) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - suma % 11
    return {11: "0", 10: "K"}.get(resto, str(resto))


def normalizar_rut(rut):
    """Devuelve el RUT como '77552628-9' o lanza ValidationError."""
    limpio = re.sub(r"[.\s-]", "", str(rut or "")).upper()
    if not re.fullmatch(r"\d{1,8}[\dK]", limpio):
        raise ValidationError("RUT con formato inválido.")
    numero, dv = limpio[:-1], limpio[-1]
    if digito_verificador(numero) != dv:
        raise ValidationError("El dígito verificador del RUT no corresponde.")
    return f"{int(numero)}-{dv}"


def formatear_rut(rut):
    """'77552628-9' -> '77.552.628-9'."""
    try:
        numero, dv = str(rut).split("-")
        return f"{int(numero):,}".replace(",", ".") + "-" + dv
    except (ValueError, AttributeError):
        return rut or ""


def formatear_clp(valor):
    try:
        return "$" + f"{int(valor):,}".replace(",", ".")
    except (TypeError, ValueError):
        return ""


def sin_ceros(valor):
    """Decimal sin ceros sobrantes: 2.000 -> '2' ; 125000.50 -> '125000.5'."""
    return format(Decimal(valor).normalize(), "f")
