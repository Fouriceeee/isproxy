"""Configurable JavaScript rewrite rules for proxied HTML and JS responses."""

from __future__ import annotations

import codecs
import fnmatch
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_NAME = "js_rules.json"


def default_config_path() -> Path:
    """Put the config next to the exe when frozen; use the home directory otherwise."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / DEFAULT_CONFIG_NAME
    return Path.home() / ".isproxy" / DEFAULT_CONFIG_NAME


DEFAULT_CONFIG_PATH = default_config_path()

HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
JS_CONTENT_TYPES = frozenset(
    {
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "text/ecmascript",
        "application/ecmascript",
    }
)

_FLAG_MAP = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
_SCRIPT_RE = re.compile(rb"(<script\b[^>]*>)(.*?)(</script\s*>)", re.IGNORECASE | re.DOTALL)
_TYPE_RE = re.compile(
    r"""(?:^|\s)type\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)
_CHARSET_RE = re.compile(r"""(?:^|;)\s*charset\s*=\s*["']?([\w.:-]+)""", re.IGNORECASE)
_JS_TYPES = frozenset(
    {
        "",
        "module",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "text/ecmascript",
        "application/ecmascript",
        "text/babel",
    }
)


@dataclass(frozen=True)
class JsRule:
    """A single literal or regex replacement rule."""

    find: str
    replace: str
    regex: bool = False
    flags: str = ""
    url: str | None = None
    count: int = 0
    enabled: bool = True
    pattern: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.find, str) or not self.find:
            raise ValueError("find 不能为空")
        if not isinstance(self.replace, str):
            raise ValueError("replace 必须是字符串")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled 必须是 true/false")
        if not isinstance(self.flags, str):
            raise ValueError("flags 必须是字符串")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 0:
            raise ValueError("count 必须是非负整数")
        unknown = set(self.flags) - set(_FLAG_MAP)
        if unknown:
            raise ValueError(f"不支持的 flags: {''.join(sorted(unknown))}（可用 i/m/s）")
        pattern = None
        if self.regex:
            try:
                pattern = re.compile(self.find, self._flags_value())
            except re.error as exc:
                raise ValueError(f"正则表达式无效 {self.find!r}: {exc}") from exc
        object.__setattr__(self, "pattern", pattern)

    def _flags_value(self) -> int:
        value = 0
        for flag in self.flags:
            value |= _FLAG_MAP[flag]
        return value

    def matches_path(self, path: str) -> bool:
        return self.url is None or fnmatch.fnmatchcase(path, self.url)

    def apply(self, text: str) -> tuple[str, int]:
        if self.regex:
            assert self.pattern is not None
            return self.pattern.subn(self.replace, text, count=self.count)
        if self.count:
            replacements = min(text.count(self.find), self.count)
            return text.replace(self.find, self.replace, self.count), replacements
        return text.replace(self.find, self.replace), text.count(self.find)

    def with_enabled(self, enabled: bool) -> "JsRule":
        return JsRule(
            find=self.find,
            replace=self.replace,
            regex=self.regex,
            flags=self.flags,
            url=self.url,
            count=self.count,
            enabled=enabled,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"find": self.find, "replace": self.replace}
        if not self.enabled:
            data["enabled"] = False
        if self.regex:
            data["regex"] = True
        if self.flags:
            data["flags"] = self.flags
        if self.url is not None:
            data["url"] = self.url
        if self.count:
            data["count"] = self.count
        return data


@dataclass(frozen=True)
class JsRewriteConfig:
    """Rules plus the scopes they apply to."""

    enabled: bool = True
    inline_scripts: bool = True
    external_scripts: bool = False
    rules: tuple[JsRule, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rules, tuple):
            object.__setattr__(self, "rules", tuple(self.rules))

    @property
    def active(self) -> bool:
        return self.enabled and any(rule.enabled for rule in self.rules)

    def apply(self, text: str, path: str = "/") -> tuple[str, int]:
        if not self.enabled:
            return text, 0
        total = 0
        for rule in self.rules:
            if rule.enabled and rule.matches_path(path):
                text, count = rule.apply(text)
                total += count
        return text, total


def config_from_mapping(data: Any) -> JsRewriteConfig:
    if not isinstance(data, dict):
        raise ValueError("配置根节点必须是 JSON 对象")
    for name, default in (("enabled", True), ("inline_scripts", True), ("external_scripts", False)):
        value = data.get(name, default)
        if not isinstance(value, bool):
            raise ValueError(f"{name} 必须是 true/false")
    return JsRewriteConfig(
        enabled=data.get("enabled", True),
        inline_scripts=data.get("inline_scripts", True),
        external_scripts=data.get("external_scripts", False),
        rules=rules_from_list(data.get("rules", [])),
    )


