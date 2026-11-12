from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path

from django.views.generic import RedirectView, TemplateView

from core.forms import IngresoForm
from core.views import NuevaClaveView, RecuperarView

urlpatterns = [
    path("cuenta/login/", LoginView.as_view(authentication_form=IngresoForm), name="login"),
    # El cambio de clave está en «Mi perfil».
    path("cuenta/password_change/", RedirectView.as_view(url="/perfil/#clave"), name="password_change"),
    path("cuenta/recuperar/", RecuperarView.as_view(), name="password_reset"),
    path("cuenta/recuperar/enviado/", TemplateView.as_view(
        template_name="registration/recuperar_enviado.html"), name="password_reset_done"),
    path("cuenta/recuperar/<uidb64>/<token>/", NuevaClaveView.as_view(),
         name="password_reset_confirm"),
    path("cuenta/logout/", LogoutView.as_view(), name="logout"),
    path("", include("core.urls")),
]
