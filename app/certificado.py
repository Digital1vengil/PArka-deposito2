"""Certificado HTTPS propio para usar la app desde celulares y tablets del WiFi.

Los navegadores solo dejan usar la cámara en páginas seguras (https). Como la app corre en esta PC,
se genera un certificado autofirmado para la IP local. La primera vez el celular avisa
"La conexión no es privada": se toca "Configuración avanzada" → "Continuar" y listo.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path


def ips_locales() -> list[str]:
    ips = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ips)


def ip_principal() -> str:
    otras = [i for i in ips_locales() if not i.startswith("127.")]
    return otras[0] if otras else "127.0.0.1"


def asegurar_certificado(carpeta: Path) -> tuple[Path, Path]:
    """Devuelve (cert, key). Lo (re)genera si no existe o si cambió la IP de la PC."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    carpeta.mkdir(parents=True, exist_ok=True)
    cert_p, key_p = carpeta / "https-cert.pem", carpeta / "https-key.pem"
    ips = ips_locales()
    if cert_p.exists() and key_p.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            en_cert = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
            vence = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after
            if set(ips) <= en_cert and vence.replace(tzinfo=None) > dt.datetime.utcnow() + dt.timedelta(days=30):
                return cert_p, key_p
        except Exception:  # noqa: BLE001 — si no se puede leer, se genera de nuevo
            pass
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PARKA Deposito"),
                        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PARKA")])
    alt = [x509.DNSName("localhost"), x509.DNSName(socket.gethostname())] + \
          [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    ahora = dt.datetime.utcnow()
    cert = (x509.CertificateBuilder().subject_name(nombre).issuer_name(nombre).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(ahora - dt.timedelta(days=1)).not_valid_after(ahora + dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption()))
    return cert_p, key_p


def qr_svg(texto: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(texto, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding="unicode")


def qr_png(texto: str) -> bytes:
    import io
    import qrcode
    buf = io.BytesIO()
    qrcode.make(texto, box_size=6, border=2).save(buf, format="PNG")
    return buf.getvalue()
