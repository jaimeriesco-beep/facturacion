import calendar
import math
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import BigIntegerField, Count, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .utils import MESES, formatear_rut, normalizar_rut, redondear

FACTURA, FACTURA_EXENTA, GUIA, NOTA_DEBITO, NOTA_CREDITO = 33, 34, 52, 56, 61
COTIZACION = 1  # documento interno, no tributario: no existe para el SII
TIPOS = [
    (FACTURA, "Factura electrónica"),
    (FACTURA_EXENTA, "Factura exenta electrónica"),
    (NOTA_CREDITO, "Nota de crédito electrónica"),
    (NOTA_DEBITO, "Nota de débito electrónica"),
    (GUIA, "Guía de despacho electrónica"),
    (COTIZACION, "Cotización"),
]
TIPOS_CORTOS = {FACTURA: "Factura", FACTURA_EXENTA: "Factura exenta",
                NOTA_CREDITO: "Nota de crédito", NOTA_DEBITO: "Nota de débito",
                GUIA: "Guía de despacho", COTIZACION: "Cotización"}
FACTURAS = (FACTURA, FACTURA_EXENTA)
NOTAS = (NOTA_CREDITO, NOTA_DEBITO)


DIAS_AVISO = 7   # desde cuántos días antes del vencimiento se marca en ámbar


def hoy():
    return timezone.localdate()


class ConRut(models.Model):
    rut = models.CharField("RUT", max_length=12)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.rut:
            self.rut = normalizar_rut(self.rut)

    @property
    def rut_formateado(self):
        return formatear_rut(self.rut)


class Sociedad(ConRut):
    razon_social = models.CharField("Razón social", max_length=100)
    giro = models.CharField(max_length=100, blank=True)
    direccion = models.CharField("Dirección", max_length=70, blank=True)
    comuna = models.CharField(max_length=20, blank=True)
    ciudad = models.CharField(max_length=20, blank=True)
    email = models.EmailField("Correo", blank=True)
    telefono = models.CharField("Teléfono", max_length=20, blank=True)
    activa = models.BooleanField(default=True)
    logo = models.BinaryField(null=True, blank=True, editable=False)  # PNG
    # Datos para la emisión electrónica propia (etapa 2).
    CERTIFICACION, PRODUCCION = "certificacion", "produccion"
    acteco = models.CharField(
        "Código de actividad económica", max_length=6, blank=True,
        help_text="Código de 6 dígitos con que la sociedad figura en el SII.")
    oficina_sii = models.CharField(
        "Unidad del SII", max_length=40, blank=True,
        help_text="Aparece en el recuadro del documento. Ej.: SANTIAGO ORIENTE")
    resolucion_fecha = models.DateField(
        "Fecha de resolución SII", null=True, blank=True,
        help_text="En certificación: la fecha en que el SII aceptó la postulación.")
    resolucion_numero = models.PositiveIntegerField(
        "Número de resolución SII", default=0, help_text="En certificación es 0.")
    # Marketing: correo de agradecimiento al emitir una factura.
    MENSAJE_GRACIAS = (
        "Hola {contacto}:\n\n"
        "Gracias por tu compra y por preferirnos. Te confirmamos que emitimos la "
        "{documento} por {total}.\n\n"
        "Cualquier consulta, estamos para ayudarte.\n\n"
        "Saludos,\n{sociedad}")
    gracias_activo = models.BooleanField("Enviar agradecimiento al emitir facturas", default=False)
    gracias_asunto = models.CharField("Asunto", max_length=120,
                                      default="Gracias por tu compra y por preferirnos")
    gracias_mensaje = models.TextField("Mensaje", default=MENSAJE_GRACIAS)
    # Inventario (módulo opcional por sociedad)
    inventario_activo = models.BooleanField("Usar el módulo de inventario", default=False)
    inventario_desde = models.DateField(
        "Rebajar stock desde", null=True, blank=True,
        help_text="Solo los documentos emitidos desde esta fecha mueven el stock.")
    inventario_rebaja = models.CharField(
        "El stock se rebaja con", max_length=7, default="factura",
        choices=[("factura", "Facturas (afectas y exentas)"), ("guia", "Guías de despacho")],
        help_text="Elige uno para no descontar dos veces la misma venta.")
    inventario_nc_devuelve = models.BooleanField(
        "Las notas de crédito que anulan una factura devuelven el stock", default=True)
    inventario_permite_negativo = models.BooleanField(
        "Permitir stock negativo", default=True,
        help_text="Si lo desactivas, no se podrá emitir un documento sin stock suficiente.")
    ambiente = models.CharField(
        "Ambiente SII", max_length=13, default=CERTIFICACION,
        choices=[(CERTIFICACION, "Certificación (pruebas)"), (PRODUCCION, "Producción (real)")],
        help_text="Cambiar a producción solo cuando el SII autorice a la sociedad.")

    class Meta:
        ordering = ["razon_social"]
        verbose_name_plural = "sociedades"
        constraints = [models.UniqueConstraint(fields=["rut"], name="sociedad_rut_unico")]

    def __str__(self):
        return self.razon_social


