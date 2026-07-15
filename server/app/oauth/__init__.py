"""Self-hosted OAuth 2.1 authorization server for the MCP surface (M5 task 3, ADR-046 §2).

The `api` app is both authorization server and resource server (ADR-003 single service): open
Dynamic Client Registration, a password + explicit-consent `/authorize` choke point with PKCE,
opaque HMAC-hashed DB tokens (~1h access + sliding refresh), and a revoke-all switch. authlib
supplies only the security-critical protocol crypto (PKCE S256, secure token generation, RFC 8414
metadata schemas); the flow orchestration lives over our own asyncpg store (rule 5), mirroring how
web session tokens are already handled by hand (`security.py`).
"""