def config_to_dict(config: JsRewriteConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "inline_scripts": config.inline_scripts,
        "external_scripts": config.external_scripts,
        "rules": rules_to_list(config.rules),
    }


def rules_from_list(data: Any) -> tuple[JsRule, ...]:
    if not isinstance(data, list):
        raise ValueError("rules 必须是数组")
    return tuple(_rule_from_mapping(index, item) for index, item in enumerate(data))


def rules_to_list(rules: tuple[JsRule, ...]) -> list[dict[str, Any]]:
    return [rule.to_dict() for rule in rules]


def _rule_from_mapping(index: int, data: Any) -> JsRule:
    if not isinstance(data, dict):
        raise ValueError(f"第 {index + 1} 条规则必须是对象")
    find = data.get("find")
    replace = data.get("replace")
    if not isinstance(find, str):
        raise ValueError(f"第 {index + 1} 条规则：find 必须是字符串")
    if not isinstance(replace, str):
        raise ValueError(f"第 {index + 1} 条规则：replace 必须是字符串")
    regex = data.get("regex", False)
    if not isinstance(regex, bool):
        raise ValueError(f"第 {index + 1} 条规则：regex 必须是 true/false")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"第 {index + 1} 条规则：enabled 必须是 true/false")
    url = data.get("url")
    if url is not None and not isinstance(url, str):
        raise ValueError(f"第 {index + 1} 条规则：url 必须是字符串")
    try:
        return JsRule(
            find=find,
            replace=replace,
            regex=regex,
            flags=data.get("flags", ""),
            url=url,
            count=data.get("count", 0),
            enabled=enabled,
        )
    except ValueError as exc:
        raise ValueError(f"第 {index + 1} 条规则：{exc}") from exc


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> JsRewriteConfig:
    config_path = Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败：{exc}") from exc
    return config_from_mapping(data)


def save_config(path: str | Path, config: JsRewriteConfig) -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(config_to_dict(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def charset_from_content_type(value: str | None) -> str:
    if not value:
        return "utf-8"
    match = _CHARSET_RE.search(value)
    return match.group(1) if match else "utf-8"


def _normalize_charset(charset: str) -> str:
    try:
        return codecs.lookup(charset).name
    except LookupError:
        return "utf-8"


def _decode_text(data: bytes, charset: str) -> str:
    return data.decode(_normalize_charset(charset), errors="surrogateescape")


def _encode_text(text: str, charset: str) -> bytes:
    return text.encode(_normalize_charset(charset), errors="surrogateescape")


def _is_javascript_script(opening_tag: bytes) -> bool:
    text = opening_tag.decode("latin-1", errors="replace")
    match = _TYPE_RE.search(text)
    if not match:
        return True
    type_value = next((group for group in match.groups() if group is not None), "")
    type_value = type_value.split(";", 1)[0].strip().lower()
    return type_value in _JS_TYPES or type_value.endswith(("javascript", "ecmascript"))


def rewrite_inline_scripts(
    data: bytes,
    config: JsRewriteConfig | None,
    charset: str,
    path: str = "/",
) -> tuple[bytes, int]:
    """Apply rules to JavaScript inside <script> blocks, leaving other HTML untouched."""
    if config is None or not config.enabled or not config.inline_scripts or not config.rules:
        return data, 0

    parts: list[bytes] = []
    total = 0
    last = 0
    for match in _SCRIPT_RE.finditer(data):
        parts.append(data[last : match.start()])
        opening, content, closing = match.groups()
        parts.append(opening)
        if content and _is_javascript_script(opening):
            text, count = config.apply(_decode_text(content, charset), path)
            if count:
                total += count
                parts.append(_encode_text(text, charset))
            else:
                parts.append(content)
        else:
            parts.append(content)
        parts.append(closing)
        last = match.end()

    if not total:
        return data, 0
    parts.append(data[last:])
    return b"".join(parts), total


def rewrite_js(
    data: bytes,
    config: JsRewriteConfig | None,
    charset: str,
    path: str = "/",
) -> tuple[bytes, int]:
    """Apply rules to a same-origin JavaScript response."""
    if config is None or not config.enabled or not config.external_scripts or not config.rules:
        return data, 0
    text, count = config.apply(_decode_text(data, charset), path)
    if not count:
        return data, 0
    return _encode_text(text, charset), count
