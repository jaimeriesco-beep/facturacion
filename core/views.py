import calendar
import re
from datetime import date, timedelta
from functools import wraps
from urllib.parse import quote
from io import BytesIO, StringIO

from django.contrib import messages
import pyotp
import segno
from django.conf import settings
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetConfirmView, PasswordResetView
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.core.management import call_command
from django.db import transaction
from django.db.models import Max, Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from . import marketing, resumen
from .dte import servicio
from .dte.motor import ErrorDTE
from .dte.pdf import generar_pdf
from .dte.secretos import ErrorSecreto, cifrar, descifrar
from .forms import (CertificadoForm, ClienteForm, DocumentoForm, EmitirForm, EnviarCorreoForm,
                    ImportarClientesForm, LineasFormSet, MarcarPagadoForm, PagoForm, ProductoForm, RecurrenteForm,
                    RecurrenteLineasFormSet, SociedadForm, UsuarioForm)
from .models import (COTIZACION, FACTURA, FACTURA_EXENTA, FACTURAS, GUIA, NOTA_CREDITO,
                     NOTA_DEBITO, NOTAS, DIAS_AVISO,
                     TIPOS, TIPOS_CORTOS, Cliente, Documento, DosPasos, Envio, Linea, Pago, Producto,
                     Recurrente, Sociedad, hoy)
from .templatetags.formato import numero
from .utils import MESES, actividades_sii, decimal_cl, formatear_clp, formatear_rut


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


def formulario_simple(request, clase, instancia, titulo, volver, al_guardar=None, cancelar=None,
                      plantilla="core/form.html"):
    form = clase(request.POST or None, request.FILES or None, instance=instancia)
    if request.method == "POST" and form.is_valid():
        objeto = form.save()
        messages.success(request, "Guardado.")
        return redirect(al_guardar(objeto) if al_guardar else volver)
    return render(request, plantilla, {
        "form": form, "titulo": titulo, "volver": cancelar or reverse(volver),
        "actividades": actividades_sii() if "giro" in form.fields else None})


