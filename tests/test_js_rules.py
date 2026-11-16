from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from isproxy.js_rules import (
    JsRewriteConfig,
    JsRule,
    charset_from_content_type,
    config_from_mapping,
    config_to_dict,
    default_config_path,
    load_config,
    rewrite_inline_scripts,
    rewrite_js,
    save_config,
)


class JsRuleTests(unittest.TestCase):
    def test_literal_rule_replaces_all(self) -> None:
        text, count = JsRule(find="OLD", replace="NEW").apply("OLD OLD")
        self.assertEqual(text, "NEW NEW")
        self.assertEqual(count, 2)

    def test_literal_rule_count(self) -> None:
        text, count = JsRule(find="a", replace="b", count=1).apply("aaa")
        self.assertEqual(text, "baa")
        self.assertEqual(count, 1)

    def test_regex_groups(self) -> None:
        rule = JsRule(find=r"window\.open\((\w+)\)", replace=r"void(\1)", regex=True)
        text, count = rule.apply("window.open(foo)")
        self.assertEqual(text, "void(foo)")
        self.assertEqual(count, 1)

    def test_regex_flag_i(self) -> None:
        text, count = JsRule(find="abc", replace="x", regex=True, flags="i").apply("ABC")
        self.assertEqual(text, "x")
        self.assertEqual(count, 1)

    def test_url_scope(self) -> None:
        rule = JsRule(find="a", replace="b", url="/python3/*")
        self.assertTrue(rule.matches_path("/python3/page"))
        self.assertFalse(rule.matches_path("/other/page"))

    def test_invalid_regex(self) -> None:
        with self.assertRaises(ValueError):
            JsRule(find="(", replace="", regex=True)

    def test_empty_find_rejected(self) -> None:
        with self.assertRaises(ValueError):
            JsRule(find="", replace="x")

    def test_bad_flags_rejected(self) -> None:
        with self.assertRaises(ValueError):
            JsRule(find="a", replace="b", regex=True, flags="x")

    def test_with_enabled_returns_copy(self) -> None:
        rule = JsRule(find="A", replace="B")
        disabled = rule.with_enabled(False)
        self.assertTrue(rule.enabled)
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.find, "A")


