import calendar
from datetime import date, timedelta
from functools import wraps
from io import BytesIO, StringIO

from django.contrib import messages
import pyotp
import segno
from django.conf import settings
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.core.management import call_command
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from .dte import servicio
from .dte.motor import ErrorDTE
from .dte.pdf import generar_pdf
from .dte.secretos import ErrorSecreto, cifrar, descifrar
from .forms import (CertificadoForm, ClienteForm, DocumentoForm, EmitirForm,
                    ImportarClientesForm, LineasFormSet, PagoForm, ProductoForm, RecurrenteForm,
                    RecurrenteLineasFormSet, SociedadForm, UsuarioForm)
from .models import (FACTURA, FACTURA_EXENTA, FACTURAS, GUIA, NOTA_CREDITO, NOTA_DEBITO, NOTAS,
                     TIPOS, TIPOS_CORTOS, Cliente, Documento, DosPasos, Envio, Linea, Pago, Producto,
                     Recurrente, Sociedad, hoy)
from .utils import MESES, formatear_clp, formatear_rut, sin_ceros


# --- Infraestructura ----------------------------------------------------------
def con_sociedad(vista):
    """Exige sesión iniciada y deja la sociedad seleccionada en request.sociedad."""
    @login_required
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        activas = Sociedad.objects.filter(activa=True)
        sociedad = (activas.filter(pk=request.session.get("sociedad_id")).first()
                    or activas.first())
        if sociedad is None:
            messages.info(request, "Crea la primera sociedad para comenzar.")
            return redirect("sociedad_nueva")
        request.sociedad = sociedad
        return vista(request, *args, **kwargs)
    return envoltura