def lista(request, titulo, columnas, filas, nuevo, texto_nuevo, vacio, extra=(), eliminar=None,
          aviso_eliminar="No se puede deshacer."):
    """Listado genérico. Con `eliminar` (función fila -> (url, nombre)) agrega un basurero."""
    filas = [{"url": url, "celdas": celdas,
              "eliminar": eliminar(i) if eliminar else None} for i, (url, celdas) in enumerate(filas)]
    return render(request, "core/lista.html", {
        "titulo": titulo, "columnas": columnas, "filas": filas, "extra": extra,
        "con_eliminar": bool(eliminar), "aviso_eliminar": aviso_eliminar,
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
                          es_prueba=False).exclude(tipo__in=(GUIA, COTIZACION))

    def suma(qs, campo="total"):
        return qs.aggregate(s=Sum(campo))["s"] or 0

    facturado = (suma(del_mes.exclude(tipo=NOTA_CREDITO))
                 - suma(del_mes.filter(tipo=NOTA_CREDITO)))
    periodo = hoy().strftime("%Y-%m")
    recurrentes_pendientes = sum(
        1 for r in request.sociedad.recurrentes.filter(activa=True)
        if r.aplica(hoy().year, hoy().month) and not r.documentos.filter(periodo=periodo).exists())
    datos = resumen.calcular(request.sociedad, request.GET.get("vista", "mes"))
    return render(request, "core/inicio.html", {
        **datos, "graficos": {**datos["series"], "antiguedad": resumen.antiguedad(request.sociedad)},
        "tabla": list(reversed(list(zip(datos["series"]["rangos"], datos["series"]["ventas"],
                                        datos["series"]["facturado"], datos["series"]["cobrado"])))),
        "por_cobrar": suma(por_cobrar, "saldo_a"),
        "vencido": suma(vencidas, "saldo_a"),
        "n_vencidas": vencidas.count(),
        "facturado": facturado,
        "mes": MESES[hoy().month - 1],
        "borradores": docs.ventas().filter(estado=Documento.BORRADOR).count(),
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
    todos = list(request.sociedad.clientes.all())
    filas = [(reverse("cliente_editar", args=[c.pk]),
              [c.nombre_fantasia or c.razon_social, c.razon_social,
               c.rut_formateado or "Falta RUT", c.giro or "Falta giro", c.comuna,
               f"{c.dias_pago} días", si_no(c.activo)])
             for c in todos]
    return lista(request, "Clientes",
                 ["Nombre", "Razón social", "RUT", "Giro", "Comuna", "Plazo de pago", "Activo"],
                 filas, "cliente_nuevo", "Nuevo cliente", "Aún no hay clientes.",
                 extra=[(reverse("clientes_importar"), "Importar desde Excel")],
                 eliminar=lambda i: (reverse("cliente_eliminar", args=[todos[i].pk]), str(todos[i])))


@require_POST
@con_sociedad
def cliente_eliminar(request, pk):
    cliente = get_object_or_404(Cliente, pk=pk, sociedad=request.sociedad)
    if cliente.documentos.exists() or cliente.recurrentes.exists():
        # Un cliente con historial no se borra: se perderían sus facturas, cotizaciones y cobranza.
        messages.error(request, f"«{cliente}» tiene documentos o facturación recurrente, así que no "
                                "se puede eliminar. Para que no aparezca al facturar, ábrelo y "
                                "desmarca «Activo».")
    else:
        nombre = str(cliente)
        cliente.delete()
        messages.success(request, f"Cliente «{nombre}» eliminado.")
    return redirect("clientes")


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


# --- Recuperación de clave -----------------------------------------------------
class RecuperarView(PasswordResetView):
    """Envía por correo un enlace de un solo uso para definir una clave nueva."""
    template_name = "registration/recuperar.html"
    email_template_name = "registration/recuperar_correo.txt"
    subject_template_name = "registration/recuperar_asunto.txt"
    success_url = reverse_lazy("password_reset_done")
    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {"correo_configurado": settings.CORREO_CONFIGURADO}

    def form_valid(self, form):
        # Máximo 3 solicitudes por hora para un mismo correo. La respuesta es siempre la
        # misma, exista o no el correo, para no revelar quién tiene cuenta.
        llave = "recuperar:" + form.cleaned_data["email"].strip().lower()
        pedidos = cache.get(llave, 0)
        if not settings.CORREO_CONFIGURADO or pedidos >= 3:
            return redirect(self.success_url)
        cache.set(llave, pedidos + 1, 60 * 60)
        try:
            return super().form_valid(form)
        except Exception:  # un fallo del proveedor de correo no debe mostrar un error técnico
            return redirect(self.success_url)


class NuevaClaveView(PasswordResetConfirmView):
    template_name = "registration/recuperar_nueva.html"
    success_url = reverse_lazy("login")

    def form_valid(self, form):
        respuesta = super().form_valid(form)
        cache.delete("ingreso:" + form.user.username.lower())  # levanta un bloqueo por intentos
        messages.success(self.request, "Clave cambiada. Ya puedes ingresar con la nueva.")
        return respuesta


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
    # Si se llegó desde una factura en preparación, al guardar se vuelve a ella con el
    # cliente ya elegido.
    origen = request.GET.get("volver", "")
    if not (origen.startswith("/documentos/") and url_has_allowed_host_and_scheme(origen, None)):
        origen = ""
    return formulario_simple(
        request, ClienteForm, instancia, "Editar cliente" if pk else "Nuevo cliente", "clientes",
        al_guardar=(lambda c: f"{origen}?cliente={c.pk}") if origen else None,
        cancelar=origen or None, plantilla="core/cliente_form.html")


@con_sociedad
def productos(request):
    productos = list(request.sociedad.productos.all())
    filas = [(reverse("producto_editar", args=[p.pk]),
              [p.nombre, p.codigo, p.unidad, "$" + numero(p.precio),
               "Exento" if p.exento else "Afecto", si_no(p.activo)])
             for p in productos]
    return lista(request, "Productos (SKU)",
                 ["Nombre", "SKU", "Unidad", "Precio neto", "IVA", "Activo"],
                 filas, "producto_nuevo", "Nuevo producto",
                 "Aún no hay productos.",
                 eliminar=lambda i: (reverse("producto_eliminar", args=[productos[i].pk]),
                                     productos[i].nombre),
                 aviso_eliminar="Las facturas que ya lo usan no cambian.")


@require_POST
@con_sociedad
def producto_eliminar(request, pk):
    producto = get_object_or_404(Producto, pk=pk, sociedad=request.sociedad)
    nombre = producto.nombre
    producto.delete()  # las facturas y recurrentes que lo usaban conservan su detalle
    messages.success(request, f"Producto «{nombre}» eliminado.")
    return redirect("productos")


@con_sociedad
def producto_form(request, pk=None):
    instancia = (get_object_or_404(Producto, pk=pk, sociedad=request.sociedad) if pk
                 else Producto(sociedad=request.sociedad))
    return formulario_simple(request, ProductoForm, instancia,
                             "Editar producto" if pk else "Nuevo producto",
                             "productos")


# --- Documentos ---------------------------------------------------------------
def _documento(request, pk):
    return get_object_or_404(
        Documento.objects.select_related("cliente", "referencia"),
        pk=pk, sociedad=request.sociedad)


@con_sociedad
def documentos(request, cotizaciones=False):
    base = request.sociedad.documentos
    qs = ((base.cotizaciones() if cotizaciones else base.ventas())
          .select_related("cliente").con_saldo().con_asociados())
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
        "documentos": qs[:500], "tipos": [t for t in TIPOS if t[0] != COTIZACION],
        "estados": Documento.ESTADOS[:2], "tipo": tipo, "estado": estado, "q": q,
        "cotizaciones": cotizaciones, "COTIZACION": COTIZACION})


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
        str(p.pk): {"descripcion": p.descripcion or p.nombre, "precio": decimal_cl(p.precio),
                    "exento": p.exento}
        for p in request.sociedad.productos.filter(activo=True)}
    return render(request, "core/documento_form.html",
                  {"form": form, "lineas": lineas, "productos_js": productos_js, **contexto})


