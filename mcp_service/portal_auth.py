# -*- coding: utf-8 -*-
"""Bearer-token verification for the MCP endpoints, against the portal's own OAuth server.

A token is accepted only if: RS256 signature checks out against the portal's published key,
`iss` is this portal, `aud` is exactly THIS endpoint's URL, it has not expired, `role` and
`scope` match the endpoint's role, and the sign-in (`sid`) has not been ended by the person.
"""
import os
import json
import logging

import jwt
from cryptography.hazmat.primitives import serialization

import service

log = logging.getLogger("portal_auth")
ISSUER = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

_pub = {}


def _public_keys():
    """kid -> public key, read from the portal's key table (refreshed if an unknown kid appears)."""
    conn = service.get_db(); cur = conn.cursor()
    cur.execute("SELECT kid, private_pem FROM oauth_keys")
    rows = cur.fetchall(); cur.close(); conn.close()
    for kid, pem in rows:
        _pub[kid] = serialization.load_pem_private_key(pem.encode("ascii"), password=None).public_key()


def _revoked(sid):
    if not sid:
        return False
    conn = service.get_db(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM revoked_sessions WHERE sid = %s", (sid,))
    r = cur.fetchone(); cur.close(); conn.close()
    return bool(r)


def _program_token_revoked(jti):
    conn = service.get_db(); cur = conn.cursor()
    cur.execute("SELECT revoked_at FROM oauth_program_tokens WHERE jti = %s", (jti,))
    r = cur.fetchone(); cur.close(); conn.close()
    return r is None or r[0] is not None


class PortalTokenVerifier:
    def __init__(self, resource_url, role):
        self.resource_url = resource_url
        self.role = role

    async def verify_token(self, token):
        from mcp.server.auth.provider import AccessToken
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            if kid not in _pub:
                _public_keys()
            claims = jwt.decode(token, _pub[kid], algorithms=["RS256"],
                                audience=self.resource_url, issuer=ISSUER,
                                options={"require": ["exp", "iss", "aud", "sub"]})
        except Exception as e:
            log.info("token rejected: %s", type(e).__name__)
            return None
        if claims.get("role") != self.role or claims.get("scope") != self.role:
            log.info("token rejected: wrong role %s for %s endpoint", claims.get("role"), self.role)
            return None
        if not str(claims.get("sub", "")).startswith(self.role + ":"):
            return None
        try:
            if claims.get("kind") == "program":
                if _program_token_revoked(claims.get("jti")):
                    return None
            elif _revoked(claims.get("sid")):
                log.info("token rejected: sign-in ended")
                return None
        except Exception:
            log.exception("revocation check failed; refusing token")
            return None
        return AccessToken(token=token, client_id=claims.get("client_id", ""), scopes=[claims["scope"]],
                           expires_at=claims["exp"], resource=self.resource_url,
                           subject=claims["sub"], claims=claims)
