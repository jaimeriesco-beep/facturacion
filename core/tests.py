from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.contrib.messages import get_messages
from django.utils import timezone
from django.urls import reverse
from openpyxl import load_workbook

from .models import (FACTURA, FACTURA_EXENTA, NOTA_CREDITO, Cliente, Documento, Linea, Pago,
                     Movimiento, Producto, Recurrente, RecurrenteLinea, Sociedad, hoy,
                     purgar_papelera, GUIA)
from .utils import formatear_clp, formatear_rut, normalizar_rut


class Base(TestCase):
    def setUp(self):
        self.soc = Sociedad.objects.create(rut="77552628-9", razon_social="Verde Vertical")
        self.cli = Cliente.objects.create(
            sociedad=self.soc, rut="76192083-9", razon_social="Cliente Uno", dias_pago=30)
        self.clave = "clave-segura-123"
        self.user = get_user_model().objects.create_user("jaime", password=self.clave, email="jaime@ejemplo.cl")
        self.client.login(username="jaime", password="clave-segura-123")

    def factura(self, lineas=((1, 100000, False),), tipo=FACTURA, folio=None, emision=None):
        doc = Documento.objects.create(
            sociedad=self.soc, cliente=self.cli, tipo=tipo, fecha_emision=emision or hoy())
        for cantidad, precio, exento in lineas:
            Linea.objects.create(documento=doc, descripcion="Servicio", cantidad=cantidad,
                                 precio=precio, exento=exento)
        doc.recalcular()
        if folio:
            self.client.post(reverse("documento_emitir", args=[doc.pk]), {"folio": folio})
            doc.refresh_from_db()
        return doc


class RutTests(TestCase):
    def test_normaliza_y_formatea(self):
        self.assertEqual(normalizar_rut("77.552.628-9"), "77552628-9")
        self.assertEqual(normalizar_rut("12345678-5"), "12345678-5")
        self.assertEqual(normalizar_rut("6-k"), "6-K")
        self.assertEqual(formatear_rut("77552628-9"), "77.552.628-9")
        self.assertEqual(formatear_clp(1234567), "$1.234.567")

    def test_rechaza_digito_incorrecto(self):
        for malo in ("77.552.628-8", "abc", "", "123456789012"):
            with self.assertRaises(ValidationError):
                normalizar_rut(malo)


class CalculoTests(Base):
    def test_totales_afecto_y_exento(self):
        doc = self.factura([(2, 50000, False), (1, 10000, True), (3, "333.33", False)])
        self.assertEqual((doc.neto, doc.exento, doc.iva, doc.total),
                         (101000, 10000, 19190, 130190))

    def test_descuento_y_redondeo(self):
        doc = self.factura([(1, 1005, False)])
        self.assertEqual(doc.iva, 191)  # 190,95 -> 191
        linea = Linea(descripcion="x", cantidad=3, precio=1000, descuento=10)
        self.assertEqual(linea.calcular_monto(), 2700)

    def test_saldo_con_pagos_y_notas(self):
        doc = self.factura(folio=10)
        self.assertEqual(doc.estado, Documento.EMITIDO)
        self.assertEqual(doc.fecha_vencimiento, doc.fecha_emision + timedelta(days=30))
        Pago.objects.create(documento=doc, monto=19000)
        self.client.post(reverse("nota_nueva", args=[doc.pk, NOTA_CREDITO]))
        nota = doc.notas.get()
        self.assertEqual(nota.total, 119000)  # copia el detalle
        self.assertEqual(doc.saldo, 100000)   # la nota en borrador no descuenta
        self.client.post(reverse("documento_emitir", args=[nota.pk]), {"folio": 1})
        self.assertEqual(Documento.objects.get(pk=doc.pk).saldo, -19000)
        anotado = Documento.objects.con_saldo().get(pk=doc.pk)
        self.assertEqual((anotado.pagado_a, anotado.nc_a, anotado.saldo_a), (19000, 119000, -19000))
        self.assertFalse(Documento.objects.por_cobrar().exists())

    def test_estado_de_cobro(self):
        vencida = self.factura(folio=1, emision=hoy() - timedelta(days=45))
        self.assertEqual((vencida.estado_cobro, vencida.dias_vencido), ("vencida", 15))
        al_dia = self.factura(folio=2)
        self.assertEqual(al_dia.estado_cobro, "pendiente")
        Pago.objects.create(documento=al_dia, monto=1000)
        self.assertEqual(al_dia.estado_cobro, "parcial")
        Pago.objects.create(documento=al_dia, monto=118000)
        self.assertEqual(al_dia.estado_cobro, "pagada")


class FlujoTests(Base):
    def test_crear_factura_por_formulario(self):
        prod = Producto.objects.create(sociedad=self.soc, nombre="Arriendo", precio=500000)
        r = self.client.post(reverse("documento_nuevo", args=[FACTURA]), {
            "cliente": self.cli.pk, "fecha_emision": "2026-11-10", "fecha_vencimiento": "",
            "observaciones": "", "lineas-TOTAL_FORMS": 2, "lineas-INITIAL_FORMS": 0,
            "lineas-0-producto": prod.pk, "lineas-0-descripcion": "Arriendo noviembre",
            "lineas-0-cantidad": 1, "lineas-0-precio": 500000, "lineas-0-descuento": 0,
            "lineas-1-producto": "", "lineas-1-descripcion": "", "lineas-1-cantidad": 1,
            "lineas-1-precio": 0, "lineas-1-descuento": 0})
        doc = Documento.objects.get()
        self.assertRedirects(r, reverse("documento", args=[doc.pk]))
        self.assertEqual((doc.total, doc.lineas.count()), (595000, 1))
        self.assertEqual(doc.fecha_vencimiento, date(2026, 12, 10))

    def test_factura_exenta_fuerza_lineas_exentas(self):
        self.client.post(reverse("documento_nuevo", args=[FACTURA_EXENTA]), {
            "cliente": self.cli.pk, "fecha_emision": "2026-11-10",
            "lineas-TOTAL_FORMS": 1, "lineas-INITIAL_FORMS": 0,
            "lineas-0-descripcion": "Arriendo habitacional", "lineas-0-cantidad": 1,
            "lineas-0-precio": 400000, "lineas-0-descuento": 0})
        doc = Documento.objects.get()
        self.assertEqual((doc.neto, doc.exento, doc.iva, doc.total), (0, 400000, 0, 400000))

    def test_emitir_valida_folio_y_contenido(self):
        self.factura(folio=5)
        repetida = self.factura(folio=5)
        self.assertEqual(repetida.estado, Documento.BORRADOR)
        vacia = self.factura(lineas=(), folio=6)
        self.assertEqual(vacia.estado, Documento.BORRADOR)
        self.assertIn("Todas las líneas son exentas",
                      " ".join(self.factura([(1, 1000, True)]).errores_para_emitir()))

    def test_pago_no_supera_saldo_y_reabrir(self):
        doc = self.factura(folio=7)
        self.client.post(reverse("pago_nuevo", args=[doc.pk]),
                         {"fecha": "2026-11-10", "monto": 200000, "medio": "transferencia"})
        self.assertEqual(doc.pagos.count(), 0)
        self.client.post(reverse("pago_nuevo", args=[doc.pk]),
                         {"fecha": "2026-11-10", "monto": 119000, "medio": "transferencia"})
        self.assertEqual(Documento.objects.get(pk=doc.pk).saldo, 0)
        self.client.post(reverse("documento_reabrir", args=[doc.pk]))
        self.assertEqual(Documento.objects.get(pk=doc.pk).estado, Documento.EMITIDO)

    def test_cliente_rut_duplicado_y_aislamiento_entre_sociedades(self):
        datos = {"rut": "76.192.083-9", "razon_social": "Cliente Uno", "nombre_fantasia": "Otro local",
                 "dias_pago": 30, "activo": "on"}
        self.assertRedirects(self.client.post(reverse("cliente_nuevo"), datos), reverse("clientes"))
        self.assertEqual(str(self.soc.clientes.get(nombre_fantasia="Otro local")), "Otro local · Cliente Uno")
        r = self.client.post(reverse("cliente_nuevo"), {**datos, "rut": "76.192.083-8"})
        self.assertContains(r, "dígito verificador")
        otra = Sociedad.objects.create(rut="76086428-5", razon_social="Otra")
        ajeno = Documento.objects.create(
            sociedad=otra, tipo=FACTURA,
            cliente=Cliente.objects.create(sociedad=otra, rut="76192083-9", razon_social="X"))
        self.client.post(reverse("cambiar_sociedad"), {"sociedad": self.soc.pk})
        self.assertEqual(self.client.get(reverse("documento", args=[ajeno.pk])).status_code, 404)
        self.client.post(reverse("cambiar_sociedad"), {"sociedad": otra.pk})
        self.assertEqual(self.client.get(reverse("documento", args=[ajeno.pk])).status_code, 200)

    def test_paginas_y_reportes(self):
        doc = self.factura(folio=8)
        for nombre in ("inicio", "documentos", "cobranza", "recurrentes", "clientes",
                       "productos", "reportes", "sociedades", "recurrente_nueva"):
            self.assertEqual(self.client.get(reverse(nombre)).status_code, 200, nombre)
        self.assertEqual(self.client.get(reverse("documento", args=[doc.pk])).status_code, 200)
        r = self.client.get(reverse("cobranza"), {"formato": "excel"})
        filas = list(load_workbook(BytesIO(r.content)).active.values)
        self.assertEqual((filas[1][0], filas[1][3], filas[1][12]), ("Cliente Uno", 8, 119000))
        r = self.client.get(reverse("reportes"), {"desde": hoy().isoformat(), "hasta": hoy().isoformat()})
        filas = list(load_workbook(BytesIO(r.content)).active.values)
        self.assertEqual((len(filas), filas[1][10]), (2, 119000))

    def test_requiere_sesion(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("inicio")).status_code, 302)


class RecurrenteTests(Base):
    def test_genera_una_vez_por_periodo(self):
        rec = Recurrente.objects.create(
            sociedad=self.soc, cliente=self.cli, nombre="Arriendo local", dia_emision=5,
            fecha_inicio=date(2026, 1, 1), fecha_fin=date(2026, 11, 30))
        RecurrenteLinea.objects.create(recurrente=rec, descripcion="Arriendo {mes} {año}",
                                       cantidad=1, precio=800000)
        doc = rec.generar(2026, 11)
        self.assertEqual(doc.lineas.get().descripcion, "Arriendo noviembre 2026")
        self.assertEqual((doc.total, doc.fecha_emision, doc.fecha_vencimiento),
                         (952000, date(2026, 11, 5), date(2026, 12, 5)))
        self.assertIsNone(rec.generar(2026, 11))   # no duplica
        self.assertIsNone(rec.generar(2026, 12))   # fuera de vigencia
        self.assertEqual(rec.monto_estimado, 952000)


# --- Etapa 2: emisión electrónica propia --------------------------------------
import base64
import os
import re
from unittest import mock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from lxml import etree

from .dte import motor, servicio, sii
from .dte.pdf import generar_pdf
from .dte.pruebas import caf_ficticio, certificado_ficticio
from .models import GUIA, NOTA_DEBITO, Caf, Envio

os.environ["FACTURACION_SIN_LLAVERO"] = "1"
PFX, CLAVE = certificado_ficticio()


