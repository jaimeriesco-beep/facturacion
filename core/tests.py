from datetime import date, timedelta
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from .models import (FACTURA, FACTURA_EXENTA, NOTA_CREDITO, Cliente, Documento, Linea, Pago,
                     Producto, Recurrente, RecurrenteLinea, Sociedad, hoy)
from .utils import formatear_clp, formatear_rut, normalizar_rut


class Base(TestCase):
    def setUp(self):
        self.soc = Sociedad.objects.create(rut="77552628-9", razon_social="Verde Vertical")
        self.cli = Cliente.objects.create(
            sociedad=self.soc, rut="76192083-9", razon_social="Cliente Uno", dias_pago=30)
        get_user_model().objects.create_user("jaime", password="clave-segura-123")
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
        datos = {"rut": "76.192.083-9", "razon_social": "Repetido", "dias_pago": 30, "activo": "on"}
        r = self.client.post(reverse("cliente_nuevo"), datos)
        self.assertContains(r, "Ya existe un cliente con ese RUT")
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
        # Al completar el RUT se valida y no se puede repetir.
        cliente = self.soc.clientes.get(razon_social="Tropera")
        r = self.client.post(reverse("cliente_editar", args=[cliente.pk]),
                             {"rut": "76.192.083-9", "razon_social": "Tropera", "dias_pago": 30, "activo": "on"})
        self.assertContains(r, "Ya existe un cliente con ese RUT")

    def test_administrador_crea_usuario(self):
        self.assertEqual(self.client.get(reverse("usuarios")).status_code, 302)  # no es admin
        get_user_model().objects.filter(username="jaime").update(is_superuser=True)
        r = self.client.post(reverse("usuario_nuevo"), {
            "username": "alberto", "first_name": "Alberto", "last_name": "Riesco",
            "email": "alberto@example.com", "clave": "una-clave-larga-2026",
            "is_superuser": "on", "is_active": "on"})
        self.assertRedirects(r, reverse("usuarios"))
        nuevo = get_user_model().objects.get(username="alberto")
        self.assertTrue(nuevo.is_superuser and nuevo.check_password("una-clave-larga-2026"))
        self.assertEqual(self.client.get(reverse("usuarios")).status_code, 200)
        self.assertEqual(self.client.get(reverse("password_change")).status_code, 200)
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
