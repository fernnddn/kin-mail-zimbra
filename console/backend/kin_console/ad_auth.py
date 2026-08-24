"""AD/LDAP bind for console login - same search/bind pattern as 06-hybrid-auth.sh.

Fail closed: unreachable AD or missing config never grants access.
Passwords are never included in exceptions or log messages.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from .ad_settings import AdSettings, load_ad_settings

log = logging.getLogger("kin_console.ad_auth")

AdFailReason = Literal[
    "not_enabled",
    "misconfigured",
    "unreachable",
    "invalid_credentials",
]


@dataclass(frozen=True)
class AdAuthResult:
    ok: bool
    reason: AdFailReason | None = None
    detail: str = ""


def _safe_detail(exc: BaseException) -> str:
    """Exception text without copying possible password material."""
    text = str(exc)
    # Strip anything that looks like a password= assignment if a library leaks it.
    text = re.sub(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+", r"\1=***", text)
    return text[:240]


def _require_tls_url(url: str) -> tuple[str, bool, bool]:
    """Return (url, use_ssl, require_starttls). Cleartext password binds are refused."""
    lower = url.lower()
    if lower.startswith("ldaps://"):
        return url, True, False
    if lower.startswith("ldap://"):
        return url, False, True
    raise ValueError("AD_LDAP_URL must start with ldaps:// or ldap:// (StartTLS required)")


def verify_ad_password(ad_username: str, password: str, settings: AdSettings | None = None) -> AdAuthResult:
    """Verify a user password against AD. Never logs password or bind secrets."""
    ad_username = (ad_username or "").strip()
    if not ad_username or password is None or password == "":
        return AdAuthResult(False, "invalid_credentials", "Invalid credentials")

    cfg = settings or load_ad_settings()
    if not cfg.enabled:
        log.info("ad_auth user=%s result=fail reason=not_enabled", ad_username)
        return AdAuthResult(
            False,
            "not_enabled",
            "AD authentication is not enabled on this appliance",
        )

    missing = []
    if not cfg.ldap_url:
        missing.append("AD_LDAP_URL")
    if not cfg.search_base:
        missing.append("AD_SEARCH_BASE")
    if not cfg.search_bind_dn:
        missing.append("AD_SEARCH_BIND_DN")
    if not cfg.search_bind_password:
        missing.append("AD_SEARCH_BIND_PASSWORD")
    if missing and not cfg.bind_dn_template:
        log.info("ad_auth user=%s result=fail reason=misconfigured", ad_username)
        return AdAuthResult(
            False,
            "misconfigured",
            "AD settings incomplete (configure via 00-config / wizard Hybrid step)",
        )

    try:
        from ldap3 import ALL, Connection, Server, Tls
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:
        log.info("ad_auth user=%s result=fail reason=misconfigured missing_ldap3", ad_username)
        return AdAuthResult(False, "misconfigured", "ldap3 package not installed")

    try:
        url, use_ssl, require_starttls = _require_tls_url(cfg.ldap_url)
    except ValueError as exc:
        log.info("ad_auth user=%s result=fail reason=misconfigured", ad_username)
        return AdAuthResult(False, "misconfigured", str(exc))

    tls = Tls(validate=0)  # appliance may use private CA; transport still encrypted
    try:
        server = Server(url, use_ssl=use_ssl, get_info=ALL, tls=tls, connect_timeout=8)
    except (LDAPException, OSError, TypeError, ValueError) as exc:
        log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
        return AdAuthResult(False, "unreachable", f"AD directory unreachable ({_safe_detail(exc)})")

    # Path A (matches optional zimbraAuthLdapBindDn): direct template bind.
    if cfg.bind_dn_template:
        bind_dn = (
            cfg.bind_dn_template.replace("%u", ad_username)
            .replace("%n", ad_username)
            .replace("%j", ad_username)
        )
        try:
            conn = Connection(
                server,
                user=bind_dn,
                password=password,
                auto_bind=False,
                receive_timeout=10,
            )
            if require_starttls:
                if not conn.open():
                    log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
                    return AdAuthResult(False, "unreachable", "AD directory unreachable (connect)")
                if not conn.start_tls():
                    log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
                    return AdAuthResult(
                        False,
                        "unreachable",
                        "AD StartTLS failed - refusing cleartext LDAP bind",
                    )
            if not conn.bind():
                log.info("ad_auth user=%s result=fail reason=invalid_credentials", ad_username)
                return AdAuthResult(False, "invalid_credentials", "Invalid credentials")
            conn.unbind()
            log.info("ad_auth user=%s result=ok method=bind_template", ad_username)
            return AdAuthResult(True)
        except LDAPException as exc:
            detail = _safe_detail(exc)
            # Distinguish network vs auth when possible.
            low = detail.lower()
            if any(x in low for x in ("timed out", "timeout", "unreachable", "connection", "socket")):
                log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
                return AdAuthResult(False, "unreachable", f"AD directory unreachable ({detail})")
            log.info("ad_auth user=%s result=fail reason=invalid_credentials", ad_username)
            return AdAuthResult(False, "invalid_credentials", "Invalid credentials")
        except OSError as exc:
            log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
            return AdAuthResult(False, "unreachable", f"AD directory unreachable ({_safe_detail(exc)})")

    # Path B (default Zimbra / 06-hybrid-auth): search bind → find DN → user bind.
    try:
        search_conn = Connection(
            server,
            user=cfg.search_bind_dn,
            password=cfg.search_bind_password,
            auto_bind=False,
            receive_timeout=10,
        )
        if require_starttls:
            if not search_conn.open():
                log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
                return AdAuthResult(False, "unreachable", "AD directory unreachable (connect)")
            if not search_conn.start_tls():
                log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
                return AdAuthResult(
                    False,
                    "unreachable",
                    "AD StartTLS failed - refusing cleartext LDAP bind",
                )
        if not search_conn.bind():
            log.info("ad_auth user=%s result=fail reason=misconfigured search_bind", ad_username)
            return AdAuthResult(
                False,
                "misconfigured",
                "AD search bind failed - check AD_SEARCH_BIND_DN in appliance config",
            )

        filt = cfg.search_filter.replace("%u", escape_filter_chars(ad_username))
        ok = search_conn.search(
            search_base=cfg.search_base,
            search_filter=filt,
            attributes=["distinguishedName"],
            size_limit=1,
        )
        if not ok or not search_conn.entries:
            search_conn.unbind()
            log.info("ad_auth user=%s result=fail reason=invalid_credentials", ad_username)
            return AdAuthResult(False, "invalid_credentials", "Invalid credentials")

        user_dn = str(search_conn.entries[0].entry_dn)
        search_conn.unbind()

        user_conn = Connection(
            server,
            user=user_dn,
            password=password,
            auto_bind=False,
            receive_timeout=10,
        )
        if require_starttls:
            if not user_conn.open():
                log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
                return AdAuthResult(False, "unreachable", "AD directory unreachable (connect)")
            if not user_conn.start_tls():
                log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
                return AdAuthResult(
                    False,
                    "unreachable",
                    "AD StartTLS failed - refusing cleartext LDAP bind",
                )
        if not user_conn.bind():
            log.info("ad_auth user=%s result=fail reason=invalid_credentials", ad_username)
            return AdAuthResult(False, "invalid_credentials", "Invalid credentials")
        user_conn.unbind()
        log.info("ad_auth user=%s result=ok method=search_bind", ad_username)
        return AdAuthResult(True)
    except LDAPException as exc:
        detail = _safe_detail(exc)
        low = detail.lower()
        if any(x in low for x in ("timed out", "timeout", "unreachable", "connection", "socket")):
            log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
            return AdAuthResult(False, "unreachable", f"AD directory unreachable ({detail})")
        log.info("ad_auth user=%s result=fail reason=invalid_credentials", ad_username)
        return AdAuthResult(False, "invalid_credentials", "Invalid credentials")
    except OSError as exc:
        log.info("ad_auth user=%s result=fail reason=unreachable", ad_username)
        return AdAuthResult(False, "unreachable", f"AD directory unreachable ({_safe_detail(exc)})")
