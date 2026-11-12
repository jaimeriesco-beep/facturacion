from datetime import timedelta

from decimal import Decimal

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.core.validators import validate_email
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from .models import (Cliente, Documento, Linea, Pago, Producto, Recurrente,
                     RecurrenteLinea, Sociedad, hoy, Bodega, Movimiento)
from .auth import buscar_usuario
from .utils import decimal_cl, formatear_rut, leer_decimal_cl


class NumeroCL(forms.DecimalField):
    """Número con coma decimal (14,5). Acepta punto solo como separador de miles."""
    widget = forms.TextInput

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.widget.attrs.update({"inputmode": "decimal", "autocomplete": "off",
                                  "data-decimal": "1"})
        self.widget.attrs.pop("step", None)

    def to_python(self, valor):
        if valor in self.empty_values:
            return None
        if isinstance(valor, (int, float, Decimal)):
            return super().to_python(valor)
        try:
            return super().to_python(str(leer_decimal_cl(valor)))
        except ValueError as e:
            raise ValidationError(str(e), code="invalid")

    def prepare_value(self, valor):
        if isinstance(valor, (int, float, Decimal)):
            return decimal_cl(valor)
        return valor

    def widget_attrs(self, widget):
        return {}


def con_coma(form, *campos):
    """Reemplaza campos decimales del formulario por casillas con coma decimal."""
    for nombre in campos:
        original = form.fields[nombre]
        form.fields[nombre] = NumeroCL(
            label=original.label, required=original.required, help_text=original.help_text,
            max_digits=original.max_digits, decimal_places=original.decimal_places,
            initial=original.initial)


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
        escrito = (self.cleaned_data.get("username") or "").strip().lower()
        conocido = buscar_usuario(escrito)  # usuario y correo cuentan como la misma persona
        llave = "ingreso:" + (conocido.username.lower() if conocido else escrito)
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
                  "ciudad", "dias_pago", "activo", "contacto_unico",
                  *(f"{rol}_{dato}" for rol, _ in Cliente.ROLES
                    for dato in ("nombre", "email", "telefono"))]

    def principales(self):
        return [self[n] for n in self.fields
                if n != "contacto_unico" and n.split("_")[0] not in dict(Cliente.ROLES)]

    def contactos(self):
        """Por cada rol: (clave, título, casillas)."""
        return [(rol, titulo, [self[f"{rol}_{dato}"] for dato in ("nombre", "email", "telefono")])
                for rol, titulo in Cliente.ROLES]

    def clean(self):
        datos = super().clean()
        if datos.get("contacto_unico"):  # una sola persona: no se guardan datos de las otras dos
            for rol in ("facturacion", "cobranza"):
                for dato in ("nombre", "email", "telefono"):
                    datos[f"{rol}_{dato}"] = ""
        return datos


class ProductoForm(FormBase):
    class Meta:
        model = Producto
        fields = ["codigo", "nombre", "descripcion", "unidad", "precio", "exento", "activo",
                  "controla_stock", "stock_minimo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        con_coma(self, "precio", "stock_minimo")
        self.fields["precio"].help_text = "Puedes usar decimales con coma, por ejemplo 14,5."
        self.fields["unidad"].help_text = "Hasta 4 letras. Ej.: KG, UN, ATAD (atado), BAND (bandeja)."
        if not self.instance.sociedad_id or not self.instance.sociedad.inventario_activo:
            del self.fields["controla_stock"], self.fields["stock_minimo"]


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
        con_coma(self, "cantidad", "precio", "descuento")


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


class MarcarPagadoForm(forms.Form):
    """Salda el documento completo. La fecha es opcional: si se deja vacía, queda la de hoy."""
    fecha = forms.DateField(label="Fecha de pago", required=False, widget=Fecha(),
                            help_text="Si la dejas vacía, queda la fecha de hoy.")
    medio = forms.ChoiceField(label="Medio de pago", choices=Pago.MEDIOS, initial="transferencia")
    nota = forms.CharField(label="Referencia o nota", max_length=120, required=False)

    def clean_fecha(self):
        fecha = self.cleaned_data.get("fecha")
        if fecha and fecha > hoy():
            raise ValidationError("La fecha de pago no puede ser futura.")
        return fecha or hoy()


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
                      "email": "También sirve para iniciar sesión.",
                      "is_superuser": "Puede crear y editar usuarios.", "is_active": ""}

    def clean_email(self):
        correo = self.cleaned_data["email"].strip()
        repetido = get_user_model().objects.filter(email__iexact=correo).exclude(pk=self.instance.pk)
        if correo and repetido.exists():
            raise ValidationError("Ya hay otro usuario con ese correo.")
        return correo

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


class EnviarCorreoForm(forms.Form):
    para = forms.CharField(
        label="Para", max_length=300,
        help_text="Puedes escribir hasta 3 correos separados por coma.")
    asunto = forms.CharField(label="Asunto", max_length=150)
    mensaje = forms.CharField(label="Mensaje", widget=forms.Textarea(attrs={"rows": 7}))

    def clean_para(self):
        correos = [c.strip() for c in self.cleaned_data["para"].replace(";", ",").split(",") if c.strip()]
        if not correos:
            raise ValidationError("Escribe al menos un correo.")
        if len(correos) > 3:
            raise ValidationError("Máximo 3 correos.")
        for correo in correos:
            try:
                validate_email(correo)
            except ValidationError:
                raise ValidationError(f"«{correo}» no es un correo válido.")
        return correos


