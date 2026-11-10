from django.core.management.base import BaseCommand

from core.models import Recurrente, hoy


class Command(BaseCommand):
    help = "Crea los borradores de facturación recurrente del mes en curso (todas las sociedades)."

    def handle(self, *args, **options):
        fecha = hoy()
        creados = [d for d in (r.generar(fecha.year, fecha.month)
                               for r in Recurrente.objects.filter(activa=True)) if d]
        self.stdout.write(f"Borradores creados: {len(creados)}")