def _form_documento(request, doc, titulo, volver):
    elegido = request.GET.get("cliente", "")
    if elegido.isdigit() and request.sociedad.clientes.filter(pk=elegido, activo=True).exists():
        doc.cliente_id = int(elegido)  # viene de crear un cliente desde esta misma pantalla
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
        "nuevo_cliente": (reverse("cliente_nuevo") + "?volver=" + quote(request.path)
                          if "cliente" in form.fields else ""),
        "solo_exento": doc.tipo == FACTURA_EXENTA})


@con_sociedad
def documento_nuevo(request, tipo):
    if tipo not in FACTURAS + (GUIA, COTIZACION):
        return redirect("documentos")
    if not request.sociedad.clientes.filter(activo=True).exists():
        messages.info(request, "Primero crea un cliente para poder facturarle.")
        return redirect("cliente_nuevo")
    doc = Documento(sociedad=request.sociedad, tipo=tipo,
                    ind_traslado=1 if tipo == GUIA else None)
    return _form_documento(request, doc, f"Nueva {TIPOS_CORTOS[tipo].lower()}",
                           reverse("cotizaciones" if tipo == COTIZACION else "documentos"))


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
        "faltantes": (servicio.faltantes(doc)
                      if doc.estado == Documento.BORRADOR and not doc.es_cotizacion else []),
        "facturas_de_guia": doc.notas.filter(tipo__in=FACTURAS) if doc.es_guia else [],
        "asociados": doc.asociados(),
        "emitir_form": EmitirForm(),
        "pago_form": PagoForm(documento=doc, initial={"monto": max(doc.saldo, 0)}),
        "NC": NOTA_CREDITO, "ND": NOTA_DEBITO})


@con_sociedad
def documento_asociados(request, pk):
    """Documentos vinculados a uno, para la ventana del listado."""
    doc = _documento(request, pk)
    viene_de, generados = doc.asociados()
    return render(request, "core/_asociados.html", {
        "doc": doc, "viene_de": viene_de, "generados": generados, "con_acciones": True,
        "NC": NOTA_CREDITO, "ND": NOTA_DEBITO})