class Cliente(ConRut):
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="clientes")
    rut = models.CharField("RUT", max_length=12, blank=True,
                           help_text="Se puede completar después; es obligatorio para emitirle.")
    nombre_fantasia = models.CharField(
        "Nombre de fantasía", max_length=100, blank=True,
        help_text="Nombre con que se conoce al cliente o al local. No va en la factura.")
    razon_social = models.CharField(
        "Razón social", max_length=100, help_text="Nombre legal de la empresa; va en la factura.")
    giro = models.CharField(
        max_length=100, blank=True,
        help_text="Elige una actividad del listado del SII o escribe el giro. "
                  "En la factura van los primeros 40 caracteres.")
    direccion = models.CharField("Dirección", max_length=70, blank=True)
    comuna = models.CharField(max_length=20, blank=True)
    ciudad = models.CharField(max_length=20, blank=True)
    # Personas de contacto por rol. Con contacto_unico, la persona «comercial» cumple los tres.
    ROLES = [("comercial", "Contacto comercial"), ("facturacion", "Contacto de facturación"),
             ("cobranza", "Contacto de cobranza")]
    recibe_gracias = models.BooleanField(
        "Recibe el agradecimiento por compra", default=True,
        help_text="Se gestiona en Marketing.")
    contacto_unico = models.BooleanField(
        "Una sola persona para todo", default=True,
        help_text="Desmárcalo para indicar personas distintas para lo comercial, "
                  "la facturación y la cobranza.")
    comercial_nombre = models.CharField("Nombre", max_length=80, blank=True)
    comercial_email = models.EmailField("Correo", blank=True)
    comercial_telefono = models.CharField("Teléfono", max_length=20, blank=True)
    facturacion_nombre = models.CharField("Nombre", max_length=80, blank=True)
    facturacion_email = models.EmailField("Correo", blank=True)
    facturacion_telefono = models.CharField("Teléfono", max_length=20, blank=True)
    cobranza_nombre = models.CharField("Nombre", max_length=80, blank=True)
    cobranza_email = models.EmailField("Correo", blank=True)
    cobranza_telefono = models.CharField("Teléfono", max_length=20, blank=True)
    dias_pago = models.PositiveSmallIntegerField(
        "Plazo de pago (días)", default=30,
        help_text="Días desde la emisión hasta el vencimiento. 0 = contado.")
    activo = models.BooleanField(default=True)

    class Meta:
        # Varios locales pueden compartir RUT y razón social (misma empresa, otra dirección).
        ordering = ["nombre_fantasia", "razon_social"]

    def contacto(self, rol):
        """Persona de contacto para un rol: diccionario con nombre, email y telefono."""
        origen = "comercial" if self.contacto_unico else rol
        return {dato: getattr(self, f"{origen}_{dato}") for dato in ("nombre", "email", "telefono")}

    def __str__(self):
        if self.nombre_fantasia and self.nombre_fantasia.lower() != self.razon_social.lower():
            return f"{self.nombre_fantasia} · {self.razon_social}"
        return self.razon_social


class Producto(models.Model):
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="productos")
    codigo = models.CharField("SKU", max_length=35, blank=True,
                              help_text="Código interno del producto.")
    nombre = models.CharField(max_length=80)
    descripcion = models.CharField("Descripción", max_length=300, blank=True)
    unidad = models.CharField(max_length=4, blank=True, help_text="Ej.: UN, HR, MES")
    precio = models.DecimalField("Precio neto", max_digits=18, decimal_places=4, default=0)
    exento = models.BooleanField("Exento de IVA", default=False)
    activo = models.BooleanField(default=True)
    controla_stock = models.BooleanField(
        "Controlar stock", default=True,
        help_text="Desmárcalo para servicios (despacho, mantención) que no tienen existencias.")
    stock_minimo = models.DecimalField(
        "Stock mínimo", max_digits=14, decimal_places=3, null=True, blank=True,
        help_text="Bajo esta cantidad el producto aparece como «por reponer».")

    class Meta:
        ordering = ["nombre"]
        verbose_name = "producto"
        verbose_name_plural = "productos (SKU)"

    def __str__(self):
        return self.nombre


class DocumentoQuerySet(models.QuerySet):
    def con_saldo(self):
        """Anota pagado, notas de crédito/débito emitidas y saldo por cobrar."""
        def suma(qs, campo, monto):
            sub = qs.values(campo).annotate(s=Sum(monto)).values("s")
            return Coalesce(Subquery(sub, output_field=BigIntegerField()), Value(0))

        notas = Documento.objects.filter(referencia=OuterRef("pk"), estado=Documento.EMITIDO)
        return self.annotate(
            pagado_a=suma(Pago.objects.filter(documento=OuterRef("pk")), "documento", "monto"),
            nc_a=suma(notas.filter(tipo=NOTA_CREDITO), "referencia", "total"),
            nd_a=suma(notas.filter(tipo=NOTA_DEBITO), "referencia", "total"),
        ).annotate(saldo_a=F("total") + F("nd_a") - F("nc_a") - F("pagado_a"))

    def con_asociados(self):
        """Anota cuántos documentos están vinculados a cada uno (en ambos sentidos)."""
        def cuenta(campo):
            sub = (Documento.objects.filter(**{campo: OuterRef("pk")}).values(campo)
                   .annotate(c=Count("pk")).values("c"))
            return Coalesce(Subquery(sub, output_field=BigIntegerField()), Value(0))
        return self.annotate(n_hijos_a=cuenta("referencia"), n_derivados_a=cuenta("origen"))

    def ventas(self):
        """Documentos tributarios (todo menos las cotizaciones)."""
        return self.exclude(tipo=COTIZACION)

    def cotizaciones(self):
        return self.filter(tipo=COTIZACION)

    def por_cobrar(self):
        return (self.filter(tipo__in=FACTURAS, estado=Documento.EMITIDO, es_prueba=False)
                .con_saldo().filter(saldo_a__gt=0))


DIAS_PAPELERA = 30   # los documentos en la papelera se borran solos después de estos días


class DocumentoManager(models.Manager.from_queryset(DocumentoQuerySet)):
    """Manager por defecto: oculta lo que está en la papelera (también en sociedad.documentos)."""
    def get_queryset(self):
        return super().get_queryset().filter(eliminado_el__isnull=True)