def verificar_firma(xml, firmante):
    """Verifica la firma XMLDSig del último <Signature> hijo de la raíz."""
    raiz = etree.fromstring(xml)
    firma = raiz.findall(f"{{{motor.DSIG}}}Signature")[-1]
    info = firma.find(f"{{{motor.DSIG}}}SignedInfo")
    valor = base64.b64decode(firma.findtext(f"{{{motor.DSIG}}}SignatureValue"))
    firmante.cert.public_key().verify(valor, motor.c14n(info), padding.PKCS1v15(), hashes.SHA1())
    uri = info.find(f".//{{{motor.DSIG}}}Reference").get("URI")[1:]
    nodo = raiz.xpath("//*[@ID=$i]", i=uri)[0]
    import hashlib
    esperado = base64.b64encode(hashlib.sha1(motor.c14n(nodo)).digest()).decode()
    assert esperado == info.findtext(f".//{{{motor.DSIG}}}DigestValue")


class MotorTests(Base):
    def setUp(self):
        super().setUp()
        Sociedad.objects.filter(pk=self.soc.pk).update(
            giro="Paisajismo", acteco="813000", direccion="Av. Ñuñoa 123", comuna="Ñuñoa",
            resolucion_fecha=date(2026, 11, 10))
        Cliente.objects.filter(pk=self.cli.pk).update(
            razon_social='Peñón "Uno" & Cía.', giro="Inmobiliaria", direccion="Los Álamos 45",
            comuna="Providencia")
        self.soc.refresh_from_db()
        self.cli.refresh_from_db()
        servicio.guardar_certificado(self.soc, PFX, CLAVE, "18.396.104-7")
        for tipo in (FACTURA, FACTURA_EXENTA, GUIA, NOTA_CREDITO, NOTA_DEBITO):
            servicio.guardar_caf(self.soc, caf_ficticio(self.soc.rut, "Verde Vertical", tipo, 1, 3))
        self.firmante = servicio.firmante_de(self.soc)

    def borrador(self, tipo=FACTURA, lineas=((1, 100000, False),), **campos):
        doc = Documento.objects.create(sociedad=self.soc, cliente=self.cli, tipo=tipo, **campos)
        for cantidad, precio, exento in lineas:
            Linea.objects.create(documento=doc, descripcion="Servicio <A&B>", cantidad=cantidad,
                                 precio=precio, exento=exento)
        doc.recalcular()
        return doc

    def test_timbra_firma_y_valida_todos_los_tipos(self):
        factura = servicio.emitir(self.borrador(caso_set="CASO 1-1"))
        docs = [
            factura,
            servicio.emitir(self.borrador(FACTURA_EXENTA, [(1, 5000, True)])),
            servicio.emitir(self.borrador(GUIA, ind_traslado=1, patente="ABCD12")),
            servicio.emitir(self.borrador(GUIA, [(2, 0, False)], ind_traslado=5)),
            servicio.emitir(self.borrador(NOTA_CREDITO, referencia=factura, codigo_referencia=1,
                                          razon_referencia="Anula")),
            servicio.emitir(self.borrador(NOTA_CREDITO, [(1, 0, False)], referencia=factura,
                                          codigo_referencia=2, razon_referencia="Corrige giro")),
            servicio.emitir(self.borrador(NOTA_DEBITO, referencia=factura, codigo_referencia=3,
                                          razon_referencia="Intereses")),
        ]
        self.assertEqual([d.folio for d in docs], [1, 1, 1, 2, 1, 2, 1])
        for d in docs:
            xml = bytes(d.xml_firmado)
            self.assertTrue(d.es_prueba and d.estado == Documento.EMITIDO)
            motor.validar(xml, "DTE_v10.xsd")
            verificar_firma(xml, self.firmante)
            # El timbre se verifica con la llave pública que viene dentro del CAF.
            dd = re.search(rb"<DD>.*?</DD>", xml, re.S).group(0)
            firma = base64.b64decode(re.search(rb"<FRMT[^>]*>(.*?)</FRMT>", xml).group(1))
            n, e = (int.from_bytes(base64.b64decode(re.search(rb"<%s>(.*?)</%s>" % (t, t), dd).group(1)), "big")
                    for t in (b"M", b"E"))
            rsa.RSAPublicNumbers(e, n).public_key().verify(firma, dd, padding.PKCS1v15(), hashes.SHA1())
            self.assertTrue(generar_pdf(d, cedible=True).startswith(b"%PDF"))
        self.assertIn(b"<TpoDocRef>SET</TpoDocRef>", bytes(factura.xml_firmado))
        self.assertIn("Peñón &quot;Uno&quot; &amp; Cía.".encode("latin-1"), bytes(factura.xml_firmado))
        # Las pruebas no entran a cobranza ni chocan con folios reales.
        self.assertFalse(Documento.objects.por_cobrar().exists())
        self.assertEqual(self.factura(folio=1).estado, Documento.EMITIDO)

        # Sobre de envío: válido, firmado, y cada DTE conserva su firma al extraerlo.
        from django.utils import timezone
        sobre = motor.generar_envio(self.soc, [(d.tipo, d.xml_firmado) for d in docs],
                                    "18396104-7", self.firmante, timezone.localtime())
        motor.validar(sobre, "EnvioDTE_v10.xsd")
        verificar_firma(sobre, self.firmante)
        partes = re.findall(rb"<DTE .*?</DTE>", sobre, re.S)
        self.assertEqual(len(partes), len(docs))
        for parte in partes:
            verificar_firma(motor.DECLARACION.encode() + parte, self.firmante)

    def test_c14n_no_agrega_xmlns_vacio(self):
        raiz = etree.fromstring(b'<A xmlns="urn:a" xmlns:x="urn:x"><B><C><D><E>1</E></D></C></B></A>')
        self.assertEqual(motor.c14n(raiz[0]),
                         b'<B xmlns="urn:a" xmlns:x="urn:x"><C><D><E>1</E></D></C></B>')

    def test_folios_se_agotan_y_no_se_consumen_si_falla(self):
        for _ in range(3):
            servicio.emitir(self.borrador())
        with self.assertRaisesMessage(motor.ErrorDTE, "Cargar folios"):
            servicio.emitir(self.borrador())
        # Un dato que el SII no acepta se detecta al validar, después de tomar el folio:
        # la emisión se deshace completa y el folio queda disponible.
        Sociedad.objects.filter(pk=self.soc.pk).update(acteco="abc")
        caf = Caf.objects.get(tipo=GUIA)
        with self.assertRaisesMessage(motor.ErrorDTE, "no cumple el formato del SII"):
            servicio.emitir(self.borrador(GUIA, ind_traslado=1))
        self.assertEqual(Caf.objects.get(pk=caf.pk).siguiente, 1)
        self.assertFalse(Documento.objects.filter(tipo=GUIA, estado=Documento.EMITIDO).exists())

    def test_caf_y_certificado_invalidos(self):
        with self.assertRaisesMessage(motor.ErrorDTE, "otro RUT"):
            servicio.guardar_caf(self.soc, caf_ficticio("76192083-9", "Otra", FACTURA, 10, 20))
        with self.assertRaises(motor.ErrorDTE):
            servicio.guardar_caf(self.soc, b"<no-es-caf/>")
        with self.assertRaises(motor.ErrorDTE):
            motor.Firmante(PFX, "clave-mala")
        self.assertNotIn(PFX[:40], bytes(self.soc.certificado.pfx))  # guardado cifrado

    def test_flujo_web_timbrar_descargar_enviar_y_consultar(self):
        doc = self.borrador()
        self.client.post(reverse("documento_timbrar", args=[doc.pk]))
        doc.refresh_from_db()
        self.assertEqual((doc.folio, doc.es_propio), (1, True))
        self.assertIn(b"<Folio>1</Folio>", self.client.get(reverse("documento_xml", args=[doc.pk])).content)
        self.assertEqual(self.client.get(reverse("documento_pdf", args=[doc.pk])).headers["Content-Type"],
                         "application/pdf")
        self.client.post(reverse("documento_reabrir", args=[doc.pk]))
        self.assertEqual(Documento.objects.get(pk=doc.pk).estado, Documento.EMITIDO)

        respuestas = {
            "CrSeed": "<SII:RESPUESTA><SII:RESP_BODY><SEMILLA>025046538916</SEMILLA></SII:RESP_BODY></SII:RESPUESTA>",
            "GetTokenFromSeed": "<SII:RESPUESTA><SII:RESP_BODY><TOKEN>ABC123TOKEN</TOKEN></SII:RESP_BODY></SII:RESPUESTA>",
            "QueryEstUp": "<SII:RESPUESTA><SII:RESP_HDR><TRACKID>777</TRACKID><ESTADO>EPR</ESTADO><GLOSA>Envio Procesado</GLOSA></SII:RESP_HDR>"
                          "<SII:RESP_BODY><INFORMADOS>1</INFORMADOS><ACEPTADOS>1</ACEPTADOS><RECHAZADOS>0</RECHAZADOS><REPAROS>0</REPAROS></SII:RESP_BODY></SII:RESPUESTA>",
        }
        llamadas = []

        def falso_post(url, **kw):
            llamadas.append((url, kw))
            r = mock.Mock(); r.raise_for_status = lambda: None
            if "DTEUpload" in url:
                r.text = "<RECEPCIONDTE><STATUS>0</STATUS><TRACKID>777</TRACKID></RECEPCIONDTE>"
            else:
                import html
                servicio_ws = url.rsplit("/", 1)[1].split(".")[0]
                r.text = f"<soapenv:Envelope><ns1:x><ret>{html.escape(respuestas[servicio_ws])}</ret></ns1:x></soapenv:Envelope>"
            return r

        with mock.patch.object(sii.requests, "post", falso_post):
            self.client.post(reverse("documento_enviar", args=[doc.pk]))
            envio = Envio.objects.get()
            self.assertEqual((envio.track_id, Documento.objects.get(pk=doc.pk).estado_sii), ("777", "enviado"))
            self.client.post(reverse("envio_estado", args=[envio.pk]), {"documento": doc.pk})
        self.assertEqual(Documento.objects.get(pk=doc.pk).estado_sii, "aceptado")
        subida = next(kw for url, kw in llamadas if "DTEUpload" in url)
        self.assertTrue(subida["headers"]["Cookie"] == "TOKEN=ABC123TOKEN" and "maullin" in llamadas[0][0])
        self.assertEqual(subida["data"], {"rutSender": "18396104", "dvSender": "7",
                                          "rutCompany": "77552628", "dvCompany": "9"})
        motor.validar(subida["files"]["archivo"][1], "EnvioDTE_v10.xsd")
        self.assertEqual(self.client.get(reverse("conexion_sii")).status_code, 200)

    def test_guia_se_factura_y_logo(self):
        guia = servicio.emitir(self.borrador(GUIA, ind_traslado=1))
        self.client.post(reverse("guia_facturar", args=[guia.pk]))
        factura = Documento.objects.get(tipo=FACTURA)
        self.assertEqual((factura.referencia, factura.total), (guia, 119000))
        xml = bytes(servicio.emitir(factura).xml_firmado)
        self.assertIn(b"<TpoDocRef>52</TpoDocRef><FolioRef>1</FolioRef>", xml)
        self.assertNotIn(b"<CodRef>", xml)
        from PIL import Image
        lienzo = Image.new("RGB", (300, 300), "white"); lienzo.paste((20, 90, 40), (100, 120, 200, 160))
        archivo = BytesIO(); lienzo.save(archivo, "PNG")
        self.soc.logo = servicio.preparar_logo(archivo.getvalue()); self.soc.save()
        self.assertEqual(Image.open(BytesIO(self.soc.logo)).size, (100, 40))
        self.assertEqual(self.client.get(reverse("sociedad_logo", args=[self.soc.pk])).status_code, 200)
        self.assertTrue(generar_pdf(guia).startswith(b"%PDF"))