class EmitirForm(forms.Form):
    folio = forms.IntegerField(min_value=1, label="Folio asignado por el SII")



# ---------------------------------------------------------------- Inventario

class InventarioConfigForm(forms.ModelForm):
    class Meta:
        model = Sociedad
        fields = ["inventario_activo", "inventario_desde", "inventario_rebaja",
                  "inventario_nc_devuelve", "inventario_permite_negativo"]
        widgets = {"inventario_desde": Fecha(), "inventario_rebaja": forms.RadioSelect}

    def clean(self):
        datos = super().clean()
        if datos.get("inventario_activo") and not datos.get("inventario_desde"):
            datos["inventario_desde"] = hoy()
        return datos


class BodegaForm(forms.ModelForm):
    class Meta:
        model = Bodega
        fields = ["nombre", "principal", "activa"]


class MovimientoForm(forms.Form):
    """Cabecera de una entrada o salida manual."""
    motivo = forms.ChoiceField(label="Motivo")
    fecha = forms.DateField(label="Fecha", widget=Fecha(), initial=hoy)
    bodega = forms.ModelChoiceField(label="Bodega", queryset=Bodega.objects.none(), empty_label=None)
    lote = forms.CharField(label="Lote o sector", max_length=60, required=False,
                           help_text="Opcional. Ej.: «Invernadero 2», «Cosecha semana 46».")
    nota = forms.CharField(label="Nota", max_length=200, required=False)

    def __init__(self, *args, sociedad, entrada, **kwargs):
        super().__init__(*args, **kwargs)
        motivos = dict(Movimiento.ENTRADAS + Movimiento.SALIDAS)
        claves = Movimiento.MANUALES_ENTRADA if entrada else Movimiento.MANUALES_SALIDA
        self.fields["motivo"].choices = [(c, motivos[c]) for c in claves]
        self.fields["bodega"].queryset = sociedad.bodegas.filter(activa=True)
        if not entrada:
            del self.fields["lote"]

    def clean_fecha(self):
        fecha = self.cleaned_data["fecha"]
        if fecha > hoy():
            raise ValidationError("La fecha no puede ser futura.")
        return fecha


class MovimientoLineaForm(forms.Form):
    producto = forms.ModelChoiceField(label="Producto", queryset=Producto.objects.none(),
                                      required=False)
    cantidad = NumeroCL(label="Cantidad", required=False, max_digits=14, decimal_places=3)
    costo_unitario = NumeroCL(label="Costo unitario", required=False, max_digits=18, decimal_places=4)

    def __init__(self, *args, productos=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["producto"].queryset = productos
        self.fields["producto"].widget.attrs["class"] = "selector-producto"

    def clean(self):
        datos = super().clean()
        producto, cantidad = datos.get("producto"), datos.get("cantidad")
        if producto and not cantidad:
            self.add_error("cantidad", "Indica la cantidad.")
        if cantidad is not None and cantidad <= 0:
            self.add_error("cantidad", "Debe ser mayor que cero.")
        if cantidad and not producto:
            self.add_error("producto", "Elige el producto.")
        if datos.get("costo_unitario") is not None and datos["costo_unitario"] < 0:
            self.add_error("costo_unitario", "No puede ser negativo.")
        return datos


MovimientoLineasFormSet = forms.formset_factory(MovimientoLineaForm, extra=0, min_num=1)


class TrasladoForm(forms.Form):
    fecha = forms.DateField(label="Fecha", widget=Fecha(), initial=hoy)
    origen = forms.ModelChoiceField(label="Desde", queryset=Bodega.objects.none(), empty_label=None)
    destino = forms.ModelChoiceField(label="Hacia", queryset=Bodega.objects.none(), empty_label=None)
    nota = forms.CharField(label="Nota", max_length=200, required=False)

    def __init__(self, *args, sociedad, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in ("origen", "destino"):
            self.fields[campo].queryset = sociedad.bodegas.filter(activa=True)

    def clean(self):
        datos = super().clean()
        if datos.get("origen") and datos.get("origen") == datos.get("destino"):
            raise ValidationError("Elige dos bodegas distintas.")
        if datos.get("fecha") and datos["fecha"] > hoy():
            self.add_error("fecha", "La fecha no puede ser futura.")
        return datos



class PerfilForm(forms.Form):
    first_name = forms.CharField(label="Nombre", max_length=150, required=False)
    last_name = forms.CharField(label="Apellido", max_length=150, required=False)
    cargo = forms.CharField(label="Cargo", max_length=80, required=False,
                            help_text="Aparece en la firma de los correos que envías.")
    telefono = forms.CharField(label="Teléfono", max_length=20, required=False)
    sociedad_inicial = forms.ModelChoiceField(
        label="Sociedad al ingresar", required=False, queryset=Sociedad.objects.filter(activa=True),
        empty_label="La última que usé", help_text="La que se abre cuando inicias sesión.")