@require_POST
@con_sociedad
def documento_emitir(request, pk):
    doc = _documento(request, pk)
    if doc.es_cotizacion:
        return redirect("documento", pk=pk)
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
    _agradecer(request, doc)
    return redirect("documento", pk=pk)


@require_POST
@con_sociedad
def documento_reabrir(request, pk):
    """Corrige un registro mal ingresado. No anula nada ante el SII."""
    doc = _documento(request, pk)
    if doc.es_cotizacion:
        if doc.estado == Documento.EMITIDO:  # se corrige y conserva su número
            doc.estado = Documento.BORRADOR
            doc.save(update_fields=["estado"])
            messages.success(request, "La cotización quedó abierta para corregirla.")
    elif doc.es_propio:
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
    destino = "cotizaciones" if doc.es_cotizacion else "documentos"
    doc.delete()
    messages.success(request, "Borrador eliminado.")
    return redirect(destino)


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


# --- Cotizaciones (documento interno, con numeración propia) --------------------
@require_POST
@con_sociedad
def cotizacion_emitir(request, pk):
    """Deja la cotización lista para enviar al cliente y le asigna su número correlativo."""
    doc = _documento(request, pk)
    errores = doc.errores_para_emitir()
    if not doc.es_cotizacion or doc.estado != Documento.BORRADOR:
        errores = ["Solo se emiten cotizaciones en borrador."]
    if errores:
        for e in errores:
            messages.error(request, e)
        return redirect("documento", pk=pk)
    with transaction.atomic():
        if not doc.folio:
            ultimo = (Sociedad.objects.select_for_update().get(pk=doc.sociedad_id)
                      .documentos.cotizaciones().aggregate(m=Max("folio"))["m"] or 0)
            doc.folio = ultimo + 1
        doc.estado = Documento.EMITIDO
        if not doc.fecha_vencimiento:
            doc.fecha_vencimiento = doc.fecha_emision + timedelta(days=30)
        doc.save()
    messages.success(request, f"{doc} emitida. Ya puedes descargar el PDF para tu cliente.")
    return redirect("documento", pk=pk)


@require_POST
@con_sociedad
def cotizacion_facturar(request, pk):
    """Crea el borrador de factura a partir de una cotización emitida."""
    cotizacion = _documento(request, pk)
    if not cotizacion.es_cotizacion or cotizacion.estado != Documento.EMITIDO:
        messages.error(request, "Solo se facturan cotizaciones emitidas.")
        return redirect("documento", pk=pk)
    todo_exento = all(l.exento for l in cotizacion.lineas.all())
    with transaction.atomic():
        factura = Documento.objects.create(
            sociedad=request.sociedad, cliente=cotizacion.cliente, origen=cotizacion,
            tipo=FACTURA_EXENTA if todo_exento else FACTURA,
            fecha_vencimiento=hoy() + timedelta(days=cotizacion.cliente.dias_pago),
            observaciones=f"Según cotización N° {cotizacion.folio}. {cotizacion.observaciones}".strip()[:300])
        for l in cotizacion.lineas.all():
            Linea.objects.create(
                documento=factura, producto=l.producto, descripcion=l.descripcion,
                cantidad=l.cantidad, precio=l.precio, descuento=l.descuento, exento=l.exento)
        factura.recalcular()
    messages.info(request, "Borrador de factura creado a partir de la cotización.")
    return redirect("documento_editar", pk=factura.pk)


# --- Marketing ----------------------------------------------------------------
def _agradecer(request, doc):
    enviado, aviso = marketing.agradecer(doc)
    if aviso:
        (messages.success if enviado else messages.warning)(request, aviso)