class Documento(models.Model):
    BORRADOR, EMITIDO, ANULADO = "borrador", "emitido", "anulado"
    ESTADOS = [(BORRADOR, "Borrador"), (EMITIDO, "Emitido"), (ANULADO, "Anulado")]
    REFERENCIAS = [
        (1, "Anula el documento de referencia"),
        (2, "Corrige el texto del documento de referencia"),
        (3, "Corrige montos"),
    ]
    SII = [
        ("", "Sin enviar"),
        ("portal", "Emitido en portal SII"),
        ("enviado", "Enviado, en revisión"),
        ("aceptado", "Aceptado"),
        ("reparo", "Aceptado con reparos"),
        ("rechazado", "Rechazado"),
    ]

    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="documentos")
    cliente = models.ForeignKey(Cliente, on_delete=models.PROTECT, related_name="documentos")
    tipo = models.PositiveSmallIntegerField(choices=TIPOS, default=FACTURA)
    folio = models.PositiveIntegerField(null=True, blank=True)
    estado = models.CharField(max_length=10, choices=ESTADOS, default=BORRADOR)
    fecha_emision = models.DateField("Fecha de emisión", default=hoy)
    fecha_vencimiento = models.DateField("Fecha de vencimiento", null=True, blank=True)
    referencia = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="notas",
        verbose_name="Documento de referencia")
    # Vínculo interno (no va al SII): la cotización desde la que se creó esta factura.
    origen = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="derivados",
        editable=False)
    codigo_referencia = models.PositiveSmallIntegerField(
        "Motivo", choices=REFERENCIAS, null=True, blank=True)
    razon_referencia = models.CharField("Razón de la referencia", max_length=90, blank=True)
    observaciones = models.CharField(max_length=300, blank=True)
    neto = models.BigIntegerField(default=0)
    exento = models.BigIntegerField(default=0)
    iva = models.BigIntegerField("IVA", default=0)
    total = models.BigIntegerField(default=0)
    TRASLADOS = [
        (1, "Operación constituye venta"),
        (2, "Venta por efectuar"),
        (3, "Consignación"),
        (4, "Entrega gratuita"),
        (5, "Traslado interno"),
        (6, "Otro traslado que no es venta"),
        (7, "Guía de devolución"),
    ]
    # Solo guías de despacho.
    ind_traslado = models.PositiveSmallIntegerField(
        "Tipo de traslado", choices=TRASLADOS, null=True, blank=True)
    direccion_destino = models.CharField("Dirección de destino", max_length=70, blank=True)
    comuna_destino = models.CharField("Comuna de destino", max_length=20, blank=True)
    patente = models.CharField("Patente del vehículo", max_length=8, blank=True)
    # Emisión electrónica propia.
    estado_sii = models.CharField("Estado SII", max_length=10, choices=SII, blank=True)
    es_prueba = models.BooleanField(default=False)  # emitido en ambiente de certificación
    caso_set = models.CharField(
        "Caso del set de pruebas", max_length=30, blank=True,
        help_text="Solo durante la certificación. Ej.: CASO 4102547-1")
    xml_firmado = models.BinaryField(null=True, blank=True, editable=False)
    timbrado = models.DateTimeField(null=True, blank=True, editable=False)
    gracias_enviado = models.DateTimeField(null=True, blank=True, editable=False)
    # Último envío por correo del documento (cotizaciones).
    correo_enviado_a = models.CharField(max_length=300, blank=True, editable=False)
    correo_enviado_el = models.DateTimeField(null=True, blank=True, editable=False)
    # Recordatorios de pago enviados desde cobranza.
    recordatorio_enviado_a = models.CharField(max_length=300, blank=True, editable=False)
    recordatorio_enviado_el = models.DateTimeField(null=True, blank=True, editable=False)
    recordatorios = models.PositiveSmallIntegerField(default=0, editable=False)
    envio = models.ForeignKey("Envio", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="documentos", editable=False)
    recurrente = models.ForeignKey(
        "Recurrente", null=True, blank=True, on_delete=models.SET_NULL, related_name="documentos")
    periodo = models.CharField(max_length=7, blank=True)  # AAAA-MM
    creado = models.DateTimeField(auto_now_add=True)
    # Papelera: con fecha, el documento no aparece en ninguna parte y se borra a los 30 días.
    eliminado_el = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    eliminado_por = models.CharField(max_length=150, blank=True, editable=False)

    objects = DocumentoManager()
    todos = DocumentoQuerySet.as_manager()   # incluye los de la papelera

    class Meta:
        ordering = ["-fecha_emision", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["sociedad", "tipo", "folio", "es_prueba"], condition=Q(folio__isnull=False),
                name="folio_unico_por_tipo"),
            models.UniqueConstraint(
                fields=["recurrente", "periodo"], condition=Q(recurrente__isnull=False),
                name="una_factura_por_periodo"),
        ]

    def __str__(self):
        numero = f"N° {self.folio}" if self.folio else "(borrador)"
        return f"{self.tipo_corto} {numero}"

    @property
    def tipo_corto(self):
        return TIPOS_CORTOS[self.tipo]

    @property
    def es_factura(self):
        return self.tipo in FACTURAS

    @property
    def es_nota(self):
        return self.tipo in NOTAS

    @property
    def es_guia(self):
        return self.tipo == GUIA

    @property
    def es_cotizacion(self):
        return self.tipo == COTIZACION

    def motivo_no_eliminable(self):
        """Por qué no puede ir a la papelera, o "" si puede.

        Un DTE timbrado y aceptado por el SII es un documento tributario vigente: la ley
        obliga a conservarlo (art. 17 y 200 del Código Tributario) y solo se anula con
        una nota de crédito. Sí pueden borrarse los borradores, las cotizaciones, los
        documentos de prueba, los rechazados por el SII y las copias registradas desde
        el portal del SII (el original sigue en el SII).
        """
        hijos = getattr(self, "n_hijos_a", None)   # anotado en los listados (con_asociados)
        if hijos is None:
            hijos = Documento.objects.filter(referencia=self).count()
        if hijos:
            return "Tiene notas de crédito o débito asociadas."
        if self.estado == self.BORRADOR or self.es_cotizacion or self.es_prueba:
            return ""
        if self.estado_sii == "rechazado":
            return ""
        if self.es_propio:
            return ("Es un documento tributario válido ante el SII y la ley obliga a conservarlo. "
                    "Para dejarlo sin efecto, emite una nota de crédito que lo anule.")
        pagado = getattr(self, "pagado_a", None)   # anotado en los listados (con_saldo)
        if pagado is None:
            pagado = self.pagos.exists()
        if pagado:
            return "Tiene pagos registrados. Elimínalos primero desde el documento."
        return ""

    @property
    def eliminable(self):
        return not self.motivo_no_eliminable()

    @property
    def aviso_papelera(self):
        """Texto extra para la confirmación de eliminar."""
        if self.estado == self.EMITIDO and not self.es_propio and not self.es_prueba and not self.es_cotizacion:
            return "Se emitió en el portal del SII: el original sigue allá; aquí solo se borra el registro."
        if self.estado_sii == "rechazado":
            return "El SII lo rechazó, así que no tiene validez tributaria."
        return ""

    @property
    def borrar_el(self):
        if self.eliminado_el:
            return self.eliminado_el + timedelta(days=DIAS_PAPELERA)

    @property
    def dias_en_papelera(self):
        """Días que le quedan antes de borrarse definitivamente."""
        if self.eliminado_el:
            segundos = (self.borrar_el - timezone.now()).total_seconds()
            return max(math.ceil(segundos / 86400), 0)

    def a_papelera(self, usuario=""):
        self.eliminado_el = timezone.now()
        self.eliminado_por = str(usuario)[:150]
        self.save(update_fields=["eliminado_el", "eliminado_por"])

    def restaurar(self):
        self.eliminado_el = None
        self.eliminado_por = ""
        self.save(update_fields=["eliminado_el", "eliminado_por"])

    def asociados(self):
        """Documentos vinculados: (de dónde viene, qué se generó a partir de este)."""
        viene_de = [d for d in (self.referencia, self.origen) if d]
        generados = list(self.notas.all()) + list(self.derivados.all())
        generados.sort(key=lambda d: (d.fecha_emision, d.pk))
        return viene_de, generados

    @property
    def n_asociados(self):
        if hasattr(self, "n_hijos_a"):
            hijos = self.n_hijos_a + self.n_derivados_a
        else:
            hijos = self.notas.count() + self.derivados.count()
        return hijos + bool(self.referencia_id) + bool(self.origen_id)

    @property
    def es_propio(self):
        """Timbrado y firmado por esta app (no registrado desde el portal del SII)."""
        return bool(self.xml_firmado)

    def recalcular(self, guardar=True):
        neto = exento = 0
        for linea in self.lineas.all():
            if linea.exento:
                exento += linea.monto
            else:
                neto += linea.monto
        self.neto, self.exento = neto, exento
        self.iva = redondear(Decimal(neto) * settings.TASA_IVA / 100)
        self.total = neto + exento + self.iva
        if guardar:
            self.save(update_fields=["neto", "exento", "iva", "total"])

    def errores_para_emitir(self):
        errores = []
        if not self.cliente.rut and not self.es_cotizacion:
            errores.append("El cliente no tiene RUT: complétalo en Clientes.")
        lineas = list(self.lineas.all())
        if not lineas:
            errores.append("El documento no tiene líneas de detalle.")
        sin_monto = ((self.es_nota and self.codigo_referencia == 2)
                     or (self.es_guia and self.ind_traslado != 1))
        if self.total <= 0 and not sin_monto:
            errores.append("El total debe ser mayor que cero.")
        if self.es_guia and not self.ind_traslado:
            errores.append("Falta indicar el tipo de traslado.")
        if self.tipo == FACTURA_EXENTA and any(not l.exento for l in lineas):
            errores.append("Una factura exenta no puede tener líneas afectas a IVA.")
        if self.tipo == FACTURA and lineas and all(l.exento for l in lineas):
            errores.append("Todas las líneas son exentas: corresponde una factura exenta.")
        if self.es_nota:
            if not self.referencia_id:
                errores.append("Falta el documento de referencia.")
            elif self.referencia.estado != self.EMITIDO:
                errores.append("El documento de referencia no está emitido.")
            if not self.codigo_referencia or not self.razon_referencia:
                errores.append("Faltan el motivo y la razón de la referencia.")
        return errores

    # --- Cobranza -------------------------------------------------------
    def _suma_notas(self, tipo):
        return self.notas.filter(tipo=tipo, estado=self.EMITIDO).aggregate(
            s=Sum("total"))["s"] or 0

    @property
    def pagado(self):
        if hasattr(self, "pagado_a"):
            return self.pagado_a
        return self.pagos.aggregate(s=Sum("monto"))["s"] or 0

    @property
    def notas_credito(self):
        return self.nc_a if hasattr(self, "nc_a") else self._suma_notas(NOTA_CREDITO)

    @property
    def notas_debito(self):
        return self.nd_a if hasattr(self, "nd_a") else self._suma_notas(NOTA_DEBITO)

    @property
    def saldo(self):
        if hasattr(self, "saldo_a"):
            return self.saldo_a
        return self.total + self.notas_debito - self.notas_credito - self.pagado

    @property
    def dias_vencido(self):
        if not self.fecha_vencimiento:
            return 0
        return (hoy() - self.fecha_vencimiento).days

    @property
    def plazo(self):
        """Días al vencimiento para la columna de cobranza: (clase, texto).

        Azul: aún hay margen. Ámbar: vence en 7 días o menos. Rojo: vencida.
        """
        if not self.fecha_vencimiento:
            return "sin", "Sin vencimiento"
        dias = -self.dias_vencido
        if dias < 0:
            return "vencida", f"Vencida hace {-dias} día{'s' if dias != -1 else ''}"
        if dias == 0:
            return "pronto", "Vence hoy"
        texto = f"Falta{'n' if dias != 1 else ''} {dias} día{'s' if dias != 1 else ''}"
        return ("pronto" if dias <= DIAS_AVISO else "al-dia"), texto

    @property
    def estado_cobro(self):
        if not self.es_factura or self.estado != self.EMITIDO or self.es_prueba:
            return ""
        if self.saldo <= 0:
            return "pagada"
        if self.dias_vencido > 0:
            return "vencida"
        return "parcial" if self.pagado > 0 else "pendiente"


