# -*- coding: utf-8 -*-
"""Bearer-token verification for the MCP endpoints, against the portal's own OAuth server.

A token is accepted only if: RS256 signature checks out against the portal's published key,
`iss` is this portal, `aud` is exactly THIS endpoint's URL, it has not expired, `role` and
`scope` match the endpoint's role, and the sign-in (`sid`) has not been ended by the person.
"""
import os
import time
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
            log.warning("TOKEN REJECTED at verification: %r", e)
            return None
        if claims.get("role") != self.role or claims.get("scope") != self.role:
            log.warning("TOKEN REJECTED: role=%r scope=%r but this endpoint is %r", claims.get("role"), claims.get("scope"), self.role)
            return None
        if not str(claims.get("sub", "")).startswith(self.role + ":"):
            log.warning("TOKEN REJECTED: subject %r is not a %s", claims.get("sub"), self.role)
            return None
        try:
            if claims.get("kind") == "program":
                if _program_token_revoked(claims.get("jti")):
                    log.warning("TOKEN REJECTED: program token revoked or unknown")
                    return None
            elif _revoked(claims.get("sid")):
                log.warning("TOKEN REJECTED: this sign-in was ended")
                return None
        except Exception:
            log.exception("revocation check failed; refusing token")
            return None
        log.info("TOKEN VERIFIED sub=%s expires_in=%ss", claims.get("sub"), int(claims["exp"] - time.time()))
        return AccessToken(token=token, client_id=claims.get("client_id", ""), scopes=[claims["scope"]],
                           expires_at=claims["exp"], resource=self.resource_url)


def subject_from_token(tok):
    """Identity of the caller. The token was already verified by PortalTokenVerifier."""
    try:
        return jwt.decode(tok.token, options={"verify_signature": False}).get("sub", "")
    except Exception:
        return ""
