from __future__ import annotations

import unittest

from isproxy.gui import parse_target_url


class ParseTargetUrlTests(unittest.TestCase):
    def test_splits_domain_and_resource_path(self) -> None:
        target, resource = parse_target_url("https://www.runoob.com/python3/python3-tutorial.html")
        self.assertEqual(target.origin, "https://www.runoob.com")
        self.assertEqual(target.base_path, "")
        self.assertEqual(resource, "/python3/python3-tutorial.html")

    def test_keeps_query_string(self) -> None:
        _, resource = parse_target_url("https://www.runoob.com/search?q=python")
        self.assertEqual(resource, "/search?q=python")

    def test_preserves_trailing_slash(self) -> None:
        _, resource = parse_target_url("https://www.runoob.com/python3/")
        self.assertEqual(resource, "/python3/")

    def test_defaults_to_root(self) -> None:
        target, resource = parse_target_url("www.runoob.com")
        self.assertEqual(target.origin, "https://www.runoob.com")
        self.assertEqual(resource, "/")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            parse_target_url("   ")


if __name__ == "__main__":
    unittest.main()