class LineaBase(models.Model):
    producto = models.ForeignKey(
        Producto, null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name="Producto (SKU)")
    descripcion = models.CharField("Descripción", max_length=300)
    cantidad = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    precio = models.DecimalField("Precio unitario", max_digits=18, decimal_places=4, default=0)
    descuento = models.DecimalField("Desc. %", max_digits=5, decimal_places=2, default=0)
    exento = models.BooleanField(default=False)

    class Meta:
        abstract = True

    def clean(self):
        super().clean()
        if self.cantidad is not None and self.cantidad <= 0:
            raise ValidationError({"cantidad": "Debe ser mayor que cero."})
        if self.precio is not None and self.precio < 0:
            raise ValidationError({"precio": "No puede ser negativo."})
        if self.descuento is not None and not 0 <= self.descuento <= 100:
            raise ValidationError({"descuento": "Debe estar entre 0 y 100."})

    def calcular_monto(self):
        """Monto neto de la línea en pesos enteros.

        Regla legal (art. 21 de la Ley 18.267; Oficio SII N° 3.257 de 2009): se desprecian
        las fracciones menores a 50 centavos y las de 50 centavos o más suben al entero
        siguiente. Base imponible, IVA y total deben quedar en pesos enteros.
        """
        bruto = Decimal(self.cantidad) * Decimal(self.precio)
        return redondear(bruto * (Decimal(100) - Decimal(self.descuento)) / 100)


