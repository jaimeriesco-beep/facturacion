from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from .models import (Cliente, Documento, Linea, Pago, Producto, Recurrente,
                     RecurrenteLinea, Sociedad)
from .utils import formatear_rut, sin_ceros


def casilla_rut(campo):
    """Casilla de RUT: se formatea sola como 12.345.678-9 (ver static/core/rut.js)."""
    campo.widget.attrs.update({
        "data-rut": "1", "placeholder": "12.345.678-9", "maxlength": 12,
        "autocomplete": "off", "pattern": r"\d{1,2}\.\d{3}\.\d{3}-[\dkK]",
        "title": "Formato 12.345.678-9"})


def etiqueta_cliente(cliente):
    """Texto del cliente en los selectores: con RUT, para poder buscarlo por él."""
    return f"{cliente} — {cliente.rut_formateado}" if cliente.rut else f"{cliente} — sin RUT"


class IngresoForm(AuthenticationForm):
    """Ingreso con freno a los intentos repetidos de adivinar la clave."""

    def clean(self):
        llave = "ingreso:" + (self.cleaned_data.get("username") or "").strip().lower()
        if cache.get(llave, 0) >= settings.INTENTOS_DE_INGRESO:
            raise ValidationError(
                "Demasiados intentos fallidos. Espera 15 minutos y vuelve a intentar.",
                code="bloqueado")
        try:
            datos = super().clean()
        except ValidationError:
            cache.set(llave, cache.get(llave, 0) + 1, settings.BLOQUEO_DE_INGRESO)
            raise
        cache.delete(llave)
        return datos


