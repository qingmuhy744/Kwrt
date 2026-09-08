import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import security_vendor as vendor


def rss(body=None, identifier="TS-2026-008"):
    body = body or ("<p>Description: Test availability issue.</p><h4>What happened?</h4>"
                    "<p>This vulnerability is fixed in Tailscale version <code>1.98.9</code> or newer.</p>"
                    "<h4>Who was affected?</h4><p>Nodes using Serve or Funnel.</p>"
                    "<h4>What do I need to do?</h4><p>Install the fixed release.</p>")
    return (f"<rss><channel><item><title>{identifier}</title>"
            f"<link>https://tailscale.com/security-bulletins/#{identifier.lower()}</link>"
            "<pubDate>Mon, 13 Jul 2026 00:00:00 GMT</pubDate>"
            f"<description>{escape(body)}</description></item></channel></rss>").encode()


class VendorTests(unittest.TestCase):
    def test_official_rss_preserves_scope_and_explicit_fixed_version(self):
        result = vendor.parse_tailscale_bulletins(rss())[0]
        self.assertEqual(result["id"], "TS-2026-008")
        self.assertEqual(result["title"], "Test availability issue.")
        self.assertEqual(result["condition"], "Nodes using Serve or Funnel.")
        self.assertEqual(result["vulnerabilities"][0]["patched_versions"], "1.98.9")

    def test_unknown_remediation_is_not_guessed_from_other_version_numbers(self):
        body = ("<p>Description: A future issue.</p><h4>What happened?</h4><p>Introduced in 1.98.9.</p>"
                "<h4>Who was affected?</h4><p>Some configurations.</p>")
        result = vendor.parse_tailscale_bulletins(rss(body))[0]
        self.assertIsNone(result["vulnerabilities"][0]["patched_versions"])

    def test_historical_heading_formats_and_nested_description_markup(self):
        for tag in ("h3", "strong"):
            body = ("<p><strong><em>Description</em></strong>: Legacy issue.</p>"
                    f"<{tag}>What happened?</{tag}><p>This vulnerability is fixed in Tailscale version 1.80.1 or newer.</p>"
                    f"<{tag}>Who is affected?</{tag}><p>Linux clients.</p>")
            result = vendor.parse_tailscale_bulletins(rss(body))[0]
            self.assertEqual(result["title"], "Legacy issue.")
            self.assertEqual(result["condition"], "Linux clients.")

    def test_invalid_or_empty_rss_and_foreign_links_are_rejected(self):
        for data in (b"<html>unavailable</html>", b"<rss>", rss().replace(b"tailscale.com/", b"example.test/")):
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                vendor.parse_tailscale_bulletins(data)

    def test_network_failures_retry_without_an_authorization_header(self):
        with patch.object(vendor, "urlopen", side_effect=[URLError("timeout"), io.BytesIO(rss())]) as request, \
                patch.object(vendor.time, "sleep"):
            self.assertEqual(len(vendor.tailscale_bulletins()), 1)
            self.assertEqual(request.call_count, 2)
            self.assertIsNone(request.call_args.args[0].get_header("Authorization"))
