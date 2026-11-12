from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        from django.contrib.auth.signals import user_logged_in

        from . import inventario  # noqa: F401  (registra la señal que mueve el stock)

        def al_ingresar(sender, request, user, **kwargs):
            from .models import Perfil
            perfil = Perfil.objects.filter(usuario=user).select_related("sociedad_inicial").first()
            if perfil and perfil.sociedad_inicial and perfil.sociedad_inicial.activa:
                request.session["sociedad_id"] = perfil.sociedad_inicial.pk
        user_logged_in.connect(al_ingresar, dispatch_uid="sociedad_inicial")
