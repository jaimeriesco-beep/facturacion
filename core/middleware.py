from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse


class DosPasosMiddleware:
    """Tras la clave, exige el código de la app de autenticación antes de dejar pasar."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (settings.EXIGIR_DOS_PASOS and request.user.is_authenticated
                and not request.session.get("dos_pasos")):
            libres = (reverse("dos_pasos"), reverse("logout"), "/" + settings.STATIC_URL)
            if not request.path.startswith(libres):
                return redirect("dos_pasos")
        return self.get_response(request)
