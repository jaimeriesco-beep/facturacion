from django.contrib.auth.views import LoginView, PasswordChangeView
from django.urls import include, path, reverse_lazy

from core.forms import IngresoForm

urlpatterns = [
    path("cuenta/login/", LoginView.as_view(authentication_form=IngresoForm), name="login"),
    path("cuenta/password_change/", PasswordChangeView.as_view(
        template_name="core/clave.html", success_url=reverse_lazy("inicio")),
        name="password_change"),
    path("cuenta/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
]
