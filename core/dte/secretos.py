"""Cifrado de certificados, claves y folios guardados en la base de datos.

La clave maestra se guarda en el Llavero de macOS (vía `keyring`). Si no hay
llavero disponible, se usa un archivo con permisos restringidos en la carpeta
de datos.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

SERVICIO, USUARIO = "facturacion-app", "clave-maestra"


class ErrorSecreto(Exception):
    pass


def _clave_maestra():
    del_entorno = os.environ.get("FACTURACION_CLAVE_MAESTRA")
    if del_entorno:  # en el servidor: cualquier texto secreto largo sirve como clave
        return base64.urlsafe_b64encode(hashlib.sha256(del_entorno.encode()).digest())
    archivo = settings.DATOS_DIR / "clave_maestra.key"
    if archivo.exists():
        return archivo.read_bytes().strip()
    if os.environ.get("FACTURACION_SIN_LLAVERO") != "1":
        try:
            import keyring
            clave = keyring.get_password(SERVICIO, USUARIO)
            if not clave:
                clave = Fernet.generate_key().decode()
                keyring.set_password(SERVICIO, USUARIO, clave)
            return clave.encode()
        except Exception:  # sin llavero en este sistema
            pass
    clave = Fernet.generate_key()
    archivo.write_bytes(clave)
    archivo.chmod(0o600)
    return clave


def cifrar(datos: bytes) -> bytes:
    return Fernet(_clave_maestra()).encrypt(datos)


def descifrar(datos) -> bytes:
    try:
        return Fernet(_clave_maestra()).decrypt(bytes(datos))
    except InvalidToken:
        raise ErrorSecreto(
            "No se pudo descifrar la información guardada. Vuelve a cargar el certificado "
            "y los folios en Conexión SII.")
