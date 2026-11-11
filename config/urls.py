from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.urls import include, path, reverse_lazy

from django.views.generic import TemplateView

from core.forms import IngresoForm
from core.views import NuevaClaveView, RecuperarView

urlpatterns = [
    path("cuenta/login/", LoginView.as_view(authentication_form=IngresoForm), name="login"),
    path("cuenta/password_change/", PasswordChangeView.as_view(
        template_name="core/clave.html", success_url=reverse_lazy("inicio")),
        name="password_change"),
    path("cuenta/recuperar/", RecuperarView.as_view(), name="password_reset"),
    path("cuenta/recuperar/enviado/", TemplateView.as_view(
        template_name="registration/recuperar_enviado.html"), name="password_reset_done"),
    path("cuenta/recuperar/<uidb64>/<token>/", NuevaClaveView.as_view(),
         name="password_reset_confirm"),
    path("cuenta/logout/", LogoutView.as_view(), name="logout"),
    path("", include("core.urls")),
]