class Linea(LineaBase):
    documento = models.ForeignKey(Documento, on_delete=models.CASCADE, related_name="lineas")
    monto = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["id"]

    def save(self, *args, **kwargs):
        self.monto = self.calcular_monto()
        super().save(*args, **kwargs)


class Pago(models.Model):
    MEDIOS = [("transferencia", "Transferencia"), ("cheque", "Cheque"),
              ("efectivo", "Efectivo"), ("tarjeta", "Tarjeta"), ("otro", "Otro")]
    documento = models.ForeignKey(Documento, on_delete=models.CASCADE, related_name="pagos")
    fecha = models.DateField(default=hoy)
    monto = models.BigIntegerField()
    medio = models.CharField(max_length=15, choices=MEDIOS, default="transferencia")
    nota = models.CharField("Referencia o nota", max_length=120, blank=True)

    class Meta:
        ordering = ["fecha", "id"]

    def clean(self):
        super().clean()
        if self.monto is not None and self.monto <= 0:
            raise ValidationError({"monto": "El monto debe ser mayor que cero."})


class Recurrente(models.Model):
    """Plantilla de facturación mensual (arriendos, servicios fijos, etc.)."""
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="recurrentes")
    cliente = models.ForeignKey(Cliente, on_delete=models.PROTECT, related_name="recurrentes")
    nombre = models.CharField(max_length=80, help_text="Ej.: Arriendo oficina 301")
    tipo = models.PositiveSmallIntegerField(
        choices=[t for t in TIPOS if t[0] in FACTURAS], default=FACTURA)
    dia_emision = models.PositiveSmallIntegerField(
        "Día de emisión", default=1, help_text="Día del mes (1 a 28).")
    fecha_inicio = models.DateField("Desde", default=hoy)
    fecha_fin = models.DateField("Hasta", null=True, blank=True,
                                 help_text="Dejar vacío si es indefinido.")
    observaciones = models.CharField(max_length=300, blank=True)
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre

    def clean(self):
        super().clean()
        if self.dia_emision is not None and not 1 <= self.dia_emision <= 28:
            raise ValidationError({"dia_emision": "Debe estar entre 1 y 28."})
        if self.fecha_fin and self.fecha_inicio and self.fecha_fin < self.fecha_inicio:
            raise ValidationError({"fecha_fin": "No puede ser anterior al inicio."})

    @property
    def monto_estimado(self):
        neto = sum(l.calcular_monto() for l in self.lineas.all() if not l.exento)
        exento = sum(l.calcular_monto() for l in self.lineas.all() if l.exento)
        return neto + exento + redondear(Decimal(neto) * settings.TASA_IVA / 100)

    def aplica(self, anio, mes):
        primero = date(anio, mes, 1)
        ultimo = date(anio, mes, calendar.monthrange(anio, mes)[1])
        return (self.activa and self.fecha_inicio <= ultimo
                and (self.fecha_fin is None or self.fecha_fin >= primero))

    @transaction.atomic
    def generar(self, anio, mes):
        """Crea el borrador del período. Devuelve None si no aplica o ya existe."""
        periodo = f"{anio}-{mes:02d}"
        if not self.aplica(anio, mes) or self.documentos.filter(periodo=periodo).exists():
            return None
        # Si la del período está en la papelera, se reemplaza por la nueva.
        Documento.todos.filter(recurrente=self, periodo=periodo, eliminado_el__isnull=False).delete()
        emision = date(anio, mes, self.dia_emision)
        doc = Documento.objects.create(
            sociedad=self.sociedad, cliente=self.cliente, tipo=self.tipo,
            fecha_emision=emision,
            fecha_vencimiento=emision + timedelta(days=self.cliente.dias_pago),
            observaciones=self.observaciones, recurrente=self, periodo=periodo)
        variables = {"mes": MESES[mes - 1], "año": str(anio), "anio": str(anio)}
        for l in self.lineas.all():
            texto = l.descripcion
            for clave, valor in variables.items():
                texto = texto.replace("{" + clave + "}", valor)
            Linea.objects.create(
                documento=doc, producto=l.producto, descripcion=texto, cantidad=l.cantidad,
                precio=l.precio, descuento=l.descuento,
                exento=l.exento or self.tipo == FACTURA_EXENTA)
        doc.recalcular()
        return doc


class RecurrenteLinea(LineaBase):
    recurrente = models.ForeignKey(Recurrente, on_delete=models.CASCADE, related_name="lineas")

    class Meta:
        ordering = ["id"]


class CertificadoDigital(models.Model):
    """Certificado .pfx del firmante. El archivo y su clave se guardan cifrados."""
    sociedad = models.OneToOneField(Sociedad, on_delete=models.CASCADE, related_name="certificado")
    titular = models.CharField(max_length=120)
    rut_titular = models.CharField("RUT del titular", max_length=12)
    vence = models.DateField()
    pfx = models.BinaryField()
    clave = models.BinaryField()
    cargado = models.DateTimeField(auto_now=True)

    @property
    def vigente(self):
        return self.vence >= hoy()