@con_sociedad
def marketing_vista(request):
    soc = request.sociedad
    clientes = soc.clientes.filter(activo=True).order_by("nombre_fantasia", "razon_social")
    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion == "guardar":
            soc.gracias_activo = request.POST.get("gracias_activo") == "on"
            soc.gracias_asunto = request.POST.get("gracias_asunto", "").strip()[:120] or \
                Sociedad._meta.get_field("gracias_asunto").default
            soc.gracias_mensaje = request.POST.get("gracias_mensaje", "").strip() or \
                Sociedad.MENSAJE_GRACIAS
            soc.save(update_fields=["gracias_activo", "gracias_asunto", "gracias_mensaje"])
            elegidos = {int(x) for x in request.POST.getlist("clientes") if x.isdigit()}
            clientes.filter(pk__in=elegidos).update(recibe_gracias=True)
            clientes.exclude(pk__in=elegidos).update(recibe_gracias=False)
            messages.success(request, "Configuración de marketing guardada.")
        elif accion == "prueba":
            destino = request.user.email
            if not destino:
                messages.error(request, "Tu usuario no tiene correo: agrégalo en Usuarios.")
            elif not settings.CORREO_CONFIGURADO:
                messages.error(request, "El envío de correos no está configurado.")
            else:
                ejemplo = clientes.exclude(comercial_email="").first() or clientes.first() or \
                    Cliente(sociedad=soc, razon_social="Cliente de ejemplo")
                asunto, mensaje = marketing.redactar(soc, ejemplo, "Factura N° 123", 119000)
                try:
                    marketing.enviar_correo(soc, destino, "[Prueba] " + asunto, mensaje)
                    messages.success(request, f"Correo de prueba enviado a {destino}.")
                except Exception:
                    messages.error(request, "No se pudo enviar el correo de prueba.")
        return redirect("marketing")
    ejemplo = clientes.exclude(comercial_email="").first() or clientes.first()
    vista_previa = (marketing.redactar(soc, ejemplo, "Factura N° 123", 119000)
                    if ejemplo else None)
    return render(request, "core/marketing.html", {
        "clientes": clientes, "correo_configurado": settings.CORREO_CONFIGURADO,
        "vista_previa": vista_previa, "ejemplo": ejemplo,
        "sin_correo": sum(1 for c in clientes if c.recibe_gracias and not c.contacto("comercial")["email"]),
        "enviados_mes": soc.documentos.filter(
            gracias_enviado__gte=timezone.now().replace(day=1, hour=0, minute=0)).count()})


def _rol_destinatario(doc):
    """Facturas y notas van al contacto de facturación; cotizaciones y guías al comercial."""
    return "facturacion" if (doc.es_factura or doc.es_nota) else "comercial"


def _recordatorio_inicial(doc, usuario):
    contacto = doc.cliente.contacto("cobranza")
    if not contacto["email"]:   # sin contacto de cobranza, se usa el de facturación
        contacto = doc.cliente.contacto("facturacion")
    saludo = contacto["nombre"] or doc.cliente.nombre_fantasia or doc.cliente.razon_social
    nombre = f"{doc.tipo_corto.lower()} N° {doc.folio}"
    emision = f"emitida el {doc.fecha_emision:%d/%m/%Y}"
    dias = doc.dias_vencido
    if not doc.fecha_vencimiento:
        estado = "se encuentra pendiente de pago"
    elif dias > 0:
        estado = f"venció el {doc.fecha_vencimiento:%d/%m/%Y} y aún figura pendiente de pago"
    elif dias == 0:
        estado = "vence hoy"
    else:
        estado = f"vence el {doc.fecha_vencimiento:%d/%m/%Y}"
    pagado = (f" Registramos abonos por {formatear_clp(doc.pagado)}, por lo que el saldo es de "
              f"{formatear_clp(doc.saldo)}.") if doc.pagado else ""
    firma = usuario.get_full_name() or doc.sociedad.razon_social
    datos_pago = ""
    if doc.sociedad.email:
        datos_pago = f" Si ya realizaste el pago, te agradecemos enviarnos el comprobante a {doc.sociedad.email}."
    else:
        datos_pago = " Si ya realizaste el pago, te agradecemos enviarnos el comprobante respondiendo este correo."
    return {
        "para": contacto["email"],
        "asunto": f"Recordatorio de pago · {doc.tipo_corto} N° {doc.folio} · {doc.sociedad.razon_social}",
        "mensaje": (f"Hola {saludo}:\n\nTe recordamos que la {nombre}, {emision} por "
                    f"{formatear_clp(doc.total)}, {estado}.{pagado}\n\n"
                    f"Te adjuntamos nuevamente el documento.{datos_pago}\n\n"
                    f"Saludos,\n{firma}\n{doc.sociedad.razon_social}")}


