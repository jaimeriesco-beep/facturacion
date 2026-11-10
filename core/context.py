from .models import Sociedad


def sociedades(request):
    if not request.user.is_authenticated:
        return {}
    return {
        "sociedades": Sociedad.objects.filter(activa=True).defer("logo"),
        "sociedad_actual": getattr(request, "sociedad", None),
    }
