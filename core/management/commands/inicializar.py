import os

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand

from pathlib import Path

from core.models import Cliente, DosPasos, Sociedad

RECURSOS = Path(__file__).resolve().parents[2] / "recursos"
LOGO_PILOTO = RECURSOS / "logo_verde_vertical.png"
CLIENTES_PILOTO = RECURSOS / "clientes_verde_vertical.txt"


class Command(BaseCommand):
    help = "Prepara la app en el primer uso: sociedad piloto y usuario de acceso."

    def handle(self, *args, **options):
        if not Sociedad.objects.exists():
            Sociedad.objects.create(rut="77552628-9", razon_social="Verde Vertical")
            self.stdout.write("Sociedad piloto creada: Verde Vertical (77.552.628-9). "
                              "Completa giro y dirección en Sociedades.")
        piloto = Sociedad.objects.filter(rut="77552628-9", logo__isnull=True).first()
        if piloto and LOGO_PILOTO.exists():
            piloto.logo = LOGO_PILOTO.read_bytes()
            piloto.save(update_fields=["logo"])
            self.stdout.write("Logo de Verde Vertical incorporado.")
        piloto = Sociedad.objects.filter(rut="77552628-9").first()
        if piloto and CLIENTES_PILOTO.exists() and not piloto.clientes.exists():
            nombres = [n.strip() for n in CLIENTES_PILOTO.read_text(encoding="utf-8").splitlines()]
            Cliente.objects.bulk_create(
                Cliente(sociedad=piloto, razon_social=n) for n in nombres if n)
            self.stdout.write(f"Clientes de Verde Vertical cargados: {piloto.clientes.count()}. "
                              "Falta completar RUT, giro y dirección.")
        if piloto:
            # Ajustes pedidos el 10-11-2026: «Don Lulo» es «Frutería Don Lulo»; «Kike» y
            # «Enrique Troncoso» no se usan. Solo se quitan si no tienen RUT ni documentos.
            sobran = piloto.clientes.filter(
                razon_social__in=["Don Lulo", "Kike", "Enrique Troncoso"], rut="",
                documentos__isnull=True, recurrentes__isnull=True)
            if piloto.clientes.filter(razon_social__iexact="Fruteria Don Lulo").exists():
                quitados, _ = sobran.delete()
                if quitados:
                    self.stdout.write(f"Clientes depurados: {quitados}.")
        Usuario = get_user_model()
        usuario = os.environ.get("FACTURACION_ADMIN_USUARIO", "").strip()
        clave = os.environ.get("FACTURACION_ADMIN_CLAVE", "")
        if usuario and clave:
            # En el servidor no hay Terminal donde preguntar: el administrador sale de las
            # variables de entorno. Se crea si no existe.
            admin = Usuario.objects.filter(username__iexact=usuario).first()
            if admin is None:
                Usuario.objects.create_superuser(
                    usuario, os.environ.get("FACTURACION_ADMIN_CORREO", ""), clave)
                self.stdout.write(f"Usuario administrador creado: {usuario}")
            elif os.environ.get("FACTURACION_ADMIN_RESTABLECER") == "1":
                # Recuperación de acceso: repone clave, permisos y verificación en dos pasos.
                admin.set_password(clave)
                admin.is_superuser = admin.is_staff = admin.is_active = True
                admin.save()
                DosPasos.objects.filter(usuario=admin).delete()
                self.stdout.write(f"Acceso restablecido para: {admin.username}")
        elif not Usuario.objects.exists():
            if os.environ.get("DATABASE_URL"):
                self.stdout.write("ATENCIÓN: no hay usuarios y faltan FACTURACION_ADMIN_USUARIO y "
                                  "FACTURACION_ADMIN_CLAVE. Nadie puede entrar todavía.")
            else:
                self.stdout.write("\nCrea tu usuario para entrar a la app:")
                call_command("createsuperuser")
