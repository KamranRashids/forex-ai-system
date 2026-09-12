# TLS certificates & keys

This directory holds **local/staging and, later, production** certificate/key
material that is mounted read-only into the Nginx container
(`nginx:/etc/nginx/certs`).

Everything here is **gitignored** — real private keys must never be committed.

Current content (2026-09-11, staging only):

- `fullchain.pem` — self-signed certificate chain (SAN: `localhost`,
  `127.0.0.1`, `::1`), CN `localhost`, valid 365 days.
- `privkey.pem` — matching RSA-2048 private key (file mode 600).

**NOT suitable for public production traffic.** This is staging material used to
exercise the full HTTPS/WSS path locally. The real production phase replaces these
files with CA-issued certificates (see `docs/phase10-tls-audit.md`) and enables
HSTS only afterwards.

Regenerate (overwrites both files):

```sh
cd infra/certs
openssl req -x509 -newkey rsa:2048 -sha256 -days 365 -nodes \
  -keyout privkey.pem -out fullchain.pem \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1"
chmod 600 privkey.pem
```