def _correo_inicial(doc, usuario):
    contacto = doc.cliente.contacto(_rol_destinatario(doc))
    saludo = contacto["nombre"] or doc.cliente.nombre_fantasia or doc.cliente.razon_social
    nombre = f"{doc.tipo_corto.lower()} N° {doc.folio}"
    if doc.es_cotizacion:
        vigencia = f", válida hasta el {doc.fecha_vencimiento:%d/%m/%Y}" if doc.fecha_vencimiento else ""
        cuerpo = f"Te enviamos adjunta la {nombre} por {formatear_clp(doc.total)}{vigencia}."
        cierre = "Quedamos atentos a tus comentarios."
    elif doc.es_factura:
        vence = f", con vencimiento el {doc.fecha_vencimiento:%d/%m/%Y}" if doc.fecha_vencimiento else ""
        cuerpo = f"Te enviamos adjunta la {nombre} por {formatear_clp(doc.total)}{vence}."
        cierre = "Cualquier consulta, estamos para ayudarte."
    elif doc.es_nota:
        ref = f", que corrige la {doc.referencia.tipo_corto.lower()} N° {doc.referencia.folio}" if doc.referencia else ""
        cuerpo = f"Te enviamos adjunta la {nombre} por {formatear_clp(doc.total)}{ref}."
        cierre = "Cualquier consulta, estamos para ayudarte."
    else:
        cuerpo = f"Te enviamos adjunta la {nombre}."
        cierre = "Cualquier consulta, estamos para ayudarte."
    firma = usuario.get_full_name() or doc.sociedad.razon_social
    return {
        "para": contacto["email"],
        "asunto": f"{doc.tipo_corto} N° {doc.folio} · {doc.sociedad.razon_social}",
        "mensaje": (f"Hola {saludo}:\n\n{cuerpo}\n\n{cierre}"
                    f"\n\nSaludos,\n{firma}\n{doc.sociedad.razon_social}")}


@con_sociedad
def documento_correo(request, pk, recordatorio=False):
    """Envía un documento emitido por correo con su PDF (y su XML si lo timbró la app).

    Con recordatorio=True es el recordatorio de pago de cobranza: va al contacto de
    cobranza y vuelve a la lista de cobranza. GET devuelve el formulario para la ventana.
    """
    doc = _documento(request, pk)
    lista_url = "cotizaciones" if doc.es_cotizacion else "documentos"
    if recordatorio:
        lista_url = "cobranza"
        if doc.estado_cobro not in ("pendiente", "parcial", "vencida"):
            messages.error(request, "Solo se envían recordatorios de facturas con saldo pendiente.")
            return redirect("cobranza")
    if doc.estado != Documento.EMITIDO:
        messages.error(request, "Emite el documento antes de enviarlo.")
        return redirect("documento", pk=pk)
    inicial = (_recordatorio_inicial if recordatorio else _correo_inicial)(doc, request.user)
    form = EnviarCorreoForm(request.POST or None, initial=inicial)
    if request.method == "POST":
        if not settings.CORREO_CONFIGURADO:
            messages.error(request, "El envío de correos no está configurado.")
        elif form.is_valid():
            datos = form.cleaned_data
            correo = EmailMessage(
                datos["asunto"], datos["mensaje"], to=datos["para"],
                from_email=f"{doc.sociedad.razon_social} <{settings.DEFAULT_FROM_EMAIL}>",
                reply_to=[c for c in (request.user.email or doc.sociedad.email,) if c] or None)
            base = re.sub(r"[^\w-]+", "_", f"{doc.tipo_corto}_{doc.folio}_{doc.sociedad.razon_social}")
            correo.attach(f"{base}.pdf", generar_pdf(doc, cedible=False), "application/pdf")
            if doc.es_propio:
                correo.attach(f"{base}.xml", bytes(doc.xml_firmado), "application/xml")
            try:
                correo.send()
            except Exception:
                messages.error(request, "No se pudo enviar el correo. Intenta de nuevo en unos minutos.")
            else:
                destinatarios = ", ".join(datos["para"])
                if recordatorio:
                    doc.recordatorio_enviado_a = destinatarios
                    doc.recordatorio_enviado_el = timezone.now()
                    doc.recordatorios += 1
                    doc.save(update_fields=["recordatorio_enviado_a", "recordatorio_enviado_el",
                                            "recordatorios"])
                    messages.success(request, f"Recordatorio de la {doc} enviado a {destinatarios}.")
                else:
                    doc.correo_enviado_a = destinatarios
                    doc.correo_enviado_el = timezone.now()
                    doc.save(update_fields=["correo_enviado_a", "correo_enviado_el"])
                    messages.success(request, f"{doc} enviada a {destinatarios}.")
                if recordatorio or request.POST.get("volver") == "lista":
                    return redirect(lista_url)
                return redirect("documento", pk=pk)
        else:
            for errores in form.errors.values():
                for e in errores:
                    messages.error(request, e)
        return redirect(lista_url if recordatorio else "documento", **({} if recordatorio else {"pk": pk}))
    rol = _rol_destinatario(doc)
    if recordatorio:
        usa_facturacion = (not doc.cliente.contacto("cobranza")["email"]
                           and doc.cliente.contacto("facturacion")["email"])
        rol = "facturacion" if usa_facturacion else "cobranza"
    return render(request, "core/_enviar_correo.html", {
        "doc": doc, "form": form, "correo_configurado": settings.CORREO_CONFIGURADO,
        "volver": request.GET.get("volver", ""), "recordatorio": recordatorio,
        "accion": reverse("cobranza_recordatorio" if recordatorio else "documento_correo", args=[pk]),
        "rol": dict(Cliente.ROLES)[rol].lower()})


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
    if _intentar(request, lambda: servicio.emitir(doc),
                 lambda d: f"{d} timbrada y firmada. Ya puedes descargarla y enviarla al SII."):
        _agradecer(request, doc)
    return redirect("documento", pk=pk)


