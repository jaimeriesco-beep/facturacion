#!/usr/bin/env bash
# Pasos de construcción en el servidor (Render).
set -o errexit
pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate --no-input
python manage.py inicializar
