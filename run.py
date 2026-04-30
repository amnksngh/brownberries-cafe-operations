import os
import socket
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path

from app import create_app
from app.extensions import socketio

app = create_app()


def _detect_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _ensure_ip_cert(lan_ip: str):
    cert_dir = Path(__file__).resolve().parent / "instance" / "certs"
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_file = cert_dir / "local-cert.pem"
    key_file = cert_dir / "local-key.pem"
    marker_file = cert_dir / "local-cert-ip.txt"

    current_ip = marker_file.read_text().strip() if marker_file.exists() else ""
    if cert_file.exists() and key_file.exists() and current_ip == lan_ip:
        return str(cert_file), str(key_file)

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Brownberries Local HTTPS")])
    alt_names = x509.SubjectAlternativeName(
        [
            x509.DNSName("localhost"),
            x509.IPAddress(ip_address("127.0.0.1")),
            x509.IPAddress(ip_address(lan_ip)),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(alt_names, critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    marker_file.write_text(lan_ip)
    return str(cert_file), str(key_file)


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5050"))
    enable_https = os.getenv("ENABLE_HTTPS", "0") == "1"
    cert_file = os.getenv("SSL_CERT_FILE", "").strip()
    key_file = os.getenv("SSL_KEY_FILE", "").strip()
    scheme = "https" if enable_https else "http"
    lan_ip = _detect_lan_ip()
    if enable_https and cert_file and key_file:
        ssl_context = (cert_file, key_file)
    elif enable_https:
        generated_cert_file, generated_key_file = _ensure_ip_cert(lan_ip)
        ssl_context = (generated_cert_file, generated_key_file)
    else:
        ssl_context = None
    print(f"Local URL:   {scheme}://127.0.0.1:{port}")
    print(f"Wi-Fi URL:   {scheme}://{lan_ip}:{port}")
    print("If Wi-Fi changes, restart server and use the new Wi-Fi URL above.")
    if enable_https:
        if cert_file and key_file:
            print(f"HTTPS mode: ENABLED (fixed cert files)")
            print(f"Cert: {cert_file}")
            print(f"Key:  {key_file}")
        else:
            print("HTTPS mode: ENABLED (auto-generated IP certificate)")
            print(f"Cert: {generated_cert_file}")
            print(f"Key:  {generated_key_file}")

    debug_mode = os.getenv("DEBUG", "1") == "1"
    if enable_https:
        # Keep HTTPS stable for mobile browsers; avoid auto-reloader cert churn.
        debug_mode = False

    socketio.run(
        app,
        host=host,
        port=port,
        debug=debug_mode,
        use_reloader=debug_mode,
        ssl_context=ssl_context,
    )
