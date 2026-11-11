from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


def buscar_usuario(identificador):
    """Usuario por nombre de usuario o por correo, sin distinguir mayúsculas."""
    Usuario = get_user_model()
    identificador = (identificador or "").strip()
    if not identificador:
        return None
    usuario = Usuario.objects.filter(username__iexact=identificador).first()
    if usuario is None and "@" in identificador:
        coincidencias = list(Usuario.objects.filter(email__iexact=identificador)[:2])
        if len(coincidencias) == 1:  # si dos personas comparten correo, no se adivina cuál es
            usuario = coincidencias[0]
    return usuario


class UsuarioOCorreo(ModelBackend):
    """Permite ingresar con el nombre de usuario o con el correo electrónico."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        usuario = buscar_usuario(username)
        if usuario is None:
            get_user_model()().set_password(password)  # mismo tiempo de respuesta que un usuario real
            return None
        if password and usuario.check_password(password) and self.user_can_authenticate(usuario):
            return usuario
        return None
