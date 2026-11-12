from .menu import construir
from .models import Sociedad


def sociedades(request):
    if not request.user.is_authenticated:
        return {}
    return {
        "sociedades": Sociedad.objects.filter(activa=True).defer("logo"),
        "sociedad_actual": getattr(request, "sociedad", None),
        "menu": construir(request) if getattr(request, "resolver_match", None) else [],
    }