class Caf(models.Model):
    """Rango de folios autorizado por el SII (archivo CAF), guardado cifrado."""
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="cafs")
    tipo = models.PositiveSmallIntegerField(choices=TIPOS)
    ambiente = models.CharField(max_length=13)
    desde = models.PositiveIntegerField()
    hasta = models.PositiveIntegerField()
    siguiente = models.PositiveIntegerField()
    fecha_autorizacion = models.DateField()
    xml = models.BinaryField()

    class Meta:
        ordering = ["tipo", "desde"]
        constraints = [models.UniqueConstraint(
            fields=["sociedad", "tipo", "ambiente", "desde"], name="caf_unico")]

    @property
    def disponibles(self):
        return max(self.hasta - self.siguiente + 1, 0)


class Envio(models.Model):
    """Sobre (EnvioDTE) enviado al SII con uno o más documentos."""
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="envios")
    ambiente = models.CharField(max_length=13)
    xml = models.BinaryField()
    track_id = models.CharField(max_length=20, blank=True)
    estado = models.CharField(max_length=10, blank=True)
    detalle = models.TextField(blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class DosPasos(models.Model):
    """Secreto de la app de autenticación (TOTP) de cada usuario, guardado cifrado."""
    usuario = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                   related_name="dos_pasos")
    secreto = models.BinaryField()
    confirmado = models.BooleanField(default=False)


def purgar_papelera(sociedad=None):
    """Borra definitivamente lo que lleva más de DIAS_PAPELERA días en la papelera."""
    limite = timezone.now() - timedelta(days=DIAS_PAPELERA)
    qs = Documento.todos.filter(eliminado_el__lt=limite)
    if sociedad is not None:
        qs = qs.filter(sociedad=sociedad)
    borrados = 0
    for doc in qs:
        try:
            doc.delete()
            borrados += 1
        except models.ProtectedError:
            pass   # algo lo referencia; se reintenta en la próxima pasada
    return borrados


# ---------------------------------------------------------------- Inventario

class Bodega(models.Model):
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="bodegas")
    nombre = models.CharField(max_length=60)
    activa = models.BooleanField(default=True)
    principal = models.BooleanField(
        default=False, help_text="Desde aquí se descuentan las ventas.")

    class Meta:
        ordering = ["-principal", "nombre"]

    def __str__(self):
        return self.nombre


class Movimiento(models.Model):
    """Entrada (cantidad positiva) o salida (negativa) de un producto en una bodega.

    El stock nunca se escribe a mano: es la suma de los movimientos.
    """
    ENTRADAS = [("inicial", "Stock inicial"), ("cosecha", "Cosecha o producción"),
                ("compra", "Compra"), ("devolucion", "Devolución de cliente"),
                ("ajuste_mas", "Ajuste: sobrante"), ("traspaso_in", "Traspaso recibido"),
                ("nota_credito", "Nota de crédito")]
    SALIDAS = [("venta", "Venta"), ("merma", "Merma o pérdida"), ("consumo", "Consumo interno"),
               ("muestra", "Muestra o donación"), ("ajuste_menos", "Ajuste: faltante"),
               ("traspaso_out", "Traspaso enviado")]
    MANUALES_ENTRADA = ["cosecha", "compra", "devolucion", "inicial"]
    MANUALES_SALIDA = ["merma", "consumo", "muestra"]

    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="movimientos")
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="movimientos")
    bodega = models.ForeignKey(Bodega, on_delete=models.PROTECT, related_name="movimientos")
    fecha = models.DateField(default=hoy)
    motivo = models.CharField(max_length=14, choices=ENTRADAS + SALIDAS)
    cantidad = models.DecimalField(max_digits=14, decimal_places=3)
    costo_unitario = models.DecimalField(
        "Costo unitario", max_digits=18, decimal_places=4, null=True, blank=True)
    documento = models.ForeignKey(
        "Documento", null=True, blank=True, on_delete=models.CASCADE, related_name="movimientos")
    lote = models.CharField("Lote o sector", max_length=60, blank=True)
    nota = models.CharField(max_length=200, blank=True)
    usuario = models.CharField(max_length=150, blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fecha", "-id"]
        indexes = [models.Index(fields=["sociedad", "producto", "fecha"])]

    def __str__(self):
        return f"{self.get_motivo_display()} · {self.producto} · {self.cantidad}"

    @property
    def es_entrada(self):
        return self.cantidad > 0

    @property
    def es_automatico(self):
        return self.documento_id is not None



class Perfil(models.Model):
    """Datos personales del usuario que no trae el modelo de Django."""
    usuario = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                   related_name="perfil")
    cargo = models.CharField(max_length=80, blank=True, help_text="Ej.: Gerente comercial")
    telefono = models.CharField("Teléfono", max_length=20, blank=True)
    sociedad_inicial = models.ForeignKey(
        Sociedad, null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name="Sociedad al ingresar",
        help_text="La que se abre cuando inicias sesión.")

    def __str__(self):
        return str(self.usuario)


def perfil_de(usuario):
    return Perfil.objects.get_or_create(usuario=usuario)[0]


def firma_de(usuario, sociedad):
    """Firma para los correos: nombre, cargo, teléfono y sociedad."""
    perfil = perfil_de(usuario)
    lineas = [usuario.get_full_name() or sociedad.razon_social]
    if perfil.cargo:
        lineas.append(perfil.cargo)
    if usuario.get_full_name():
        lineas.append(sociedad.razon_social)
    if perfil.telefono:
        lineas.append(perfil.telefono)
    return "\n".join(lineas)


# ---------------------------------------------------------------- Compras

CATEGORIAS_INICIALES = [
    "Semillas y plantines", "Fertilizantes y sustratos", "Envases y embalaje", "Insumos de riego",
    "Combustible y transporte", "Arriendo", "Servicios básicos (luz, agua, gas)",
    "Mantención y reparaciones", "Herramientas y equipos", "Servicios profesionales",
    "Telefonía e internet", "Otros gastos",
]


