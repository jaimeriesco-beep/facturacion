"""Utilidades: RUT, redondeo y nombres de meses."""
import csv
import re
from functools import lru_cache
from pathlib import Path
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


@lru_cache(maxsize=1)
def actividades_sii():
    """Listado de actividades económicas del SII: lista de (código, descripción)."""
    ruta = Path(__file__).parent / "recursos" / "actividades_sii.csv"
    with ruta.open(encoding="utf-8", newline="") as archivo:
        return [(f["codigo"], f["descripcion"]) for f in csv.DictReader(archivo, delimiter=";")]


def redondear_arriba(valor):
    """Redondea a pesos enteros hacia arriba: 42,3 -> 43 ; 42 -> 42."""
    return int(Decimal(valor).quantize(Decimal("1"), rounding=ROUND_CEILING))


def decimal_cl(valor):
    """Número para editar, con coma decimal y sin separador de miles: 14.50 -> '14,5'."""
    return sin_ceros(valor).replace(".", ",")


def leer_decimal_cl(texto):
    """Lee un número escrito a la chilena: coma decimal y punto opcional para miles.

    '14,5' -> 14.5 ; '1.250' -> 1250 ; '1.250,75' -> 1250.75. Un punto que no separa
    miles ('14.5') se rechaza en vez de adivinar.
    """
    t = re.sub(r"\s", "", str(texto or ""))
    if not t:
        return None
    if "." in t:
        entero = t.split(",")[0].lstrip("-")
        grupos = entero.split(".")
        if not (re.fullmatch(r"\d{1,3}", grupos[0]) and all(re.fullmatch(r"\d{3}", g) for g in grupos[1:])):
            raise ValueError("Usa coma para los decimales, por ejemplo 14,5.")
        t = t.replace(".", "")
    if t.count(",") > 1 or not re.fullmatch(r"-?\d*,?\d*", t) or t in ("-", ",", "-,"):
        raise ValueError("Escribe un número válido, por ejemplo 14,5.")
    return Decimal(t.replace(",", "."))


def redondear(valor):
    """Redondea a pesos enteros según el art. 21 de la Ley 18.267: menos de 50 centavos
    se desprecia; 50 centavos o más sube al entero siguiente."""
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
        return "$" + f"{int(redondear(valor)):,}".replace(",", ".")
    except (TypeError, ValueError, ArithmeticError):
        return ""


def sin_ceros(valor):
    """Decimal sin ceros sobrantes: 2.000 -> '2' ; 125000.50 -> '125000.5'."""
    return format(Decimal(valor).normalize(), "f")