def excel(nombre, encabezados, filas):
    libro = Workbook()
    hoja = libro.active
    hoja.append(encabezados)
    for celda in hoja[1]:
        celda.font = Font(bold=True)
    for fila in filas:
        hoja.append(fila)
    for columna in hoja.columns:
        ancho = max(len(str(c.value)) if c.value is not None else 0 for c in columna)
        hoja.column_dimensions[columna[0].column_letter].width = min(max(ancho + 2, 10), 50)
    hoja.freeze_panes = "A2"
    salida = BytesIO()
    libro.save(salida)
    respuesta = HttpResponse(
        salida.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    respuesta["Content-Disposition"] = f'attachment; filename="{nombre}.xlsx"'
    return respuesta


def formulario_simple(request, clase, instancia, titulo, volver):
    form = clase(request.POST or None, request.FILES or None, instance=instancia)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Guardado.")
        return redirect(volver)
    return render(request, "core/form.html",
                  {"form": form, "titulo": titulo, "volver": reverse(volver)})


def lista(request, titulo, columnas, filas, nuevo, texto_nuevo, vacio, extra=()):
    return render(request, "core/lista.html", {
        "titulo": titulo, "columnas": columnas, "filas": filas, "extra": extra,
        "nuevo": reverse(nuevo), "texto_nuevo": texto_nuevo, "vacio": vacio})


def si_no(valor):
    return "Sí" if valor else "No"


# --- Inicio -------------------------------------------------------------------
@con_sociedad
def inicio(request):
    docs = request.sociedad.documentos
    por_cobrar = docs.por_cobrar()
    vencidas = por_cobrar.filter(fecha_vencimiento__lt=hoy())
    inicio_mes = hoy().replace(day=1)
    del_mes = docs.filter(estado=Documento.EMITIDO, fecha_emision__gte=inicio_mes,
                          es_prueba=False).exclude(tipo=GUIA)

    def suma(qs, campo="total"):
        return qs.aggregate(s=Sum(campo))["s"] or 0

    facturado = (suma(del_mes.exclude(tipo=NOTA_CREDITO))
                 - suma(del_mes.filter(tipo=NOTA_CREDITO)))
    periodo = hoy().strftime("%Y-%m")
    recurrentes_pendientes = sum(
        1 for r in request.sociedad.recurrentes.filter(activa=True)
        if r.aplica(hoy().year, hoy().month) and not r.documentos.filter(periodo=periodo).exists())
    return render(request, "core/inicio.html", {
        "por_cobrar": suma(por_cobrar, "saldo_a"),
        "vencido": suma(vencidas, "saldo_a"),
        "n_vencidas": vencidas.count(),
        "facturado": facturado,
        "mes": MESES[hoy().month - 1],
        "borradores": docs.filter(estado=Documento.BORRADOR).count(),
        "recurrentes_pendientes": recurrentes_pendientes,
        "proximas": por_cobrar.select_related("cliente").order_by("fecha_vencimiento")[:8],
    })


@require_POST
@login_required
def cambiar_sociedad(request):
    request.session["sociedad_id"] = int(request.POST.get("sociedad") or 0)
    return redirect("inicio")


# --- Sociedades, clientes y productos -----------------------------------------
@login_required
def sociedades(request):
    filas = [(reverse("sociedad_editar", args=[s.pk]),
              [s.razon_social, s.rut_formateado, s.giro, s.comuna, si_no(s.activa)])
             for s in Sociedad.objects.all()]
    return lista(request, "Sociedades", ["Razón social", "RUT", "Giro", "Comuna", "Activa"],
                 filas, "sociedad_nueva", "Nueva sociedad", "Aún no hay sociedades.")


@login_required
def sociedad_form(request, pk=None):
    instancia = get_object_or_404(Sociedad, pk=pk) if pk else Sociedad()
    return formulario_simple(request, SociedadForm, instancia,
                             "Editar sociedad" if pk else "Nueva sociedad", "sociedades")


@con_sociedad
def clientes(request):
    filas = [(reverse("cliente_editar", args=[c.pk]),
              [c.nombre_fantasia or c.razon_social, c.razon_social,
               c.rut_formateado or "Falta RUT", c.giro or "Falta giro", c.comuna,
               f"{c.dias_pago} días", si_no(c.activo)])
             for c in request.sociedad.clientes.all()]
    return lista(request, "Clientes",
                 ["Nombre", "Razón social", "RUT", "Giro", "Comuna", "Plazo de pago", "Activo"],
                 filas, "cliente_nuevo", "Nuevo cliente", "Aún no hay clientes.",
                 extra=[(reverse("clientes_importar"), "Importar desde Excel")])


def nombres_de_clientes(archivo):
    """Nombres únicos de la columna «Cliente(s)» de la primera hoja de un Excel."""
    try:
        hoja = load_workbook(archivo, read_only=True, data_only=True).worksheets[0]
    except Exception:
        raise ValueError("No se pudo leer el archivo: debe ser un Excel .xlsx.")
    columna, nombres, vistos = None, [], set()
    for n, fila in enumerate(hoja.iter_rows(values_only=True)):
        if columna is None:
            for i, celda in enumerate(fila):
                if str(celda or "").strip().lower() in ("cliente", "clientes"):
                    columna = i
            if columna is None and n >= 20:
                break
            continue
        valor = fila[columna] if columna < len(fila) else None
        nombre = " ".join(str(valor or "").split())
        if nombre and nombre.lower() not in vistos:
            vistos.add(nombre.lower())
            nombres.append(nombre)
    if columna is None:
        raise ValueError("No encontré una columna titulada «Cliente» o «Clientes» en la primera hoja.")
    return nombres


@con_sociedad
def clientes_importar(request):
    form = ImportarClientesForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            nombres = nombres_de_clientes(form.cleaned_data["archivo"])
        except ValueError as e:
            messages.error(request, str(e))
        else:
            existentes = {n.lower() for par in request.sociedad.clientes.values_list(
                "razon_social", "nombre_fantasia") for n in par if n}
            nuevos = [Cliente(sociedad=request.sociedad, razon_social=n[:100],
                              nombre_fantasia=n[:100])
                      for n in nombres if n.lower() not in existentes]
            Cliente.objects.bulk_create(nuevos)
            messages.success(
                request, f"Se crearon {len(nuevos)} clientes. "
                         f"{len(nombres) - len(nuevos)} ya existían y no se tocaron. "
                         "Completa RUT, giro y dirección de cada uno antes de emitirle.")
            return redirect("clientes")
    return render(request, "core/form.html", {
        "form": form, "titulo": "Importar clientes desde Excel", "volver": reverse("clientes"),
        "texto_boton": "Importar"})


# --- Verificación en dos pasos ------------------------------------------------
@login_required
def dos_pasos(request):
    """Pide el código de la app de autenticación; la primera vez muestra el QR para enlazarla."""
    if request.session.get("dos_pasos") or not settings.EXIGIR_DOS_PASOS:
        return redirect("inicio")
    registro = DosPasos.objects.filter(usuario=request.user).first()
    if registro is None:
        registro = DosPasos.objects.create(
            usuario=request.user, secreto=cifrar(pyotp.random_base32().encode()))
    secreto = descifrar(registro.secreto).decode()
    totp = pyotp.TOTP(secreto)
    if request.method == "POST":
        codigo = "".join(request.POST.get("codigo", "").split())
        if codigo.isdigit() and totp.verify(codigo, valid_window=1):
            registro.confirmado = True
            registro.save(update_fields=["confirmado"])
            request.session["dos_pasos"] = True
            request.session.pop("dos_pasos_fallos", None)
            return redirect("inicio")
        fallos = request.session.get("dos_pasos_fallos", 0) + 1
        request.session["dos_pasos_fallos"] = fallos
        if fallos >= 5:
            logout(request)
            messages.error(request, "Demasiados intentos fallidos. Vuelve a iniciar sesión.")
            return redirect("login")
        messages.error(request, "Código incorrecto. Revisa la app de autenticación e intenta de nuevo.")
    contexto = {"confirmado": registro.confirmado}
    if not registro.confirmado:
        enlace = totp.provisioning_uri(name=request.user.username, issuer_name="Facturación")
        contexto.update(secreto=secreto, qr=segno.make(enlace, error="m").svg_data_uri(scale=5))
    return render(request, "registration/dos_pasos.html", contexto)


# --- Usuarios -----------------------------------------------------------------
def solo_administradores(vista):
    @login_required
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not request.user.is_superuser:
            messages.error(request, "Solo los administradores pueden gestionar usuarios.")
            return redirect("inicio")
        return vista(request, *args, **kwargs)
    return envoltura


@solo_administradores
def usuarios(request):
    filas = [(reverse("usuario_editar", args=[u.pk]),
              [u.username, u.get_full_name(), u.email, si_no(u.is_superuser), si_no(u.is_active),
               u.last_login.strftime("%d/%m/%Y") if u.last_login else "Nunca"])
             for u in get_user_model().objects.order_by("username")]
    return lista(request, "Usuarios",
                 ["Usuario", "Nombre", "Correo", "Administrador", "Activo", "Último ingreso"],
                 filas, "usuario_nuevo", "Nuevo usuario", "",
                 extra=[(reverse("respaldo"), "Descargar respaldo")])


@solo_administradores
def respaldo(request):
    """Descarga un respaldo completo de los datos (certificados y folios van cifrados)."""
    salida = StringIO()
    call_command("dumpdata", "core", "auth.user", indent=1, stdout=salida)
    return _descarga(salida.getvalue().encode(), "application/json",
                     f"respaldo_facturacion_{hoy():%Y%m%d}.json")


@solo_administradores
def usuario_form(request, pk=None):
    Usuario = get_user_model()
    instancia = get_object_or_404(Usuario, pk=pk) if pk else Usuario(is_superuser=True)
    if pk and instancia == request.user and request.method == "POST" and not (
            request.POST.get("is_superuser") and request.POST.get("is_active")):
        messages.error(request, "No puedes quitarte a ti mismo el acceso de administrador.")
        return redirect("usuarios")
    return formulario_simple(request, UsuarioForm, instancia,
                             "Editar usuario" if pk else "Nuevo usuario", "usuarios")


@con_sociedad
def cliente_form(request, pk=None):
    instancia = (get_object_or_404(Cliente, pk=pk, sociedad=request.sociedad) if pk
                 else Cliente(sociedad=request.sociedad))
    return formulario_simple(request, ClienteForm, instancia,
                             "Editar cliente" if pk else "Nuevo cliente", "clientes")


@con_sociedad
def productos(request):
    filas = [(reverse("producto_editar", args=[p.pk]),
              [p.nombre, p.codigo, p.unidad, formatear_clp(p.precio),
               "Exento" if p.exento else "Afecto", si_no(p.activo)])
             for p in request.sociedad.productos.all()]
    return lista(request, "Productos y servicios",
                 ["Nombre", "Código", "Unidad", "Precio neto", "IVA", "Activo"],
                 filas, "producto_nuevo", "Nuevo producto o servicio",
                 "Aún no hay productos ni servicios.")


@con_sociedad
def producto_form(request, pk=None):
    instancia = (get_object_or_404(Producto, pk=pk, sociedad=request.sociedad) if pk
                 else Producto(sociedad=request.sociedad))
    return formulario_simple(request, ProductoForm, instancia,
                             "Editar producto o servicio" if pk else "Nuevo producto o servicio",
                             "productos")


# --- Documentos ---------------------------------------------------------------
def _documento(request, pk):
    return get_object_or_404(
        Documento.objects.select_related("cliente", "referencia"),
        pk=pk, sociedad=request.sociedad)


@con_sociedad
def documentos(request):
    qs = request.sociedad.documentos.select_related("cliente").con_saldo()
    tipo, estado, q = (request.GET.get(k, "").strip() for k in ("tipo", "estado", "q"))
    if tipo.isdigit():
        qs = qs.filter(tipo=int(tipo))
    if estado:
        qs = qs.filter(estado=estado)
    if q:
        filtro = (Q(cliente__razon_social__icontains=q) | Q(cliente__rut__icontains=q)
                  | Q(cliente__nombre_fantasia__icontains=q))
        if q.isdigit():
            filtro |= Q(folio=int(q))
        qs = qs.filter(filtro)
    return render(request, "core/documentos.html", {
        "documentos": qs[:500], "tipos": TIPOS, "estados": Documento.ESTADOS[:2],
        "tipo": tipo, "estado": estado, "q": q})


def _editar_con_lineas(request, form, lineas, al_guardar, contexto):
    if request.method == "POST" and form.is_valid() and lineas.is_valid():
        with transaction.atomic():
            objeto = form.save()
            lineas.instance = objeto
            lineas.save()
            destino = al_guardar(objeto)
        messages.success(request, "Guardado.")
        return redirect(destino)
    productos_js = {
        str(p.pk): {"descripcion": p.descripcion or p.nombre, "precio": sin_ceros(p.precio),
                    "exento": p.exento}
        for p in request.sociedad.productos.filter(activo=True)}
    return render(request, "core/documento_form.html",
                  {"form": form, "lineas": lineas, "productos_js": productos_js, **contexto})


def _form_documento(request, doc, titulo, volver):
    form = DocumentoForm(request.POST or None, instance=doc, sociedad=request.sociedad)
    lineas = LineasFormSet(request.POST or None, instance=doc,
                           form_kwargs={"sociedad": request.sociedad})

    def al_guardar(d):
        if d.tipo == FACTURA_EXENTA:
            d.lineas.update(exento=True)
        d.recalcular()
        return reverse("documento", args=[d.pk])

    return _editar_con_lineas(request, form, lineas, al_guardar, {
        "titulo": titulo, "volver": volver, "doc": doc,
        "solo_exento": doc.tipo == FACTURA_EXENTA})


@con_sociedad
def documento_nuevo(request, tipo):
    if tipo not in FACTURAS + (GUIA,):
        return redirect("documentos")
    if not request.sociedad.clientes.filter(activo=True).exists():
        messages.info(request, "Primero crea un cliente para poder facturarle.")
        return redirect("cliente_nuevo")
    doc = Documento(sociedad=request.sociedad, tipo=tipo,
                    ind_traslado=1 if tipo == GUIA else None)
    return _form_documento(request, doc, f"Nueva {TIPOS_CORTOS[tipo].lower()}",
                           reverse("documentos"))


@con_sociedad
def documento_editar(request, pk):
    doc = _documento(request, pk)
    if doc.estado != Documento.BORRADOR:
        messages.error(request, "Solo se pueden editar los borradores.")
        return redirect("documento", pk=pk)
    return _form_documento(request, doc, f"Editar {doc.tipo_corto.lower()}",
                           reverse("documento", args=[pk]))


@con_sociedad
def documento_detalle(request, pk):
    doc = _documento(request, pk)
    return render(request, "core/documento_detalle.html", {
        "doc": doc, "lineas": doc.lineas.all(), "pagos": doc.pagos.all(),
        "notas": doc.notas.filter(tipo__in=NOTAS),
        "errores": doc.errores_para_emitir() if doc.estado == Documento.BORRADOR else [],
        "faltantes": servicio.faltantes(doc) if doc.estado == Documento.BORRADOR else [],
        "facturas_de_guia": doc.notas.filter(tipo__in=FACTURAS) if doc.es_guia else [],
        "emitir_form": EmitirForm(),
        "pago_form": PagoForm(documento=doc, initial={"monto": max(doc.saldo, 0)}),
        "NC": NOTA_CREDITO, "ND": NOTA_DEBITO})


@require_POST
@con_sociedad
def documento_emitir(request, pk):
    doc = _documento(request, pk)
    form = EmitirForm(request.POST)
    errores = doc.errores_para_emitir()
    if doc.estado != Documento.BORRADOR:
        errores = ["El documento ya está emitido."]
    elif not form.is_valid():
        errores.append("Indica un folio válido.")
    elif request.sociedad.documentos.filter(
            tipo=doc.tipo, folio=form.cleaned_data["folio"], es_prueba=False).exists():
        errores.append(f"Ya existe una {doc.tipo_corto.lower()} con el folio "
                       f"{form.cleaned_data['folio']}.")
    if errores:
        for e in errores:
            messages.error(request, e)
        return redirect("documento", pk=pk)
    doc.folio = form.cleaned_data["folio"]
    doc.estado = Documento.EMITIDO
    doc.estado_sii = "portal"
    if doc.es_factura and not doc.fecha_vencimiento:
        doc.fecha_vencimiento = doc.fecha_emision + timedelta(days=doc.cliente.dias_pago)
    doc.save()
    messages.success(request, f"{doc} registrada como emitida.")
    return redirect("documento", pk=pk)


@require_POST
@con_sociedad
def documento_reabrir(request, pk):
    """Corrige un registro mal ingresado. No anula nada ante el SII."""
    doc = _documento(request, pk)
    if doc.es_propio:
        messages.error(request, "Este documento fue timbrado y firmado: su folio ya está usado. "
                                "Para dejarlo sin efecto, emite una nota de crédito.")
    elif doc.pagos.exists() or doc.notas.exists():
        messages.error(request, "No se puede reabrir: tiene pagos o notas asociadas.")
    elif doc.estado == Documento.EMITIDO:
        doc.estado, doc.folio, doc.estado_sii = Documento.BORRADOR, None, ""
        doc.save()
        messages.success(request, "El documento volvió a borrador.")
    return redirect("documento", pk=pk)


@require_POST
@con_sociedad
def documento_eliminar(request, pk):
    doc = _documento(request, pk)
    if doc.estado != Documento.BORRADOR:
        messages.error(request, "Solo se pueden eliminar los borradores.")
        return redirect("documento", pk=pk)
    doc.delete()
    messages.success(request, "Borrador eliminado.")
    return redirect("documentos")


@require_POST
@con_sociedad
def nota_nueva(request, pk, tipo):
    """Crea el borrador de una nota de crédito o débito sobre un documento emitido."""
    origen = _documento(request, pk)
    if tipo not in NOTAS or not origen.es_factura or origen.estado != Documento.EMITIDO:
        messages.error(request, "Las notas se emiten sobre facturas ya emitidas.")
        return redirect("documento", pk=pk)
    with transaction.atomic():
        nota = Documento.objects.create(
            sociedad=request.sociedad, cliente=origen.cliente, tipo=tipo, referencia=origen,
            codigo_referencia=1 if tipo == NOTA_CREDITO else 3,
            razon_referencia=(f"Anula {origen.tipo_corto.lower()} N° {origen.folio}"
                              if tipo == NOTA_CREDITO else ""))
        if tipo == NOTA_CREDITO:
            for l in origen.lineas.all():
                Linea.objects.create(
                    documento=nota, producto=l.producto, descripcion=l.descripcion,
                    cantidad=l.cantidad, precio=l.precio, descuento=l.descuento, exento=l.exento)
            nota.recalcular()
    messages.info(request, "Borrador creado. Revisa el motivo y el detalle antes de emitir.")
    return redirect("documento_editar", pk=nota.pk)


@require_POST
@con_sociedad
def guia_facturar(request, pk):
    """Crea el borrador de factura a partir de una guía de despacho emitida."""
    guia = _documento(request, pk)
    if not guia.es_guia or guia.estado != Documento.EMITIDO:
        messages.error(request, "Solo se facturan guías de despacho emitidas.")
        return redirect("documento", pk=pk)
    with transaction.atomic():
        factura = Documento.objects.create(
            sociedad=request.sociedad, cliente=guia.cliente, tipo=FACTURA, referencia=guia,
            razon_referencia=f"Guía de despacho N° {guia.folio}",
            fecha_vencimiento=hoy() + timedelta(days=guia.cliente.dias_pago))
        for l in guia.lineas.all():
            Linea.objects.create(
                documento=factura, producto=l.producto, descripcion=l.descripcion,
                cantidad=l.cantidad, precio=l.precio, descuento=l.descuento, exento=l.exento)
        factura.recalcular()
    messages.info(request, "Borrador de factura creado a partir de la guía.")
    return redirect("documento_editar", pk=factura.pk)


# --- Emisión electrónica propia ------------------------------------------------
def _intentar(request, accion, exito):
    """Ejecuta una operación del motor y muestra el resultado como mensaje."""
    try:
        resultado = accion()
    except (ErrorDTE, ErrorSecreto) as e:
        messages.error(request, str(e))
        return None
    messages.success(request, exito(resultado) if callable(exito) else exito)
    return resultado


@require_POST
@con_sociedad
def documento_timbrar(request, pk):
    doc = _documento(request, pk)
    _intentar(request, lambda: servicio.emitir(doc),
              lambda d: f"{d} timbrada y firmada. Ya puedes descargarla y enviarla al SII.")
    return redirect("documento", pk=pk)


def _descarga(contenido, tipo, nombre, adjunto=True):
    respuesta = HttpResponse(contenido, content_type=tipo)
    modo = "attachment" if adjunto else "inline"
    respuesta["Content-Disposition"] = f'{modo}; filename="{nombre}"'
    return respuesta


def _nombre_archivo(doc):
    return f"{doc.sociedad.rut}_T{doc.tipo}_F{doc.folio}"


@con_sociedad
def documento_xml(request, pk):
    doc = _documento(request, pk)
    if not doc.es_propio:
        return redirect("documento", pk=pk)
    return _descarga(bytes(doc.xml_firmado), "application/xml", _nombre_archivo(doc) + ".xml")


@con_sociedad
def documento_pdf(request, pk):
    doc = _documento(request, pk)
    if not doc.es_propio:
        return redirect("documento", pk=pk)
    cedible = request.GET.get("cedible") == "1"
    nombre = _nombre_archivo(doc) + ("_cedible" if cedible else "") + ".pdf"
    return _descarga(generar_pdf(doc, cedible=cedible), "application/pdf", nombre, adjunto=False)


@require_POST
@con_sociedad
def documento_enviar(request, pk):
    doc = _documento(request, pk)
    _intentar(request, lambda: servicio.enviar(request.sociedad, [doc]),
              lambda e: f"Enviado al SII. Track ID {e.track_id}. Consulta el estado en unos minutos.")
    return redirect("documento", pk=pk)


@require_POST
@con_sociedad
def enviar_pendientes(request):
    """Envía en un solo sobre todos los documentos timbrados que aún no se han enviado."""
    pendientes = list(request.sociedad.documentos.filter(
        xml_firmado__isnull=False, envio__isnull=True,
        es_prueba=request.sociedad.ambiente == Sociedad.CERTIFICACION).order_by("tipo", "folio"))
    _intentar(request, lambda: servicio.enviar(request.sociedad, pendientes),
              lambda e: f"{len(pendientes)} documentos enviados en un sobre. Track ID {e.track_id}.")
    return redirect("conexion_sii")


@require_POST
@con_sociedad
def envio_estado(request, pk):
    envio = get_object_or_404(Envio, pk=pk, sociedad=request.sociedad)
    _intentar(request, lambda: servicio.consultar(envio),
              lambda e: f"Estado del envío {e.track_id}: {e.detalle or e.estado}")
    doc_id = request.POST.get("documento", "")
    return redirect("documento", pk=int(doc_id)) if doc_id.isdigit() else redirect("conexion_sii")


@con_sociedad
def conexion_sii(request):
    soc = request.sociedad
    cert_form = CertificadoForm()
    if request.method == "POST" and request.POST.get("accion") == "certificado":
        cert_form = CertificadoForm(request.POST, request.FILES)
        if cert_form.is_valid():
            datos = cert_form.cleaned_data
            if _intentar(request, lambda: servicio.guardar_certificado(
                    soc, datos["archivo"].read(), datos["clave"], datos["rut_titular"]),
                    lambda f: f"Certificado de {f.titular} cargado. Vence el {f.vence:%d/%m/%Y}."):
                return redirect("conexion_sii")
    elif request.method == "POST" and request.POST.get("accion") == "caf":
        archivos = request.FILES.getlist("archivos")
        if not archivos:
            messages.error(request, "Selecciona al menos un archivo CAF.")
        for archivo in archivos:
            _intentar(request, lambda: servicio.guardar_caf(soc, archivo.read()),
                      lambda c: f"Folios de {TIPOS_CORTOS[c.tipo].lower()} cargados: "
                                f"{c.desde} al {c.hasta}.")
        return redirect("conexion_sii")
    por_enviar = soc.documentos.filter(
        xml_firmado__isnull=False, envio__isnull=True,
        es_prueba=soc.ambiente == Sociedad.CERTIFICACION).count()
    return render(request, "core/sii.html", {
        "certificado": getattr(soc, "certificado", None), "cert_form": cert_form,
        "cafs": soc.cafs.filter(ambiente=soc.ambiente), "envios": soc.envios.all()[:20],
        "por_enviar": por_enviar,
        "faltan_datos": [n for n, v in (
            ("giro", soc.giro), ("código de actividad económica", soc.acteco),
            ("dirección", soc.direccion), ("comuna", soc.comuna),
            ("fecha de resolución SII", soc.resolucion_fecha)) if not v]})


@login_required
def sociedad_logo(request, pk):
    sociedad = get_object_or_404(Sociedad, pk=pk)
    if not sociedad.logo:
        return HttpResponse(status=404)
    respuesta = HttpResponse(bytes(sociedad.logo), content_type="image/png")
    respuesta["Cache-Control"] = "private, max-age=300"
    return respuesta


# --- Pagos y cobranza ---------------------------------------------------------
@require_POST
@con_sociedad
def pago_nuevo(request, pk):
    doc = _documento(request, pk)
    form = PagoForm(request.POST, documento=doc)
    if not doc.es_factura or doc.estado != Documento.EMITIDO:
        messages.error(request, "Solo se registran pagos sobre facturas emitidas.")
    elif form.is_valid():
        pago = form.save(commit=False)
        pago.documento = doc
        pago.save()
        messages.success(request, "Pago registrado.")
    else:
        for errores in form.errors.values():
            for e in errores:
                messages.error(request, e)
    return redirect("documento", pk=pk)


@require_POST
@con_sociedad
def pago_eliminar(request, pk):
    pago = get_object_or_404(Pago, pk=pk, documento__sociedad=request.sociedad)
    doc_id = pago.documento_id
    pago.delete()
    messages.success(request, "Pago eliminado.")
    return redirect("documento", pk=doc_id)


TRAMOS = ["Por vencer", "1 a 30 días", "31 a 60 días", "61 a 90 días", "Más de 90 días"]


def _tramo(dias):
    if dias <= 0:
        return 0
    return min((dias - 1) // 30 + 1, 4)


@con_sociedad
def cobranza(request):
    qs = (request.sociedad.documentos.por_cobrar().select_related("cliente")
          .order_by("fecha_vencimiento", "id"))
    pendientes = list(qs)
    con_deuda = sorted({d.cliente for d in pendientes}, key=lambda c: str(c).lower())
    cliente = request.GET.get("cliente", "")
    if cliente.isdigit():
        pendientes = [d for d in pendientes if d.cliente_id == int(cliente)]
    if request.GET.get("formato") == "excel":
        return excel(f"cobranza_{hoy():%Y%m%d}", [
            "Cliente", "RUT", "Documento", "Folio", "Emisión", "Vencimiento", "Días vencido",
            "Tramo", "Total", "Notas de crédito", "Notas de débito", "Pagado", "Saldo",
            "Contacto", "Correo"],
            [[d.cliente.razon_social, formatear_rut(d.cliente.rut), d.tipo_corto, d.folio,
              d.fecha_emision, d.fecha_vencimiento, max(d.dias_vencido, 0),
              TRAMOS[_tramo(d.dias_vencido)], d.total, d.notas_credito, d.notas_debito,
              d.pagado, d.saldo, d.cliente.contacto, d.cliente.email] for d in pendientes])
    tramos = [0] * 5
    for d in pendientes:
        tramos[_tramo(d.dias_vencido)] += d.saldo
    return render(request, "core/cobranza.html", {
        "pendientes": pendientes, "tramos": list(zip(TRAMOS, tramos)),
        "total": sum(tramos), "cliente": cliente,
        "clientes": con_deuda})


# --- Facturación recurrente ---------------------------------------------------
@con_sociedad
def recurrentes(request):
    periodo = hoy().strftime("%Y-%m")
    items = []
    for r in request.sociedad.recurrentes.select_related("cliente").prefetch_related("lineas"):
        items.append((r, r.documentos.filter(periodo=periodo).first(),
                      r.aplica(hoy().year, hoy().month)))
    return render(request, "core/recurrentes.html", {
        "items": items, "mes": MESES[hoy().month - 1], "anio": hoy().year,
        "pendientes": sum(1 for _, doc, aplica in items if aplica and not doc)})


@con_sociedad
def recurrente_form(request, pk=None):
    if not request.sociedad.clientes.filter(activo=True).exists():
        messages.info(request, "Primero crea un cliente.")
        return redirect("cliente_nuevo")
    rec = (get_object_or_404(Recurrente, pk=pk, sociedad=request.sociedad) if pk
           else Recurrente(sociedad=request.sociedad))
    form = RecurrenteForm(request.POST or None, instance=rec, sociedad=request.sociedad)
    lineas = RecurrenteLineasFormSet(request.POST or None, instance=rec,
                                     form_kwargs={"sociedad": request.sociedad})
    return _editar_con_lineas(request, form, lineas, lambda r: reverse("recurrentes"), {
        "titulo": "Editar facturación recurrente" if pk else "Nueva facturación recurrente",
        "volver": reverse("recurrentes"), "es_recurrente": True})


@require_POST
@con_sociedad
def recurrentes_generar(request):
    creados = [d for d in (r.generar(hoy().year, hoy().month)
                           for r in request.sociedad.recurrentes.filter(activa=True)) if d]
    if creados:
        messages.success(request, f"Se crearon {len(creados)} borradores para revisar y emitir.")
        return redirect(reverse("documentos") + "?estado=borrador")
    messages.info(request, "No había facturas recurrentes pendientes este mes.")
    return redirect("recurrentes")


# --- Reportes -----------------------------------------------------------------
@con_sociedad
def reportes(request):
    try:
        desde = date.fromisoformat(request.GET.get("desde", ""))
        hasta = date.fromisoformat(request.GET.get("hasta", ""))
    except ValueError:
        desde = hoy().replace(day=1)
        hasta = desde.replace(day=calendar.monthrange(desde.year, desde.month)[1])
        return render(request, "core/reportes.html", {"desde": desde, "hasta": hasta})
    docs = (request.sociedad.documentos.filter(
        estado=Documento.EMITIDO, es_prueba=False, fecha_emision__range=(desde, hasta))
        .select_related("cliente", "referencia").con_saldo().order_by("fecha_emision", "tipo", "folio"))
    filas = []
    for d in docs:
        signo = -1 if d.tipo == NOTA_CREDITO else 1
        filas.append([
            d.tipo, d.tipo_corto, d.folio, d.fecha_emision, d.fecha_vencimiento,
            formatear_rut(d.cliente.rut), d.cliente.razon_social,
            signo * d.neto, signo * d.exento, signo * d.iva, signo * d.total,
            d.pagado if d.es_factura else None, d.saldo if d.es_factura else None,
            str(d.referencia) if d.referencia else "", d.observaciones])
    return excel(f"ventas_{desde:%Y%m%d}_{hasta:%Y%m%d}", [
        "Código", "Tipo", "Folio", "Emisión", "Vencimiento", "RUT", "Cliente", "Neto",
        "Exento", "IVA", "Total", "Pagado", "Saldo", "Referencia", "Observaciones"], filas)