class ConfigTests(unittest.TestCase):
    def test_mapping_round_trip(self) -> None:
        config = config_from_mapping(
            {
                "enabled": False,
                "inline_scripts": True,
                "external_scripts": True,
                "rules": [
                    {"find": "a", "replace": "b"},
                    {"find": "c", "replace": "d", "regex": True, "flags": "i", "url": "/x/*", "count": 2},
                ],
            }
        )
        self.assertFalse(config.enabled)
        self.assertTrue(config.external_scripts)
        self.assertEqual(len(config.rules), 2)
        self.assertEqual(config.rules[1].count, 2)
        round_trip = config_to_dict(config)
        self.assertEqual(round_trip["rules"][1]["flags"], "i")
        self.assertEqual(round_trip["rules"][1]["url"], "/x/*")

    def test_rule_index_in_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "第 2 条"):
            config_from_mapping(
                {"rules": [{"find": "a", "replace": "b"}, {"find": "(", "replace": "", "regex": True}]}
            )

    def test_save_and_load(self) -> None:
        config = JsRewriteConfig(
            enabled=True,
            inline_scripts=True,
            external_scripts=True,
            rules=(JsRule(find="a", replace="b"),),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "rules.json"
            save_config(path, config)
            loaded = load_config(path)
        self.assertEqual(loaded, config)

    def test_url_scope_filters_rules(self) -> None:
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW", url="/python3/*"),))
        self.assertEqual(config.apply("OLD", "/python3/page"), ("NEW", 1))
        self.assertEqual(config.apply("OLD", "/other"), ("OLD", 0))

    def test_disabled_config_does_nothing(self) -> None:
        config = JsRewriteConfig(enabled=False, rules=(JsRule(find="OLD", replace="NEW"),))
        self.assertEqual(config.apply("OLD"), ("OLD", 0))

    def test_disabled_rule_is_skipped(self) -> None:
        config = JsRewriteConfig(
            rules=(
                JsRule(find="A", replace="B", enabled=False),
                JsRule(find="C", replace="D"),
            )
        )
        self.assertEqual(config.apply("A C"), ("A D", 1))
        self.assertTrue(config.active)

    def test_all_rules_disabled_means_inactive(self) -> None:
        config = JsRewriteConfig(rules=(JsRule(find="A", replace="B", enabled=False),))
        self.assertFalse(config.active)

    def test_enabled_round_trip(self) -> None:
        config = config_from_mapping({"rules": [{"find": "A", "replace": "B", "enabled": False}]})
        self.assertFalse(config.rules[0].enabled)
        exported = config_to_dict(config)
        self.assertIs(exported["rules"][0]["enabled"], False)
        self.assertEqual(config_from_mapping(exported), config)

    def test_enabled_must_be_bool(self) -> None:
        with self.assertRaises(ValueError):
            config_from_mapping({"rules": [{"find": "A", "replace": "B", "enabled": "false"}]})


class InlineScriptTests(unittest.TestCase):
    def test_only_script_content_is_rewritten(self) -> None:
        data = b'<p>OLD</p><script>var x = "OLD";</script><div>OLD</div>'
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW"),))
        result, count = rewrite_inline_scripts(data, config, "utf-8")
        self.assertEqual(count, 1)
        self.assertIn(b"<p>OLD</p>", result)
        self.assertIn(b'var x = "NEW";', result)
        self.assertIn(b"<div>OLD</div>", result)

    def test_multiple_scripts_with_attributes(self) -> None:
        data = b'<script type="text/javascript">OLD</script><SCRIPT data-x="1">OLD</SCRIPT>'
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW"),))
        result, count = rewrite_inline_scripts(data, config, "utf-8")
        self.assertEqual(count, 2)
        self.assertEqual(result, b'<script type="text/javascript">NEW</script><SCRIPT data-x="1">NEW</SCRIPT>')

    def test_json_scripts_are_untouched(self) -> None:
        data = (
            b'<script type="application/json">{"v": "OLD"}</script>'
            b"<script>OLD</script>"
        )
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW"),))
        result, count = rewrite_inline_scripts(data, config, "utf-8")
        self.assertEqual(count, 1)
        self.assertIn(b'{"v": "OLD"}', result)
        self.assertIn(b"<script>NEW</script>", result)

    def test_data_type_attribute_is_not_script_type(self) -> None:
        data = b'<script data-type="application/json">OLD</script>'
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW"),))
        _, count = rewrite_inline_scripts(data, config, "utf-8")
        self.assertEqual(count, 1)

    def test_gbk_charset(self) -> None:
        data = '<script>var s = "旧值";</script>'.encode("gbk")
        config = JsRewriteConfig(rules=(JsRule(find="旧值", replace="新值"),))
        result, count = rewrite_inline_scripts(data, config, "gbk")
        self.assertEqual(count, 1)
        self.assertIn("新值".encode("gbk"), result)

    def test_inline_scope_can_be_disabled(self) -> None:
        data = b"<script>OLD</script>"
        config = JsRewriteConfig(inline_scripts=False, rules=(JsRule(find="OLD", replace="NEW"),))
        result, count = rewrite_inline_scripts(data, config, "utf-8")
        self.assertEqual(count, 0)
        self.assertEqual(result, data)

    def test_url_scope_applies_to_inline_scripts(self) -> None:
        data = b"<script>OLD</script>"
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW", url="/python3/*"),))
        self.assertEqual(rewrite_inline_scripts(data, config, "utf-8", "/other")[1], 0)
        self.assertEqual(rewrite_inline_scripts(data, config, "utf-8", "/python3/page")[1], 1)

    def test_no_config_passthrough(self) -> None:
        data = b"<script>OLD</script>"
        self.assertEqual(rewrite_inline_scripts(data, None, "utf-8"), (data, 0))

    def test_disabled_rule_leaves_script_untouched(self) -> None:
        data = b"<script>OLD</script>"
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW", enabled=False),))
        self.assertEqual(rewrite_inline_scripts(data, config, "utf-8"), (data, 0))


class ExternalJsTests(unittest.TestCase):
    def test_external_js_rewrite(self) -> None:
        data = b'var x = "OLD";'
        config = JsRewriteConfig(external_scripts=True, rules=(JsRule(find="OLD", replace="NEW"),))
        result, count = rewrite_js(data, config, "utf-8")
        self.assertEqual(result, b'var x = "NEW";')
        self.assertEqual(count, 1)

    def test_external_js_disabled_by_default(self) -> None:
        data = b'var x = "OLD";'
        config = JsRewriteConfig(rules=(JsRule(find="OLD", replace="NEW"),))
        result, count = rewrite_js(data, config, "utf-8")
        self.assertEqual(result, data)
        self.assertEqual(count, 0)

    def test_charset_from_content_type(self) -> None:
        self.assertEqual(charset_from_content_type("text/html; charset=GBK"), "GBK")
        self.assertEqual(charset_from_content_type("text/html"), "utf-8")
        self.assertEqual(charset_from_content_type(None), "utf-8")


class ConfigPathTests(unittest.TestCase):
    def test_frozen_app_uses_executable_folder(self) -> None:
        fake_exe = Path(tempfile.gettempdir()) / "isproxy-test" / "isproxy-gui.exe"
        with (
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "executable", str(fake_exe)),
        ):
            self.assertEqual(default_config_path(), fake_exe.parent / "js_rules.json")

    def test_source_run_uses_home_directory(self) -> None:
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(
                default_config_path(),
                Path.home() / ".isproxy" / "js_rules.json",
            )


if __name__ == "__main__":
    unittest.main()
