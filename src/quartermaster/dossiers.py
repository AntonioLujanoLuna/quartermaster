"""Versioned, read-only character dossier snapshots.

Quartermaster stores a snapshot for orientation and explanation. It does not
turn the snapshot into a rules engine or use stale data to authorize a mechanic.
The first supported source is an explicit DM manual import; a future provider
adapter can replace the source without changing the Activity read contract.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from .clock import iso_now
from .db import SQLiteStore
from .events import append_event, session_event_destination
from .receipts import ReceiptRepository, ReceiptResult

DOSSIER_SOURCES = ("MANUAL_IMPORT",)
DOSSIER_FRESHNESS = ("CURRENT", "STALE")
_JSON_FIELD_LIMIT = 40


class DossierError(RuntimeError):
    """Raised for an invalid or unavailable character dossier."""


def _bounded_mapping(value: object, *, field: str, values: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DossierError(f"{field} must be an object")
    if len(value) > _JSON_FIELD_LIMIT:
        raise DossierError(f"{field} has too many entries")
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        if not key or len(key) > 80:
            raise DossierError(f"{field} contains an invalid key")
        if values == "int":
            if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                raise DossierError(f"{field}.{key} must be a whole number")
            if abs(raw_value) > 1000:
                raise DossierError(f"{field}.{key} is outside the supported range")
            result[key] = raw_value
        else:
            text = str(raw_value).strip()
            if not text or len(text) > 200:
                raise DossierError(f"{field}.{key} must be a short non-empty value")
            result[key] = text
    return result


def _optional_int(
    value: object,
    *,
    field: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DossierError(f"{field} must be a whole number")
    if minimum is not None and value < minimum:
        raise DossierError(f"{field} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise DossierError(f"{field} must be at most {maximum}")
    return value


def _text(value: object, *, field: str, maximum: int, required: bool = True) -> str | None:
    text = "" if value is None else str(value).strip()
    if not text and not required:
        return None
    if not text:
        raise DossierError(f"{field} is required")
    if len(text) > maximum:
        raise DossierError(f"{field} is too long")
    return text


class CharacterDossierService:
    def __init__(self, store: SQLiteStore, receipts: ReceiptRepository) -> None:
        self.store = store
        self.receipts = receipts

    def read_for_actor(self, actor_id: str) -> dict[str, Any]:
        with self.store.read() as connection:
            character = connection.execute(
                """SELECT id, name, discord_user_id, lifecycle
                     FROM characters
                    WHERE discord_user_id = ? AND lifecycle = 'ACTIVE'
                    ORDER BY created_at, id LIMIT 1""",
                (actor_id,),
            ).fetchone()
            if character is None:
                return {
                    "status": "UNAVAILABLE",
                    "reason": "no active character is registered for this player",
                    "character": None,
                    "snapshot": None,
                }
            row = connection.execute(
                "SELECT * FROM character_dossiers WHERE character_id = ?", (character["id"],)
            ).fetchone()
        if row is None:
            return {
                "status": "UNAVAILABLE",
                "reason": "no verified character snapshot has been imported",
                "character": dict(character),
                "snapshot": None,
            }
        snapshot = self._row_snapshot(row)
        return {
            "status": str(row["source_freshness"]),
            "reason": (
                "this snapshot was marked stale by its source"
                if row["source_freshness"] == "STALE"
                else "verified snapshot"
            ),
            "character": dict(character),
            "snapshot": snapshot,
        }

    def save_interaction(
        self,
        interaction_id: str,
        *,
        actor_id: str | None,
        character_id: str,
        snapshot: Mapping[str, Any],
    ) -> ReceiptResult:
        normalized = self._validate_snapshot(snapshot)
        return self.receipts.execute_fast(
            interaction_id,
            actor_id=actor_id,
            response_kind="character-dossier",
            mutation=lambda connection, operation_id: self._save_in_transaction(
                connection, operation_id, actor_id, character_id, normalized
            ),
        )

    def _validate_snapshot(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        source = _text(snapshot.get("source"), field="source", maximum=32)
        if source not in DOSSIER_SOURCES:
            raise DossierError("source must be MANUAL_IMPORT")
        freshness = _text(
            snapshot.get("source_freshness", "CURRENT"),
            field="source_freshness",
            maximum=16,
        )
        if freshness not in DOSSIER_FRESHNESS:
            raise DossierError("source_freshness must be CURRENT or STALE")
        observed_at = _text(snapshot.get("observed_at"), field="observed_at", maximum=64)
        return {
            "source": source,
            "source_reference": _text(
                snapshot.get("source_reference"),
                field="source_reference",
                maximum=200,
                required=False,
            ),
            "system": _text(snapshot.get("system"), field="system", maximum=80),
            "rules_version": _text(
                snapshot.get("rules_version"), field="rules_version", maximum=80
            ),
            "level": _optional_int(snapshot.get("level"), field="level", minimum=1, maximum=30),
            "proficiency_bonus": _optional_int(
                snapshot.get("proficiency_bonus"),
                field="proficiency_bonus",
                minimum=0,
                maximum=20,
            ),
            "ability_scores": _bounded_mapping(
                snapshot.get("ability_scores"), field="ability_scores", values="int"
            ),
            "ability_modifiers": _bounded_mapping(
                snapshot.get("ability_modifiers"), field="ability_modifiers", values="int"
            ),
            "armor_class": _optional_int(
                snapshot.get("armor_class"), field="armor_class", minimum=0, maximum=100
            ),
            "hit_points": _optional_int(
                snapshot.get("hit_points"), field="hit_points", minimum=0, maximum=1000
            ),
            "temporary_hit_points": _optional_int(
                snapshot.get("temporary_hit_points", 0),
                field="temporary_hit_points",
                minimum=0,
                maximum=1000,
            )
            or 0,
            "initiative": _optional_int(
                snapshot.get("initiative"), field="initiative", minimum=-100, maximum=100
            ),
            "saving_throws": _bounded_mapping(
                snapshot.get("saving_throws"), field="saving_throws", values="int"
            ),
            "spell_attack_modifier": _optional_int(
                snapshot.get("spell_attack_modifier"),
                field="spell_attack_modifier",
                minimum=-100,
                maximum=100,
            ),
            "spell_save_dc": _optional_int(
                snapshot.get("spell_save_dc"), field="spell_save_dc", minimum=0, maximum=100
            ),
            "spell_resources": _bounded_mapping(
                snapshot.get("spell_resources"), field="spell_resources", values="int"
            ),
            "equipped": _bounded_mapping(
                snapshot.get("equipped"), field="equipped", values="text"
            ),
            "observed_at": observed_at,
            "source_freshness": freshness,
        }

    def _save_in_transaction(
        self,
        connection: Any,
        operation_id: str,
        actor_id: str | None,
        character_id: str,
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        character = connection.execute(
            "SELECT id, name FROM characters WHERE id = ?", (character_id,)
        ).fetchone()
        if character is None:
            raise DossierError("character not found")
        now = iso_now()
        dossier_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO character_dossiers(
                id, character_id, snapshot_version, source, source_reference,
                system, rules_version, level, proficiency_bonus,
                ability_scores, ability_modifiers, armor_class, hit_points,
                temporary_hit_points, initiative, saving_throws,
                spell_attack_modifier, spell_save_dc, spell_resources, equipped,
                observed_at, source_freshness, created_at, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(character_id) DO UPDATE SET
                snapshot_version = character_dossiers.snapshot_version + 1,
                source = excluded.source,
                source_reference = excluded.source_reference,
                system = excluded.system,
                rules_version = excluded.rules_version,
                level = excluded.level,
                proficiency_bonus = excluded.proficiency_bonus,
                ability_scores = excluded.ability_scores,
                ability_modifiers = excluded.ability_modifiers,
                armor_class = excluded.armor_class,
                hit_points = excluded.hit_points,
                temporary_hit_points = excluded.temporary_hit_points,
                initiative = excluded.initiative,
                saving_throws = excluded.saving_throws,
                spell_attack_modifier = excluded.spell_attack_modifier,
                spell_save_dc = excluded.spell_save_dc,
                spell_resources = excluded.spell_resources,
                equipped = excluded.equipped,
                observed_at = excluded.observed_at,
                source_freshness = excluded.source_freshness,
                updated_at = excluded.updated_at""",
            (
                dossier_id,
                character_id,
                snapshot["source"],
                snapshot["source_reference"],
                snapshot["system"],
                snapshot["rules_version"],
                snapshot["level"],
                snapshot["proficiency_bonus"],
                json.dumps(snapshot["ability_scores"], sort_keys=True),
                json.dumps(snapshot["ability_modifiers"], sort_keys=True),
                snapshot["armor_class"],
                snapshot["hit_points"],
                snapshot["temporary_hit_points"],
                snapshot["initiative"],
                json.dumps(snapshot["saving_throws"], sort_keys=True),
                snapshot["spell_attack_modifier"],
                snapshot["spell_save_dc"],
                json.dumps(snapshot["spell_resources"], sort_keys=True),
                json.dumps(snapshot["equipped"], sort_keys=True),
                snapshot["observed_at"],
                snapshot["source_freshness"],
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT snapshot_version FROM character_dossiers WHERE character_id = ?", (character_id,)
        ).fetchone()
        append_event(
            connection,
            operation_id=operation_id,
            actor_id=actor_id,
            event_type="CHARACTER_DOSSIER_IMPORTED",
            payload={
                "character_id": character_id,
                "character_name": character["name"],
                "snapshot_version": int(row["snapshot_version"]),
                "source": snapshot["source"],
                "source_freshness": snapshot["source_freshness"],
            },
            destination=session_event_destination(connection),
        )
        return {
            "status": "IMPORTED",
            "character_id": character_id,
            "character_name": character["name"],
            "snapshot_version": int(row["snapshot_version"]),
            "source_freshness": snapshot["source_freshness"],
        }

    @staticmethod
    def _row_snapshot(row: Any) -> dict[str, Any]:
        json_fields = (
            "ability_scores",
            "ability_modifiers",
            "saving_throws",
            "spell_resources",
            "equipped",
        )
        # `row.keys()` rather than iterating the row: `in` on a sqlite3.Row
        # tests its values, not its column names, so SIM118's rewrite would
        # quietly stop stripping the columns this exists to strip.
        result = {
            key: row[key]
            for key in row.keys()  # noqa: SIM118
            if key not in {"id", "character_id", "created_at", "updated_at"}
        }
        for field in json_fields:
            result[field] = json.loads(row[field])
        return result


__all__ = ["CharacterDossierService", "DossierError"]
