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

    def test_no_duplica_compras_manuales_filas_repetidas_ni_retrocede_estado(self):
        from .models import Compra, Proveedor
        prov = Proveedor.objects.create(sociedad=self.soc, rut="76192083-9", razon_social="Semillas")
        Compra.objects.create(sociedad=self.soc, proveedor=prov, tipo=33, folio=1234, origen="manual",
                              estado_rcv="manual", fecha_emision=date(2026, 11, 5), periodo="2026-11",
                              neto=100000, iva=19000, total=119000)
        lineas = RCV_EJEMPLO.decode("cp1252").splitlines()
        repetido = "\n".join(lineas + [lineas[1]]).encode("cp1252")      # misma factura dos veces en el archivo
        r = self.importar(repetido)
        self.assertContains(r, "2 nuevas")
        self.assertContains(r, "ingresado a mano")
        self.assertEqual(Compra.objects.filter(folio=1234).count(), 1)
        self.assertEqual(Compra.objects.get(folio=1234).estado_rcv, "registro")
        self.importar(nombre="RCV_COMPRA_PENDIENTE_77552628-9_202611.csv")
        self.assertEqual(Compra.objects.count(), 3)
        self.assertEqual(Compra.objects.get(folio=1234).estado_rcv, "registro")

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


CARTOLA = (
    "VERDE VERTICAL SPA                           (077552628-9) cta:001234567890\r\n"
    "Fecha;Detalle Movimiento;Cheque o Cargo;Deposito o Abono;Saldo;Docto. Nro.;Trn;Caja;Sucursal\r\n"
    "01/11/2026;TRASPASO DE:CLIENTE UNO SPA          ;00000000000;+0000119000;+0000619000;00000000000;0;0;INTERNET       \r\n"
    "03/11/2026;TRASPASO A:Semillas del Sur          ;+0000059500;00000000000;+0000559500;00000000000;0;0;INTERNET       \r\n"
    "05/11/2026;PAGO TESORERIA GRAL REPUBLICA 123    ;+0000080000;00000000000;+0000479500;00000000000;0;0;INTERNET       \r\n"
    "05/11/2026;COMISION MANTENCION CTA CTE          ;+0000005000;00000000000;+0000474500;00000000000;0;0;INTERNET       \r\n"
).encode("utf-8")


