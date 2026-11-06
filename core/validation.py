"""Deterministic integrity checks for recorder output."""

from __future__ import annotations

from collections import Counter
from typing import Any


def validate_recording(
    raw_actions: list[dict[str, Any]],
    semantic_actions: list[dict[str, Any]],
    action_step_ids: list[str],
    schemas: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    schemas = schemas or []

    semantic_ids = [action.get("semantic_action_id") for action in semantic_actions]
    expected_sequences = list(range(1, len(semantic_actions) + 1))
    sequences = [action.get("semantic_sequence") for action in semantic_actions]
    if len(semantic_actions) != len(action_step_ids):
        errors.append({
            "code": "semantic_step_count_mismatch",
            "semantic_actions": len(semantic_actions),
            "action_steps": len(action_step_ids),
        })
    if None in semantic_ids or len(set(semantic_ids)) != len(semantic_ids):
        errors.append({"code": "invalid_or_duplicate_semantic_action_id"})
    if len(set(action_step_ids)) != len(action_step_ids):
        errors.append({"code": "duplicate_step_id"})
    if sequences != expected_sequences:
        errors.append({"code": "non_contiguous_semantic_sequence", "actual": sequences})

    raw_ids = {action.get("raw_action_id") for action in raw_actions}
    raw_sequences = [action.get("raw_sequence") for action in raw_actions]
    if raw_sequences != list(range(1, len(raw_actions) + 1)):
        errors.append({"code": "non_contiguous_raw_sequence", "actual": raw_sequences})
    if None in raw_ids or len(raw_ids) != len(raw_actions):
        errors.append({"code": "invalid_or_duplicate_raw_action_id"})
    known_widget_keys = {
        widget.get("widget_key") for schema in schemas for widget in schema.get("widgets", [])
        if widget.get("widget_key")
    }
    raw_by_id = {raw.get("raw_action_id"): raw for raw in raw_actions}
    ownership: Counter[str] = Counter(
        raw_id for action in semantic_actions for raw_id in action.get("raw_action_ids", [])
    )
    shared_violations = sorted(
        raw_id for raw_id, count in ownership.items()
        if count > 1 and not (raw_by_id.get(raw_id) or {}).get("shared")
    )
    if shared_violations:
        errors.append({"code": "raw_action_assigned_multiple_times", "raw_action_ids": shared_violations})
    for action in semantic_actions:
        missing = [raw_id for raw_id in action.get("raw_action_ids", []) if raw_id not in raw_ids]
        if missing:
            errors.append({
                "code": "unresolved_raw_action_reference",
                "semantic_action_id": action.get("semantic_action_id"),
                "raw_action_ids": missing,
            })
        if action.get("type") == "SELECT_OPTION" and action.get("outcome") not in {
            "success", "failed", "unverified"
        }:
            errors.append({
                "code": "select_missing_outcome",
                "semantic_action_id": action.get("semantic_action_id"),
            })
        if action.get("type") == "SELECT_OPTION" and not (
            action.get("option_label") or action.get("option_value") is not None
        ):
            errors.append({
                "code": "select_option_missing_option_identity",
                "semantic_action_id": action.get("semantic_action_id"),
            })
        if action.get("type") == "SELECT_OPTION" and action.get("outcome") == "success":
            verification = action.get("verification") or {}
            if not verification.get("method") or verification.get("actual") in (None, [], ""):
                errors.append({
                    "code": "unverified_select_marked_success",
                    "semantic_action_id": action.get("semantic_action_id"),
                })
            expected_values = {
                str(value).strip().casefold() for value in (
                    action.get("option_label"), action.get("option_value")
                ) if value is not None
            }
            actual_values: set[str] = set()
            for value in verification.get("actual") or []:
                if isinstance(value, dict):
                    actual_values.update(
                        str(candidate).strip().casefold()
                        for candidate in (value.get("label"), value.get("value")) if candidate is not None
                    )
                else:
                    actual_values.add(str(value).strip().casefold())
            if not expected_values.intersection(actual_values):
                errors.append({
                    "code": "select_option_postcondition_failed",
                    "semantic_action_id": action.get("semantic_action_id"),
                })
        if action.get("type") in {"SELECT_OPTION", "FILL_FIELD", "SET_CHECKBOX", "SET_RADIO"}:
            expected_key = (
                f"widget:{action['widget_key']}" if action.get("widget_key")
                else f"field:{action['field_key']}" if action.get("field_key") else None
            )
            leaked = [
                raw_id for raw_id in action.get("raw_action_ids", [])
                if expected_key and (raw_by_id.get(raw_id) or {}).get("interaction_key") not in {None, expected_key}
            ]
            if leaked:
                errors.append({
                    "code": "raw_action_cross_interaction_leak",
                    "semantic_action_id": action.get("semantic_action_id"),
                    "raw_action_ids": leaked,
                })
        if (
            action.get("type") == "STATE_CHANGE"
            and action.get("change_kind") == "VALUE_CHANGED"
            and action.get("source") == "autofill_or_programmatic"
            and action.get("raw_action_ids")
        ):
            errors.append({
                "code": "widget_change_misattributed",
                "semantic_action_id": action.get("semantic_action_id"),
            })
        if action.get("type") == "FORM_TRANSITION":
            state = action.get("immediate_post_state") or {}
            before, after = state.get("before"), state.get("after")
            if not before or not after or before == after:
                errors.append({
                    "code": "false_form_transition",
                    "semantic_action_id": action.get("semantic_action_id"),
                })
        matcher_supported = {"FILL_FIELD", "SELECT_OPTION", "SET_CHECKBOX", "SET_RADIO", "UPLOAD_FILE"}
        if action.get("matcher_training_eligible") and (
            action.get("type") not in matcher_supported
            or action.get("outcome") != "success"
            or not (action.get("verification") or {}).get("method")
            or not (action.get("field_key") or action.get("widget_key"))
        ):
            errors.append({
                "code": "matcher_training_action_invalid",
                "semantic_action_id": action.get("semantic_action_id"),
            })
        if action.get("widget_key") and known_widget_keys and action.get("widget_key") not in known_widget_keys:
            errors.append({
                "code": "unresolved_widget_reference",
                "semantic_action_id": action.get("semantic_action_id"),
                "widget_key": action.get("widget_key"),
            })

    for schema in schemas:
        stable = schema.get("stable_post_state") or {}
        if stable.get("may_include_later_ui_state") and schema.get("snapshot_authority") != "immediate_event_state":
            errors.append({
                "code": "future_state_without_action_owned_snapshot",
                "step_id": schema.get("step_id"),
            })
        keys = [
            field.get("field_key") for field in schema.get("fields", [])
            if field.get("field_key") and field.get("classification") == "user_field"
            and not field.get("repeating_collection")
        ]
        duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
        if duplicates:
            errors.append({
                "code": "duplicate_user_field_keys",
                "step_id": schema.get("step_id"),
                "field_keys": duplicates,
            })
        for widget in schema.get("widgets", []):
            option_keys: list[str] = []
            invalid_rows: list[dict[str, Any]] = []
            for option in widget.get("available_options", []):
                label = str(option.get("label") or "").strip()
                value = option.get("value")
                if not label:
                    invalid_rows.append(option)
                    continue
                option_keys.append(
                    f"value:{value}" if value is not None else f"label:{' '.join(label.split()).casefold()}"
                )
            duplicate_options = sorted(key for key, count in Counter(option_keys).items() if count > 1)
            if duplicate_options:
                errors.append({
                    "code": "duplicate_available_option",
                    "widget_key": widget.get("widget_key"), "option_keys": duplicate_options,
                })
            if invalid_rows:
                errors.append({
                    "code": "invalid_available_option_row",
                    "widget_key": widget.get("widget_key"), "count": len(invalid_rows),
                })

    successful = sum(action.get("outcome") == "success" for action in semantic_actions)
    unverified = sum(action.get("outcome") == "unverified" for action in semantic_actions)
    if unverified:
        warnings.append({"code": "unverified_actions", "count": unverified})
    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "raw_action_count": len(raw_actions),
            "semantic_action_count": len(semantic_actions),
            "action_step_count": len(action_step_ids),
            "verified_success_count": successful,
        },
    }
