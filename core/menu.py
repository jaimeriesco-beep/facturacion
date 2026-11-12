"""Menú lateral: grupos que se despliegan hacia la derecha con sus subcategorías."""
from django.urls import reverse

# (clave, título, ícono, [(nombre_url, título, descripción, [otras urls que lo activan])])
GRUPOS = [
    ("ventas", "Ventas", "etiqueta", [
        ("documentos", "Documentos de venta", "Facturas, notas de crédito y guías",
         ["documento", "documento_nuevo", "documento_editar", "nota_nueva", "guia_facturar"]),
        ("cotizaciones", "Cotizaciones", "Presupuestos para clientes", ["cotizacion_emitir"]),
        ("cobranza", "Cobranza", "Por cobrar, recordatorios y pagos", []),
        ("recurrentes", "Facturación recurrente", "Arriendos y servicios mensuales",
         ["recurrente_nueva", "recurrente_editar"]),
        ("clientes", "Clientes", "Datos y contactos", ["cliente_nuevo", "cliente_editar", "clientes_importar"]),
        ("papelera", "Papelera", "Documentos eliminados (30 días)", []),
    ]),
    ("compras", "Compras", "compras", [
        ("compras", "Compras y gastos", "Facturas de proveedores del SII", ["compra", "compra_nueva"]),
        ("compras_importar", "Importar desde el SII", "Registro de Compras (RCV)", []),
        ("proveedores", "Proveedores", "Datos, categoría y saldos", ["proveedor"]),
        ("compras_categorias", "Categorías de gasto", "En qué se gasta", []),
    ]),
    ("productos", "Productos e inventario", "productos", [
        ("productos", "Productos (SKU)", "Precios, unidades y stock mínimo", ["producto_nuevo", "producto_editar"]),
        ("inventario_stock", "Stock", "Existencias, entradas y salidas",
         ["inventario_kardex", "inventario_entrada", "inventario_salida", "inventario_toma", "inventario_traslado"]),
        ("inventario_movimientos", "Movimientos de inventario", "Kardex de todos los productos", []),
        ("inventario_config", "Configuración de inventario", "Activar, bodegas y reglas", []),
    ]),
    ("banco", "Banco", "edificio-banco", [
        ("banco", "Movimientos y conciliación", "Cartolas, cobros y pagos", ["banco_movimiento"]),
        ("banco_importar", "Subir cartola", "Banco de Chile y otros", []),
        ("banco_configuracion", "Categorías y reglas", "Sueldos, impuestos, contribuciones…", []),
    ]),
    ("marketing", "Marketing", "marketing", [
        ("marketing", "Agradecimientos", "Correo automático a clientes", []),
    ]),
    ("reportes", "Reportes", "reportes", [
        ("reportes", "Reportes", "Ventas y cobranza en Excel", []),
    ]),
    ("ajustes", "Ajustes", "engranaje", [
        ("conexion_sii", "Conexión SII", "Certificado, folios y envíos", ["enviar_pendientes", "envio_estado"]),
        ("sociedades", "Sociedades", "Datos de cada empresa", ["sociedad_nueva", "sociedad_editar"]),
        ("usuarios", "Usuarios", "Cuentas, roles y respaldos", ["usuario_nuevo", "usuario_editar"]),
        ("perfil", "Mi perfil", "Mis datos y clave", []),
    ]),
]


def construir(request):
    actual = getattr(request.resolver_match, "url_name", "") or ""
    sociedad = getattr(request, "sociedad", None)
    inventario = bool(sociedad and sociedad.inventario_activo)
    grupos = []
    for clave, titulo, icono, items in GRUPOS:
        visibles = []
        for nombre, texto, descripcion, otras in items:
            if nombre == "usuarios" and not request.user.is_superuser:
                continue
            if nombre in ("inventario_stock", "inventario_movimientos") and not inventario:
                continue
            visibles.append({"url": reverse(nombre), "titulo": texto, "descripcion": descripcion,
                             "activo": actual == nombre or actual in otras})
        if not visibles:
            continue
        grupos.append({"clave": clave, "titulo": titulo, "icono": icono, "items": visibles,
                       "activo": any(i["activo"] for i in visibles),
                       "directo": visibles[0]["url"] if len(visibles) == 1 else None})
    return grupos
