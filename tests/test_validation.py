import unittest

from tg_directory_bot.validation import (
    blocked_terms,
    looks_like_keyword_address_payload,
    normalize_url,
    parse_keyword_address_payload,
    parse_submission_payload,
)


class ValidationTest(unittest.TestCase):
    def test_normalize_url_lowercases_host_and_removes_fragment(self):
        self.assertEqual(
            normalize_url("HTTPS://Example.COM/path?q=1#frag"),
            "https://example.com/path?q=1",
        )

    def test_parse_submission_payload_defaults_category(self):
        submission = parse_submission_payload("https://example.com | Example", ("other",))
        self.assertEqual(submission.category, "other")
        self.assertEqual(submission.title, "Example")

    def test_blocked_terms_are_case_insensitive(self):
        submission = parse_submission_payload(
            "https://example.com | Casino Link | other | demo",
            ("other",),
        )
        self.assertEqual(blocked_terms(submission, ("casino",)), ["casino"])

    def test_parse_keyword_address_payload(self):
        submission = parse_keyword_address_payload("888 https://Example.com/group", ("other",))
        self.assertEqual(submission.title, "888")
        self.assertEqual(submission.url, "https://example.com/group")
        self.assertEqual(submission.category, "other")
        short = parse_keyword_address_payload("菠菜 t.me/example", ("other",))
        self.assertEqual(short.url, "https://t.me/example")
        username = parse_keyword_address_payload("客服 @example_bot", ("other",))
        self.assertEqual(username.url, "https://t.me/example_bot")
        self.assertTrue(looks_like_keyword_address_payload("菠菜 t.me/example"))
        self.assertFalse(looks_like_keyword_address_payload("这是一条普通客服消息"))


if __name__ == "__main__":
    unittest.main()