class Fecha(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class FormBase(forms.ModelForm):
    """Valida también las restricciones que incluyen la sociedad (que no va en el formulario)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "rut" in self.fields:
            casilla_rut(self.fields["rut"])
            if self.initial.get("rut"):
                self.initial["rut"] = formatear_rut(self.initial["rut"])
        if "giro" in self.fields:
            # El giro sugiere las actividades económicas del SII (se puede escribir otro texto).
            self.fields["giro"].widget.attrs.update(
                {"list": "actividades-sii", "autocomplete": "off",
                 "placeholder": "Escribe para buscar en el listado del SII"})

    def _post_clean(self):
        super()._post_clean()
        if not self.errors:
            try:
                self.instance.validate_constraints()
            except ValidationError as e:
                self.add_error(None, e)


class SociedadForm(FormBase):
    logo_archivo = forms.FileField(
        label="Logo", required=False,
        help_text="PNG o JPG. Se muestra en la app y en los documentos emitidos.")

    class Meta:
        model = Sociedad
        fields = ["rut", "razon_social", "giro", "direccion", "comuna", "ciudad",
                  "email", "telefono", "activa", "acteco", "oficina_sii",
                  "resolucion_fecha", "resolucion_numero", "ambiente"]
        widgets = {"resolucion_fecha": Fecha()}

    def clean_acteco(self):
        acteco = self.cleaned_data["acteco"].strip()
        if acteco and not (acteco.isdigit() and len(acteco) <= 6):
            raise ValidationError("Debe ser un número de hasta 6 dígitos.")
        return acteco

    def clean_logo_archivo(self):
        archivo = self.cleaned_data.get("logo_archivo")
        if not archivo:
            return None
        from .dte.motor import ErrorDTE
        from .dte.servicio import preparar_logo
        try:
            return preparar_logo(archivo.read())
        except ErrorDTE as e:
            raise ValidationError(str(e))

    def save(self, commit=True):
        if self.cleaned_data.get("logo_archivo"):
            self.instance.logo = self.cleaned_data["logo_archivo"]
        return super().save(commit)


class ClienteForm(FormBase):
    class Meta:
        model = Cliente
        fields = ["nombre_fantasia", "razon_social", "rut", "giro", "direccion", "comuna",
                  "ciudad", "email", "contacto", "telefono", "dias_pago", "activo"]


class ProductoForm(FormBase):
    class Meta:
        model = Producto
        fields = ["codigo", "nombre", "descripcion", "unidad", "precio", "exento", "activo"]


class DocumentoForm(FormBase):
    class Meta:
        model = Documento
        fields = ["cliente", "fecha_emision", "fecha_vencimiento", "codigo_referencia",
                  "razon_referencia", "ind_traslado", "direccion_destino", "comuna_destino",
                  "patente", "observaciones", "caso_set"]
        widgets = {"fecha_emision": Fecha(), "fecha_vencimiento": Fecha()}

    def __init__(self, *args, sociedad, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.es_guia:
            self.fields["ind_traslado"].required = True
            del self.fields["fecha_vencimiento"]
        else:
            for campo in ("ind_traslado", "direccion_destino", "comuna_destino", "patente"):
                del self.fields[campo]
        en_certificacion = (sociedad.ambiente == Sociedad.CERTIFICACION
                            and hasattr(sociedad, "certificado"))
        if not en_certificacion or self.instance.es_cotizacion:
            del self.fields["caso_set"]
        if self.instance.es_nota:
            # El cliente de una nota es siempre el del documento de referencia.
            del self.fields["cliente"], self.fields["fecha_vencimiento"]
            self.fields["codigo_referencia"].required = True
            self.fields["razon_referencia"].required = True
        else:
            del self.fields["codigo_referencia"], self.fields["razon_referencia"]
            self.fields["cliente"].queryset = sociedad.clientes.filter(activo=True)
            self.fields["cliente"].label_from_instance = etiqueta_cliente
            if self.instance.es_cotizacion:
                self.fields["fecha_vencimiento"].label = "Válida hasta"
                self.fields["fecha_vencimiento"].help_text = "Si se deja vacío, 30 días."
            elif "fecha_vencimiento" in self.fields:
                self.fields["fecha_vencimiento"].help_text = (
                    "Si se deja vacío, se calcula con el plazo de pago del cliente.")

    def clean(self):
        datos = super().clean()
        if "fecha_vencimiento" in self.fields:
            emision, cliente = datos.get("fecha_emision"), datos.get("cliente")
            vence = datos.get("fecha_vencimiento")
            if emision and cliente and not vence:
                dias = 30 if self.instance.es_cotizacion else cliente.dias_pago
                datos["fecha_vencimiento"] = emision + timedelta(days=dias)
            elif emision and vence and vence < emision:
                self.add_error("fecha_vencimiento", "No puede ser anterior a la emisión.")
        return datos


class LineaForm(forms.ModelForm):
    class Meta:
        model = Linea
        fields = ["producto", "descripcion", "cantidad", "precio", "descuento", "exento"]

    def __init__(self, *args, sociedad, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["producto"].queryset = sociedad.productos.filter(activo=True)
        self.fields["producto"].empty_label = "— libre —"
        for campo in ("cantidad", "precio", "descuento"):
            if self.initial.get(campo) is not None:
                self.initial[campo] = sin_ceros(self.initial[campo])


class RecurrenteLineaForm(LineaForm):
    class Meta(LineaForm.Meta):
        model = RecurrenteLinea


def formset_lineas(padre, modelo, formulario):
    return inlineformset_factory(padre, modelo, form=formulario, extra=1, can_delete=True)


LineasFormSet = formset_lineas(Documento, Linea, LineaForm)
RecurrenteLineasFormSet = formset_lineas(Recurrente, RecurrenteLinea, RecurrenteLineaForm)


class RecurrenteForm(FormBase):
    class Meta:
        model = Recurrente
        fields = ["nombre", "cliente", "tipo", "dia_emision", "fecha_inicio", "fecha_fin",
                  "observaciones", "activa"]
        widgets = {"fecha_inicio": Fecha(), "fecha_fin": Fecha()}

    def __init__(self, *args, sociedad, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cliente"].queryset = sociedad.clientes.filter(activo=True)
        self.fields["cliente"].label_from_instance = etiqueta_cliente


class PagoForm(forms.ModelForm):
    class Meta:
        model = Pago
        fields = ["fecha", "monto", "medio", "nota"]
        widgets = {"fecha": Fecha()}

    def __init__(self, *args, documento, **kwargs):
        super().__init__(*args, **kwargs)
        self.documento = documento

    def clean_monto(self):
        monto = self.cleaned_data["monto"]
        if monto > self.documento.saldo:
            raise ValidationError("El pago supera el saldo pendiente del documento.")
        return monto


class CertificadoForm(forms.Form):
    archivo = forms.FileField(label="Archivo del certificado (.pfx o .p12)")
    clave = forms.CharField(label="Clave del certificado", widget=forms.PasswordInput,
                            required=False)
    rut_titular = forms.CharField(label="RUT del titular", max_length=12)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        casilla_rut(self.fields["rut_titular"])

    def clean_rut_titular(self):
        from .utils import normalizar_rut
        return normalizar_rut(self.cleaned_data["rut_titular"])


class ImportarClientesForm(forms.Form):
    archivo = forms.FileField(
        label="Archivo Excel (.xlsx)",
        help_text="Se lee la primera hoja y se toma la columna titulada «Cliente» o «Clientes».")


class UsuarioForm(forms.ModelForm):
    clave = forms.CharField(
        label="Clave", widget=forms.PasswordInput, required=False,
        help_text="Clave inicial; cada persona puede cambiarla después desde su sesión.")

    restablecer_dos_pasos = forms.BooleanField(
        label="Restablecer verificación en dos pasos", required=False,
        help_text="Úsalo si la persona cambió o perdió su teléfono: tendrá que enlazar "
                  "de nuevo su app de autenticación.")

    class Meta:
        model = get_user_model()
        fields = ["username", "first_name", "last_name", "email", "is_superuser", "is_active"]
        labels = {"is_superuser": "Administrador", "is_active": "Activo"}
        help_texts = {"username": "Con este nombre se inicia sesión.",
                      "is_superuser": "Puede crear y editar usuarios.", "is_active": ""}

    def clean_clave(self):
        clave = self.cleaned_data["clave"]
        if not clave and not self.instance.pk:
            raise ValidationError("Indica una clave inicial.")
        if clave:
            validate_password(clave, self.instance)
        return clave

    def save(self, commit=True):
        usuario = super().save(commit=False)
        usuario.is_staff = usuario.is_superuser
        if self.cleaned_data["clave"]:
            usuario.set_password(self.cleaned_data["clave"])
        if commit:
            usuario.save()
            if self.cleaned_data.get("restablecer_dos_pasos"):
                from .models import DosPasos
                DosPasos.objects.filter(usuario=usuario).delete()
        return usuario


class EmitirForm(forms.Form):
    folio = forms.IntegerField(min_value=1, label="Folio asignado por el SII")
