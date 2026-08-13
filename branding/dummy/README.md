# Dummy brand (placeholder)

These files prove the KIN Mail white-label **mechanism** (Timeline 6.1–6.2). They are
intentionally fake: terracotta accent `#C45C26`, the words **KIN DUMMY BRAND**, and
**PLACEHOLDER — NOT A CUSTOMER**.

Do not ship this directory as a real customer identity.

## How it is applied

`install/12-branding.sh` copies this tree to `/etc/kin-mail/brand/` (outside
`/opt/zimbra/jetty_base/webapps/zimbra/skins/`), serves it at `/kin-brand/` via a
marked nginx include, and sets Maldua FOSS chameleon LDAP attributes
(`zimbraSkinLogoLoginBanner`, `zimbraSkinLogoAppBanner`, `zimbraSkinLogoURL`,
`zimbraSkinFavicon`, and the four `zimbraSkin*Color` attrs).

FOSS under CPAL is a logo re-brand with Zimbra attribution, not a full Synacor
Network Edition white-label. The dummy login banner keeps a **ZIMBRA FOSS** mark
on purpose.

Regenerate the bitmaps (stdlib only):

```
python3 branding/dummy/generate.py
```
