"""Local HTTPS for PhonePad.

iPhone Safari only gives motion-sensor data to secure (https) pages, so the
server also listens with TLS. That needs a certificate the phone trusts:

- a small local CA, created once and installed on the iPhone as a profile;
- a server certificate for this Mac's current addresses, re-issued by that CA
  whenever the addresses change (the phone keeps trusting it).

The CA is name-constrained to local names (*.local, localhost) and private /
local IP ranges, so even if its key leaked it could not impersonate a real
website. Its key never leaves .certs/ on this Mac.
"""
import datetime
import ipaddress
import json
import os
from pathlib import Path

# Where the CA may issue certificates. IPv6 global unicast (2000::/3) has to be
# included because some home and mobile networks hand out only public IPv6.
PERMITTED_NETS = [
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "169.254.0.0/16", "100.64.0.0/10",
    "::1/128", "fc00::/7", "fe80::/10", "2000::/3",
]
LEAF_DAYS = 397


def _write(path, data, private=False):
    path.write_bytes(data)
    if private:
        os.chmod(path, 0o600)


def ensure(cert_dir, hostnames, ips, label):
    """Create (or reuse) the CA and a server certificate for these names/IPs.
    Returns (cert_path, key_path, ca_path)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    cert_dir = Path(cert_dir)
    cert_dir.mkdir(mode=0o700, exist_ok=True)
    ca_key_p, ca_p = cert_dir / "ca.key", cert_dir / "ca.crt"
    leaf_key_p, leaf_p, meta_p = cert_dir / "server.key", cert_dir / "server.crt", cert_dir / "server.json"
    now = datetime.datetime.now(datetime.timezone.utc)
    pem = serialization.Encoding.PEM

    def key_bytes(key):
        return key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())

    if ca_key_p.exists() and ca_p.exists():
        ca_key = serialization.load_pem_private_key(ca_key_p.read_bytes(), None)
        ca = x509.load_pem_x509_certificate(ca_p.read_bytes())
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        # A common name may be at most 64 characters, and Mac hostnames can be longer.
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"PhonePad local CA ({label[:40]})")])
        permitted = [x509.DNSName("local"), x509.DNSName("localhost")] + [
            x509.IPAddress(ipaddress.ip_network(n)) for n in PERMITTED_NETS]
        ca = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None),
                           critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        _write(ca_key_p, key_bytes(ca_key), private=True)
        _write(ca_p, ca.public_bytes(pem))
        leaf_p.unlink(missing_ok=True)

    names = sorted(set(hostnames))
    addrs = sorted({str(ipaddress.ip_address(i)) for i in ips})
    want = {"names": names, "ips": addrs, "ca": ca.serial_number}
    try:
        meta = json.loads(meta_p.read_text())
        leaf = x509.load_pem_x509_certificate(leaf_p.read_bytes())
        fresh = leaf.not_valid_after_utc - now > datetime.timedelta(days=30)
        if meta == want and fresh and leaf_key_p.exists():
            return leaf_p, leaf_key_p, ca_p
    except (OSError, ValueError):
        pass

    key = ec.generate_private_key(ec.SECP256R1())
    sans = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(a)) for a in addrs]
    leaf = (
        x509.CertificateBuilder()
        # Browsers match the SAN list, so the common name is only a label.
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PhonePad server")]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=LEAF_DAYS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=False, crl_sign=False,
            content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write(leaf_key_p, key_bytes(key), private=True)
    _write(leaf_p, leaf.public_bytes(pem))
    meta_p.write_text(json.dumps(want))
    return leaf_p, leaf_key_p, ca_p
