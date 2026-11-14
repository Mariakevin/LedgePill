import unittest

from ledgepill.services.feeds import _clean_text, FeedService
from ledgepill.services.network import NetworkService


class CleanTextTest(unittest.TestCase):
    def test_strips_html_tags(self):
        self.assertEqual(_clean_text("<b>hello</b> world"), "hello world")

    def test_unescapes_entities(self):
        self.assertEqual(_clean_text("a &amp; b &lt;c&gt;"), "a & b")

    def test_collapses_whitespace(self):
        self.assertEqual(_clean_text("  a   b\n\nc  "), "a b c")

    def test_handles_none_and_empty(self):
        self.assertEqual(_clean_text(None), "")
        self.assertEqual(_clean_text(""), "")


class NormalizeFeedsTest(unittest.TestCase):
    def test_keeps_valid_http_feeds(self):
        out = FeedService._normalize_feeds(
            [{"name": "Example", "type": "rss", "url": "https://example.com/feed.xml"}]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://example.com/feed.xml")

    def test_drops_invalid_entries(self):
        out = FeedService._normalize_feeds(
            [
                "bad",
                {"type": "rss", "url": "ftp://example.com/feed.xml"},
                {"type": "xml", "url": "https://example.com/feed.xml"},
                {"type": "rss", "url": "not-a-url"},
            ]
        )
        self.assertEqual(out, [])

    def test_caps_at_twenty(self):
        feeds = [
            {"type": "rss", "url": f"https://example.com/{i}.xml"} for i in range(30)
        ]
        out = FeedService._normalize_feeds(feeds)
        self.assertEqual(len(out), 20)


class NmcliWifiTest(unittest.TestCase):
    def test_parses_single_network(self):
        raw = "*:MyNet:75:WPA2"
        out = NetworkService.parse_nmcli_wifi(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ssid"], "MyNet")
        self.assertTrue(out[0]["active"])
        self.assertEqual(out[0]["signal"], 75)
        self.assertEqual(out[0]["security"], "WPA2")

    def test_skips_empty_and_dashes(self):
        raw = "::--:--\n"
        out = NetworkService.parse_nmcli_wifi(raw)
        self.assertEqual(out, [])

    def test_clamps_signal(self):
        raw = " :Net:200:OPEN"
        out = NetworkService.parse_nmcli_wifi(raw)
        self.assertEqual(out[0]["signal"], 100)

    def test_sorts_active_first(self):
        raw = " :A:50:OPEN\n*:B:40:OPEN"
        out = NetworkService.parse_nmcli_wifi(raw)
        self.assertEqual(out[0]["ssid"], "B")

    def test_escapes_colon_in_ssid(self):
        raw = " :My\\:Net:60:OPEN"
        out = NetworkService.parse_nmcli_wifi(raw)
        self.assertEqual(out[0]["ssid"], "My:Net")


class NmcliIpTest(unittest.TestCase):
    def test_returns_first_non_loopback(self):
        raw = "IP4.ADDRESS[1]:192.168.1.5/24\nIP4.ADDRESS[2]:10.0.0.2/8"
        self.assertEqual(NetworkService.parse_nmcli_ip(raw), "192.168.1.5")

    def test_skips_loopback(self):
        raw = "IP4.ADDRESS[1]:127.0.0.1/8\nIP4.ADDRESS[2]:10.0.0.2/8"
        self.assertEqual(NetworkService.parse_nmcli_ip(raw), "10.0.0.2")

    def test_returns_empty_when_none(self):
        self.assertEqual(NetworkService.parse_nmcli_ip(""), "")


if __name__ == "__main__":
    unittest.main()
