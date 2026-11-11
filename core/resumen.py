"""Cifras del panel de Inicio: ventas, cobros y cobranza por período."""
from collections import defaultdict
from datetime import date, timedelta

from .models import COTIZACION, FACTURAS, GUIA, NOTA_CREDITO, NOTA_DEBITO, Documento, Pago, hoy
from .utils import MESES

VISTAS = {
    "dia": ("Diario", "Últimos 30 días"),
    "semana": ("Semanal", "Últimas 12 semanas"),
    "mes": ("Mensual", "Últimos 12 meses"),
    "anio": ("Anual", "Últimos 5 años"),
}
ACTUAL = {"dia": "Hoy", "semana": "Esta semana", "mes": "Este mes", "anio": "Este año"}
ANTERIOR = {"dia": "Ayer", "semana": "Semana anterior", "mes": "Mes anterior", "anio": "Año anterior"}


def _inicio_de(fecha, vista):
    if vista == "dia":
        return fecha
    if vista == "semana":
        return fecha - timedelta(days=fecha.weekday())
    if vista == "mes":
        return fecha.replace(day=1)
    return fecha.replace(month=1, day=1)


def _anterior(inicio, vista):
    if vista == "dia":
        return inicio - timedelta(days=1)
    if vista == "semana":
        return inicio - timedelta(days=7)
    if vista == "mes":
        return (inicio - timedelta(days=1)).replace(day=1)
    return inicio.replace(year=inicio.year - 1)


def _etiqueta(inicio, vista):
    if vista == "dia":
        return f"{inicio:%d/%m}"
    if vista == "semana":
        return f"{inicio:%d/%m}"
    if vista == "mes":
        return f"{MESES[inicio.month - 1][:3]} {inicio:%y}"
    return str(inicio.year)


def periodos(vista, cantidad=None):
    """Inicios de los últimos períodos, del más antiguo al actual."""
    cantidad = cantidad or {"dia": 30, "semana": 12, "mes": 12, "anio": 5}[vista]
    actual = _inicio_de(hoy(), vista)
    inicios = [actual]
    for _ in range(cantidad - 1):
        inicios.append(_anterior(inicios[-1], vista))
    return list(reversed(inicios))


def calcular(sociedad, vista):
    if vista not in VISTAS:
        vista = "mes"
    inicios = periodos(vista)
    desde = inicios[0]
    indice = {d: i for i, d in enumerate(inicios)}
    n = len(inicios)
    ventas, facturado, cobrado, documentos = [0] * n, [0] * n, [0] * n, [0] * n
    por_cliente = defaultdict(int)

    emitidos = (Documento.objects.filter(
        sociedad=sociedad, estado=Documento.EMITIDO, es_prueba=False, fecha_emision__gte=desde)
        .exclude(tipo__in=(GUIA, COTIZACION)).select_related("cliente"))
    for d in emitidos:
        i = indice.get(_inicio_de(d.fecha_emision, vista))
        if i is None:
            continue
        signo = -1 if d.tipo == NOTA_CREDITO else 1
        ventas[i] += signo * (d.neto + d.exento)      # ventas sin IVA
        facturado[i] += signo * d.total               # con IVA
        if d.tipo in FACTURAS:
            documentos[i] += 1
        por_cliente[d.cliente] += signo * (d.neto + d.exento)

    for p in Pago.objects.filter(documento__sociedad=sociedad, documento__es_prueba=False,
                                 fecha__gte=desde):
        i = indice.get(_inicio_de(p.fecha, vista))
        if i is not None:
            cobrado[i] += p.monto

    etiquetas = [_etiqueta(d, vista) for d in inicios]
    clientes = sorted(((c, v) for c, v in por_cliente.items() if v > 0), key=lambda x: -x[1])
    top = clientes[:5]
    otros = sum(v for _, v in clientes[5:])
    return {
        "vista": vista, "vistas": VISTAS, "rango": VISTAS[vista][1],
        "rotulo_actual": ACTUAL[vista], "rotulo_anterior": ANTERIOR[vista],
        "actual": ventas[-1], "anterior": ventas[-2] if n > 1 else 0,
        "docs_actual": documentos[-1],
        "promedio": round(sum(ventas[:-1]) / (n - 1)) if n > 1 else 0,
        "total_rango": sum(ventas), "cobrado_rango": sum(cobrado), "facturado_rango": sum(facturado),
        "series": {
            "etiquetas": etiquetas,
            "rangos": [_rango_texto(d, vista) for d in inicios],
            "ventas": ventas, "facturado": facturado, "cobrado": cobrado,
            "clientes": [{"nombre": c.nombre_fantasia or c.razon_social, "valor": v} for c, v in top]
                        + ([{"nombre": f"Otros ({len(clientes) - 5})", "valor": otros}] if otros else []),
        },
    }


def _rango_texto(inicio, vista):
    if vista == "dia":
        return f"{inicio:%d/%m/%Y}"
    if vista == "semana":
        return f"Semana del {inicio:%d/%m} al {inicio + timedelta(days=6):%d/%m/%Y}"
    if vista == "mes":
        return f"{MESES[inicio.month - 1].capitalize()} {inicio.year}"
    return f"Año {inicio.year}"


def antiguedad(sociedad):
    """Saldo por cobrar según días de atraso."""
    tramos = ["Por vencer", "1 a 30 días", "31 a 60 días", "61 a 90 días", "Más de 90 días"]
    montos = [0] * 5
    for d in sociedad.documentos.por_cobrar():
        dias = d.dias_vencido
        montos[0 if dias <= 0 else min((dias - 1) // 30 + 1, 4)] += d.saldo
    return {"etiquetas": tramos, "valores": montos}
