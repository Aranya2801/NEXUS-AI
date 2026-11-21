"""Credential encryption at rest and the SSRF guard for user-supplied URLs."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import socket
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, ValidationFailed


def _fernet(s: Settings) -> Fernet:
    key = s.connector_secret_key or base64.urlsafe_b64encode(hashlib.sha256(("nexus-connectors:" + s.jwt_secret).encode()).digest()).decode()
    try:
        return Fernet(key)
    except ValueError as exc:
        raise ConfigurationError("NEXUS_CONNECTOR_SECRET_KEY must be a urlsafe-base64 32-byte Fernet key") from exc


def encrypt_secrets(s: Settings, data: dict) -> str:
    return _fernet(s).encrypt(json.dumps(data).encode()).decode()


def decrypt_secrets(s: Settings, token: str) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet(s).decrypt(token.encode()))
    except InvalidToken as exc:
        raise ConfigurationError("Stored connector credentials cannot be decrypted (key changed?)") from exc


def _is_blocked(ip: ipaddress._BaseAddress) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def assert_public_url(url: str, s: Settings, *, allow_private: bool = False) -> None:
    """Refuses non-http(s) schemes, credentials in the URL, and hosts that are (or resolve to) internal addresses
    (loopback, RFC1918, link-local incl. cloud metadata 169.254.169.254). Not DNS-rebinding proof: see docs/connectors.md."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValidationFailed("URL must be http(s) with a host")
    if u.username or u.password:
        raise ValidationFailed("Credentials must not be embedded in the URL")
    if allow_private:
        return
    host = u.hostname
    if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith((".local", ".internal")):
        raise ValidationFailed("Internal hosts are not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked(literal):
            raise ValidationFailed("Internal addresses are not allowed")
        return
    if s.connector_dns_check:
        try:
            infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise ValidationFailed(f"Host does not resolve: {host}") from exc
        for info in infos:
            if _is_blocked(ipaddress.ip_address(info[4][0])):
                raise ValidationFailed("Host resolves to an internal address")