class CategoriaGasto(models.Model):
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="categorias_gasto")
    nombre = models.CharField(max_length=60)
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ["nombre"]
        constraints = [models.UniqueConstraint(fields=["sociedad", "nombre"], name="categoria_unica")]

    def __str__(self):
        return self.nombre


def categorias_de(sociedad):
    """Las categorías de gasto de la sociedad; crea las iniciales la primera vez."""
    if not sociedad.categorias_gasto.exists():
        CategoriaGasto.objects.bulk_create(
            [CategoriaGasto(sociedad=sociedad, nombre=n) for n in CATEGORIAS_INICIALES])
    return sociedad.categorias_gasto.filter(activa=True)


class Proveedor(models.Model):
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="proveedores")
    rut = models.CharField("RUT", max_length=12)
    razon_social = models.CharField("Razón social", max_length=100)
    nombre_fantasia = models.CharField("Nombre de fantasía", max_length=100, blank=True)
    categoria = models.ForeignKey(
        CategoriaGasto, null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name="Categoría habitual",
        help_text="Las compras nuevas de este proveedor quedan con esta categoría.")
    dias_pago = models.PositiveSmallIntegerField(
        "Días de pago", default=30, help_text="Plazo habitual para pagarle.")
    email = models.EmailField("Correo", blank=True)
    telefono = models.CharField("Teléfono", max_length=20, blank=True)
    banco = models.CharField("Datos de transferencia", max_length=200, blank=True,
                             help_text="Banco, tipo y número de cuenta.")

    class Meta:
        ordering = ["razon_social"]
        constraints = [models.UniqueConstraint(fields=["sociedad", "rut"], name="proveedor_rut_unico")]

    def __str__(self):
        return self.nombre_fantasia or self.razon_social


TIPOS_COMPRA = {
    30: "Factura", 32: "Factura no afecta o exenta", 33: "Factura electrónica",
    34: "Factura exenta electrónica", 45: "Factura de compra", 46: "Factura de compra electrónica",
    55: "Nota de débito", 56: "Nota de débito electrónica", 60: "Nota de crédito",
    61: "Nota de crédito electrónica", 914: "Declaración de ingreso (DIN)",
}


class CompraQuerySet(models.QuerySet):
    def con_saldo(self):
        pagado = (PagoCompra.objects.filter(compra=OuterRef("pk")).values("compra")
                  .annotate(s=Sum("monto")).values("s"))
        return self.annotate(pagado_a=Coalesce(Subquery(pagado, output_field=BigIntegerField()), Value(0)))


class Compra(models.Model):
    ESTADOS_RCV = [("registro", "Registrada"), ("pendiente", "Pendiente"),
                   ("no_incluir", "No incluir"), ("reclamado", "Reclamada"), ("manual", "Ingreso manual")]
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="compras")
    proveedor = models.ForeignKey(Proveedor, on_delete=models.PROTECT, related_name="compras")
    tipo = models.PositiveSmallIntegerField(choices=sorted(TIPOS_COMPRA.items()))
    folio = models.BigIntegerField()
    fecha_emision = models.DateField("Fecha del documento")
    fecha_recepcion = models.DateField("Recepción en el SII", null=True, blank=True)
    periodo = models.CharField("Período tributario", max_length=7, help_text="AAAA-MM")
    estado_rcv = models.CharField("Estado en el SII", max_length=10, choices=ESTADOS_RCV, default="registro")
    tipo_compra = models.CharField(max_length=40, blank=True)
    exento = models.BigIntegerField(default=0)
    neto = models.BigIntegerField(default=0)
    iva = models.BigIntegerField("IVA recuperable", default=0)
    iva_no_recuperable = models.BigIntegerField(default=0)
    otros_impuestos = models.BigIntegerField(default=0)
    total = models.BigIntegerField(default=0)
    categoria = models.ForeignKey(CategoriaGasto, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="compras")
    fecha_vencimiento = models.DateField("Vence", null=True, blank=True)
    nota = models.CharField(max_length=200, blank=True)
    origen = models.CharField(max_length=10, default="rcv", editable=False)
    creado = models.DateTimeField(auto_now_add=True)

    objects = CompraQuerySet.as_manager()

    class Meta:
        ordering = ["-fecha_emision", "-id"]
        constraints = [models.UniqueConstraint(fields=["sociedad", "proveedor", "tipo", "folio"],
                                               name="compra_unica")]

    def __str__(self):
        return f"{self.tipo_corto} N° {self.folio}"

    @property
    def tipo_corto(self):
        if self.es_nota_credito:
            return "Nota de crédito"
        if self.tipo in (55, 56):
            return "Nota de débito"
        if self.tipo == 914:
            return "DIN"
        return "Factura exenta" if self.tipo in (32, 34) else "Factura"

    @property
    def es_nota_credito(self):
        return self.tipo in (60, 61)

    @property
    def signo(self):
        return -1 if self.es_nota_credito else 1

    @property
    def gasto(self):
        """Lo que pesa como gasto: neto + exento + IVA no recuperable (NC en negativo)."""
        return self.signo * (self.neto + self.exento + self.iva_no_recuperable)

    @property
    def pagado(self):
        valor = getattr(self, "pagado_a", None)
        if valor is None:
            valor = self.pagos.aggregate(s=Sum("monto"))["s"] or 0
        return valor

    @property
    def saldo(self):
        if self.es_nota_credito:
            return 0
        return max(self.total - self.pagado, 0)

    @property
    def estado_pago(self):
        if self.es_nota_credito:
            return "nc"
        if self.saldo <= 0:
            return "pagada"
        if self.fecha_vencimiento and self.fecha_vencimiento < hoy():
            return "vencida"
        return "parcial" if self.pagado else "pendiente"


class PagoCompra(models.Model):
    compra = models.ForeignKey(Compra, on_delete=models.CASCADE, related_name="pagos")
    fecha = models.DateField(default=hoy)
    monto = models.BigIntegerField()
    medio = models.CharField(max_length=15, choices=Pago.MEDIOS, default="transferencia")
    nota = models.CharField("Referencia o nota", max_length=120, blank=True)

    class Meta:
        ordering = ["fecha", "id"]


