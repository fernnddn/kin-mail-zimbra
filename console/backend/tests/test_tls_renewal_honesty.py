"""Every TLS method must tell the operator the truth about renewal.

A certificate that nobody renews is a mail outage ninety days after a
successful deploy, and it is the failure this product is most likely to ship,
because everything looks correct until the day it stops.

There are three methods and only one of them renews itself:

  cloudflare  certbot renews over DNS-01 on a timer, and a deploy hook loads
              the result into Zimbra
  manual      certbot needs a human to answer a DNS challenge every time.
              Nothing renews it. The install prints that once, into a
              transcript nobody reads on day sixty.
  customer    the operator's own CA issued it. There is no Let's Encrypt
              lineage, no timer and no hook, and renewal means getting new
              files from that authority.

The Settings page told all three "Renewal uses the same DNS-01 flow as
install", which is true for one of them.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SETTINGS = REPO / "console/frontend/src/pages/Settings.tsx"
TLS_SH = REPO / "install/04-tls-dkim.sh"


class TheConsoleSaysWhetherAnythingRenewsIt(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(SETTINGS.is_file(), f"missing {SETTINGS}")
        self.src = SETTINGS.read_text(encoding="utf-8")

    def test_the_blanket_dns01_claim_is_gone(self) -> None:
        self.assertNotIn(
            "Renewal uses the same\n            DNS-01 flow as install",
            self.src,
            "that sentence is only true for TLS_METHOD=cloudflare",
        )

    def test_each_method_gets_its_own_answer(self) -> None:
        for method in ("cloudflare", "manual", "customer"):
            with self.subTest(method=method):
                self.assertIn(f'"{method}"', self.src)

    def test_manual_is_told_that_nothing_renews_it(self) -> None:
        self.assertIn("Renewal is NOT automatic on this appliance", self.src)

    def test_customer_is_not_offered_a_lets_encrypt_renewal(self) -> None:
        self.assertIn("not from Let's Encrypt", self.src)

    def test_an_unknown_method_is_treated_as_manual(self) -> None:
        # Failing safe: assuming automatic renewal for a method we could not
        # read is the assumption that ends in an expired certificate.
        self.assertIn("Treat it as manual until that is confirmed", self.src)

    def test_only_cloudflare_counts_as_self_renewing(self) -> None:
        self.assertIn('const tlsAutoRenews = tlsMethod === "cloudflare"', self.src)


class TheInstallerRefusesABadTrustRoot(unittest.TestCase):
    """The chain Zimbra serves gets ISRG Root X1 appended to it.

    A plain curl of a URL that 404s writes the error page to disk, and that
    HTML then gets appended to chain.pem, producing a chain no client will
    accept. The download has to fail loudly instead.
    """

    def setUp(self) -> None:
        self.src = TLS_SH.read_text(encoding="utf-8")

    def test_the_download_fails_on_an_http_error(self) -> None:
        self.assertRegex(self.src, r"curl -fsS[^\n]*-o \"\$tmp\"")

    def test_what_was_downloaded_is_verified_as_that_root(self) -> None:
        self.assertIn("openssl x509", self.src)
        self.assertIn("ISRG Root X1", self.src)

    def test_a_bad_download_never_replaces_a_good_file(self) -> None:
        # Fetched to a temp path and only installed after validating, so a
        # captive portal cannot overwrite a working root.
        body = self.src.split("install_isrg_root()")[1].split("\n}")[0]
        self.assertIn("mktemp", body)
        install_at = body.index('install -m 0644 "$tmp" "$dest"')
        validate_at = body.index('if ! isrg_root_is_valid "$tmp"')
        self.assertLess(validate_at, install_at, "installed before validating")


class RenewalHasToActuallyBeScheduled(unittest.TestCase):
    def setUp(self) -> None:
        self.src = TLS_SH.read_text(encoding="utf-8")

    def test_a_masked_timer_is_called_out(self) -> None:
        # A passing --dry-run only proves renewal WOULD work. A masked timer
        # means it never runs, with every other signal green.
        self.assertIn("masked", self.src)
        self.assertIn("renewal will never run", self.src)

    def test_a_missing_timer_is_called_out(self) -> None:
        self.assertIn("Renewal will NOT happen on its own", self.src)

    def test_the_force_flag_cannot_word_split(self) -> None:
        # Compared as a quoted string and pushed into a bash array, never
        # expanded unquoted into a command line.
        for line in self.src.splitlines():
            if "KIN_TLS_FORCE_RENEW" in line:
                with self.subTest(line=line.strip()):
                    self.assertIn('"${KIN_TLS_FORCE_RENEW:-0}"', line)


class TheDeployHookSurvivesASingleNode(unittest.TestCase):
    """On 1vm there is no Pacemaker, and the hook must not need one."""

    def setUp(self) -> None:
        src = TLS_SH.read_text(encoding="utf-8")
        start = src.index("cat > /etc/letsencrypt/renewal-hooks/deploy/zimbra-deploy.sh")
        self.hook = src[start : src.index("\nHOOK", start)]

    def test_pcs_being_absent_is_not_a_failure(self) -> None:
        # command -v pcs returns non-zero on a single node; the helper must
        # treat that as "not clustered", not as an error under set -e.
        self.assertIn("command -v pcs >/dev/null 2>&1 || return 1", self.hook)

    def test_nothing_is_unmanaged_unless_pacemaker_owns_it(self) -> None:
        self.assertIn("if pcs_has_kin_zimbra; then", self.hook)
        self.assertIn('if [ "$PCS_UNMANAGED" = "1" ]; then', self.hook)

    def test_a_node_without_zimbra_mounted_exits_clean(self) -> None:
        # Otherwise certbot reports a renewal failure twice a day for a node
        # that is simply not the one serving.
        self.assertIn("if [ ! -x /opt/zimbra/bin/zmcertmgr ]; then", self.hook)
        self.assertRegex(self.hook, r"zmcertmgr \]; then(?:.|\n)*?exit 0")

    def test_it_only_acts_on_its_own_lineage(self) -> None:
        self.assertIn('[ "${RENEWED_LINEAGE:-$LE}" = "$LE" ] || exit 0', self.hook)


class TheCustomerMethodInstallsNoAutomation(unittest.TestCase):
    def setUp(self) -> None:
        src = TLS_SH.read_text(encoding="utf-8")
        self.body = src.split("tls_customer()")[1].split("\n}")[0]

    def test_it_never_runs_certbot(self) -> None:
        # An invocation, not the word. The body legitimately says "certbot
        # skipped" in a message, and matching that told us nothing.
        for line in self.body.splitlines():
            with self.subTest(line=line.strip()):
                self.assertNotRegex(line, r"^\s*certbot\b")
        self.assertNotIn("certbot certonly", self.body)
        self.assertNotIn("certbot renew", self.body)

    def test_it_installs_no_deploy_hook_or_timer(self) -> None:
        # A Let's Encrypt hook or timer here would eventually overwrite the
        # customer's own certificate.
        self.assertNotIn("renewal-hooks", self.body)
        self.assertNotIn("certbot.timer", self.body)

    def test_the_dispatch_sends_customer_to_it(self) -> None:
        src = TLS_SH.read_text(encoding="utf-8")
        self.assertIn("customer)   tls_customer ;;", src)


if __name__ == "__main__":
    unittest.main()