class ClientesYUsuariosTests(Base):
    def test_importa_clientes_sin_rut_y_no_duplica(self):
        from openpyxl import Workbook
        libro = Workbook(); hoja = libro.active
        for fila in (["Fecha", "Cliente", "Total"], [1, "Tropera", 10], [2, "tropera ", 5],
                     [3, " Club  Aleman", 7], [4, None, 1], [5, "Cliente Uno", 2]):
            hoja.append(fila)
        archivo = BytesIO(); libro.save(archivo); archivo.seek(0); archivo.name = "ventas.xlsx"
        r = self.client.post(reverse("clientes_importar"), {"archivo": archivo}, follow=True)
        self.assertContains(r, "Se crearon 2 clientes")
        self.assertEqual(sorted(self.soc.clientes.values_list("razon_social", "rut")),
                         [("Cliente Uno", "76192083-9"), ("Club Aleman", ""), ("Tropera", "")])
        # Sin RUT se puede preparar el borrador, pero no emitir.
        doc = Documento.objects.create(sociedad=self.soc, cliente=self.soc.clientes.get(razon_social="Tropera"))
        Linea.objects.create(documento=doc, descripcion="Albahaca", cantidad=1, precio=1000); doc.recalcular()
        self.client.post(reverse("documento_emitir", args=[doc.pk]), {"folio": 99})
        self.assertEqual(Documento.objects.get(pk=doc.pk).estado, Documento.BORRADOR)
        self.assertEqual(self.soc.clientes.get(razon_social="Tropera").nombre_fantasia, "Tropera")
        r = self.client.get(reverse("cliente_editar", args=[self.cli.pk]))
        self.assertContains(r, 'value="76.192.083-9"')   # el RUT guardado se muestra con puntos
        self.assertContains(r, "data-rut")
        r = self.client.get(reverse("cliente_nuevo"))
        self.assertContains(r, 'list="actividades-sii"')
        self.assertContains(r, 'data-codigo="561000">561000</option>')
        self.assertContains(r, "Actividades de restaurantes y de servicio móvil de comidas")
        r = self.client.post(reverse("cliente_nuevo"), {
            "razon_social": "Restaurante Largo", "dias_pago": 30, "activo": "on",
            "giro": "Venta al por menor de alimentos en comercios especializados (almacenes pequeños y minimarket)"})
        self.assertRedirects(r, reverse("clientes"))
        self.assertNotContains(self.client.get(reverse("producto_nuevo")), "actividades-sii")
        # En la factura, el selector de cliente muestra el RUT para poder buscar por él.
        r = self.client.get(reverse("documento_nuevo", args=[FACTURA]))
        ruta = reverse("documento_nuevo", args=[FACTURA])
        self.assertContains(r, f'href="/clientes/nuevo/?volver={ruta}"')
        r = self.client.post(f"/clientes/nuevo/?volver={ruta}",
                             {"razon_social": "Creado Desde Factura", "dias_pago": 30, "activo": "on"})
        nuevo = self.soc.clientes.get(razon_social="Creado Desde Factura")
        self.assertRedirects(r, f"{ruta}?cliente={nuevo.pk}")
        self.assertContains(self.client.get(f"{ruta}?cliente={nuevo.pk}"),
                            f'<option value="{nuevo.pk}" selected>')
        r = self.client.post("/clientes/nuevo/?volver=https://malo.example/x",
                             {"razon_social": "Otro Más", "dias_pago": 30, "activo": "on"})
        self.assertRedirects(r, reverse("clientes"))
        r = self.client.get(reverse("documento_nuevo", args=[FACTURA]))
        self.assertContains(r, "Cliente Uno — 76.192.083-9")
        self.assertContains(r, "Tropera — sin RUT")

    def test_administrador_crea_usuario(self):
        self.assertEqual(self.client.get(reverse("usuarios")).status_code, 302)  # no es admin
        get_user_model().objects.filter(username="jaime").update(is_superuser=True)
        r = self.client.post(reverse("usuario_nuevo"), {
            "username": "alberto", "first_name": "Alberto", "last_name": "Riesco",
            "email": "alberto@example.com", "clave": "una-clave-larga-2026",
            "rol": "admin", "is_active": "on"})
        self.assertRedirects(r, reverse("usuarios"))
        nuevo = get_user_model().objects.get(username="alberto")
        self.assertTrue(nuevo.is_superuser and nuevo.check_password("una-clave-larga-2026"))
        self.assertEqual(self.client.get(reverse("usuarios")).status_code, 200)
        self.assertRedirects(self.client.get(reverse("password_change")), "/perfil/#clave",
                             fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("perfil")), "Repite la clave nueva")
        r = self.client.post(reverse("perfil"), {
            "accion": "clave", "old_password": "mala-clave-123", "new_password1": "otra-clave-larga-2027",
            "new_password2": "otra-clave-larga-2027"})
        self.assertContains(r, "La clave actual no es correcta.")
        r = self.client.post(reverse("perfil"), {
            "accion": "clave", "old_password": "clave-segura-123", "new_password1": "otra-clave-larga-2027",
            "new_password2": "otra-clave-larga-2028"})
        self.assertContains(r, "Las dos claves nuevas no coinciden.")
        r = self.client.post(reverse("perfil"), {
            "accion": "clave", "old_password": "clave-segura-123", "new_password1": "otra-clave-larga-2027",
            "new_password2": "otra-clave-larga-2027"})
        self.assertRedirects(r, reverse("perfil"))
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        import json
        datos = json.loads(self.client.get(reverse("respaldo")).content)
        self.assertEqual({"core.sociedad", "core.cliente", "auth.user"} - {d["model"] for d in datos}, set())


from django.test import override_settings


@override_settings(EXIGIR_DOS_PASOS=True)
class DosPasosTests(Base):
    def test_exige_codigo_y_lo_recuerda_en_la_sesion(self):
        import pyotp
        from .dte.secretos import descifrar
        from .models import DosPasos
        self.assertRedirects(self.client.get(reverse("inicio")), reverse("dos_pasos"))
        r = self.client.get(reverse("dos_pasos"))
        self.assertContains(r, "data:image/svg+xml")
        secreto = descifrar(DosPasos.objects.get().secreto).decode()
        self.assertContains(r, secreto)
        self.client.post(reverse("dos_pasos"), {"codigo": "000000"})
        self.assertEqual(self.client.get(reverse("clientes")).status_code, 302)
        r = self.client.post(reverse("dos_pasos"), {"codigo": pyotp.TOTP(secreto).now()})
        self.assertRedirects(r, reverse("inicio"))
        self.assertEqual(self.client.get(reverse("clientes")).status_code, 200)
        # Una sesión nueva vuelve a pedir el código, ya sin mostrar el QR ni la clave.
        self.client.logout()
        self.client.login(username="jaime", password="clave-segura-123")
        r = self.client.get(reverse("dos_pasos"))
        self.assertNotContains(r, secreto)
        for _ in range(5):
            r = self.client.post(reverse("dos_pasos"), {"codigo": "111111"})
        self.assertRedirects(r, reverse("login"))  # cinco fallos cierran la sesión


class VerYPdfTests(Base):
    def test_botones_ver_y_pdf_en_cada_documento(self):
        borrador = self.factura()
        emitida = self.factura(folio=77)
        r = self.client.get(reverse("documentos"))
        for doc in (borrador, emitida):
            self.assertContains(r, f'data-ver="/documentos/{doc.pk}/vista/"')
            self.assertContains(r, f'href="/documentos/{doc.pk}/pdf/?descargar=1" title="Descargar PDF"')
        vista = self.client.get(reverse("documento_vista", args=[emitida.pk]))
        self.assertContains(vista, "N° 77")
        self.assertContains(vista, "$119.000")
        self.assertNotContains(vista, "<html")
        self.assertContains(self.client.get(reverse("documento_vista", args=[borrador.pk])), "BORRADOR")
        # PDF para todos: sin timbre en borradores y en lo emitido desde el portal del SII.
        for doc in (borrador, emitida):
            r = self.client.get(reverse("documento_pdf", args=[doc.pk]), {"descargar": "1"})
            self.assertTrue(r.content.startswith(b"%PDF"))
            self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn("borrador", self.client.get(
            reverse("documento_pdf", args=[borrador.pk])).headers["Content-Disposition"])


class IngresoTests(Base):
    def test_sin_dos_pasos_y_con_bloqueo_por_intentos(self):
        from django.core.cache import cache
        cache.clear()
        self.assertEqual(self.client.get(reverse("inicio")).status_code, 200)  # sin segundo paso
        self.client.logout()
        for _ in range(5):
            r = self.client.post(reverse("login"), {"username": "Jaime", "password": "mala"})
            self.assertContains(r, "correo o clave incorrectos")
        r = self.client.post(reverse("login"), {"username": "jaime", "password": "clave-segura-123"})
        self.assertContains(r, "Demasiados intentos fallidos")   # ni la clave correcta entra
        cache.clear()
        r = self.client.post(reverse("login"), {"username": "jaime", "password": "clave-segura-123"})
        self.assertRedirects(r, reverse("inicio"))


class CotizacionTests(Base):
    def test_cotizacion_con_numeracion_propia_y_fuera_de_lo_tributario(self):
        from .models import COTIZACION
        self.factura(folio=1)                                   # factura N° 1 ya existe
        sin_rut = Cliente.objects.create(sociedad=self.soc, razon_social="Prospecto", nombre_fantasia="Prospecto")
        r = self.client.post(reverse("documento_nuevo", args=[COTIZACION]), {
            "cliente": sin_rut.pk, "fecha_emision": "2026-11-10", "fecha_vencimiento": "",
            "lineas-TOTAL_FORMS": 1, "lineas-INITIAL_FORMS": 0,
            "lineas-0-descripcion": "Albahaca semanal", "lineas-0-cantidad": 10,
            "lineas-0-precio": 5000, "lineas-0-descuento": 0})
        cot = Documento.objects.get(tipo=COTIZACION)
        self.assertRedirects(r, reverse("documento", args=[cot.pk]))
        self.assertEqual((cot.total, cot.fecha_vencimiento, cot.folio), (59500, date(2026, 12, 10), None))
        self.assertNotContains(self.client.get(reverse("documento", args=[cot.pk])), "portal del SII")
        self.client.post(reverse("cotizacion_emitir", args=[cot.pk]))
        cot.refresh_from_db()
        self.assertEqual((str(cot), cot.estado), ("Cotización N° 1", Documento.EMITIDO))  # folio propio
        otra = Documento.objects.create(sociedad=self.soc, cliente=self.cli, tipo=COTIZACION)
        Linea.objects.create(documento=otra, descripcion="x", cantidad=1, precio=100); otra.recalcular()
        self.client.post(reverse("cotizacion_emitir", args=[otra.pk]))
        self.assertEqual(Documento.objects.get(pk=otra.pk).folio, 2)
        # Corregir conserva el número.
        self.client.post(reverse("documento_reabrir", args=[cot.pk]))
        self.assertEqual(Documento.objects.get(pk=cot.pk).estado, Documento.BORRADOR)
        self.client.post(reverse("cotizacion_emitir", args=[cot.pk]))
        self.assertEqual(Documento.objects.get(pk=cot.pk).folio, 1)
        # Listados separados; nada tributario ni de cobranza.
        self.assertContains(self.client.get(reverse("cotizaciones")), "Cotización N° 1")
        self.assertNotContains(self.client.get(reverse("documentos")), "Cotización N°")
        self.assertNotContains(self.client.get(reverse("cobranza")), "Prospecto")
        self.client.post(reverse("documento_timbrar", args=[cot.pk]))
        self.client.post(reverse("documento_emitir", args=[cot.pk]), {"folio": 500})
        self.assertEqual(Documento.objects.get(pk=cot.pk).folio, 1)
        r = self.client.get(reverse("reportes"), {"desde": "2026-01-01", "hasta": "2026-12-31"})
        self.assertEqual(len(list(load_workbook(BytesIO(r.content)).active.values)), 2)  # solo la factura
        pdf = self.client.get(reverse("documento_pdf", args=[cot.pk]))
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertContains(self.client.get(reverse("documento_vista", args=[cot.pk])), "COTIZACIÓN")
        # Convertir en factura: borrador con el mismo detalle; exige RUT recién al emitir.
        self.client.post(reverse("cotizacion_facturar", args=[cot.pk]))
        factura = Documento.objects.filter(tipo=FACTURA, estado=Documento.BORRADOR).get()
        self.assertEqual((factura.total, factura.cliente), (59500, sin_rut))
        self.assertIn("Según cotización N° 1", factura.observaciones)
        self.assertIn("no tiene RUT", " ".join(factura.errores_para_emitir()))