def _descarga(contenido, tipo, nombre, adjunto=True):
    respuesta = HttpResponse(contenido, content_type=tipo)
    modo = "attachment" if adjunto else "inline"
    respuesta["Content-Disposition"] = f'{modo}; filename="{nombre}"'
    return respuesta


def _nombre_archivo(doc):
    return f"{doc.sociedad.rut}_T{doc.tipo}_" + (f"F{doc.folio}" if doc.folio else f"borrador{doc.pk}")


@con_sociedad
def documento_xml(request, pk):
    doc = _documento(request, pk)
    if not doc.es_propio:
        return redirect("documento", pk=pk)
    return _descarga(bytes(doc.xml_firmado), "application/xml", _nombre_archivo(doc) + ".xml")


@con_sociedad
def documento_pdf(request, pk):
    """PDF del documento. Sin timbre si es borrador o si se emitió en el portal del SII."""
    doc = _documento(request, pk)
    cedible = request.GET.get("cedible") == "1" and doc.es_propio
    nombre = _nombre_archivo(doc) + ("_cedible" if cedible else "") + ".pdf"
    return _descarga(generar_pdf(doc, cedible=cedible), "application/pdf", nombre,
                     adjunto=request.GET.get("descargar") == "1")


@con_sociedad
def documento_vista(request, pk):
    """Vista rápida del documento, para mostrarla en una ventana sobre el listado."""
    doc = _documento(request, pk)
    return render(request, "core/_vista.html", {"doc": doc, "lineas": doc.lineas.all()})


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
def pago_marcar(request, pk=None):
    """«Marcar como pagado» de cobranza: registra un pago por el saldo completo.

    Con pk salda una factura; sin pk salda todas las marcadas en la lista
    (campo «documentos»), con la misma fecha, medio y nota.
    """
    volver = request.POST.get("volver", "")
    destino = volver if volver.startswith("/") and not volver.startswith("//") else reverse("cobranza")
    ids = [pk] if pk else [int(i) for i in request.POST.getlist("documentos") if i.isdigit()]
    if not ids:
        messages.error(request, "Marca al menos una factura.")
        return redirect(destino)
    form = MarcarPagadoForm(request.POST)
    if not form.is_valid():
        for errores in form.errors.values():
            for e in errores:
                messages.error(request, e)
        return redirect(destino)
    datos = form.cleaned_data
    docs = list(request.sociedad.documentos.filter(pk__in=ids).select_related("cliente"))
    if pk and not docs:
        raise Http404
    pagadas, omitidas, total = [], [], 0
    with transaction.atomic():
        for doc in docs:
            if not doc.es_factura or doc.estado != Documento.EMITIDO or doc.saldo <= 0:
                omitidas.append(doc)
                continue
            total += doc.saldo
            Pago.objects.create(documento=doc, monto=doc.saldo, fecha=datos["fecha"],
                                medio=datos["medio"], nota=datos["nota"])
            pagadas.append(doc)
    medio = dict(Pago.MEDIOS)[datos["medio"]].lower()
    if len(pagadas) == 1:
        messages.success(request, f"{pagadas[0]} marcada como pagada el {datos['fecha']:%d/%m/%Y} ({medio}).")
    elif pagadas:
        messages.success(request, f"{len(pagadas)} facturas marcadas como pagadas el {datos['fecha']:%d/%m/%Y} "
                                  f"({medio}), por {formatear_clp(total)} en total.")
    for doc in omitidas:
        messages.info(request, f"{doc} no se marcó: no tiene saldo pendiente o no está emitida.")
    return redirect(destino)


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


