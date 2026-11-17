from .menu import construir
from .models import Sociedad


def sociedades(request):
    if not request.user.is_authenticated:
        return {}
    return {
        "sociedades": Sociedad.objects.filter(activa=True).defer("logo"),
        "sociedad_actual": getattr(request, "sociedad", None),
        "menu": construir(request) if getattr(request, "resolver_match", None) else [],
        "rayo": _rayo(request),
    }


def _rayo(request):
    sociedad = getattr(request, "sociedad", None)
    if not sociedad or not sociedad.cuentas.exists():
        return None
    from .banco import resumen_rayo
    try:
        return resumen_rayo(sociedad)
    except Exception:          # el aviso nunca debe impedir que cargue una página
        return None
