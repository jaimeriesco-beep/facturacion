"""Certificado y CAF ficticios para pruebas automáticas. No sirven ante el SII."""
import base64
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12


def certificado_ficticio(nombre="Firmante De Prueba", clave="clave-prueba"):
    llave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sujeto = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, nombre)])
    ahora = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(sujeto).issuer_name(sujeto)
            .public_key(llave.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(ahora - timedelta(days=1)).not_valid_after(ahora + timedelta(days=365))
            .sign(llave, hashes.SHA256()))
    pfx = pkcs12.serialize_key_and_certificates(
        b"prueba", llave, cert, None, serialization.BestAvailableEncryption(clave.encode()))
    return pfx, clave


def caf_ficticio(rut, razon_social, tipo, desde=1, hasta=50):
    llave = rsa.generate_private_key(public_exponent=3, key_size=1024)
    numeros = llave.public_key().public_numbers()

    def b64(n):
        return base64.b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode()

    pem = llave.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.NoEncryption()).decode()
    return f"""<?xml version="1.0"?>
<AUTORIZACION>
<CAF version="1.0">
<DA>
<RE>{rut}</RE>
<RS>{razon_social[:40].upper()}</RS>
<TD>{tipo}</TD>
<RNG><D>{desde}</D><H>{hasta}</H></RNG>
<FA>2026-11-01</FA>
<RSAPK><M>{b64(numeros.n)}</M><E>{b64(numeros.e)}</E></RSAPK>
<IDK>100</IDK>
</DA>
<FRMA algoritmo="SHA1withRSA">{base64.b64encode(b"firma-ficticia-del-sii" * 3).decode()}</FRMA>
</CAF>
<RSASK>{pem}</RSASK>
</AUTORIZACION>
""".encode("latin-1")
