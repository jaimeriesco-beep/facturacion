from django.contrib import admin

from . import models

for modelo in (models.Sociedad, models.Cliente, models.Producto, models.Documento,
               models.Pago, models.Recurrente):
    admin.site.register(modelo)