# ---------------------------------------------------------------- Banco

CATEGORIAS_BANCO = [
    ("Cobro a clientes", "ingreso"), ("Pago a proveedores", "egreso"),
    ("Sueldos y honorarios", "egreso"), ("Previsión (AFP, salud, seguro de cesantía)", "egreso"),
    ("Impuestos (F29, PPM, renta)", "egreso"), ("Contribuciones", "egreso"), ("Arriendo", "egreso"),
    ("Comisiones y gastos bancarios", "egreso"), ("Créditos y préstamos", "ambos"),
    ("Traspasos entre cuentas propias", "ambos"), ("Retiros de socios", "egreso"),
    ("Aportes de socios", "ingreso"), ("Seguros", "egreso"), ("Otros ingresos", "ingreso"),
    ("Otros egresos", "egreso"),
]
COBRO_CLIENTES, PAGO_PROVEEDORES = "Cobro a clientes", "Pago a proveedores"


class CuentaBancaria(models.Model):
    BANCOS = ["Banco de Chile", "Banco Santander", "BancoEstado", "BCI", "Itaú", "Scotiabank",
              "Banco Security", "Banco BICE", "Otro"]
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="cuentas")
    banco = models.CharField(max_length=40, choices=[(b, b) for b in BANCOS], default="Banco de Chile")
    numero = models.CharField("Número de cuenta", max_length=30)
    alias = models.CharField(max_length=60, blank=True, help_text="Ej.: Cuenta corriente principal")
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ["banco", "numero"]
        constraints = [models.UniqueConstraint(fields=["sociedad", "banco", "numero"], name="cuenta_unica")]

    def __str__(self):
        return self.alias or f"{self.banco} · {self.numero}"

    @property
    def ultimo(self):
        return self.movimientos.exclude(saldo=None).order_by("-fecha", "-orden").first()


class CategoriaBanco(models.Model):
    TIPOS = [("ingreso", "Ingreso"), ("egreso", "Egreso"), ("ambos", "Ingreso o egreso")]
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="categorias_banco")
    nombre = models.CharField(max_length=60)
    tipo = models.CharField(max_length=7, choices=TIPOS, default="ambos")
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ["nombre"]
        constraints = [models.UniqueConstraint(fields=["sociedad", "nombre"], name="categoria_banco_unica")]

    def __str__(self):
        return self.nombre


def categorias_banco_de(sociedad):
    if not sociedad.categorias_banco.exists():
        CategoriaBanco.objects.bulk_create(
            [CategoriaBanco(sociedad=sociedad, nombre=n, tipo=t) for n, t in CATEGORIAS_BANCO])
    return sociedad.categorias_banco.filter(activa=True)


class ReglaBanco(models.Model):
    """Si la descripción del movimiento contiene `texto`, se le asigna la categoría."""
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="reglas_banco")
    texto = models.CharField("Si la descripción contiene", max_length=80)
    categoria = models.ForeignKey(CategoriaBanco, on_delete=models.CASCADE, related_name="reglas")
    solo = models.CharField(max_length=6, blank=True, choices=[("", "Cargos y abonos"), ("cargo", "Solo cargos"),
                                                               ("abono", "Solo abonos")])

    class Meta:
        ordering = ["texto"]

    def __str__(self):
        return f"«{self.texto}» → {self.categoria}"

    def aplica(self, movimiento):
        if self.solo == "cargo" and movimiento.monto > 0 or self.solo == "abono" and movimiento.monto < 0:
            return False
        return self.texto.lower() in movimiento.descripcion.lower()


class MovimientoBancario(models.Model):
    cuenta = models.ForeignKey(CuentaBancaria, on_delete=models.CASCADE, related_name="movimientos")
    fecha = models.DateField()
    orden = models.PositiveIntegerField(default=0, help_text="Posición dentro del día en la cartola.")
    descripcion = models.CharField("Descripción", max_length=200)
    monto = models.BigIntegerField(help_text="Positivo: abono. Negativo: cargo.")
    saldo = models.BigIntegerField(null=True, blank=True)
    documento = models.CharField("N° documento", max_length=30, blank=True)
    sucursal = models.CharField(max_length=40, blank=True)
    huella = models.CharField(max_length=64, editable=False)
    categoria = models.ForeignKey(CategoriaBanco, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="movimientos")
    ignorado = models.BooleanField(default=False)
    nota = models.CharField(max_length=200, blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fecha", "-orden", "-id"]
        constraints = [models.UniqueConstraint(fields=["cuenta", "huella"], name="movimiento_unico")]

    def __str__(self):
        return f"{self.fecha:%d/%m/%Y} · {self.descripcion} · {self.monto}"

    @property
    def es_abono(self):
        return self.monto > 0

    @property
    def conciliado_monto(self):
        valor = getattr(self, "conciliado_a", None)
        if valor is None:
            valor = self.conciliaciones.aggregate(s=Sum("monto"))["s"] or 0
        return valor

    @property
    def por_conciliar(self):
        return max(abs(self.monto) - self.conciliado_monto, 0)

    @property
    def estado(self):
        if self.conciliado_monto and self.por_conciliar == 0:
            return "conciliado"
        if self.conciliado_monto:
            return "parcial"
        if self.ignorado:
            return "ignorado"
        if self.categoria_id:
            return "categorizado"
        return "pendiente"


class Conciliacion(models.Model):
    """Une un movimiento del banco con el pago (cobro) de una venta o el pago de una compra."""
    movimiento = models.ForeignKey(MovimientoBancario, on_delete=models.CASCADE, related_name="conciliaciones")
    pago = models.OneToOneField(Pago, null=True, blank=True, on_delete=models.CASCADE, related_name="conciliacion")
    pago_compra = models.OneToOneField(PagoCompra, null=True, blank=True, on_delete=models.CASCADE,
                                       related_name="conciliacion")
    monto = models.BigIntegerField()
    creado_pago = models.BooleanField(default=True, help_text="El pago se creó al conciliar (se borra al deshacer).")

    @property
    def documento(self):
        return self.pago.documento if self.pago_id else self.pago_compra.compra