ESTADOS_PLAZO = [("vencida", "Vencidas"), ("pronto", "Por vencer"), ("al-dia", "Al día")]


@con_sociedad
def cobranza(request):
    qs = (request.sociedad.documentos.por_cobrar().select_related("cliente")
          .order_by("fecha_vencimiento", "id"))
    pendientes = list(qs)
    con_deuda = sorted({d.cliente for d in pendientes}, key=lambda c: str(c).lower())
    # Filtros combinables: varios estados y varios clientes a la vez.
    estados = [e for e in request.GET.getlist("estado") if e in dict(ESTADOS_PLAZO)]
    clientes_sel = {int(c) for c in request.GET.getlist("cliente") if c.isdigit()}
    def estado(d):
        return "al-dia" if d.plazo[0] == "sin" else d.plazo[0]
    if clientes_sel:
        pendientes = [d for d in pendientes if d.cliente_id in clientes_sel]
    conteo = {clave: sum(1 for d in pendientes if estado(d) == clave) for clave, _ in ESTADOS_PLAZO}
    if estados:
        pendientes = [d for d in pendientes if estado(d) in estados]
    if request.GET.get("formato") == "excel":
        return excel(f"cobranza_{hoy():%Y%m%d}", [
            "Cliente", "RUT", "Documento", "Folio", "Emisión", "Vencimiento", "Días vencido",
            "Tramo", "Total", "Notas de crédito", "Notas de débito", "Pagado", "Saldo",
            "Contacto de cobranza", "Correo", "Teléfono"],
            [[d.cliente.razon_social, formatear_rut(d.cliente.rut), d.tipo_corto, d.folio,
              d.fecha_emision, d.fecha_vencimiento, max(d.dias_vencido, 0),
              TRAMOS[_tramo(d.dias_vencido)], d.total, d.notas_credito, d.notas_debito,
              d.pagado, d.saldo, *d.cliente.contacto("cobranza").values()] for d in pendientes])
    tramos = [0] * 5
    for d in pendientes:
        tramos[_tramo(d.dias_vencido)] += d.saldo
    return render(request, "core/cobranza.html", {
        "pendientes": pendientes, "tramos": list(zip(TRAMOS, tramos)),
        "total": sum(tramos), "clientes": con_deuda, "clientes_sel": clientes_sel,
        "estados": [(c, n, conteo[c]) for c, n in ESTADOS_PLAZO], "estados_sel": estados, "filtrado": bool(estados or clientes_sel),
        "nombres_sel": [str(c) for c in con_deuda if c.pk in clientes_sel],
        "pagado_form": MarcarPagadoForm(), "dias_aviso": DIAS_AVISO})


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
    docs = (request.sociedad.documentos.ventas().filter(
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