class IngresoConCorreoTests(Base):
    def test_ingresa_con_usuario_o_con_correo(self):
        from django.core.cache import cache
        cache.clear()
        Usuario = get_user_model()
        Usuario.objects.filter(username="jaime").update(email="Jaime@Ejemplo.cl", is_superuser=True)
        self.client.logout()
        for identificador in ("jaime@ejemplo.cl", "JAIME@EJEMPLO.CL", "Jaime", "jaime"):
            r = self.client.post(reverse("login"), {"username": identificador, "password": "clave-segura-123"})
            self.assertRedirects(r, reverse("inicio"), msg_prefix=identificador)
            self.client.logout()
        r = self.client.post(reverse("login"), {"username": "jaime@ejemplo.cl", "password": "mala"})
        self.assertContains(r, "correo o clave incorrectos")
        r = self.client.post(reverse("login"), {"username": "nadie@ejemplo.cl", "password": "clave-segura-123"})
        self.assertContains(r, "correo o clave incorrectos")
        # El bloqueo cuenta juntos los intentos por usuario y por correo.
        cache.clear()
        for identificador in ("jaime", "jaime@ejemplo.cl", "JAIME", "jaime@ejemplo.cl", "jaime"):
            self.client.post(reverse("login"), {"username": identificador, "password": "mala"})
        r = self.client.post(reverse("login"), {"username": "jaime@ejemplo.cl", "password": "clave-segura-123"})
        self.assertContains(r, "Demasiados intentos fallidos")
        cache.clear()
        # Un usuario inactivo no entra, y no se aceptan dos usuarios con el mismo correo.
        self.client.login(username="jaime", password="clave-segura-123")
        r = self.client.post(reverse("usuario_nuevo"), {
            "username": "otro", "email": "jaime@ejemplo.cl", "clave": "una-clave-larga-2026",
            "rol": "basica", "is_active": "on"})
        self.assertContains(r, "Ya hay otro usuario con ese correo")
        Usuario.objects.create_user("baja", email="baja@ejemplo.cl", password="clave-segura-123", is_active=False)
        self.client.logout()
        r = self.client.post(reverse("login"), {"username": "baja@ejemplo.cl", "password": "clave-segura-123"})
        self.assertEqual(r.status_code, 200)


