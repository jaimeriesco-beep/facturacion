# App de facturación

Gestión de facturación para varias sociedades: clientes, productos y servicios,
facturas afectas y exentas, notas de crédito y débito, pagos, cobranza,
facturación mensual recurrente y reportes en Excel.

## Cómo iniciarla

1. Abre Terminal y ejecuta (o haz doble clic en `iniciar.command`):

       bash "RUTA_A_ESTA_CARPETA/iniciar.command"

2. La primera vez instala lo necesario y te pide crear tu usuario y clave.
3. Se abre en el navegador: http://127.0.0.1:8000

Requiere Python 3.10 o superior (https://www.python.org/downloads/).

## Dónde quedan los datos

En `~/FacturacionDatos` (fuera de Google Drive): la base de datos
`facturacion.sqlite3`, la clave interna y el entorno de Python. Esa carpeta es la
que hay que respaldar. No la muevas a Drive: la sincronización puede corromper la
base de datos, y más adelante guardará certificados digitales y folios.

## Estado del proyecto

- **Etapa 1 (lista):** gestión, cobranza, recurrentes y reportes.
- **Etapa 2 (construida, falta probarla contra el SII):** la app timbra y firma
  facturas afectas y exentas, notas de crédito y débito y guías de despacho;
  genera el PDF con timbre y el XML; envía al SII y consulta el estado.
  Todo se firma en este computador. Se configura en **Conexión SII**.
- **Etapa 3:** certificación de la sociedad piloto ante el SII y paso a producción.
- **Etapa 4:** réplica en las demás sociedades.

Mientras una sociedad esté en ambiente de certificación, lo que la app timbre
son documentos de prueba: no tienen validez tributaria ni entran a cobranza.
Las facturas reales se siguen emitiendo en el portal del SII y se registran con
"Ya lo emití en el portal del SII".

## Seguridad

El certificado digital, su clave y los folios (CAF) se guardan cifrados en la
base de datos. La clave de cifrado queda en el Llavero de macOS. No se envían a
ningún servicio externo: la app solo se conecta con el SII.

## Uso diario

- **Documentos → Nueva factura:** se guarda como borrador. Al emitirla en el
  portal del SII, anota el folio en "Registrar emisión".
- **Notas de crédito y débito:** desde la factura emitida.
- **Guías de despacho:** Documentos → Nueva guía de despacho. Una guía emitida
  se puede convertir en factura con "Facturar esta guía".
- **Logo:** se carga en Sociedades; aparece en la app y en los PDF.
- **Pagos:** desde la factura; el saldo descuenta pagos y notas de crédito.
- **Recurrentes:** plantillas mensuales; el botón "Generar" crea los borradores
  del mes. En la descripción se puede usar `{mes}` y `{año}`.
- **Reportes:** Excel de documentos emitidos por período y de cobranza.

## Publicarla en internet (Render + Neon, sin costo)

`render.yaml` crea el servicio web en el plan gratuito de Render. La base de
datos va en Neon (plan gratuito), porque la base gratuita de Render caduca a
los 30 días.

1. Crear en Neon un proyecto y copiar su cadena de conexión (`postgresql://...`).
2. Subir este código a un repositorio **privado** de GitHub.
3. En Render: New → Blueprint → elegir el repositorio. Render pide la cadena de
   conexión (`DATABASE_URL`) y el usuario y la clave del primer administrador
   (clave de 12 caracteres o más).
4. En el servicio `facturacion` → Settings → Custom Domains, agregar
   `facturacion.verdevertical.cl` y crear en Cloudflare el CNAME que Render indique.

Límites del plan gratuito: la app se duerme tras 15 minutos sin uso y tarda
cerca de un minuto en despertar. Neon guarda solo 6 horas de historial, así que
conviene bajar un respaldo cada cierto tiempo desde Usuarios → Descargar respaldo.

En el servidor la app exige HTTPS y **verificación en dos pasos**. Los datos del
servidor son independientes de los de la app local.

## Para quien mantenga el código

    python manage.py test                  # pruebas automáticas
    python manage.py generar_recurrentes   # borradores del mes, todas las sociedades

Aplicación Django; el código está en `core/` y la configuración en `config/`.