class BancoTests(Base):
    def subir(self, contenido=CARTOLA, **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(reverse("banco_importar"), {
            "archivos": [SimpleUploadedFile("CartolaEmitida.xls", contenido)], **extra})

    def test_importa_cartola_banco_de_chile_sin_duplicar(self):
        from .models import CuentaBancaria, MovimientoBancario
        r = self.subir()
        self.assertContains(r, "4 movimientos nuevos")
        self.assertContains(r, "Los saldos de la cartola cuadran")
        cuenta = CuentaBancaria.objects.get()
        self.assertEqual((cuenta.banco, cuenta.numero), ("Banco de Chile", "1234567890"))
        montos = list(MovimientoBancario.objects.order_by("fecha", "orden").values_list("monto", flat=True))
        self.assertEqual(montos, [119000, -59500, -80000, -5000])
        self.assertContains(self.subir(), "4 ya estaban")
        self.assertEqual(MovimientoBancario.objects.count(), 4)
        r = self.client.get(reverse("banco"))
        self.assertEqual((r.context["abonos"], r.context["cargos"], r.context["por_revisar"]), (119000, 144500, 4))

    def test_rechaza_cartola_de_otro_rut(self):
        otra = CARTOLA.replace(b"(077552628-9)", b"(096724880-0)")
        self.assertContains(self.subir(otra), "no de Verde Vertical")
        self.assertContains(self.subir(otra, otro_rut="on"), "4 movimientos nuevos")

    def test_concilia_cobro_con_sugerencia_y_deshace(self):
        from .banco import sugerencias
        from .models import MovimientoBancario
        doc = self.factura(folio=10)              # 119.000 a Cliente Uno
        self.subir()
        abono = MovimientoBancario.objects.get(monto=119000)
        s = sugerencias(abono)
        self.assertEqual(s[0]["doc"], doc)
        self.assertTrue(s[0]["exacto"] and s[0]["nombre"])
        self.client.post(reverse("banco_movimiento", args=[abono.pk]),
                         {"accion": "conciliar", "doc": [f"v-{doc.pk}"], f"monto-v-{doc.pk}": "119.000"})
        abono.refresh_from_db()
        self.assertEqual(abono.estado, "conciliado")
        self.assertEqual(abono.categoria.nombre, "Cobro a clientes")
        pago = doc.pagos.get()
        self.assertEqual((pago.monto, pago.fecha), (119000, date(2026, 11, 1)))
        self.assertEqual(Documento.objects.con_saldo().get(pk=doc.pk).saldo, 0)
        self.client.post(reverse("banco_movimiento", args=[abono.pk]), {"accion": "deshacer"})
        self.assertFalse(doc.pagos.exists())
        abono.refresh_from_db()
        self.assertEqual(abono.estado, "pendiente")

    def test_concilia_pago_de_compra(self):
        from .models import Compra, MovimientoBancario, Proveedor
        prov = Proveedor.objects.create(sociedad=self.soc, rut="76192083-9", razon_social="Semillas del Sur SpA")
        compra = Compra.objects.create(sociedad=self.soc, proveedor=prov, tipo=33, folio=5,
                                       fecha_emision=date(2026, 10, 30), periodo="2026-10",
                                       neto=50000, iva=9500, total=59500)
        self.subir()
        cargo = MovimientoBancario.objects.get(monto=-59500)
        self.client.post(reverse("banco_movimiento", args=[cargo.pk]),
                         {"accion": "conciliar", "doc": [f"c-{compra.pk}"], f"monto-c-{compra.pk}": "59500"})
        self.assertEqual(Compra.objects.con_saldo().get(pk=compra.pk).saldo, 0)

    def test_regla_categoriza_y_se_aplica_al_importar(self):
        from .models import CategoriaBanco, MovimientoBancario, categorias_banco_de
        categorias_banco_de(self.soc)
        impuestos = CategoriaBanco.objects.get(sociedad=self.soc, nombre__startswith="Impuestos")
        self.client.post(reverse("banco_regla_nueva"), {"texto": "TESORERIA", "categoria": impuestos.pk,
                                                        "solo": "cargo", "prioridad": 100, "activa": "on",
                                                        "accion": "guardar"})
        self.subir()
        m = MovimientoBancario.objects.get(descripcion__contains="TESORERIA")
        self.assertEqual(m.categoria, impuestos)
        comision = MovimientoBancario.objects.get(descripcion__contains="COMISION")
        gastos = CategoriaBanco.objects.get(sociedad=self.soc, nombre__startswith="Comisiones")
        self.client.post(reverse("banco_movimiento", args=[comision.pk]),
                         {"accion": "categoria", "categoria": gastos.pk, "nota": "", "regla": "COMISION"})
        comision.refresh_from_db()
        self.assertEqual(comision.categoria, gastos)
        self.assertTrue(self.soc.reglas_banco.filter(texto="COMISION").exists())

    def test_rechaza_pdf(self):
        self.assertContains(self.subir(b"%PDF-1.4 ..."), "No pude abrir el PDF")


class CategoriasYReglasTests(Base):
    subir = BancoTests.subir

    def setUp(self):
        super().setUp()
        from .models import categorias_banco_de
        categorias_banco_de(self.soc)
        self.subir()

    def cat(self, inicio):
        return self.soc.categorias_banco.get(nombre__startswith=inicio)

    def test_crea_categoria_de_egreso_con_grupo_y_no_duplica(self):
        url = reverse("banco_configuracion")
        self.client.post(url, {"nombre": "Mantención invernaderos", "tipo": "egreso", "grupo": "Gastos operacionales"})
        c = self.soc.categorias_banco.get(nombre="Mantención invernaderos")
        self.assertEqual((c.tipo, c.grupo), ("egreso", "Gastos operacionales"))
        r = self.client.post(url, {"nombre": "mantención INVERNADEROS", "tipo": "egreso"}, follow=True)
        self.assertContains(r, "Ya existe")
        r = self.client.get(url)
        self.assertContains(r, "Mantención invernaderos")
        self.assertContains(r, "Gastos operacionales")

    def test_eliminar_categoria_usada_exige_destino_y_fusiona(self):
        from .models import MovimientoBancario, ReglaBanco
        origen, destino = self.cat("Comisiones"), self.cat("Otros egresos")
        m = MovimientoBancario.objects.get(descripcion__contains="COMISION")
        m.categoria = origen
        m.save()
        regla = ReglaBanco.objects.create(sociedad=self.soc, texto="COMISION", categoria=origen)
        url = reverse("banco_categoria_eliminar", args=[origen.pk])
        r = self.client.post(url, follow=True)
        self.assertContains(r, "elige a qué categoría")
        self.assertTrue(self.soc.categorias_banco.filter(pk=origen.pk).exists())
        self.client.post(url, {"destino": destino.pk})
        self.assertFalse(self.soc.categorias_banco.filter(pk=origen.pk).exists())
        m.refresh_from_db(); regla.refresh_from_db()
        self.assertEqual((m.categoria, regla.categoria), (destino, destino))

    def test_regla_con_varias_palabras_monto_y_prioridad(self):
        from .banco import aplicar_reglas
        from .models import MovimientoBancario, ReglaBanco
        imp, otros = self.cat("Impuestos"), self.cat("Otros egresos")
        ReglaBanco.objects.create(sociedad=self.soc, texto="tgr, tesorería", categoria=imp, prioridad=10,
                                  monto_min=50000)
        ReglaBanco.objects.create(sociedad=self.soc, texto="TESORERIA", categoria=otros, prioridad=50)
        ReglaBanco.objects.create(sociedad=self.soc, texto="COMISION", categoria=imp, monto_min=10000)
        pendientes = list(MovimientoBancario.objects.filter(categoria__isnull=True))
        aplicar_reglas(self.soc, pendientes)
        tes = MovimientoBancario.objects.get(descripcion__contains="TESORERIA")
        com = MovimientoBancario.objects.get(descripcion__contains="COMISION")
        self.assertEqual(tes.categoria, imp)            # sin tilde, gana la de menor prioridad
        self.assertIsNone(com.categoria)                # 5.000 < monto mínimo
        self.assertEqual(ReglaBanco.objects.get(prioridad=10).veces, 1)

    def test_probar_no_guarda_y_guardar_aplica(self):
        from .models import MovimientoBancario, ReglaBanco
        imp = self.cat("Impuestos")
        datos = {"texto": "TESORERIA", "categoria": imp.pk, "solo": "cargo", "prioridad": 100, "activa": "on"}
        r = self.client.post(reverse("banco_regla_nueva"), {**datos, "accion": "probar"})
        self.assertEqual(r.context["resumen_vista"]["total"], 1)
        self.assertFalse(ReglaBanco.objects.filter(texto="TESORERIA").exists())
        self.client.post(reverse("banco_regla_nueva"), {**datos, "accion": "guardar", "aplicar": "on"})
        self.assertEqual(MovimientoBancario.objects.get(descripcion__contains="TESORERIA").categoria, imp)
        r = self.client.post(reverse("banco_regla_nueva"), {**datos, "texto": "ab", "accion": "guardar"})
        self.assertContains(r, "al menos 3 letras")

    def test_pausar_editar_y_borrar_regla(self):
        from .models import ReglaBanco
        regla = ReglaBanco.objects.create(sociedad=self.soc, texto="TESORERIA", categoria=self.cat("Impuestos"))
        self.client.post(reverse("banco_reglas"), {"accion": "activar", "pk": regla.pk})
        regla.refresh_from_db()
        self.assertFalse(regla.activa)
        r = self.client.get(reverse("banco_regla", args=[regla.pk]))
        self.assertEqual(r.context["resumen_vista"]["total"], 1)
        self.client.post(reverse("banco_regla", args=[regla.pk]), {
            "texto": "TESORERIA, TGR", "categoria": regla.categoria_id, "prioridad": 5, "accion": "guardar"})
        regla.refresh_from_db()
        self.assertEqual((regla.texto, regla.prioridad), ("TESORERIA, TGR", 5))
        self.client.post(reverse("banco_reglas"), {"accion": "borrar", "pk": regla.pk})
        self.assertFalse(ReglaBanco.objects.filter(pk=regla.pk).exists())

    def test_sugerencias_de_movimientos_repetidos(self):
        from .banco import sugerir_reglas
        from .models import CuentaBancaria, MovimientoBancario
        cuenta = CuentaBancaria.objects.get()
        for i, dia in enumerate((3, 10, 17)):
            MovimientoBancario.objects.create(cuenta=cuenta, fecha=date(2026, 11, dia), orden=100 + i, monto=-30000,
                                              descripcion=f"PAGO ARRIENDO BODEGA 00{i}", huella=f"x{i}")
        s = sugerir_reglas(self.soc)
        self.assertEqual((s[0]["texto"], s[0]["n"], s[0]["solo"]), ("PAGO ARRIENDO BODEGA", 3, "cargo"))
        self.assertContains(self.client.get(reverse("banco_reglas")), "PAGO ARRIENDO BODEGA")

    def test_resumen_por_categoria_y_excel(self):
        from .models import MovimientoBancario
        imp = self.cat("Impuestos")
        MovimientoBancario.objects.filter(descripcion__contains="TESORERIA").update(categoria=imp)
        r = self.client.get(reverse("banco_resumen"), {"meses": 3})
        egresos = r.context["secciones"][1]
        nombres = [f["nombre"] for g in egresos["grupos"] for f in g["filas"]]
        self.assertIn(imp.nombre, nombres)
        self.assertEqual(egresos["total"], 144500)
        self.assertEqual(sum(r.context["neto"]), 119000 - 144500)
        r = self.client.get(reverse("banco_resumen"), {"formato": "excel"})
        self.assertEqual(r["Content-Type"].split(";")[0],
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        r = self.client.get(reverse("banco"), {"mes": "todos", "categoria": "sin"})
        self.assertEqual(len(r.context["movimientos"]), 3)


RCV_VENTAS = (
    "Nro;Tipo Doc;Tipo Venta;Rut cliente;Razon Social;Folio;Fecha Docto;Fecha Recepcion;Fecha Acuse Recibo;"
    "Fecha Reclamo;Monto Exento;Monto Neto;Monto IVA;Monto total;IVA Retenido Total;Tipo Docto. Referencia;"
    "Folio Docto. Referencia\n"
    "1;33;Del Giro;{rut};CLIENTE UNO;10;03/11/2026;03/11/2026 10:00:00;;;0;100000;19000;119000;0;;\n"
    "2;33;Del Giro;76086428-5;AGUAS ANDINAS S.A.;11;04/11/2026;04/11/2026 10:00:00;;;0;50000;9500;59500;0;;\n"
    "3;61;Del Giro;76086428-5;AGUAS ANDINAS S.A.;3;05/11/2026;05/11/2026 10:00:00;;;0;50000;9500;59500;0;33;11\n"
    "4;39;Del Giro;66666666-6;BOLETAS;1;05/11/2026;;;;0;1000;190;1190;0;;\n"
    "5;33;Del Giro;76086428-5;AGUAS ANDINAS S.A.;11;04/11/2026;04/11/2026 10:00:00;;;0;50000;9500;59500;0;;\n"
)


class VentasSIITests(Base):
    def importar(self, contenido):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(reverse("ventas_importar"), {
            "archivos": [SimpleUploadedFile("RCV_VENTA_77552628-9_202611.csv", contenido, content_type="text/csv")]})

    def test_modulo_opcional(self):
        r = self.client.get(reverse("ventas_importar"))
        self.assertContains(r, "Módulo opcional")
        self.client.post(reverse("ventas_importar"), {"accion": "activar"})     # cuenta básica: no puede
        self.soc.refresh_from_db()
        self.assertFalse(self.soc.ventas_sii_activo)
        self.user.is_superuser = True
        self.user.save()
        self.client.post(reverse("ventas_importar"), {"accion": "activar"})
        self.soc.refresh_from_db()
        self.assertTrue(self.soc.ventas_sii_activo)

    def test_no_duplica_facturas_de_la_plataforma_ni_reimportaciones(self):
        self.soc.ventas_sii_activo = True
        self.soc.save()
        propia = self.factura(folio=10)                         # emitida desde la app: 119.000
        self.assertEqual(propia.estado, Documento.EMITIDO)
        contenido = RCV_VENTAS.format(rut=self.cli.rut).encode("cp1252")
        r = self.importar(contenido)
        self.assertContains(r, "2 documentos nuevos")
        self.assertContains(r, "1 ya estaba en la plataforma")
        self.assertContains(r, "no se importa")             # la boleta
        self.assertEqual(Documento.objects.filter(tipo=FACTURA, folio=10).count(), 1)
        propia.refresh_from_db()
        self.assertFalse(propia.importado_sii)
        f11 = Documento.objects.get(folio=11, tipo=FACTURA)
        self.assertEqual((f11.total, f11.estado_sii, f11.importado_sii, f11.cliente.razon_social),
                         (59500, "portal", True, "AGUAS ANDINAS S.A."))
        nc = Documento.objects.get(tipo=61, folio=3)
        self.assertEqual((nc.referencia, nc.codigo_referencia), (f11, 1))
        self.assertEqual(Documento.objects.con_saldo().get(pk=f11.pk).saldo_a, 0)
        r = self.importar(contenido)                             # el mismo mes otra vez
        self.assertContains(r, "0 documentos nuevos")
        self.assertEqual(Documento.objects.ventas().count(), 3)

    def test_avisa_diferencia_de_monto_y_rechaza_archivo_de_compras(self):
        self.soc.ventas_sii_activo = True
        self.soc.save()
        self.factura(folio=10, lineas=((1, 90000, False),))
        r = self.importar(RCV_VENTAS.format(rut=self.cli.rut).encode("cp1252"))
        self.assertContains(r, "total distinto")
        self.assertContains(self.importar(RCV_EJEMPLO), "registro de COMPRAS")


class PaginacionTests(Base):
    def test_listados_de_50_con_paginas_y_filtro_opcional_por_mes(self):
        from .models import Compra, Proveedor
        for i in range(55):
            Documento.objects.create(sociedad=self.soc, cliente=self.cli, tipo=FACTURA, folio=100 + i,
                                     estado=Documento.EMITIDO, fecha_emision=date(2026, 10 + (i < 5), 1 + i % 28))
        r = self.client.get(reverse("documentos"))
        self.assertEqual((len(r.context["documentos"]), r.context["pagina"].paginator.count), (50, 55))
        self.assertContains(r, "1–50 de 55")
        r = self.client.get(reverse("documentos"), {"pagina": 2})
        self.assertEqual(len(r.context["documentos"]), 5)
        r = self.client.get(reverse("documentos"), {"mes": "2026-11"})
        self.assertEqual(r.context["pagina"].paginator.count, 5)
        prov = Proveedor.objects.create(sociedad=self.soc, rut="76192083-9", razon_social="Semillas")
        for i in range(52):
            Compra.objects.create(sociedad=self.soc, proveedor=prov, tipo=33, folio=i + 1,
                                  fecha_emision=date(2026, 9 + i % 2, 1), periodo=f"2026-{9 + i % 2:02d}",
                                  neto=1000, iva=190, total=1190)
        r = self.client.get(reverse("compras"))
        self.assertEqual((len(r.context["compras"]), r.context["n_total"]), (50, 52))
        self.assertEqual(r.context["gasto"], 52 * 1000)        # los totales consideran todo, no solo la página
        r = self.client.get(reverse("compras"), {"periodo": "2026-09"})
        self.assertEqual(r.context["n_total"], 26)

    def test_movimientos_del_banco_paginados_mas_recientes_primero(self):
        from .models import CuentaBancaria, MovimientoBancario
        cuenta = CuentaBancaria.objects.create(sociedad=self.soc, banco="Banco de Chile", numero="123")
        for i in range(53):
            MovimientoBancario.objects.create(cuenta=cuenta, fecha=date(2026, 10 + (i >= 50), 1 + i % 28),
                                              orden=i, monto=-1000 - i, descripcion=f"MOV {i}", huella=f"h{i}")
        r = self.client.get(reverse("banco"))
        self.assertEqual((len(r.context["movimientos"]), r.context["pagina"].paginator.count), (50, 53))
        self.assertEqual(r.context["movimientos"][0].fecha.month, 11)
        self.assertEqual(len(self.client.get(reverse("banco"), {"pagina": 2}).context["movimientos"]), 3)
        self.assertEqual(self.client.get(reverse("banco"), {"mes": "2026-11"}).context["total"], 3)


class DescargaGraficosTests(Base):
    def test_cada_grafico_del_inicio_se_descarga_con_datos_y_grafico(self):
        import io
        from openpyxl import load_workbook
        doc = self.factura(folio=10)
        esperadas = {"ventas": ["Gráfico", "Documentos considerados"],
                     "cobros": ["Gráfico", "Documentos considerados", "Pagos considerados"],
                     "clientes": ["Gráfico", "Documentos considerados"],
                     "antiguedad": ["Gráfico", "Documentos por cobrar"],
                     "categorias": ["Gráfico", "Compras consideradas"],
                     "gastos": ["Gráfico", "Documentos considerados", "Compras consideradas"]}
        for clave, hojas in esperadas.items():
            r = self.client.get(reverse("inicio_descargar", args=[clave]), {"vista": "mes"})
            self.assertEqual(r.status_code, 200, clave)
            libro = load_workbook(io.BytesIO(r.content))
            self.assertEqual(libro.sheetnames, hojas, clave)
        libro = load_workbook(io.BytesIO(self.client.get(reverse("inicio_descargar", args=["ventas"])).content))
        hoja = libro["Gráfico"]
        self.assertEqual(len(hoja._charts), 1)
        valores = [f[1] for f in hoja.iter_rows(min_row=5, values_only=True)]
        self.assertEqual(valores[-1], 100000)                     # total del período = venta neta de la factura
        det = libro["Documentos considerados"]
        self.assertEqual([c for c in next(det.iter_rows(min_row=5, values_only=True))][2], 10)
        self.assertEqual(self.client.get(reverse("inicio_descargar", args=["otro"])).status_code, 404)
        self.assertContains(self.client.get(reverse("inicio")), "Imagen PNG")


class VolverAlListadoTests(Base):
    def test_guardar_categoria_vuelve_al_listado_con_sus_filtros(self):
        from .models import Compra, CuentaBancaria, MovimientoBancario, Proveedor, categorias_banco_de, categorias_de
        cuenta = CuentaBancaria.objects.create(sociedad=self.soc, banco="Banco de Chile", numero="123")
        m = MovimientoBancario.objects.create(cuenta=cuenta, fecha=date(2026, 11, 3), monto=-5000,
                                              descripcion="COMISION", huella="h1")
        cat = categorias_banco_de(self.soc).first()
        lista = reverse("banco") + "?cuenta=%d&mes=todos&estado=por_revisar&pagina=2" % cuenta.pk
        self.assertContains(self.client.get(lista.replace("&pagina=2", "")), "volver=")
        r = self.client.post(reverse("banco_movimiento", args=[m.pk]),
                             {"accion": "categoria", "categoria": cat.pk, "nota": "", "volver": lista})
        self.assertRedirects(r, lista + f"#mov-{m.pk}", fetch_redirect_response=False)
        r = self.client.post(reverse("banco_movimiento", args=[m.pk]),
                             {"accion": "categoria", "categoria": cat.pk, "volver": "https://otro.sitio/"})
        self.assertTrue(r["Location"].startswith(reverse("banco") + "?cuenta="))
        prov = Proveedor.objects.create(sociedad=self.soc, rut="76192083-9", razon_social="Semillas")
        c = Compra.objects.create(sociedad=self.soc, proveedor=prov, tipo=33, folio=1, periodo="2026-11",
                                  fecha_emision=date(2026, 11, 1), neto=1000, iva=190, total=1190)
        volver = reverse("compras") + "?categoria=sin&pagina=3"
        r = self.client.post(reverse("compra", args=[c.pk]), {"accion": "datos", "volver": volver,
                                                              "categoria": categorias_de(self.soc).first().pk})
        self.assertRedirects(r, volver + f"#compra-{c.pk}", fetch_redirect_response=False)


class ComprobanteIngresoTests(Base):
    def setUp(self):
        super().setUp()
        from .models import CuentaBancaria, MovimientoBancario
        self.cuenta = CuentaBancaria.objects.create(sociedad=self.soc, banco="Banco Santander", numero="123")
        self.abono = MovimientoBancario.objects.create(cuenta=self.cuenta, fecha=date(2026, 10, 5), monto=850000,
                                                       descripcion="0995932008 TRANSF DE JUAN PEREZ", huella="a1")

    def test_crear_desde_el_banco_concilia_y_cuenta_en_el_inicio(self):
        from .models import COMPROBANTE, MovimientoBancario
        r = self.client.post(reverse("banco_movimiento", args=[self.abono.pk]), {
            "accion": "comprobante", "cliente": "", "nombre": "Juan Pérez", "rut": "",
            "concepto": "Arriendo de terreno octubre 2026"})
        self.assertEqual(r.status_code, 302)
        doc = Documento.objects.get(tipo=COMPROBANTE)
        self.assertEqual((doc.folio, doc.estado, doc.total, doc.iva, doc.estado_sii), (1, "emitido", 850000, 0, ""))
        self.assertEqual(Documento.objects.con_saldo().get(pk=doc.pk).saldo_a, 0)
        self.assertEqual(MovimientoBancario.objects.get(pk=self.abono.pk).estado, "conciliado")
        from . import resumen
        datos = resumen.calcular(self.soc, "anio")
        self.assertEqual(datos["series"]["ventas"][-1], 850000)
        self.assertEqual(datos["series"]["cobrado"][-1], 850000)
        self.assertIn("Juan Pérez", [c["nombre"] for c in datos["series"]["clientes"]])
        # El mes siguiente propone el mismo cliente y concepto con el mes actualizado.
        nov = MovimientoBancario.objects.create(cuenta=self.cuenta, fecha=date(2026, 11, 5), monto=850000,
                                                descripcion="0112281223 TRANSF DE JUAN PEREZ", huella="a2")
        r = self.client.get(reverse("banco_movimiento", args=[nov.pk]))
        self.assertEqual(r.context["cliente_sugerido"], doc.cliente_id)
        self.assertEqual(r.context["concepto_sugerido"], "Arriendo de terreno noviembre 2026")
        self.client.post(reverse("banco_movimiento", args=[nov.pk]), {
            "accion": "comprobante", "cliente": doc.cliente_id, "concepto": r.context["concepto_sugerido"]})
        self.assertEqual(list(Documento.objects.filter(tipo=COMPROBANTE).values_list("folio", flat=True)), [2, 1])
        pdf = self.client.get(reverse("documento_pdf", args=[doc.pk]))
        self.assertEqual((pdf.status_code, pdf["Content-Type"]), (200, "application/pdf"))

    def test_manual_sin_rut_no_va_al_sii_y_entra_a_cobranza(self):
        from .models import COMPROBANTE, Cliente, Linea
        sin_rut = Cliente.objects.create(sociedad=self.soc, razon_social="Arrendatario", dias_pago=5)
        doc = Documento.objects.create(sociedad=self.soc, cliente=sin_rut, tipo=COMPROBANTE)
        Linea.objects.create(documento=doc, descripcion="Arriendo", cantidad=1, precio=100000, exento=True)
        doc.recalcular()
        self.assertEqual(doc.errores_para_emitir(), [])
        self.client.post(reverse("cotizacion_emitir", args=[doc.pk]))
        doc.refresh_from_db()
        self.assertEqual((doc.estado, doc.folio, doc.iva), ("emitido", 1, 0))
        self.assertIn(doc, Documento.objects.por_cobrar())
        self.client.post(reverse("documento_emitir", args=[doc.pk]), {"folio": 99})
        doc.refresh_from_db()
        self.assertEqual(doc.folio, 1)


class FlujoDAPTests(Base):
    def test_dap_es_inversion_y_no_gasto(self):
        from .banco import aplicar_reglas
        from .models import CuentaBancaria, MovimientoBancario, categorias_banco_de
        categorias_banco_de(self.soc)
        cuenta = CuentaBancaria.objects.create(sociedad=self.soc, banco="Banco de Chile", numero="9")
        movs = [MovimientoBancario.objects.create(cuenta=cuenta, fecha=hoy().replace(day=1), monto=m, descripcion=d,
                                                  huella=str(i))
                for i, (m, d) in enumerate([(-5000000, "INVERSION DAP 30 DIAS"), (-40000, "PAGO ADAPTADOR ELECTRICO"),
                                            (300000, "TRANSF DE CLIENTE")])]
        aplicar_reglas(self.soc, movs)
        dap, adaptador, _ = [MovimientoBancario.objects.get(pk=m.pk) for m in movs]
        self.assertEqual((dap.categoria.nombre, dap.categoria.flujo), ("Depósitos a plazo (DAP)", "inversion"))
        self.assertIsNone(adaptador.categoria)                   # «DAP» no calza dentro de «ADAPTADOR»
        r = self.client.get(reverse("banco_resumen"))
        self.assertEqual(r.context["secciones"][1]["total"], 40000)        # el DAP no es egreso
        self.assertEqual(r.context["neto_total"], 300000 - 40000)
        self.assertEqual(r.context["no_operacionales"][0]["total"], -5000000)
        self.assertEqual(r.context["variacion_total"], 300000 - 40000 - 5000000)
        r = self.client.get(reverse("banco"), {"cuenta": cuenta.pk, "mes": "todos"})
        self.assertNotIn("Depósitos a plazo (DAP)", [n for n, _ in r.context["por_categoria"]["egresos"]])


class RayoTests(Base):
    def setUp(self):
        super().setUp()
        from .models import Compra, CuentaBancaria, MovimientoBancario, Proveedor
        self.cuenta = CuentaBancaria.objects.create(sociedad=self.soc, banco="Banco de Chile", numero="1")
        self.f1 = self.factura(folio=10, emision=date(2026, 11, 2))                       # 119.000 a Cliente Uno
        self.f2 = self.factura(lineas=((1, 50000, False),), folio=11, emision=date(2026, 11, 2))   # 59.500
        self.f3 = self.factura(lineas=((1, 50000, False),), folio=12, emision=date(2026, 11, 3))   # 59.500
        prov = Proveedor.objects.create(sociedad=self.soc, rut="76192083-9", razon_social="Semillas del Sur SpA")
        self.compra = Compra.objects.create(sociedad=self.soc, proveedor=prov, tipo=33, folio=5, periodo="2026-11",
                                            fecha_emision=date(2026, 11, 1), neto=20000, iva=3800, total=23800)
        mov = lambda d, m, t: MovimientoBancario.objects.create(  # noqa: E731
            cuenta=self.cuenta, fecha=date(2026, 11, d), monto=m, descripcion=t, huella=t)
        self.m_nombre = mov(5, 119000, "TRASPASO DE: CLIENTE UNO")
        self.m_ambiguo = mov(6, 59500, "TRASPASO DE: OTRA PERSONA")   # dos facturas de 59.500: no se adivina
        self.m_compra = mov(7, -23800, "TRASPASO A: SEMILLAS DEL SUR")
        self.m_nada = mov(8, -777, "COMISION")

    def test_sugiere_por_descripcion_y_monto_sin_adivinar_y_concilia_todas(self):
        from .banco import sugerencias_automaticas
        pares = {(s["mov"].pk, s["doc"].pk, s["motivo"]) for s in sugerencias_automaticas(self.soc)}
        # m_ambiguo («OTRA PERSONA») tiene dos facturas posibles de 59.500: no se adivina
        self.assertEqual(pares, {(self.m_nombre.pk, self.f1.pk, "descripcion"),
                                 (self.m_compra.pk, self.compra.pk, "descripcion")})
        r = self.client.get(reverse("banco_sugerencias"))
        self.assertContains(r, "Conciliar todas (2)")
        self.assertContains(self.client.get(reverse("inicio")), "2 sugerencias de conciliación")
        self.client.post(reverse("banco_sugerencias"), {"accion": "todas", "tipo": "c"})   # solo las compras
        self.assertEqual([x["tipo"] for x in sugerencias_automaticas(self.soc)], ["v"])
        self.client.post(reverse("banco_sugerencias"), {"accion": "todas"})
        self.assertEqual(Documento.objects.con_saldo().get(pk=self.f1.pk).saldo_a, 0)
        from .models import Compra
        self.assertEqual(Compra.objects.con_saldo().get(pk=self.compra.pk).saldo, 0)
        self.assertEqual(sugerencias_automaticas(self.soc), [])

    def test_rechazar_y_sugerencia_unica_por_monto(self):
        from .banco import sugerencias_automaticas
        self.f3.a_papelera()                                        # queda una sola factura de 59.500
        s = {x["mov"].pk: x for x in sugerencias_automaticas(self.soc)}
        self.assertEqual((s[self.m_ambiguo.pk]["doc"], s[self.m_ambiguo.pk]["motivo"]), (self.f2, "monto"))
        self.client.post(reverse("banco_sugerencias"), {"accion": "rechazar", "mov": self.m_ambiguo.pk})
        self.assertNotIn(self.m_ambiguo.pk, {x["mov"].pk for x in sugerencias_automaticas(self.soc)})

    def test_conciliar_desde_la_factura_y_desde_la_compra(self):
        r = self.client.get(reverse("documento", args=[self.f1.pk]))
        self.assertEqual(r.context["movs_banco"][0]["mov"], self.m_nombre)
        self.client.post(reverse("conciliar_documento", args=["v", self.f1.pk]),
                         {"movimiento": self.m_nombre.pk, "monto": "119.000"})
        self.assertEqual(Documento.objects.con_saldo().get(pk=self.f1.pk).saldo_a, 0)
        self.assertNotIn(self.f1, self.client.get(reverse("cobranza")).context["pendientes"])
        r = self.client.get(reverse("cobranza"), {"vista": "pagadas"})
        self.assertEqual([(d, d.por_banco) for d in r.context["pagadas"]], [(self.f1, True)])
        self.assertContains(r, "Conciliada con el banco")
        r = self.client.get(reverse("compra", args=[self.compra.pk]))
        self.assertEqual(r.context["movs_banco"][0]["mov"], self.m_compra)
        self.client.post(reverse("conciliar_documento", args=["c", self.compra.pk]), {"movimiento": self.m_compra.pk})
        self.m_compra.refresh_from_db()
        self.assertEqual(self.m_compra.conciliaciones.count(), 1)
        r = self.client.post(reverse("conciliar_documento", args=["v", self.f2.pk]), {"movimiento": self.m_nada.pk},
                             follow=True)
        self.assertContains(r, "Elige el movimiento")              # un cargo no paga una venta


class FacturasAntiguasTests(Base):
    """Un cliente reconocido en la cartola paga sus facturas de la más antigua a la más nueva."""

    def setUp(self):
        super().setUp()
        from .models import Cliente, CuentaBancaria, MovimientoBancario
        self.cuenta = CuentaBancaria.objects.create(sociedad=self.soc, banco="Banco de Chile", numero="1")
        self.gran = Cliente.objects.create(sociedad=self.soc, rut="76086428-5", razon_social="Comercial Gran Colorado Spa")
        self.cli = self.gran
        self.a = self.factura(lineas=((1, 10000, False),), folio=20, emision=date(2026, 10, 1))   # 11.900
        self.b = self.factura(lineas=((1, 20000, False),), folio=21, emision=date(2026, 10, 15))  # 23.800
        self.c = self.factura(lineas=((1, 30000, False),), folio=22, emision=date(2026, 11, 1))   # 35.700
        self.mov = lambda d, m, t: MovimientoBancario.objects.create(  # noqa: E731
            cuenta=self.cuenta, fecha=date(2026, 11, d), monto=m, descripcion=t, huella=f"{d}{m}{t}")

    def test_paga_de_la_mas_antigua_a_la_mas_nueva(self):
        from .banco import sugerencias_automaticas
        m1 = self.mov(5, 20000, "Traspaso De: Comercial Gran Colorado Spa")
        m2 = self.mov(6, 30000, "Traspaso De: Comercial Gran Colorado Spa")
        s = {x["mov"].pk: x for x in sugerencias_automaticas(self.soc)}
        self.assertEqual([(d.folio, p) for d, p in s[m1.pk]["asignaciones"]], [(20, 11900), (21, 8100)])
        self.assertEqual(s[m1.pk]["motivo"], "antiguas")
        # el segundo pago sigue donde quedó el primero
        self.assertEqual([(d.folio, p) for d, p in s[m2.pk]["asignaciones"]], [(21, 15700), (22, 14300)])
        self.client.post(reverse("banco_sugerencias"), {"accion": "todas"})
        saldos = {d.folio: d.saldo_a for d in Documento.objects.con_saldo().filter(folio__in=(20, 21, 22))}
        self.assertEqual(saldos, {20: 0, 21: 0, 22: 35700 - 14300})

    def test_monto_exacto_del_cliente_gana_a_la_mas_antigua(self):
        from .banco import sugerencias_automaticas
        m = self.mov(5, 35700, "TRASPASO DE: COMERCIAL GRAN COLORADO")
        x = sugerencias_automaticas(self.soc)[0]
        self.assertEqual((x["mov"], [d.folio for d, _ in x["asignaciones"]], x["motivo"]), (m, [22], "descripcion"))

    def test_aprende_como_viene_el_nombre_al_conciliar_a_mano(self):
        from .banco import sugerencias_automaticas
        m = self.mov(5, 11900, "Traspaso De: GCC Distribuidora Ltda")      # nombre distinto al de la factura
        self.mov(20, 23800, "Traspaso De: GCC Distribuidora Ltda")
        r = self.client.post(reverse("banco_movimiento", args=[m.pk]),
                             {"accion": "conciliar", "doc": [f"v-{self.a.pk}"], f"monto-v-{self.a.pk}": "11900"}, follow=True)
        self.assertContains(r, "Desde ahora reconoceré")
        self.gran.refresh_from_db()
        self.assertEqual(self.gran.alias_banco, "GCC Distribuidora Ltda")
        x = sugerencias_automaticas(self.soc)[0]
        self.assertEqual(([d.folio for d, _ in x["asignaciones"]], x["como"]), ([21], "alias"))

    def test_reconoce_por_rut_en_la_descripcion(self):
        from .banco import ruts_en, sugerencias_automaticas
        self.assertEqual(ruts_en("Pago: Proveedores 0760864285 Oficina Central"), ["76086428-5"])
        m = self.mov(5, 11900, "Pago: Proveedores 0760864285")
        x = sugerencias_automaticas(self.soc)[0]
        self.assertEqual((x["mov"], [d.folio for d, _ in x["asignaciones"]], x["como"]), (m, [20], "rut"))

    def test_aprende_de_pagos_anteriores_que_se_repiten(self):
        from .banco import conciliar, sugerencias_automaticas
        from .models import MovimientoBancario
        # Dos pagos de «Puerto Colono» (nombre distinto al del cliente) ya se conciliaron con sus facturas.
        for i, d in enumerate((self.a, self.b)):
            m = self.mov(3 + i, d.total, "Traspaso De: Puerto Colono Spa")
            conciliar(MovimientoBancario.objects.get(pk=m.pk), [(Documento.objects.con_saldo().get(pk=d.pk), d.total)])
        nuevo = self.mov(9, 35700, "Traspaso De: Puerto Colono Spa")
        x = sugerencias_automaticas(self.soc)[0]
        self.assertEqual((x["mov"], [d.folio for d, _ in x["asignaciones"]], x["como"]), (nuevo, [22], "historial"))


class CartolaSantanderTests(Base):
    def test_excel_con_cargo_y_abono_del_mas_reciente_al_mas_antiguo(self):
        import datetime as dt
        import io
        from openpyxl import Workbook
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import CuentaBancaria, MovimientoBancario
        wb = Workbook()
        h = wb.active
        h.append(["Banco Santander - Office Banking"])
        h.append(["Empresa: VERDE VERTICAL SPA   RUT: 77.552.628-9"])
        h.append(["Cuenta Corriente N° 0-000-71234567-8"])
        h.append(["Fecha", "Descripción Movimiento", "N° Documento", "Monto Cargo ($)", "Monto Abono ($)", "Saldo ($)"])
        h.append([dt.datetime(2026, 11, 5), "PAGO PROVEEDOR", "123", 59500, None, 440500])
        h.append([dt.datetime(2026, 11, 3), "TRANSF DE CLIENTE UNO SPA", "", None, 119000, 500000])
        h.append([dt.datetime(2026, 11, 1), "DEPOSITO", "", None, 381000, 381000])
        archivo = io.BytesIO()
        wb.save(archivo)
        r = self.client.post(reverse("banco_importar"), {
            "archivos": [SimpleUploadedFile("cartola.xlsx", archivo.getvalue())]})
        self.assertContains(r, "3 movimientos nuevos")
        self.assertContains(r, "Los saldos de la cartola cuadran")
        cuenta = CuentaBancaria.objects.get()
        self.assertEqual((cuenta.banco, cuenta.numero), ("Banco Santander", "712345678"))
        self.assertEqual(sorted(MovimientoBancario.objects.values_list("monto", flat=True)), [-59500, 119000, 381000])

    def test_monto_con_columna_cargo_abono(self):
        from .banco import leer
        datos = leer("Fecha;Descripción;Monto;Cargo/Abono;Saldo\n01/11/2026;ABONO;1.000;A;11.000\n"
                     "02/11/2026;CARGO;500;C;10.500\n".encode("cp1252"))
        self.assertEqual([m["monto"] for m in datos["movimientos"]], [1000, -500])

    def test_crear_cuenta_a_mano(self):
        from .models import CuentaBancaria
        self.client.post(reverse("banco_cuentas"), {"accion": "cuenta_nueva", "banco": "Banco Santander",
                                                    "numero": "0-000-7123456-7", "alias": "Santander CC"})
        c = CuentaBancaria.objects.get()
        self.assertEqual((c.banco, c.numero, str(c)), ("Banco Santander", "71234567", "Santander CC"))


def pdf_cartola_santander(movimientos, inicial, final, comisiones=()):
    """PDF con la misma estructura que la cartola histórica de Santander (datos inventados)."""
    import io
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    salida = io.BytesIO()
    c = canvas.Canvas(salida, pagesize=letter)
    alto = letter[1]
    t = lambda x, y, s: c.drawString(x, alto - y, s)  # noqa: E731
    d = lambda x1, y, s: c.drawRightString(x1, alto - y, s)  # noqa: E731
    c.setFont("Helvetica", 8)
    t(30, 40, "Cartolas históricas de Cta.Cte y Líneas de Crédito.")
    t(30, 60, "Empresa: VERDE VERTICAL SPA")
    t(330, 60, "RUT empresa: 77.552.628-9")
    t(30, 90, "Cuenta: 0-000-1234567-8 Moneda: PESOS DE CHILE")
    t(30, 120, f"Saldo inicial: $ {inicial:,}".replace(",", "."))
    t(330, 120, f"Saldo final: $ {final:,}".replace(",", "."))

    def tabla(y, titulo, filas):
        t(30, y, titulo)
        y += 20
        for x, s in ((32, "FECHA"), (134, "CARGO"), (201, "ABONO"), (243, "DESCRIPCIÓN"), (401, "SALDO"),
                     (441, "N°"), (452, "DOC"), (492, "SUCURSAL")):
            t(x, y, s)
        for fecha, monto, desc in filas:
            y += 22
            t(32, y, fecha)
            d(164 if monto < 0 else 231, y, f"$ {abs(monto):,}".replace(",", "."))
            partes = desc.split("|")
            if len(partes) == 2:
                t(243, y - 4, partes[0])
                t(243, y + 4, partes[1])
            else:
                t(243, y, desc)
            d(429, y, "$ 1.000")
            t(459, y, "0")
            t(492, y, "Internet")
        return y + 30
    y = tabla(160, "Detalle movimientos", movimientos)
    y = tabla(y, "Resumen comisiones", comisiones)
    t(30, y, "Saldos diarios")
    c.save()
    return salida.getvalue()


def pdf_ultimos_santander(filas, cuenta="0-000-1234567-8"):
    """PDF con la estructura de la «Consulta de movimientos» de Santander (datos inventados).
    filas: (fecha, monto, saldo, descripción con | para segunda línea, n° movimiento), del más reciente al más antiguo."""
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    salida = io.BytesIO()
    c = canvas.Canvas(salida, pagesize=A4)
    alto = A4[1]
    t = lambda x, y, s: c.drawString(x, alto - y, s)  # noqa: E731
    d = lambda x1, y, s: c.drawRightString(x1, alto - y, s)  # noqa: E731
    c.setFont("Helvetica", 8)
    t(30, 40, "Consulta de movimientos de Cuentas Corrientes")
    t(30, 70, "Empresa: VERDE VERTICAL SPA")
    t(330, 70, "RUT empresa: 77.552.628-9")
    t(30, 140, f"Cuenta Corriente N°: {cuenta} Moneda: PESOS DE CHILE Sucursal: 0495 OFICINA")
    t(32, 206, "Detalle movimientos")
    for x, texto in ((32, "MONTO"), (106, "DESCRIPCIÓN MOVIMIENTO"), (229, "FECHA"), (273, "SALDO"),
                     (360, "N° DOCUMENTO"), (428, "SUCURSAL"), (500, "N° MOVIMIENTO")):
        t(x, 229, texto)
    y = 225
    for fecha, monto, saldo, desc, nmov in filas:
        y += 22
        t(56, y, "$")
        d(94, y, f"{monto:,}".replace(",", "."))
        partes = desc.split("|")
        t(106, y - 4 if len(partes) > 1 else y, partes[0])
        if len(partes) > 1:
            t(106, y + 4, partes[1])
        t(229, y, fecha)
        t(305, y, "$")
        d(348, y, f"{saldo:,}".replace(",", "."))
        t(360, y, "000000000")
        t(428, y, "INTERNET")
        t(500, y, f"{nmov:09d}")
    t(30, 786, "Nota: Información provisoria sujeta a confirmación por parte del Banco")
    c.save()
    return salida.getvalue()


class UltimosMovimientosSantanderTests(Base):
    def subir(self, contenido, nombre="CartolaMovimiento.pdf"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(reverse("banco_importar"), {
            "archivos": [SimpleUploadedFile(nombre, contenido, content_type="application/pdf")]})

    FILAS = [("06/11/2026", -50000, 950000, "1111111111 Transf a|Proveedor Uno", 45),
             ("05/11/2026", 200000, 1000000, "2222222222 PAGO|CLIENTE DOS", 44)]

    def test_lee_monto_con_signo_saldo_y_descripcion_en_dos_lineas(self):
        from .banco import leer
        d = leer(pdf_ultimos_santander(self.FILAS), "x.pdf")
        self.assertEqual((d["banco"], d["cuenta"], d["rut"], d["provisorio"]),
                         ("Banco Santander", "12345678", "77552628-9", True))
        self.assertEqual([(m["fecha"].day, m["monto"], m["saldo"]) for m in d["movimientos"]],
                         [(5, 200000, 1000000), (6, -50000, 950000)])
        self.assertEqual(d["movimientos"][1]["descripcion"], "1111111111 Transf a Proveedor Uno")
        self.assertEqual((d["movimientos"][1]["documento"], d["movimientos"][1]["sucursal"]), ("", "INTERNET"))

    def test_importa_provisorio_y_la_cartola_definitiva_lo_confirma_sin_duplicar(self):
        from .models import CategoriaBanco, MovimientoBancario, categorias_banco_de
        r = self.subir(pdf_ultimos_santander(self.FILAS))
        self.assertContains(r, "2 movimientos nuevos")
        self.assertContains(r, "saldos de la cartola cuadran")
        self.assertContains(self.subir(pdf_ultimos_santander(self.FILAS)), "2 ya estaban")
        categorias_banco_de(self.soc)
        cat = CategoriaBanco.objects.filter(sociedad=self.soc).first()
        m = MovimientoBancario.objects.get(monto=-50000)
        m.categoria = cat
        m.save()
        self.assertTrue(m.provisorio)
        definitiva = pdf_cartola_santander([("05/11/2026", 200000, "ABONO CLIENTE DOS"),
                                            ("06/11/2026", -50000, "TRANSFERENCIA A PROVEEDOR UNO"),
                                            ("07/11/2026", -10000, "COMISION")], 800000, 940000)
        r = self.client.post(reverse("banco_importar"), {"archivos": [__import__(
            "django.core.files.uploadedfile", fromlist=["x"]).SimpleUploadedFile("hist.pdf", definitiva)]})
        self.assertContains(r, "1 movimiento nuevo")
        self.assertContains(r, "2 provisorios confirmados")
        self.assertEqual(MovimientoBancario.objects.count(), 3)
        m.refresh_from_db()
        self.assertEqual((m.provisorio, m.categoria, m.descripcion), (False, cat, "TRANSFERENCIA A PROVEEDOR UNO"))
        self.assertContains(self.subir(pdf_ultimos_santander(self.FILAS)), "2 ya estaban")
        self.assertEqual(MovimientoBancario.objects.count(), 3)


def pdf_banco_chile(filas, cuenta="00-001-23456-7890", por_pagina=3):
    """PDF con la estructura de «Movimientos» del Banco de Chile (datos inventados).
    filas: (fecha, descripción con | para 2ª línea, canal, cargo, abono, saldo), del más reciente al más antiguo.
    Simula el corte de página: los montos de la primera fila de cada página quedan al pie de la anterior."""
    import io
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    salida = io.BytesIO()
    c = canvas.Canvas(salida, pagesize=letter)
    alto = letter[1]
    t = lambda x, y, s: c.drawString(x, alto - y, s)  # noqa: E731
    d = lambda x1, y, s: c.drawRightString(x1, alto - y, s)  # noqa: E731
    pie = lambda y: (t(154, y, "Infórmese sobre la garantía estatal de los depósitos en su banco o en www.sbif.cl."),  # noqa: E731
                     t(154, y + 10, "© 2015 Banco de Chile. Todos los Derechos Reservados"))
    c.setFont("Helvetica", 8)
    t(50, 60, "Sr(a).: Persona Inventada")
    t(50, 75, "Nombre Empresa: VERDE VERTICAL SpA")
    t(50, 90, "Rut: 77.552.628-9")
    t(50, 105, f"Cuenta N°: {cuenta}")
    cargos = sum(f[3] for f in filas)
    abonos = sum(f[4] for f in filas)
    t(86, 268, "Total Cargos")
    t(213, 268, "Total Abonos")
    t(94, 281, f"{cargos:,}".replace(",", "."))
    t(222, 281, f"{abonos:,}".replace(",", "."))
    t(50, 337, "Movimientos al 17/11/2026")
    for x, s in ((237, "Canal o"), (56, "Fecha"), (102, "Descripción"), (301, "Nro. Docto."),
                 (355, "Cargos (CLP)"), (423, "Abonos (CLP)"), (491, "Saldo (CLP)")):
        t(x, 359 if x != 237 else 354, s)
    t(237, 363, "Sucursal")
    y = 380
    pendientes = None
    for i, (fecha, desc, canal, cargo, abono, saldo) in enumerate(filas):
        corte = i and i % por_pagina == 0
        montos = lambda yy: (d(414, yy, f"{cargo:,}".replace(",", ".")) if cargo else d(482, yy, f"{abono:,}".replace(",", ".")),  # noqa: E731
                             d(559, yy, f"{saldo:,}".replace(",", ".")))
        if corte:
            montos(y)                       # al pie de la página, sin fecha ni descripción
            pie(737)
            c.showPage(); c.setFont("Helvetica", 8)
            y = 85
        partes = desc.split("|")
        t(53, y, fecha); t(99, y, partes[0]); t(234, y, canal)
        if not corte:
            montos(y)
        if len(partes) > 1:
            y += 9; t(99, y, partes[1])
        y += 19
    pie(737)
    c.save()
    return salida.getvalue()


class CartolaPDFBancoChileTests(Base):
    FILAS = [("05/11/2026", "Comision Mantencion", "Oficina Central", 5000, 0, 474500),
             ("05/11/2026", "Pago: Tesoreria Gral|Republica", "Internet", 80000, 0, 479500),
             ("03/11/2026", "App-traspaso A: Semillas|del Sur", "Internet", 59500, 0, 559500),
             ("01/11/2026", "Traspaso De: Cliente Uno Spa", "Internet", 0, 119000, 619000)]

    def subir(self, contenido, nombre):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(reverse("banco_importar"), {"archivos": [SimpleUploadedFile(nombre, contenido)]})

    def test_lee_corte_de_pagina_descripciones_largas_y_totales(self):
        from .banco import leer
        d = leer(pdf_banco_chile(self.FILAS), "cartola.pdf")
        self.assertEqual((d["banco"], d["cuenta"], d["rut"], d["totales_ok"]),
                         ("Banco de Chile", "1234567890", "77552628-9", True))
        self.assertEqual([(m["fecha"].day, m["monto"], m["saldo"]) for m in d["movimientos"]],
                         [(1, 119000, 619000), (3, -59500, 559500), (5, -80000, 479500), (5, -5000, 474500)])
        self.assertEqual(d["movimientos"][1]["descripcion"], "App-traspaso A: Semillas del Sur")
        self.assertEqual(d["movimientos"][3]["sucursal"], "Oficina Central")

    def test_no_duplica_si_el_mismo_mes_se_sube_en_txt_y_en_pdf(self):
        from .models import MovimientoBancario
        self.assertContains(self.subir(CARTOLA, "CartolaEmitida.txt"), "4 movimientos nuevos")
        r = self.subir(pdf_banco_chile(self.FILAS), "cartola_noviembre.pdf")
        self.assertContains(r, "4 ya estaban")
        self.assertContains(r, "saldos de la cartola cuadran")
        self.assertEqual(MovimientoBancario.objects.count(), 4)
        mas = [("06/11/2026", "Traspaso De: Cliente Dos", "Internet", 0, 20000, 494500)] + self.FILAS
        self.assertContains(self.subir(pdf_banco_chile(mas), "cartola.pdf"), "1 movimiento nuevo")
        self.assertEqual(MovimientoBancario.objects.count(), 5)


class CartolaPDFTests(Base):
    def subir(self, contenido):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(reverse("banco_importar"), {
            "archivos": [SimpleUploadedFile("cartola.pdf", contenido, content_type="application/pdf")]})

    def test_pdf_santander_descripcion_en_dos_lineas_y_saldos(self):
        from .models import CuentaBancaria, MovimientoBancario
        pdf = pdf_cartola_santander([("04/11/2026", 119000, "TRANSF DE|CLIENTE UNO SPA"),
                                     ("10/11/2026", -50000, "PAGO TESORERIA")], 100000, 169000)
        r = self.subir(pdf)
        self.assertContains(r, "2 movimientos nuevos")
        self.assertContains(r, "Los saldos de la cartola cuadran")
        cuenta = CuentaBancaria.objects.get()
        self.assertEqual((cuenta.banco, cuenta.numero), ("Banco Santander", "12345678"))
        movs = list(MovimientoBancario.objects.order_by("fecha").values_list("descripcion", "monto", "saldo"))
        self.assertEqual(movs, [("TRANSF DE CLIENTE UNO SPA", 119000, 219000), ("PAGO TESORERIA", -50000, 169000)])
        self.assertContains(self.subir(pdf), "2 ya estaban")

    def test_pdf_incluye_comisiones_solo_si_cuadra(self):
        from .banco import leer
        pdf = pdf_cartola_santander([("04/11/2026", 10000, "ABONO")], 0, 7000,
                                    comisiones=[("30/11/2026", -3000, "COMISION MANTENCION")])
        movs = leer(pdf)["movimientos"]
        self.assertEqual([m["monto"] for m in movs], [10000, -3000])
        pdf = pdf_cartola_santander([("04/11/2026", 10000, "ABONO"), ("30/11/2026", -3000, "COMISION MANTENCION")],
                                    0, 7000, comisiones=[("30/11/2026", -3000, "COMISION MANTENCION")])
        self.assertEqual([m["monto"] for m in leer(pdf)["movimientos"]], [10000, -3000])

    def test_pdf_que_no_es_cartola(self):
        import io
        from reportlab.pdfgen import canvas
        salida = io.BytesIO()
        c = canvas.Canvas(salida)
        c.drawString(50, 700, "Factura de venta")
        c.save()
        self.assertContains(self.subir(salida.getvalue()), "no tiene el formato de cartola")