class RecuperarClaveTests(Base):
    def test_sin_correo_configurado_lo_dice(self):
        self.client.logout()
        self.assertContains(self.client.get(reverse("login")), "¿Olvidaste tu clave?")
        self.assertContains(self.client.get(reverse("password_reset")), "aún no está configurado")

    @override_settings(CORREO_CONFIGURADO=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                       DEFAULT_FROM_EMAIL="facturacion@ejemplo.cl")
    def test_flujo_completo_por_correo(self):
        from django.core import mail
        from django.core.cache import cache
        cache.clear()
        get_user_model().objects.filter(username="jaime").update(email="jaime@ejemplo.cl")
        self.client.logout()
        r = self.client.post(reverse("password_reset"), {"email": "nadie@ejemplo.cl"})
        self.assertRedirects(r, reverse("password_reset_done"))   # misma respuesta, sin correo
        self.assertEqual(len(mail.outbox), 0)
        for _ in range(5):                                       # tope de 3 por hora
            self.client.post(reverse("password_reset"), {"email": "JAIME@ejemplo.cl"})
        self.assertEqual(len(mail.outbox), 3)
        self.assertEqual(mail.outbox[0].to, ["jaime@ejemplo.cl"])
        enlace = re.search(r"https?://\S+", mail.outbox[0].body).group(0)
        ruta = "/" + enlace.split("/", 3)[3]
        for _ in range(5):                                       # usuario bloqueado por intentos
            self.client.post(reverse("login"), {"username": "jaime", "password": "mala"})
        r = self.client.get(ruta, follow=True)
        self.assertContains(r, "Clave nueva")
        r = self.client.post(r.request["PATH_INFO"], {"new_password1": "corta", "new_password2": "corta"})
        self.assertEqual(r.status_code, 200)                     # clave débil: no pasa
        r = self.client.post(r.request["PATH_INFO"], {
            "new_password1": "clave-recuperada-2026", "new_password2": "clave-recuperada-2026"}, follow=True)
        self.assertContains(r, "Clave cambiada")
        r = self.client.post(reverse("login"), {"username": "jaime@ejemplo.cl", "password": "clave-recuperada-2026"})
        self.assertRedirects(r, reverse("inicio"))               # entra, y el bloqueo se levantó
        self.client.logout()
        self.assertContains(self.client.get(ruta, follow=True), "ya se usó o venció")

    def test_envio_por_brevo(self):
        from django.core.mail import EmailMessage
        from .correo import BrevoBackend
        with override_settings(BREVO_API_KEY="llave-de-prueba", DEFAULT_FROM_EMAIL="facturacion@ejemplo.cl"), \
                mock.patch("core.correo.requests.post") as post:
            post.return_value.raise_for_status = lambda: None
            enviados = BrevoBackend().send_messages([EmailMessage("Asunto", "Texto", to=["a@ejemplo.cl"])])
        self.assertEqual(enviados, 1)
        args, kw = post.call_args
        self.assertEqual(args[0], "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(kw["headers"]["api-key"], "llave-de-prueba")
        self.assertEqual(kw["json"]["to"], [{"email": "a@ejemplo.cl"}])
        self.assertEqual(kw["json"]["sender"]["email"], "facturacion@ejemplo.cl")


class ContactosTests(Base):
    DATOS = {"razon_social": "Cliente Uno", "rut": "76.192.083-9", "dias_pago": 30, "activo": "on",
             "comercial_nombre": "Ana Comercial", "comercial_email": "ana@ejemplo.cl", "comercial_telefono": "111",
             "facturacion_nombre": "Beto Factura", "facturacion_email": "beto@ejemplo.cl", "facturacion_telefono": "222",
             "cobranza_nombre": "Carla Cobro", "cobranza_email": "carla@ejemplo.cl", "cobranza_telefono": "333"}

    def test_un_contacto_para_todo_o_uno_por_rol(self):
        url = reverse("cliente_editar", args=[self.cli.pk])
        r = self.client.get(url)
        self.assertContains(r, "Personas de contacto")
        self.assertContains(r, 'name="contacto_unico"')
        self.assertContains(r, "checked")                       # por defecto, una sola persona
        # Tres personas distintas.
        self.assertRedirects(self.client.post(url, self.DATOS), reverse("clientes"))
        self.cli.refresh_from_db()
        self.assertFalse(self.cli.contacto_unico)
        self.assertEqual(self.cli.contacto("cobranza"),
                         {"nombre": "Carla Cobro", "email": "carla@ejemplo.cl", "telefono": "333"})
        self.assertEqual(self.cli.contacto("facturacion")["email"], "beto@ejemplo.cl")
        doc = self.factura(folio=9)
        filas = list(load_workbook(BytesIO(self.client.get(reverse("cobranza"), {"formato": "excel"}).content)).active.values)
        self.assertEqual(filas[1][-3:], ("Carla Cobro", "carla@ejemplo.cl", "333"))
        # Una sola persona: cumple los tres roles y se descartan las otras dos.
        self.client.post(url, {**self.DATOS, "contacto_unico": "on"})
        self.cli.refresh_from_db()
        for rol in ("comercial", "facturacion", "cobranza"):
            self.assertEqual(self.cli.contacto(rol)["nombre"], "Ana Comercial")
        self.assertEqual((self.cli.facturacion_nombre, self.cli.cobranza_email), ("", ""))
        r = self.client.post(url, {**self.DATOS, "cobranza_email": "no-es-correo"})
        self.assertContains(r, "correo electrónico válid")


@override_settings(CORREO_CONFIGURADO=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DEFAULT_FROM_EMAIL="facturacion@ejemplo.cl")
class MarketingTests(Base):
    def setUp(self):
        super().setUp()
        Cliente.objects.filter(pk=self.cli.pk).update(comercial_nombre="Ana", comercial_email="ana@ejemplo.cl")
        Sociedad.objects.filter(pk=self.soc.pk).update(email="contacto@verdevertical.cl")

    def test_agradecimiento_opcional_por_sociedad_y_por_cliente(self):
        from django.core import mail
        self.factura(folio=1)
        self.assertEqual(len(mail.outbox), 0)                       # desactivado por defecto
        otro = Cliente.objects.create(sociedad=self.soc, rut="12345678-5", razon_social="Otro",
                                      comercial_email="otro@ejemplo.cl")
        r = self.client.post(reverse("marketing"), {
            "accion": "guardar", "gracias_activo": "on", "gracias_asunto": "Gracias, {cliente}",
            "gracias_mensaje": "Hola {contacto}: gracias por la {documento} de {total}. {sociedad}",
            "clientes": [self.cli.pk]})
        self.assertRedirects(r, reverse("marketing"))
        self.assertFalse(Cliente.objects.get(pk=otro.pk).recibe_gracias)
        doc = self.factura(folio=2)
        self.assertEqual(len(mail.outbox), 1)
        m = mail.outbox[0]
        self.assertEqual((m.to, m.subject, m.reply_to), (["ana@ejemplo.cl"], "Gracias, Cliente Uno",
                                                         ["contacto@verdevertical.cl"]))
        self.assertIn("Hola Ana: gracias por la Factura N° 2 de $119.000. Verde Vertical", m.body)
        self.assertIn("Verde Vertical <", m.from_email)
        self.assertIsNotNone(Documento.objects.get(pk=doc.pk).gracias_enviado)
        # No se repite al corregir y volver a emitir.
        self.client.post(reverse("documento_reabrir", args=[doc.pk]))
        self.client.post(reverse("documento_emitir", args=[doc.pk]), {"folio": 3})
        self.assertEqual(len(mail.outbox), 1)
        # Cliente excluido: no recibe.
        d = Documento.objects.create(sociedad=self.soc, cliente=otro)
        Linea.objects.create(documento=d, descripcion="x", cantidad=1, precio=1000); d.recalcular()
        self.client.post(reverse("documento_emitir", args=[d.pk]), {"folio": 4})
        self.assertEqual(len(mail.outbox), 1)
        # Sin correo comercial: la factura se emite y se avisa.
        Cliente.objects.filter(pk=self.cli.pk).update(comercial_email="")
        d = self.factura()
        r = self.client.post(reverse("documento_emitir", args=[d.pk]), {"folio": 5}, follow=True)
        self.assertContains(r, "no tiene correo de contacto comercial")
        self.assertEqual(Documento.objects.get(pk=d.pk).estado, Documento.EMITIDO)
        self.assertContains(self.client.get(reverse("marketing")), "Falta correo")

    def test_no_se_envia_en_otros_documentos_y_falla_sin_romper(self):
        from django.core import mail
        Sociedad.objects.filter(pk=self.soc.pk).update(gracias_activo=True)
        cot = Documento.objects.create(sociedad=self.soc, cliente=self.cli, tipo=1)
        Linea.objects.create(documento=cot, descripcion="x", cantidad=1, precio=1000); cot.recalcular()
        self.client.post(reverse("cotizacion_emitir", args=[cot.pk]))
        self.assertEqual(len(mail.outbox), 0)
        with mock.patch("core.marketing.EmailMessage.send", side_effect=RuntimeError("caído")):
            d = self.factura()
            r = self.client.post(reverse("documento_emitir", args=[d.pk]), {"folio": 7}, follow=True)
        self.assertContains(r, "La factura quedó emitida igual")
        self.assertEqual(Documento.objects.get(pk=d.pk).estado, Documento.EMITIDO)

    def test_correo_de_prueba(self):
        from django.core import mail
        get_user_model().objects.filter(username="jaime").update(email="")
        r = self.client.post(reverse("marketing"), {"accion": "prueba"}, follow=True)
        self.assertContains(r, "no tiene correo")
        get_user_model().objects.filter(username="jaime").update(email="jaime@ejemplo.cl")
        r = self.client.post(reverse("marketing"), {"accion": "prueba"}, follow=True)
        self.assertContains(r, "Correo de prueba enviado")
        self.assertTrue(mail.outbox[0].subject.startswith("[Prueba] "))

    def test_brevo_usa_nombre_de_la_sociedad_y_responder_a(self):
        from django.core.mail import EmailMessage
        from .correo import BrevoBackend
        m = EmailMessage("A", "B", to=["x@ejemplo.cl"], from_email="Verde Vertical <f@ejemplo.cl>",
                         reply_to=["contacto@verdevertical.cl"])
        cuerpo = BrevoBackend._cuerpo(m)
        self.assertEqual(cuerpo["sender"], {"name": "Verde Vertical", "email": "facturacion@ejemplo.cl"})
        self.assertEqual(cuerpo["replyTo"], {"email": "contacto@verdevertical.cl"})


class EliminarProductoTests(Base):
    def test_basurero_elimina_sin_afectar_facturas(self):
        prod = Producto.objects.create(sociedad=self.soc, nombre="Albahaca", precio=1000)
        Producto.objects.create(sociedad=self.soc, nombre="Granel", precio="14.5")
        self.assertContains(self.client.get(reverse("productos")), "$14,5")
        doc = Documento.objects.create(sociedad=self.soc, cliente=self.cli)
        Linea.objects.create(documento=doc, producto=prod, descripcion="Albahaca", cantidad=2, precio=1000)
        doc.recalcular()
        r = self.client.get(reverse("productos"))
        self.assertContains(r, f'action="/productos/{prod.pk}/eliminar/"')
        self.assertContains(r, 'class="icono-accion basurero"')
        self.assertContains(r, f'href="/productos/{prod.pk}/" class="icono-accion editar"')
        self.assertEqual(self.client.get(reverse("producto_eliminar", args=[prod.pk])).status_code, 405)
        otra = Sociedad.objects.create(rut="76086428-5", razon_social="Otra")
        ajeno = Producto.objects.create(sociedad=otra, nombre="Ajeno")
        self.client.post(reverse("cambiar_sociedad"), {"sociedad": self.soc.pk})
        self.assertEqual(self.client.post(reverse("producto_eliminar", args=[ajeno.pk])).status_code, 404)
        r = self.client.post(reverse("producto_eliminar", args=[prod.pk]), follow=True)
        self.assertContains(r, "Producto «Albahaca» eliminado")
        self.assertFalse(Producto.objects.filter(pk=prod.pk).exists())
        linea = Linea.objects.get(documento=doc)
        self.assertEqual((linea.producto, linea.descripcion, Documento.objects.get(pk=doc.pk).total),
                         (None, "Albahaca", 2380))


class AsociadosTests(Base):
    def test_documentos_asociados_en_ambos_sentidos(self):
        from .models import COTIZACION, GUIA
        factura = self.factura(folio=10)
        sola = self.factura(folio=11)
        self.client.post(reverse("nota_nueva", args=[factura.pk, NOTA_CREDITO]))
        nota = Documento.objects.get(tipo=NOTA_CREDITO)
        guia = Documento.objects.create(sociedad=self.soc, cliente=self.cli, tipo=GUIA, ind_traslado=1)
        Linea.objects.create(documento=guia, descripcion="Plantas", cantidad=1, precio=5000); guia.recalcular()
        self.client.post(reverse("documento_emitir", args=[guia.pk]), {"folio": 3})
        self.client.post(reverse("guia_facturar", args=[guia.pk]))
        de_guia = Documento.objects.get(referencia=guia)
        cot = Documento.objects.create(sociedad=self.soc, cliente=self.cli, tipo=COTIZACION)
        Linea.objects.create(documento=cot, descripcion="x", cantidad=1, precio=100); cot.recalcular()
        self.client.post(reverse("cotizacion_emitir", args=[cot.pk]))
        self.client.post(reverse("cotizacion_facturar", args=[cot.pk]))
        de_cot = Documento.objects.get(origen=cot)

        self.assertEqual(factura.asociados(), ([], [nota]))
        self.assertEqual(nota.asociados(), ([factura], []))
        self.assertEqual(de_guia.asociados()[0], [guia])
        self.assertEqual(de_cot.asociados()[0], [cot])
        self.assertEqual(cot.asociados()[1], [de_cot])
        # En el listado: botón con la cantidad, solo donde hay asociados.
        r = self.client.get(reverse("documentos"))
        self.assertContains(r, f'data-ver="/documentos/{factura.pk}/asociados/"')
        self.assertContains(r, "Asociados (1)")
        self.assertNotContains(r, f'/documentos/{sola.pk}/asociados/')
        self.assertContains(self.client.get(reverse("cotizaciones")), f"/documentos/{cot.pk}/asociados/")
        # Ventana y ficha.
        r = self.client.get(reverse("documento_asociados", args=[factura.pk]))
        self.assertContains(r, "Generados a partir de Factura N° 10")
        self.assertContains(r, str(nota))
        self.assertContains(r, "Crear un documento a partir de")
        self.assertContains(self.client.get(reverse("documento_asociados", args=[nota.pk])), "Viene de")
        r = self.client.get(reverse("documento", args=[guia.pk]))
        self.assertContains(r, "Documentos asociados")
        self.assertNotContains(r, "Crear un documento a partir de")
        self.assertNotContains(self.client.get(reverse("documento", args=[sola.pk])), "Documentos asociados")
        # La cotización no viaja al SII como referencia.
        self.assertIsNone(de_cot.referencia)


class EliminarClienteTests(Base):
    def test_editar_y_eliminar_clientes(self):
        libre = Cliente.objects.create(sociedad=self.soc, razon_social="Sin historial", nombre_fantasia="Libre")
        self.factura(folio=1)                                     # Cliente Uno queda con historial
        r = self.client.get(reverse("clientes"))
        self.assertContains(r, f'href="/clientes/{libre.pk}/" class="icono-accion editar"')
        self.assertContains(r, f'action="/clientes/{libre.pk}/eliminar/"')
        self.assertContains(r, "No se puede deshacer.")
        r = self.client.post(reverse("cliente_eliminar", args=[self.cli.pk]), follow=True)
        self.assertContains(r, "no se puede eliminar")
        self.assertTrue(Cliente.objects.filter(pk=self.cli.pk).exists())
        r = self.client.post(reverse("cliente_eliminar", args=[libre.pk]), follow=True)
        self.assertContains(r, "Cliente «Libre · Sin historial» eliminado")
        self.assertFalse(Cliente.objects.filter(pk=libre.pk).exists())
        self.assertEqual(self.client.get(reverse("cliente_eliminar", args=[self.cli.pk])).status_code, 405)


@override_settings(CORREO_CONFIGURADO=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DEFAULT_FROM_EMAIL="facturacion@ejemplo.cl")
class EnviarCotizacionTests(Base):
    def cotizacion(self):
        from .models import COTIZACION
        cot = Documento.objects.create(sociedad=self.soc, cliente=self.cli, tipo=COTIZACION)
        Linea.objects.create(documento=cot, descripcion="Albahaca", cantidad=10, precio=5000); cot.recalcular()
        return cot

    def test_envia_pdf_al_contacto_o_a_otro_correo(self):
        from django.core import mail
        cot = self.cotizacion()
        self.assertRedirects(self.client.get(reverse("cotizacion_enviar", args=[cot.pk])),
                             reverse("documento", args=[cot.pk]))                    # borrador: no
        self.client.post(reverse("cotizacion_emitir", args=[cot.pk]))
        # Sin contacto comercial: el campo viene vacío y se puede escribir.
        r = self.client.get(reverse("cotizacion_enviar", args=[cot.pk]))
        self.assertContains(r, "no tiene correo de contacto comercial")
        self.assertNotContains(r, "portal del SII")
        Cliente.objects.filter(pk=self.cli.pk).update(comercial_nombre="Ana", comercial_email="ana@ejemplo.cl")
        get_user_model().objects.filter(username="jaime").update(email="jaime@ejemplo.cl")
        r = self.client.get(reverse("cotizacion_enviar", args=[cot.pk]))
        self.assertContains(r, 'value="ana@ejemplo.cl"')
        self.assertContains(r, "Hola Ana:")
        self.assertContains(r, "cotización N° 1 por $59.500")
        r = self.client.post(reverse("cotizacion_enviar", args=[cot.pk]), {
            "para": "ana@ejemplo.cl, compras@ejemplo.cl", "asunto": "Cotización N° 1",
            "mensaje": "Adjunto."}, follow=True)
        self.assertContains(r, "enviada a ana@ejemplo.cl, compras@ejemplo.cl")
        m = mail.outbox[0]
        self.assertEqual((m.to, m.reply_to), (["ana@ejemplo.cl", "compras@ejemplo.cl"], ["jaime@ejemplo.cl"]))
        nombre, contenido, tipo = m.attachments[0]
        self.assertTrue(contenido.startswith(b"%PDF") and nombre.endswith(".pdf") and tipo == "application/pdf")
        self.assertContains(self.client.get(reverse("documento", args=[cot.pk])), "Enviado por correo")
        self.assertContains(self.client.get(reverse("cotizaciones")), f"/documentos/{cot.pk}/correo/?volver=lista")
        # Desde el listado vuelve al listado; correos inválidos o demasiados se rechazan.
        r = self.client.post(reverse("cotizacion_enviar", args=[cot.pk]),
                             {"para": "otro@ejemplo.cl", "asunto": "a", "mensaje": "b", "volver": "lista"})
        self.assertRedirects(r, reverse("cotizaciones"))
        for malo, error in (("no-es-correo", "no es un correo válido"),
                            ("a@x.cl,b@x.cl,c@x.cl,d@x.cl", "Máximo 3 correos")):
            r = self.client.post(reverse("cotizacion_enviar", args=[cot.pk]),
                                 {"para": malo, "asunto": "a", "mensaje": "b"}, follow=True)
            self.assertContains(r, error)
        self.assertEqual(len(mail.outbox), 2)

    def test_brevo_lleva_el_adjunto(self):
        from django.core.mail import EmailMessage
        from .correo import BrevoBackend
        m = EmailMessage("A", "B", to=["x@ejemplo.cl"])
        m.attach("cotizacion.pdf", b"%PDF-1.4 prueba", "application/pdf")
        cuerpo = BrevoBackend._cuerpo(m)
        self.assertEqual(cuerpo["attachment"], [{"name": "cotizacion.pdf", "content": "JVBERi0xLjQgcHJ1ZWJh"}])


class DecimalesTests(Base):
    def test_lectura_con_coma(self):
        from decimal import Decimal as D
        from .utils import leer_decimal_cl, redondear_arriba
        casos = {"14,5": D("14.5"), "1.250": D("1250"), "1.250,75": D("1250.75"), "350000": D("350000"),
                 "0,3333": D("0.3333"), " 2,5 ": D("2.5"), "1.000.000": D("1000000"), ",5": D("0.5")}
        for texto, esperado in casos.items():
            self.assertEqual(leer_decimal_cl(texto), esperado, texto)
        for malo in ("14.5", "1.25", "12.3456", "1,2,3", "abc", "1.2.3"):
            with self.assertRaises(ValueError, msg=malo):
                leer_decimal_cl(malo)
        from .utils import redondear
        self.assertEqual([redondear(x) for x in (D("42.3"), D("42.5"), D("42.49"), D("225000.9"))],
                         [42, 43, 42, 225001])

    def test_factura_con_decimales_y_redondeo_legal(self):
        r = self.client.post(reverse("documento_nuevo", args=[FACTURA]), {
            "cliente": self.cli.pk, "fecha_emision": "2026-11-10",
            "lineas-TOTAL_FORMS": 2, "lineas-INITIAL_FORMS": 0,
            "lineas-0-descripcion": "Albahaca", "lineas-0-cantidad": "3", "lineas-0-precio": "14,1",
            "lineas-0-descuento": "0",
            "lineas-1-descripcion": "Microgreens", "lineas-1-cantidad": "1,5", "lineas-1-precio": "1.250,33",
            "lineas-1-descuento": "2,5"})
        doc = Documento.objects.get()
        self.assertRedirects(r, reverse("documento", args=[doc.pk]))
        montos = list(doc.lineas.values_list("monto", flat=True))
        self.assertEqual(montos, [42, 1829])          # 42,3 -> 42 ; 1.828,61 -> 1.829 (art. 21 Ley 18.267)
        self.assertEqual((doc.neto, doc.iva, doc.total), (1871, 355, 2226))
        r = self.client.get(reverse("documento_editar", args=[doc.pk]))
        self.assertContains(r, 'value="14,1"')
        self.assertContains(r, 'value="1250,33"')
        self.assertContains(self.client.get(reverse("documento", args=[doc.pk])), "$1.250,33")
        # Un punto usado como decimal se rechaza con un mensaje claro.
        r = self.client.post(reverse("documento_editar", args=[doc.pk]), {
            "cliente": self.cli.pk, "fecha_emision": "2026-11-10",
            "lineas-TOTAL_FORMS": 1, "lineas-INITIAL_FORMS": 0,
            "lineas-0-descripcion": "x", "lineas-0-cantidad": "1", "lineas-0-precio": "14.5",
            "lineas-0-descuento": "0"})
        self.assertContains(r, "Usa coma para los decimales")

    def test_producto_con_coma(self):
        r = self.client.post(reverse("producto_nuevo"), {"nombre": "Granel", "precio": "14,5", "activo": "on"})
        self.assertRedirects(r, reverse("productos"))
        prod = Producto.objects.get(nombre="Granel")
        self.assertEqual(str(prod.precio.normalize()), "14.5")
        self.assertContains(self.client.get(reverse("producto_editar", args=[prod.pk])), 'value="14,5"')


@override_settings(CORREO_CONFIGURADO=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DEFAULT_FROM_EMAIL="facturacion@ejemplo.cl")
class EnviarDocumentosTests(Base):
    def test_factura_va_al_contacto_de_facturacion(self):
        from django.core import mail
        Cliente.objects.filter(pk=self.cli.pk).update(
            contacto_unico=False, comercial_email="ventas@ejemplo.cl",
            facturacion_nombre="Beto", facturacion_email="pagos@ejemplo.cl")
        borrador = self.factura()
        self.assertNotContains(self.client.get(reverse("documentos")), f"/documentos/{borrador.pk}/correo/")
        doc = self.factura(folio=15)
        self.assertContains(self.client.get(reverse("documentos")), f"/documentos/{doc.pk}/correo/?volver=lista")
        self.assertContains(self.client.get(reverse("documento", args=[doc.pk])), "Enviar por correo")
        r = self.client.get(reverse("documento_correo", args=[doc.pk]))
        self.assertContains(r, 'value="pagos@ejemplo.cl"')
        self.assertContains(r, "Hola Beto:")
        self.assertContains(r, "factura N° 15 por $119.000, con vencimiento el")
        self.assertContains(r, "portal del SII")            # registrada desde el portal: copia sin timbre
        r = self.client.post(reverse("documento_correo", args=[doc.pk]),
                             {"para": "pagos@ejemplo.cl", "asunto": "Factura N° 15", "mensaje": "Adjunto.",
                              "volver": "lista"})
        self.assertRedirects(r, reverse("documentos"))
        self.assertEqual([a[0] for a in mail.outbox[0].attachments], ["Factura_15_Verde_Vertical.pdf"])
        self.client.post(reverse("nota_nueva", args=[doc.pk, NOTA_CREDITO]))
        nota = Documento.objects.get(tipo=NOTA_CREDITO)
        self.client.post(reverse("documento_emitir", args=[nota.pk]), {"folio": 2})
        self.assertContains(self.client.get(reverse("documento_correo", args=[nota.pk])),
                            "que corrige la factura N° 15")


class LineasTests(Base):
    def test_basurero_elimina_linea_existente(self):
        doc = self.factura([(1, 1000, False), (2, 500, False)])
        ids = list(doc.lineas.values_list("pk", flat=True))
        r = self.client.get(reverse("documento_editar", args=[doc.pk]))
        self.assertContains(r, 'class="icono-accion basurero quitar-linea"', count=4)  # 2 líneas, 1 vacía y la plantilla
        datos = {"cliente": self.cli.pk, "fecha_emision": "2026-11-10", "fecha_vencimiento": "",
                 "lineas-TOTAL_FORMS": 3, "lineas-INITIAL_FORMS": 2}
        for i, (pk, precio) in enumerate(zip(ids, ("1000", "500"))):
            datos.update({f"lineas-{i}-id": pk, f"lineas-{i}-descripcion": "x", f"lineas-{i}-cantidad": "1",
                          f"lineas-{i}-precio": precio, f"lineas-{i}-descuento": "0"})
        datos["lineas-1-DELETE"] = "on"
        datos.update({"lineas-2-descripcion": "", "lineas-2-cantidad": "1", "lineas-2-precio": "abc",
                      "lineas-2-descuento": "0", "lineas-2-DELETE": "on"})   # una línea nueva descartada
        self.assertRedirects(self.client.post(reverse("documento_editar", args=[doc.pk]), datos),
                             reverse("documento", args=[doc.pk]))
        doc.refresh_from_db()
        self.assertEqual((doc.lineas.count(), doc.neto), (1, 1000))

    def test_panel_de_inicio(self):
        self.factura(folio=1)
        for vista in ("dia", "semana", "mes", "anio", "otra"):
            r = self.client.get(reverse("inicio"), {"vista": vista})
            self.assertContains(r, 'id="graficos-datos"')
            self.assertContains(r, "Ventas netas")
        r = self.client.get(reverse("inicio"), {"vista": "mes"})
        self.assertContains(r, "$100.000")            # venta neta del mes, sin IVA
        self.assertContains(r, "Principales clientes")


class MarcarPagadoTests(Base):
    def test_sin_fecha_queda_la_de_hoy(self):
        doc = self.factura(folio=1)
        r = self.client.get(reverse("cobranza"))
        self.assertContains(r, reverse("pago_marcar", args=[doc.pk]))
        self.assertContains(r, "<title>Cobranza</title>", html=False)
        r = self.client.post(reverse("pago_marcar", args=[doc.pk]),
                             {"fecha": "", "medio": "cheque", "nota": "", "volver": "/cobranza/?cliente="})
        self.assertRedirects(r, "/cobranza/?cliente=", fetch_redirect_response=False)
        pago = doc.pagos.get()
        self.assertEqual((pago.fecha, pago.medio, pago.monto), (date.today(), "cheque", 119000))
        doc.refresh_from_db()
        self.assertEqual(doc.saldo, 0)
        self.assertNotContains(self.client.get(reverse("cobranza")), reverse("pago_marcar", args=[doc.pk]))

    def test_con_fecha_elegida_y_saldo_parcial(self):
        doc = self.factura(folio=2)
        Pago.objects.create(documento=doc, monto=19000)
        self.client.post(reverse("pago_marcar", args=[doc.pk]),
                         {"fecha": "2026-10-05", "medio": "efectivo", "nota": "Caja"})
        pago = doc.pagos.order_by("-id").first()
        self.assertEqual((pago.fecha, pago.medio, pago.monto, pago.nota),
                         (date(2026, 10, 5), "efectivo", 100000, "Caja"))

    def test_rechaza_fecha_futura_y_redireccion_externa(self):
        doc = self.factura(folio=3)
        r = self.client.post(reverse("pago_marcar", args=[doc.pk]),
                             {"fecha": (date.today() + timedelta(days=3)).isoformat(),
                              "medio": "transferencia", "volver": "//malo.com/"})
        self.assertRedirects(r, reverse("cobranza"), fetch_redirect_response=False)
        self.assertFalse(doc.pagos.exists())


@override_settings(CORREO_CONFIGURADO=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CobranzaTests(Base):
    def test_plazo_colores(self):
        doc = self.factura(folio=1)
        casos = [(20, ("al-dia", "Faltan 20 días")), (7, ("pronto", "Faltan 7 días")),
                 (1, ("pronto", "Falta 1 día")), (0, ("pronto", "Vence hoy")),
                 (-1, ("vencida", "Vencida hace 1 día")), (-40, ("vencida", "Vencida hace 40 días"))]
        for dias, esperado in casos:
            doc.fecha_vencimiento = date.today() + timedelta(days=dias)
            self.assertEqual(doc.plazo, esperado)
        doc.save()
        self.assertContains(self.client.get(reverse("cobranza")), 'class="plazo p-vencida"')

    def test_recordatorio_va_al_contacto_de_cobranza(self):
        from django.core import mail
        self.cli.cobranza_email = "pagos@cliente.cl"
        self.cli.cobranza_nombre = "Marta"
        self.cli.contacto_unico = False
        self.cli.save()
        doc = self.factura(folio=7)
        doc.fecha_vencimiento = date.today() - timedelta(days=5)
        doc.save()
        r = self.client.get(reverse("cobranza_recordatorio", args=[doc.pk]))
        self.assertContains(r, "pagos@cliente.cl")
        self.assertContains(r, "Hola Marta")
        self.assertContains(r, "aún figura pendiente de pago")
        r = self.client.post(reverse("cobranza_recordatorio", args=[doc.pk]), {
            "para": "pagos@cliente.cl", "asunto": "Recordatorio", "mensaje": "Hola"})
        self.assertRedirects(r, reverse("cobranza"), fetch_redirect_response=False)
        self.assertEqual(mail.outbox[0].to, ["pagos@cliente.cl"])
        self.assertTrue(mail.outbox[0].attachments)
        doc.refresh_from_db()
        self.assertEqual(doc.recordatorios, 1)
        self.assertContains(self.client.get(reverse("cobranza")), "Recordado el")

    def test_sin_saldo_no_hay_recordatorio(self):
        doc = self.factura(folio=8)
        Pago.objects.create(documento=doc, monto=doc.total)
        r = self.client.get(reverse("cobranza_recordatorio", args=[doc.pk]))
        self.assertRedirects(r, reverse("cobranza"), fetch_redirect_response=False)


class CobranzaFiltrosTests(Base):
    def setUp(self):
        super().setUp()
        self.vencida = self.factura(folio=11)
        self.pronto = self.factura(folio=12)
        self.al_dia = self.factura(folio=13)
        for doc, dias in ((self.vencida, -10), (self.pronto, 3), (self.al_dia, 30)):
            doc.fecha_vencimiento = date.today() + timedelta(days=dias)
            doc.save()
        self.otro = Cliente.objects.create(sociedad=self.soc, razon_social="Otro Cliente SpA", rut="76086428-5")
        self.del_otro = self.factura(folio=14)
        self.del_otro.cliente = self.otro
        self.del_otro.fecha_vencimiento = date.today() - timedelta(days=2)
        self.del_otro.save()

    def mostradas(self, **params):
        r = self.client.get(reverse("cobranza"), params)
        return {d.pk for d in r.context["pendientes"]}

    def test_filtra_por_varios_estados(self):
        self.assertEqual(self.mostradas(estado=["vencida"]), {self.vencida.pk, self.del_otro.pk})
        self.assertEqual(self.mostradas(estado=["vencida", "pronto"]),
                         {self.vencida.pk, self.pronto.pk, self.del_otro.pk})
        self.assertEqual(self.mostradas(estado=["al-dia"]), {self.al_dia.pk})

    def test_filtra_por_varios_clientes_y_combina_con_estado(self):
        self.assertEqual(self.mostradas(cliente=[self.otro.pk]), {self.del_otro.pk})
        self.assertEqual(len(self.mostradas(cliente=[self.otro.pk, self.cli.pk])), 4)
        self.assertEqual(self.mostradas(cliente=[self.cli.pk], estado=["vencida"]), {self.vencida.pk})

    def test_marcar_varias_como_pagadas(self):
        r = self.client.post(reverse("pago_marcar_varios"), {
            "documentos": [self.vencida.pk, self.pronto.pk], "fecha": "2026-11-02",
            "medio": "transferencia", "nota": "Lote", "volver": "/cobranza/?estado=vencida"})
        self.assertRedirects(r, "/cobranza/?estado=vencida", fetch_redirect_response=False)
        for doc in (self.vencida, self.pronto):
            pago = doc.pagos.get()
            self.assertEqual((pago.fecha, pago.medio, pago.nota, pago.monto),
                             (date(2026, 11, 2), "transferencia", "Lote", doc.total))
        self.assertFalse(self.al_dia.pagos.exists())
        self.assertEqual(self.mostradas(), {self.al_dia.pk, self.del_otro.pk})

    def test_varias_ignora_documentos_de_otra_sociedad_y_sin_saldo(self):
        Pago.objects.create(documento=self.al_dia, monto=self.al_dia.total)
        self.client.post(reverse("pago_marcar_varios"), {
            "documentos": [self.al_dia.pk, self.pronto.pk, 99999], "medio": "cheque"})
        self.assertEqual(self.al_dia.pagos.count(), 1)
        self.assertEqual(self.pronto.pagos.get().fecha, date.today())


class PapeleraTests(Base):
    def eliminar(self, doc, **extra):
        return self.client.post(reverse("documento_eliminar", args=[doc.pk]), extra)

    def test_borrador_va_a_la_papelera_y_se_restaura(self):
        doc = self.factura()
        r = self.eliminar(doc)
        self.assertRedirects(r, reverse("documentos"), fetch_redirect_response=False)
        self.assertFalse(Documento.objects.filter(pk=doc.pk).exists())
        self.assertTrue(Documento.todos.filter(pk=doc.pk, eliminado_el__isnull=False).exists())
        r = self.client.get(reverse("documentos"))
        self.assertNotIn(doc, r.context["documentos"])
        self.assertEqual(r.context["deshacer"], doc)                # aviso «Deshacer»
        self.assertEqual(self.client.get(reverse("documento", args=[doc.pk])).status_code, 404)
        self.assertContains(self.client.get(reverse("papelera")), str(doc))
        self.client.post(reverse("papelera_accion"), {"accion": "restaurar", "documentos": [doc.pk]})
        self.assertTrue(Documento.objects.filter(pk=doc.pk).exists())

    def test_dte_timbrado_no_se_puede_eliminar(self):
        doc = self.factura(folio=5)
        Documento.objects.filter(pk=doc.pk).update(xml_firmado=b"<DTE/>")
        r = self.eliminar(doc)
        self.assertTrue(Documento.objects.filter(pk=doc.pk).exists())
        mensajes = " ".join(str(m) for m in get_messages(r.wsgi_request))
        self.assertIn("emite una nota de crédito", mensajes)
        # Rechazado por el SII: sí se puede.
        Documento.objects.filter(pk=doc.pk).update(estado_sii="rechazado")
        self.eliminar(doc)
        self.assertFalse(Documento.objects.filter(pk=doc.pk).exists())

    def test_registrada_del_portal_con_pagos_o_notas_no_se_elimina(self):
        doc = self.factura(folio=6)
        pago = Pago.objects.create(documento=doc, monto=1000)
        self.eliminar(doc)
        self.assertTrue(Documento.objects.filter(pk=doc.pk).exists())
        pago.delete()
        nota = Documento.objects.create(sociedad=self.soc, cliente=self.cli, tipo=NOTA_CREDITO, referencia=doc)
        self.eliminar(doc)
        self.assertTrue(Documento.objects.filter(pk=doc.pk).exists())
        nota.delete()
        self.eliminar(doc)
        self.assertFalse(Documento.objects.filter(pk=doc.pk).exists())
        self.assertEqual(self.client.get(reverse("cobranza")).context["pendientes"], [])

    def test_se_borra_sola_a_los_30_dias(self):
        vieja, nueva = self.factura(), self.factura()
        for d in (vieja, nueva):
            self.eliminar(d)
        Documento.todos.filter(pk=vieja.pk).update(eliminado_el=timezone.now() - timedelta(days=31))
        self.assertEqual(purgar_papelera(), 1)
        self.assertFalse(Documento.todos.filter(pk=vieja.pk).exists())
        self.assertTrue(Documento.todos.filter(pk=nueva.pk).exists())

    def test_eliminar_definitivo_y_vaciar(self):
        a, b, c = self.factura(), self.factura(), self.factura()
        for d in (a, b, c):
            self.eliminar(d)
        self.client.post(reverse("papelera_accion"), {"accion": "eliminar", "documentos": [a.pk]})
        self.assertFalse(Documento.todos.filter(pk=a.pk).exists())
        self.client.post(reverse("papelera_accion"), {"accion": "vaciar"})
        self.assertFalse(Documento.todos.filter(pk__in=[b.pk, c.pk]).exists())

    def test_recurrente_en_papelera_se_puede_regenerar(self):
        rec = Recurrente.objects.create(sociedad=self.soc, cliente=self.cli, dia_emision=1)
        RecurrenteLinea.objects.create(recurrente=rec, descripcion="Arriendo", cantidad=1, precio=1000)
        doc = rec.generar(2027, 1)
        self.eliminar(doc)
        nuevo = rec.generar(2027, 1)
        self.assertIsNotNone(nuevo)
        self.assertFalse(Documento.todos.filter(pk=doc.pk).exists())

    def test_folio_en_papelera_no_se_repite(self):
        from .models import COTIZACION
        cot = self.factura(tipo=COTIZACION)
        self.client.post(reverse("cotizacion_emitir", args=[cot.pk]))
        cot.refresh_from_db()
        self.eliminar(cot)
        otra = self.factura(tipo=COTIZACION)
        self.client.post(reverse("cotizacion_emitir", args=[otra.pk]))
        otra.refresh_from_db()
        self.assertEqual(otra.folio, cot.folio + 1)


class InventarioTests(Base):
    def setUp(self):
        super().setUp()
        self.soc.inventario_activo = True
        self.soc.inventario_desde = date(2020, 1, 1)
        self.soc.save()
        self.lechuga = Producto.objects.create(sociedad=self.soc, nombre="Lechuga hidropónica",
                                               unidad="UN", precio=800, stock_minimo=20)
        self.servicio = Producto.objects.create(sociedad=self.soc, nombre="Despacho", unidad="UN",
                                                precio=5000, controla_stock=False)

    def stock(self):
        from .inventario import stock
        return stock(self.soc).get(self.lechuga.pk, 0)

    def venta(self, cantidad=30, tipo=FACTURA, folio=100):
        doc = Documento.objects.create(sociedad=self.soc, cliente=self.cli, tipo=tipo, fecha_emision=hoy(),
                                       ind_traslado=1 if tipo == GUIA else None)
        Linea.objects.create(documento=doc, producto=self.lechuga, descripcion="Lechuga",
                             cantidad=cantidad, precio=800)
        Linea.objects.create(documento=doc, producto=self.servicio, descripcion="Despacho",
                             cantidad=1, precio=5000)
        doc.recalcular()
        return doc, self.client.post(reverse("documento_emitir", args=[doc.pk]), {"folio": folio})

    def entrada(self, cantidad, motivo="cosecha", costo=""):
        return self.client.post(reverse("inventario_entrada"), {
            "motivo": motivo, "fecha": hoy().isoformat(), "bodega": self.soc.bodegas.get().pk,
            "lote": "Invernadero 2", "nota": "",
            "lineas-TOTAL_FORMS": 1, "lineas-INITIAL_FORMS": 0, "lineas-MIN_NUM_FORMS": 1,
            "lineas-MAX_NUM_FORMS": 1000, "lineas-0-producto": self.lechuga.pk,
            "lineas-0-cantidad": cantidad, "lineas-0-costo_unitario": costo})

    def test_entrada_y_venta_rebajan_stock(self):
        self.client.get(reverse("inventario_config"))
        from .inventario import bodega_principal
        bodega_principal(self.soc)
        r = self.entrada("100,5", costo="300")
        self.assertRedirects(r, reverse("inventario_stock"), fetch_redirect_response=False)
        self.assertEqual(self.stock(), Decimal("100.5"))
        doc, _ = self.venta(30)
        self.assertEqual(self.stock(), Decimal("70.5"))
        self.assertFalse(Movimiento.objects.filter(producto=self.servicio).exists())  # servicio
        # Volver a borrador devuelve el stock; la papelera también.
        self.client.post(reverse("documento_reabrir", args=[doc.pk]))
        self.assertEqual(self.stock(), Decimal("100.5"))
        r = self.client.get(reverse("inventario_stock"))
        self.assertContains(r, "Lechuga hidropónica")
        self.assertContains(r, "$30.150")    # valor: 100,5 × 300

    def test_nota_de_credito_que_anula_devuelve(self):
        from .inventario import bodega_principal
        bodega_principal(self.soc)
        doc, _ = self.venta(10)
        self.assertEqual(self.stock(), -10)
        doc.refresh_from_db()
        nc = Documento.objects.create(sociedad=self.soc, cliente=self.cli, tipo=NOTA_CREDITO,
                                      referencia=doc, codigo_referencia=1, razon_referencia="Anula",
                                      fecha_emision=hoy())
        Linea.objects.create(documento=nc, producto=self.lechuga, descripcion="Lechuga", cantidad=10, precio=800)
        nc.estado = Documento.EMITIDO
        nc.folio = 5
        nc.save()
        self.assertEqual(self.stock(), 0)

    def test_guia_rebaja_solo_si_se_configura(self):
        from .inventario import bodega_principal
        bodega_principal(self.soc)
        self.venta(5, tipo=GUIA, folio=7)
        self.assertEqual(self.stock(), 0)
        self.soc.inventario_rebaja = "guia"
        self.soc.save()
        doc, r = self.venta(5, tipo=GUIA, folio=8)
        self.assertEqual(self.stock(), -5, [str(m) for m in get_messages(r.wsgi_request)])

    def test_sin_stock_negativo_bloquea_emision(self):
        self.soc.inventario_permite_negativo = False
        self.soc.save()
        doc, r = self.venta(30)
        doc.refresh_from_db()
        self.assertEqual(doc.estado, Documento.BORRADOR)
        self.assertIn("Stock insuficiente", " ".join(str(m) for m in get_messages(r.wsgi_request)))

    def test_toma_de_inventario_ajusta_diferencias(self):
        from .inventario import bodega_principal
        bodega_principal(self.soc)
        self.entrada("50")
        self.client.post(reverse("inventario_toma"), {
            "bodega": self.soc.bodegas.get().pk, "fecha": hoy().isoformat(),
            f"contado_{self.lechuga.pk}": "47,5"})
        self.assertEqual(self.stock(), Decimal("47.5"))
        ajuste = Movimiento.objects.get(motivo="ajuste_menos")
        self.assertEqual(ajuste.cantidad, Decimal("-2.5"))

    def test_merma_y_eliminar_movimiento_manual(self):
        from .inventario import bodega_principal
        bodega_principal(self.soc)
        self.entrada("20")
        self.client.post(reverse("inventario_salida"), {
            "motivo": "merma", "fecha": hoy().isoformat(), "bodega": self.soc.bodegas.get().pk,
            "nota": "Daño por calor", "lineas-TOTAL_FORMS": 1, "lineas-INITIAL_FORMS": 0,
            "lineas-MIN_NUM_FORMS": 1, "lineas-MAX_NUM_FORMS": 1000,
            "lineas-0-producto": self.lechuga.pk, "lineas-0-cantidad": "3"})
        self.assertEqual(self.stock(), 17)
        merma = Movimiento.objects.get(motivo="merma")
        self.client.post(reverse("inventario_movimiento_eliminar", args=[merma.pk]))
        self.assertEqual(self.stock(), 20)
        self.assertContains(self.client.get(reverse("inventario_kardex", args=[self.lechuga.pk])), "Cosecha")

    def test_documentos_anteriores_a_la_activacion_no_mueven(self):
        self.soc.inventario_desde = hoy() + timedelta(days=1)
        self.soc.save()
        self.venta(10)
        self.assertEqual(self.stock(), 0)

    def test_desactivado_redirige_a_configuracion(self):
        self.soc.inventario_activo = False
        self.soc.save()
        self.assertRedirects(self.client.get(reverse("inventario_stock")), reverse("inventario_config"))
        self.assertNotContains(self.client.get(reverse("inicio")), "Movimientos</span>")


class PerfilTests(Base):
    def test_editar_datos_y_firma(self):
        r = self.client.get(reverse("perfil"))
        self.assertContains(r, self.user.email or "—")
        self.client.post(reverse("perfil"), {"accion": "datos", "first_name": "Jaime", "last_name": "Riesco",
                                             "cargo": "Gerente", "telefono": "+56 9 1234 5678",
                                             "sociedad_inicial": self.soc.pk})
        self.user.refresh_from_db()
        self.assertEqual(self.user.get_full_name(), "Jaime Riesco")
        from .models import firma_de
        self.assertEqual(firma_de(self.user, self.soc), "Jaime Riesco\nGerente\nVerde Vertical\n+56 9 1234 5678")
        self.assertContains(self.client.get(reverse("inicio")), "Jaime Riesco")

    def test_el_correo_no_se_puede_cambiar(self):
        antes = self.user.email
        self.client.post(reverse("perfil"), {"accion": "datos", "first_name": "X", "email": "otro@x.cl"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, antes)

    def test_cambiar_clave_sin_cerrar_sesion(self):
        r = self.client.post(reverse("perfil"), {"accion": "clave", "old_password": self.clave,
                                                 "new_password1": "Huerto-Hidroponico-2026",
                                                 "new_password2": "Huerto-Hidroponico-2026"})
        self.assertRedirects(r, reverse("perfil"))
        self.assertEqual(self.client.get(reverse("inicio")).status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Huerto-Hidroponico-2026"))



class RolesTests(Base):
    def setUp(self):
        super().setUp()
        get_user_model().objects.filter(pk=self.user.pk).update(is_superuser=True, is_staff=True)

    def test_admin_asigna_cargo_y_cuenta_basica(self):
        r = self.client.post(reverse("usuario_nuevo"), {
            "username": "maria", "first_name": "María", "last_name": "Soto", "email": "maria@ejemplo.cl",
            "cargo": "Gerente Comercial", "clave": "una-clave-larga-2026", "rol": "basica",
            "is_active": "on"})
        self.assertRedirects(r, reverse("usuarios"))
        maria = get_user_model().objects.get(username="maria")
        self.assertFalse(maria.is_superuser)
        self.assertEqual(maria.perfil.cargo, "Gerente Comercial")
        lista = self.client.get(reverse("usuarios"))
        self.assertContains(lista, "Gerente Comercial")
        self.assertContains(lista, '<span class="rol rol-basica">Cuenta básica</span>', html=True)
        self.assertContains(lista, '<span class="estado-u activo">Activo</span>', html=True)
        # La cuenta básica no entra a Usuarios ni edita a otros.
        self.client.login(username="maria", password="una-clave-larga-2026")
        self.assertEqual(self.client.get(reverse("usuarios")).status_code, 302)
        self.assertEqual(self.client.get(reverse("usuario_editar", args=[self.user.pk])).status_code, 302)
        self.assertNotContains(self.client.get(reverse("inicio")), 'href="/usuarios/"')

    def test_desactivar_y_ultimo_administrador(self):
        otro = get_user_model().objects.create_user("pedro", password="x" * 14)
        r = self.client.post(reverse("usuario_editar", args=[otro.pk]), {
            "username": "pedro", "rol": "basica"})   # sin is_active => inactivo
        otro.refresh_from_db()
        self.assertFalse(otro.is_active)
        self.assertContains(self.client.get(reverse("usuarios")), '<span class="estado-u inactivo">Inactivo</span>', html=True)
        # No puede quitarse a sí mismo el rol de administrador.
        self.client.post(reverse("usuario_editar", args=[self.user.pk]), {
            "username": "jaime", "rol": "basica", "is_active": "on"})
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_superuser)


RCV_EJEMPLO = (
    "Nro;Tipo Doc;Tipo Compra;RUT Proveedor;Razon Social;Folio;Fecha Docto;Fecha Recepcion;Fecha Acuse;"
    "Monto Exento;Monto Neto;Monto IVA Recuperable;Monto Iva No Recuperable;Codigo IVA No Rec.;Monto Total;"
    "Monto Neto Activo Fijo;IVA Activo Fijo;IVA uso Comun;Impto. Sin Derecho a Credito;IVA No Retenido;"
    "Tabacos Puros;Tabacos Cigarrillos;Tabacos Elaborados;NCE o NDE sobre Fact. de Compra;"
    "Codigo Otro Impuesto;Valor Otro Impuesto;Tasa Otro Impuesto\n"
    "1;33;Del Giro;76192083-9;SEMILLAS DEL SUR SPA;1234;05/11/2026;06/11/2026 10:11:12;;0;100000;19000;0;;119000;0;0;0;0;0;0;0;0;0;;;\n"
    "2;34;Del Giro;76086428-5;AGUAS ANDINAS S.A.;88;10/11/2026;10/11/2026 08:00:00;;25000;0;0;0;;25000;0;0;0;0;0;0;0;0;0;;;\n"
    "3;61;Del Giro;76192083-9;SEMILLAS DEL SUR SPA;45;12/11/2026;12/11/2026 09:00:00;;0;10000;1900;0;;11900;0;0;0;0;0;0;0;0;0;;;\n"
    "4;33;Del Giro;11111111-2;RUT MALO;9;12/11/2026;;;0;1000;190;0;;1190;0;0;0;0;0;0;0;0;0;;;\n"
).encode("cp1252")


class ComprasTests(Base):
    def importar(self, contenido=RCV_EJEMPLO, nombre="RCV_COMPRA_REGISTRO_77552628-9_202611.csv"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(reverse("compras_importar"), {
            "archivos": [SimpleUploadedFile(nombre, contenido, content_type="text/csv")], "estado": ""})

    def test_importa_rcv_y_no_duplica(self):
        from .models import Compra, Proveedor
        r = self.importar()
        self.assertContains(r, "3 nuevas")
        self.assertContains(r, "Línea 5: RUT")
        self.assertEqual(Compra.objects.count(), 3)
        self.assertEqual(Proveedor.objects.count(), 2)
        f = Compra.objects.get(folio=1234)
        self.assertEqual((f.neto, f.iva, f.total, f.periodo, f.estado_rcv), (100000, 19000, 119000, "2026-11", "registro"))
        self.assertEqual(f.fecha_vencimiento, date(2026, 12, 5))
        nc = Compra.objects.get(folio=45)
        self.assertEqual((nc.gasto, nc.saldo), (-10000, 0))
        r = self.importar()
        self.assertContains(r, "0 nuevas")
        self.assertEqual(Compra.objects.count(), 3)
        lista = self.client.get(reverse("compras"))
        self.assertEqual(lista.context["gasto"], 100000 + 25000 - 10000)
        self.assertEqual(lista.context["iva"], 19000 - 1900)

    def test_archivo_que_no_es_rcv(self):
        r = self.importar(b"a;b;c\n1;2;3\n", "otro.csv")
        self.assertContains(r, "no parece un detalle de compras")

    def test_categorizar_recordar_y_pagar_varias(self):
        from .models import CategoriaGasto, Compra, PagoCompra
        self.importar()
        semillas = CategoriaGasto.objects.get(sociedad=self.soc, nombre="Semillas y plantines")
        f = Compra.objects.get(folio=1234)
        agua = Compra.objects.get(folio=88)
        self.client.post(reverse("compras_accion"), {"accion": "categoria", "compras": [f.pk],
                                                     "categoria": semillas.pk, "recordar": "on"})
        f.refresh_from_db()
        self.assertEqual(f.categoria, semillas)
        self.assertEqual(f.proveedor.categoria, semillas)
        self.client.post(reverse("compras_accion"), {"accion": "pagar", "compras": [f.pk, agua.pk],
                                                     "fecha": "2026-11-11", "medio": "transferencia"})
        self.assertEqual(PagoCompra.objects.count(), 2)
        self.assertEqual(Compra.objects.con_saldo().get(pk=f.pk).saldo, 0)
        r = self.client.get(reverse("inicio"))
        self.assertContains(r, "Ventas y gastos")

    def test_compra_manual_y_pago_parcial(self):
        from .models import Compra
        r = self.client.post(reverse("compra_nueva"), {
            "rut_proveedor": "76.192.083-9", "razon_social": "Ferretería Uno", "tipo": 33, "folio": 77,
            "fecha_emision": "2026-11-03", "neto": 50000, "iva": 9500, "total": "", "exento": ""})
        c = Compra.objects.get(folio=77)
        self.assertRedirects(r, reverse("compra", args=[c.pk]))
        self.assertEqual((c.total, c.origen, c.periodo), (59500, "manual", "2026-11"))
        self.client.post(reverse("compra", args=[c.pk]), {"accion": "pago", "monto": "20.000",
                                                          "fecha": "2026-11-05", "medio": "cheque"})
        self.assertEqual(Compra.objects.con_saldo().get(pk=c.pk).saldo, 39500)
        self.client.post(reverse("compra_eliminar", args=[c.pk]))
        self.assertFalse(Compra.objects.filter(pk=c.pk).exists())
