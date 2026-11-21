# ADR-002: bcrypt directly (no passlib), SHA-256 pre-hash

passlib 1.7.4 is unmaintained and fails against bcrypt >= 4.1. bcrypt truncates at 72 bytes, so
passwords are SHA-256 hashed and base64-encoded before bcrypt. JWTs carry `sub`, `org`, `role`, `jti`.
