#!/bin/bash
# Inicia la app de facturación en este computador.
# Doble clic en Finder, o en Terminal:  bash iniciar.command
cd "$(dirname "$0")" || exit 1

DATOS="${FACTURACION_DATOS:-$HOME/FacturacionDatos}"
mkdir -p "$DATOS"

# Busca un Python 3.10 o superior.
PY=""
for candidato in python3.13 python3.12 python3.11 python3.10 python3 \
                 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidato" >/dev/null 2>&1 &&
     "$candidato" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$candidato"; break
  fi
done
if [ -z "$PY" ]; then
  echo "No encontré Python 3.10 o superior."
  echo "Instálalo desde https://www.python.org/downloads/ y vuelve a abrir este archivo."
  read -r -p "Presiona Enter para cerrar..." _
  exit 1
fi

# El entorno de Python vive fuera de Google Drive, junto a los datos.
if [ ! -x "$DATOS/venv/bin/python" ]; then
  echo "Preparando la app por primera vez (puede tardar un minuto)..."
  "$PY" -m venv "$DATOS/venv" || exit 1
fi
"$DATOS/venv/bin/pip" install --quiet --disable-pip-version-check -r requirements.txt || exit 1

# Si quedó abierta una versión anterior de la app, se cierra para iniciar la nueva.
ANTERIOR="$(lsof -ti tcp:8000 2>/dev/null)"
if [ -n "$ANTERIOR" ]; then
  echo "Cerrando la versión anterior de la app..."
  kill $ANTERIOR 2>/dev/null; sleep 2
fi

export PYTHONDONTWRITEBYTECODE=1
"$DATOS/venv/bin/python" manage.py migrate --verbosity 0 || exit 1
"$DATOS/venv/bin/python" manage.py inicializar || exit 1

echo
echo "App disponible en http://127.0.0.1:8000  (para detenerla: Control + C)"
(sleep 2; command -v open >/dev/null 2>&1 && open "http://127.0.0.1:8000") &
exec "$DATOS/venv/bin/python" manage.py runserver 127.0.0.1:8000
