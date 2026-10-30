from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any


SWITCH_TOOL_NAME = "catty_tool_switch"
SWITCH_MESSAGE_NAME = "catty_tool_switches"
SWITCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": SWITCH_TOOL_NAME,
        "description": "Enable or disable optional tools for this request only. The full tool catalog stays visible. Permission-locked tools cannot be enabled. Call this before using a switched-off tool, then continue in the next round.",
        "parameters": {
            "type": "object",
            "properties": {
                "enable": {"type": "array", "maxItems": 128, "items": {"type": "string"}},
                "disable": {"type": "array", "maxItems": 128, "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    },
}


def tool_name(schema: Mapping[str, Any]) -> str:
    function = schema.get("function", schema)
    return str(function.get("name", "")) if isinstance(function, Mapping) else ""


class ToolSwitches:
    def __init__(
        self, role: str, *,
        permitted: Collection[str] | Callable[[], Collection[str]] | None = None,
        enabled: Collection[str] | None = None,
    ):
        self.role = str(role)[:80]
        self._permission_source = permitted if callable(permitted) else None if permitted is None else frozenset(permitted)
        self._initial_enabled = None if enabled is None else frozenset(enabled)
        self._overrides: dict[str, bool] = {}
        self.names: frozenset[str] = frozenset()
        self.permitted: frozenset[str] = frozenset()
        self.enabled: frozenset[str] = frozenset()

    def refresh(self, tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        catalog = list(tools)
        self.names = frozenset(tool_name(schema) for schema in catalog if isinstance(schema, Mapping)) - {""}
        if self.names and SWITCH_TOOL_NAME not in self.names:
            catalog.append(SWITCH_TOOL_SCHEMA)
            self.names = self.names | {SWITCH_TOOL_NAME}
        permission_source = self._permission_source() if callable(self._permission_source) else self._permission_source
        self.permitted = self.names if permission_source is None else (self.names & frozenset(permission_source)) | ({SWITCH_TOOL_NAME} & self.names)
        defaults = self.permitted if self._initial_enabled is None else self.permitted & self._initial_enabled
        self._overrides = {name: value for name, value in self._overrides.items() if name in self.permitted}
        self.enabled = frozenset(name for name in self.permitted if self._overrides.get(name, name in defaults)) | ({SWITCH_TOOL_NAME} & self.names)
        return catalog

    def denial(self, name: str) -> dict[str, Any] | None:
        if name not in self.permitted:
            return {"ok": False, "error": "tool_not_allowed", "tool": name, "message": "This tool is permission-locked for the current caller."}
        if name not in self.enabled:
            return {"ok": False, "error": "tool_switched_off", "tool": name, "message": "Enable this tool with catty_tool_switch, then continue in the next round."}
        return None

    def apply(self, arguments_json: str) -> dict[str, Any]:
        try:
            arguments = json.loads(arguments_json or "{}")
        except (TypeError, ValueError):
            return {"ok": False, "error": "invalid_tool_switch"}
        if not isinstance(arguments, dict) or set(arguments) - {"enable", "disable"}:
            return {"ok": False, "error": "invalid_tool_switch"}
        changes = {}
        for field, value in (("enable", True), ("disable", False)):
            names = arguments.get(field, [])
            if not isinstance(names, list) or len(names) > 128 or any(not isinstance(name, str) for name in names):
                return {"ok": False, "error": "invalid_tool_switch"}
            for name in names:
                if name == SWITCH_TOOL_NAME or name not in self.permitted or name in changes:
                    return {"ok": False, "error": "tool_not_allowed", "tool": name}
                changes[name] = value
        self._overrides.update(changes)
        self.enabled = (self.enabled - changes.keys()) | {name for name, value in changes.items() if value}
        return {"ok": True, "enabled": sorted(name for name, value in changes.items() if value), "disabled": sorted(name for name, value in changes.items() if not value)}

    def marker(self) -> str:
        disabled = self.names - self.enabled
        use_on = len(self.enabled) < len(disabled)
        flags = {"role": self.role, "default": "off" if use_on else "on", "on" if use_on else "off": sorted(self.enabled if use_on else disabled)}
        return (
            "[Current tool switches] Full schemas remain visible; these flags control this request only. "
            "Use catty_tool_switch before an optional off tool. Caller permissions remain authoritative.\n"
            + json.dumps(flags, ensure_ascii=True, separators=(",", ":"))
        )

    def update_messages(self, messages: list[dict[str, Any]]) -> None:
        marker = {"role": "system", "name": SWITCH_MESSAGE_NAME, "content": self.marker()}
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") == "system" and messages[index].get("name") == SWITCH_MESSAGE_NAME:
                messages[index] = marker
                return
        messages.append(marker)