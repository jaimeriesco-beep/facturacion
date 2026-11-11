import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import BigIntegerField, F, OuterRef, Q, Subquery, Sum, Value
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
    email = models.EmailField("Correo de facturación", blank=True)
    contacto = models.CharField("Contacto de cobranza", max_length=80, blank=True)
    telefono = models.CharField("Teléfono", max_length=20, blank=True)
    dias_pago = models.PositiveSmallIntegerField(
        "Plazo de pago (días)", default=30,
        help_text="Días desde la emisión hasta el vencimiento. 0 = contado.")
    activo = models.BooleanField(default=True)

    class Meta:
        # Varios locales pueden compartir RUT y razón social (misma empresa, otra dirección).
        ordering = ["nombre_fantasia", "razon_social"]

    def __str__(self):
        if self.nombre_fantasia and self.nombre_fantasia.lower() != self.razon_social.lower():
            return f"{self.nombre_fantasia} · {self.razon_social}"
        return self.razon_social


class Producto(models.Model):
    sociedad = models.ForeignKey(Sociedad, on_delete=models.CASCADE, related_name="productos")
    codigo = models.CharField("Código", max_length=35, blank=True)
    nombre = models.CharField(max_length=80)
    descripcion = models.CharField("Descripción", max_length=300, blank=True)
    unidad = models.CharField(max_length=4, blank=True, help_text="Ej.: UN, HR, MES")
    precio = models.DecimalField("Precio neto", max_digits=14, decimal_places=2, default=0)
    exento = models.BooleanField("Exento de IVA", default=False)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["nombre"]
        verbose_name = "producto o servicio"
        verbose_name_plural = "productos y servicios"

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

    def ventas(self):
        """Documentos tributarios (todo menos las cotizaciones)."""
        return self.exclude(tipo=COTIZACION)

    def cotizaciones(self):
        return self.filter(tipo=COTIZACION)

    def por_cobrar(self):
        return (self.filter(tipo__in=FACTURAS, estado=Documento.EMITIDO, es_prueba=False)
                .con_saldo().filter(saldo_a__gt=0))


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
    envio = models.ForeignKey("Envio", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="documentos", editable=False)
    recurrente = models.ForeignKey(
        "Recurrente", null=True, blank=True, on_delete=models.SET_NULL, related_name="documentos")
    periodo = models.CharField(max_length=7, blank=True)  # AAAA-MM
    creado = models.DateTimeField(auto_now_add=True)

    objects = DocumentoQuerySet.as_manager()

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
        verbose_name="Producto o servicio")
    descripcion = models.CharField("Descripción", max_length=300)
    cantidad = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    precio = models.DecimalField("Precio unitario", max_digits=14, decimal_places=2, default=0)
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
