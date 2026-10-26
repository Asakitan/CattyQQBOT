"""Local persona operations and per-turn identity snapshots; no NoneBot imports."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from .persona_store import PERSONA_SCHEMA, PersonaDefinitionStore

_TURN_PERSONA: ContextVar[tuple[str, Any] | None] = ContextVar("catty_turn_persona", default=None)


def _base_scope(scope: str) -> str:
    parts = str(scope or "").split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else str(scope or "")


def active_persona_for_scope(scope: str) -> Any:
    snapshot = _TURN_PERSONA.get()
    return snapshot[1] if snapshot is not None and snapshot[0] == _base_scope(scope) else None


def bind_turn_persona(scope: str, persona: Any) -> Token:
    return _TURN_PERSONA.set((_base_scope(scope), persona))


def reset_turn_persona(token: Token) -> None:
    _TURN_PERSONA.reset(token)


class PersonaRuntime:
    """Serialize definition writes, publish immutable registry snapshots afterwards.

    Switching only writes the scope override. Existing in-flight replies retain
    their captured persona; the next admitted turn uses normal history isolation.
    No chat/admission/user/group lock is acquired here.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.store = PersonaDefinitionStore(self.root)
        self._lock = asyncio.Lock()

    def publish(self) -> None:
        from .personas import install_custom_personas
        install_custom_personas(self.store.definitions(), root=self.root)

    def load_sync(self) -> dict[str, Any]:
        result = self.store.reload()
        self.publish()
        return result

    async def execute(
        self, args: dict[str, Any], *, scope: str, set_by: str,
        config: Any, overrides: Any,
    ) -> dict[str, Any]:
        owner = str(getattr(config, "catty_owner_qq", "") or "").strip()
        if owner in {"", "0"} or owner != str(set_by).strip():
            return {"ok": False, "error": "owner_only"}
        if not re.fullmatch(r"(?:group|private):[^:\s]+", scope):
            return {"ok": False, "error": "invalid_scope"}
        from .personas import PERSONAS, normalize_persona_name, resolve_persona_name
        action = args.get("action")
        async with self._lock:
            try:
                if action == "schema":
                    return {"ok": True, "schema": deepcopy(PERSONA_SCHEMA), "save_activates": False}
                if action == "list":
                    return {
                        "ok": True, "scope": scope,
                        "current": resolve_persona_name(scope, config=config, override_store=overrides),
                        "personas": [
                            {"name": name, "char_name": persona.char_name,
                             "builtin": name in {"catty", "fadianji"},
                             "revision": getattr(persona, "definition_revision", None)}
                            for name, persona in sorted(PERSONAS.items())
                        ],
                        "switch_commands": self.store.switch_commands(),
                    }
                if action == "save":
                    definition = args.get("definition")
                    if not isinstance(definition, dict):
                        return {"ok": False, "error": "definition_required"}
                    if args.get("name") and str(args["name"]).lower() != str(definition.get("name", "")).lower():
                        return {"ok": False, "error": "name_mismatch"}
                    result = await asyncio.to_thread(
                        self.store.save, definition,
                        overwrite=bool(args.get("overwrite", False)),
                        expected_sha256=args.get("expected_sha256"),
                    )
                    if result.get("ok"):
                        try:
                            self.publish()
                        except Exception as exc:
                            return {**result, "ok": False, "status": "saved_not_loaded", "error": type(exc).__name__, "loaded": False}
                        result = {**result, "loaded": True, "activated": False, "registry_updated": True}
                    return result
                if action == "reload":
                    result = await asyncio.to_thread(self.store.reload)
                    self.publish()
                    return {**result, "registry_updated": True}
                if action == "unbind":
                    result = await asyncio.to_thread(self.store.unbind, str(args.get("command") or ""))
                    if result.get("ok"):
                        self.publish()
                        result = {**result, "registry_updated": True}
                    return result
                if action == "reset":
                    changed = await asyncio.to_thread(overrides.clear, scope)
                    return {"ok": True, "status": "override_cleared", "changed": changed,
                            "scope": scope, "name": resolve_persona_name(scope, config=config, override_store=overrides),
                            "effective_after": "current_reply"}
                name = normalize_persona_name(str(args.get("name") or ""))
                if name is None:
                    return {"ok": False, "error": "unknown_persona", "name": str(args.get("name") or "")}
                if action == "show":
                    record = self.store.get(name)
                    return {"ok": True, "name": name, "builtin": record is None,
                            "char_name": PERSONAS[name].char_name, **(record or {})}
                if action == "bind":
                    result = await asyncio.to_thread(self.store.bind, str(args.get("command") or ""), name,
                                                     overwrite=bool(args.get("overwrite", False)))
                    if result.get("ok"):
                        self.publish()
                        result = {**result, "registry_updated": True}
                    return result
                if action == "switch":
                    await asyncio.to_thread(overrides.set, scope, name, set_by=set_by)
                    return {"ok": True, "status": "switch_saved", "scope": scope, "name": name,
                            "effective_after": "current_reply"}
                return {"ok": False, "error": "unknown_action"}
            except Exception as exc:
                return {"ok": False, "status": "error", "error": type(exc).__name__}