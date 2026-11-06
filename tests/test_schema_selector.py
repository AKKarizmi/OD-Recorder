import unittest
from collections import Counter

from core.recorder import Recorder, WAIT_FOR_STABLE_DOM_SCRIPT, normalize_category
from core.schema_extractor import (
    SCHEMA_VERSION,
    _validate_selector_sets,
    ensure_unique_field_keys,
    extract_form_schema,
    generate_multi_strategy_selector,
    get_field_identity,
    normalize_widget_options,
)
from core.validation import validate_recording
from core.session_manager import SessionState
from core.utils import is_sensitive_field, safe_field_value


class FakeFrame:
    parent_frame = None

    def __init__(self, result, selector_counts=None):
        self.result = result
        self.selector_counts = selector_counts
        self.arguments = []

    def evaluate(self, script, argument=None):
        self.arguments.append(argument)
        if script == "() => Date.now()":
            return 1234
        if isinstance(argument, list):
            return self.selector_counts or [1] * len(argument)
        return self.result


class FakePage(FakeFrame):
    def __init__(self, result, selector_counts=None):
        super().__init__(result, selector_counts)
        self.frames = [self]


def make_recorder(fields=None):
    recorder = Recorder.__new__(Recorder)
    recorder.actions = []
    recorder.last_schema = {"fields": fields or []}
    recorder.session_state = None
    recorder._ensure_action_state()
    return recorder


def field_hint(*, xpath, name=None, label=None, input_type="text", form_id="registration"):
    return {
        "tag": "INPUT", "inputType": input_type, "id": None, "name": name,
        "label": label, "xpath": xpath,
        "form": {"id": form_id, "name": None, "action": "/register"},
    }


def widget_hint(label, widget_key, xpath):
    field = field_hint(xpath=xpath, label=label)
    field.update({"widget_kind": "multiselect", "widget_key": widget_key, "widget_label": label})
    return field


def select_widget(recorder, *, label, widget_key, option, start, value=None):
    field = widget_hint(label, widget_key, f"/form/{label.casefold().replace(' ', '_')}/input")
    selected = {
        "label": option, "value": value, "value_source": "data-value" if value is not None else "unavailable",
        "widget_key": widget_key, "widget_label": label, "controller": field,
        "scoped_selector": {"widget_key": widget_key, "option_label": option},
    }
    recorder._on_event({"type": "pointerdown", "raw_only": True, "timestamp": start, "field": field})
    recorder._on_event({"type": "focus", "timestamp": start + 1, "field": field})
    recorder._on_event({"type": "pointerdown", "raw_only": True, "timestamp": start + 2,
                        "field": field, "selected_option": selected})
    recorder._on_event({
        "type": "click", "timestamp": start + 3, "field": field, "selected_option": selected,
        "immediate_post_state": {
            "kind": "multiselect",
            "selected_values": [{"label": option, "value": value, "value_source": selected["value_source"]}],
            "semantic_value": [option], "value_present": True,
        },
    })
    return field


class SelectorGenerationTests(unittest.TestCase):
    def test_safe_id_creates_candidate_but_not_unvalidated_primary(self):
        selectors = generate_multi_strategy_selector({"tag": "input", "id": "email"})
        self.assertIn("#email", [candidate["value"] for candidate in selectors["candidates"]])
        self.assertIsNone(selectors["css"])
        self.assertIsNone(selectors["primary"])

    def test_generated_dropdown_id_is_rejected(self):
        selectors = generate_multi_strategy_selector({
            "tag": "span", "id": "null-0", "xpath": '//*[@id="null-0"]',
        })
        self.assertNotIn("#null-0", [candidate["value"] for candidate in selectors["candidates"]])

    def test_unsafe_id_does_not_create_invalid_css_selector(self):
        selectors = generate_multi_strategy_selector({"tag": "input", "id": "field:1"})
        self.assertNotEqual(selectors["css"], "#field:1")

    def test_only_unique_selector_becomes_primary(self):
        record = {"selectors": {"primary": None, "candidates": [
            {"type": "css", "value": "button.text-left", "validated": False},
            {"type": "xpath", "value": "/html/body/button[3]", "validated": False},
        ]}}
        _validate_selector_sets(FakeFrame({}, selector_counts=[4, 1]), [record])
        self.assertFalse(record["selectors"]["candidates"][0]["validated"])
        self.assertEqual(record["selectors"]["candidates"][0]["match_count"], 4)
        self.assertEqual(record["selectors"]["primary"], "/html/body/button[3]")

    def test_schema_version_marks_semantic_action_format(self):
        self.assertEqual(SCHEMA_VERSION, 7)

    def test_extract_schema_keeps_document_content_outside_form_scope(self):
        page = FakePage({
            "fields": [], "widgets": [],
            "scope": {"type": "form", "formId": "registration"},
            "pageContent": {
                "root": "main", "title": "Applicant profile",
                "records": [{
                    "content_key": "main|heading|#profile", "kind": "heading",
                    "text": "Applicant profile", "tag": "h1", "visible": True,
                    "selector": "#profile", "xpath": "//*[@id=\"profile\"]",
                    "frame_path": "main", "section": None,
                }],
            },
        })
        schema = extract_form_schema(
            page, "initial_load", "https://example.test/profile",
            active_scope={"formId": "registration"}, prefer_form_scope=True,
        )
        self.assertEqual(schema["scope"]["formId"], "registration")
        self.assertEqual(schema["page_content"]["title"], "Applicant profile")
        self.assertEqual(schema["page_content"]["records"][0]["kind"], "heading")
        self.assertTrue(schema["page_content"]["fingerprint"].startswith("sha256:"))

    def test_button_text_is_a_relative_selector_fallback(self):
        selectors = generate_multi_strategy_selector({
            "tag": "button", "label": 'Create "account"',
            "xpath": "/html/body/div[4]/button[1]",
        })
        text_candidate = next(item for item in selectors["candidates"] if item["strategy"] == "visible_text")
        self.assertNotIn("/html/body", text_candidate["value"])

    def test_extract_schema_keeps_compact_widget_state_and_scope(self):
        page = FakePage({
            "fields": [{
                "type": "control", "tag": "input", "inputType": "text",
                "id": None, "name": "language", "label": "Language",
                "xpath": "/html/body/form[1]/input[1]", "visible": True,
                "disabled": False, "readonly": False, "required": True,
                "attributes": {"name": "language"},
                "form": {"formId": "registration", "formName": {"node": True}},
            }],
            "widgets": [{
                "widget_key": "registration|multiselect|name:language|0",
                "controller_xpath": "/html/body/form[1]/input[1]",
                "selected_labels": ["English"], "expanded": False,
            }],
            "scope": {"type": "form", "formId": "registration"},
        })
        schema = extract_form_schema(
            page, "select_language", "https://example.test/register",
            debug_capture_values=True, capture_sensitive_values=True,
            active_scope={"formId": "registration"},
        )
        self.assertNotIn("dynamic_options", schema)
        self.assertEqual(schema["widgets"][0]["selected_labels"], ["English"])
        self.assertEqual(schema["widgets"][0]["controller_field_key"], schema["fields"][0]["field_key"])
        self.assertEqual(schema["scope"]["formId"], "registration")
        self.assertIsNone(schema["fields"][0]["form"]["formName"])
        self.assertEqual(page.arguments[0]["activeScope"]["formId"], "registration")
        self.assertTrue(page.arguments[0]["captureSensitiveValues"])

    def test_recorder_waits_for_a_quiet_dom_window(self):
        expected = {"stable": True, "timed_out": False, "mutation_count": 2}
        page = FakePage(expected)
        recorder = Recorder.__new__(Recorder)
        recorder.page = page
        self.assertEqual(recorder._wait_for_stable_dom(), expected)
        self.assertEqual(page.arguments[0]["quietMs"], 600)
        self.assertIn("MutationObserver", WAIT_FOR_STABLE_DOM_SCRIPT)


class LogicalActionTests(unittest.TestCase):
    def test_many_input_events_become_one_fill_action(self):
        xpath = "/html/body/form[1]/input[1]"
        recorder = make_recorder([{"xpath": xpath, "field_key": "registration.email"}])
        field = field_hint(xpath=xpath, name="email", label="Email")
        recorder._on_event({"type": "focus", "field": field, "timestamp": 1})
        for timestamp in range(2, 24):
            recorder._on_event({"type": "input", "field": field, "inputType": "email",
                                "timestamp": timestamp, "value": "must-not-persist"})
        recorder._on_event({"type": "change", "field": field, "inputType": "email",
                            "timestamp": 24, "value_fingerprint": "fnv1a32:abc:22"})
        self.assertEqual(len(recorder.actions), 1)
        self.assertEqual(recorder.actions[0]["type"], "FILL_FIELD")
        self.assertEqual(recorder.actions[0]["field_key"], "registration.email")
        self.assertEqual(recorder.actions[0]["value"], "must-not-persist")
        self.assertGreater(len(recorder.actions[0]["raw_action_ids"]), 20)
        self.assertTrue(all(raw["semantic_action_id"] == recorder.actions[0]["semantic_action_id"] for raw in recorder.raw_actions))

    def test_password_and_hidden_security_values_are_not_persisted(self):
        recorder = make_recorder()
        for name, input_type, value in (
            ("password", "password", "secret-value"),
            ("_token", "hidden", "csrf-value"),
        ):
            field = field_hint(xpath=f"/form/{name}", name=name, label=name, input_type=input_type)
            recorder._on_event({"type": "focus", "field": field, "timestamp": 1})
            recorder._on_event({"type": "input", "field": field, "inputType": input_type,
                                "timestamp": 2, "value": value})
            recorder._on_event({"type": "blur", "field": field, "inputType": input_type,
                                "timestamp": 3})

        self.assertEqual(len(recorder.actions), 2)
        self.assertTrue(all("value" not in action for action in recorder.actions))
        self.assertTrue(all("value" not in raw for raw in recorder.raw_actions))

    def test_safe_value_policy_allows_normal_prefilled_values(self):
        field = field_hint(xpath="/form/name", name="full_name", label="Full name")
        self.assertFalse(is_sensitive_field(field))
        self.assertEqual(safe_field_value("Ada Lovelace", field), "Ada Lovelace")

    def test_prefilled_schema_values_update_session_form_data(self):
        recorder = make_recorder()
        recorder.session_state = SessionState("session", {}, "Registration")
        recorder._update_session_form_data([
            {"field_key": "registration.name", "value_present": True, "value": "Ada Lovelace",
             "inputType": "text", "name": "full_name"},
            {"field_key": "registration.password", "value_present": True, "value": "secret",
             "inputType": "password", "name": "password"},
        ])
        self.assertEqual(recorder.session_state.form_data["registration.name"]["value"], "Ada Lovelace")
        self.assertEqual(recorder.session_state.form_data["registration.password"], {
            "filled": True, "sensitive": True,
        })

    def test_recorded_existing_data_summary_keeps_only_safe_values(self):
        recorder = make_recorder()
        schema = {
            "fields": [
                {"field_key": "registration.name", "label": "Full name", "inputType": "text",
                 "name": "full_name", "visible": True, "value_present": True,
                 "value": "Ada Lovelace", "value_fingerprint": "fnv1a32:abc:12"},
                {"field_key": "registration.password", "label": "Password", "inputType": "password",
                 "name": "password", "visible": True, "value_present": True,
                 "value": "secret-pass", "value_fingerprint": "fnv1a32:def:11"},
            ],
            "widgets": [{"widget_key": "registration.country", "selected_labels": ["France"]}],
            "timestamp": 1700000000000,
        }

        existing = recorder._collect_existing_data(schema)
        self.assertEqual(existing["total_values"], 1)
        self.assertEqual(existing["values"][0]["field_key"], "registration.name")
        self.assertEqual(existing["values"][0]["value"], "Ada Lovelace")
        self.assertEqual(existing["widgets"][0]["widget_key"], "registration.country")
        self.assertFalse(any(item["field_key"] == "registration.password" for item in existing["values"]))

    def test_page_content_marks_initial_changed_added_and_removed_records(self):
        recorder = make_recorder()
        recorder.last_schema = {
            "url": "/profile",
            "page_content": {"records": [
                {"content_key": "name", "kind": "text", "text": "Ada"},
                {"content_key": "status", "kind": "status", "text": "Pending"},
            ]},
        }
        schema = {
            "url": "/profile", "timestamp": 20,
            "page_content": {"captured_at": 20, "records": [
                {"content_key": "name", "kind": "text", "text": "Ada Lovelace"},
                {"content_key": "country", "kind": "text", "text": "France"},
            ]},
        }
        recorder._annotate_page_content(schema)
        statuses = {item["content_key"]: item["change_status"] for item in schema["page_content"]["changes"]}
        self.assertEqual(statuses, {"name": "changed", "country": "added", "status": "removed"})
        changed = next(item for item in schema["page_content"]["changes"] if item["content_key"] == "name")
        self.assertEqual(changed["previous_text"], "Ada")

    def test_page_content_timeline_deduplicates_unchanged_snapshots(self):
        recorder = make_recorder()
        recorder._captured_schemas = [
            {"step": "initial_load", "change_type": "initial_load",
             "page_content": {"url": "/profile", "fingerprint": "same", "records": []}},
            {"step": "click", "change_type": "action",
             "page_content": {"url": "/profile", "fingerprint": "same", "records": []}},
            {"step": "dynamic", "change_type": "dynamic",
             "page_content": {"url": "/profile", "fingerprint": "changed", "records": []}},
        ]
        timeline = recorder._page_content_timeline()
        self.assertEqual([item["step"] for item in timeline], ["initial_load", "dynamic"])

    def test_checkbox_event_sequence_becomes_one_set_action(self):
        xpath = "/html/body/form[1]/input[2]"
        recorder = make_recorder([{"xpath": xpath, "field_key": "registration.consent"}])
        field = field_hint(xpath=xpath, name="consent", label="Consent", input_type="checkbox")
        for event_type in ("click", "click", "input"):
            recorder._on_event({"type": event_type, "field": field, "tag": "INPUT",
                                "inputType": "checkbox", "timestamp": 10})
        recorder._on_event({"type": "change", "field": field, "tag": "INPUT",
                            "inputType": "checkbox", "checked": True, "timestamp": 11})
        self.assertEqual(len(recorder.actions), 1)
        self.assertEqual(recorder.actions[0]["type"], "SET_CHECKBOX")
        self.assertTrue(recorder.actions[0]["checked"])
        self.assertNotIn("checked", field)

    def test_custom_options_are_scoped_to_distinct_widgets(self):
        recorder = make_recorder()
        language = field_hint(xpath="/form/input[1]", label="Language")
        citizenship = field_hint(xpath="/form/input[2]", label="Citizenship")
        for timestamp, controller, widget, option in (
            (10, language, "registration|multiselect|label:Language|0", "Russian"),
            (20, citizenship, "registration|multiselect|label:Citizenship|1", "Abkhazia"),
        ):
            recorder._on_event({"type": "click", "timestamp": timestamp, "selected_option": {
                "label": option, "value": None, "controller": controller, "widget_key": widget,
                "scoped_selector": {"widget_key": widget, "option_label": option},
            }})
        self.assertEqual([action["type"] for action in recorder.actions], ["SELECT_OPTION", "SELECT_OPTION"])
        self.assertNotEqual(recorder.actions[0]["widget_key"], recorder.actions[1]["widget_key"])
        self.assertNotIn("option_value", recorder.actions[0])
        self.assertEqual(recorder.actions[0]["option_value_source"], "unavailable")
        self.assertNotIn("xpath", recorder.actions[0]["scoped_selector"])

    def test_focus_assigns_provisional_key_before_first_character(self):
        recorder = make_recorder()
        field = field_hint(xpath="/form/input[1]", name="email", label="Email")
        recorder._on_event({"type": "focus", "field": field, "timestamp": 1})
        recorder._on_event({"type": "input", "field": field, "inputType": "email", "timestamp": 2})
        pending = next(iter(recorder._pending_inputs.values()))
        self.assertIn("name:email", pending["field_key"])

    def test_multiselect_search_typing_is_not_a_fill_action(self):
        recorder = make_recorder()
        field = field_hint(xpath="/form/div/input[1]", label="Language")
        field["widget_kind"] = "multiselect"
        recorder._on_event({"type": "input", "field": field, "inputType": "text", "timestamp": 1})
        recorder._on_event({"type": "blur", "field": field, "inputType": "text", "timestamp": 2})
        self.assertEqual(recorder.actions, [])

    def test_step_name_comes_from_logical_action(self):
        recorder = make_recorder()
        name = recorder._step_name("navigation", {
            "action": {"type": "SELECT_OPTION", "field": {"label": "Citizenship"}}
        })
        self.assertEqual(name, "select_citizenship")

    def test_category_typo_is_normalized(self):
        self.assertEqual(normalize_category("Registeration"), "Registration")

    def test_fast_selections_keep_action_owned_immediate_state(self):
        recorder = make_recorder()
        recorder._pending_action_ids = []
        for timestamp, label in ((10, "Russian"), (11, "English")):
            recorder._on_event({
                "type": "click", "timestamp": timestamp,
                "immediate_post_state": {"selected_values": [label], "value_present": True},
                "selected_option": {
                    "label": label, "value": label, "widget_key": "language",
                    "controller": field_hint(xpath="/form/input[1]", label="Language"),
                },
            })
        schemas = []
        for action in recorder.actions:
            schema = {"fields": [], "widgets": [{"widget_key": "language", "selected_labels": ["English"]}]}
            recorder._apply_action_result(schema, action)
            schemas.append(schema)
        self.assertEqual(schemas[0]["widgets"][0]["selected_labels"], ["Russian"])
        self.assertEqual(schemas[1]["widgets"][0]["selected_labels"], ["English"])
        self.assertEqual([action["outcome"] for action in recorder.actions], ["success", "success"])

    def test_failed_selection_is_not_marked_success(self):
        recorder = make_recorder()
        action = {"type": "SELECT_OPTION", "option_label": "Applied Mathematics", "immediate_post_state": {"selected_values": []}}
        recorder._apply_action_result({"fields": [], "widgets": []}, action)
        self.assertEqual(action["outcome"], "failed")

    def test_same_url_form_replacement_is_detected(self):
        before = {"url": "/register", "scope": {"formAction": "/register-email"}, "fields": [
            {"field_key": "email", "label": "Email", "classification": "user_field"}
        ]}
        after = {"url": "/register", "scope": {"formAction": "/register"}, "fields": [
            {"field_key": "name", "label": "First name", "classification": "user_field"},
            {"field_key": "citizenship", "label": "Citizenship", "classification": "user_field"},
        ]}
        self.assertEqual(Recorder.detect_state_transition(before, after), "FORM_TRANSITION")

    def test_password_autofill_records_only_safe_state(self):
        recorder = make_recorder([{
            "field_key": "password", "label": "Password", "inputType": "password",
            "classification": "user_field", "value_present": False,
        }])
        recorder.last_schema["url"] = "/register"
        recorder.last_schema["scope"] = {"type": "form", "formAction": "/register"}
        candidate = {
            "url": "/register", "timestamp": 42,
            "scope": {"type": "form", "formAction": "/register"},
            "fields": [{
                "field_key": "password", "label": "Password", "inputType": "password",
                "classification": "user_field", "value_present": True,
                "value_fingerprint": "must-not-survive",
            }],
        }
        recorder._record_implicit_changes(candidate, None)
        action = recorder.actions[0]
        self.assertEqual(action["change_kind"], "VALUE_CHANGED")
        self.assertEqual(action["source"], "autofill_or_programmatic")
        self.assertTrue(action["sensitive"])
        self.assertNotIn("value_fingerprint", action["immediate_post_state"])

    def test_value_only_page_change_is_detected(self):
        before = {"url": "/login", "fields": [{
            "tag": "input", "inputType": "text", "name": "email", "label": "E-mail",
            "xpath": "/form/input[1]", "value_present": False,
        }]}
        after = {"url": "/login", "fields": [{
            "tag": "input", "inputType": "text", "name": "email", "label": "E-mail",
            "xpath": "/form/input[1]", "value_present": True,
            "value": "globalaitest@gmail.com", "value_fingerprint": "fnv1a32:email:22",
        }]}
        self.assertNotEqual(Recorder._signature(before), Recorder._signature(after))

    def test_duplicate_placeholders_use_semantic_context(self):
        keys = {
            get_field_identity({
                "tag": "input", "inputType": "text", "label": label,
                "placeholder": "Select option", "xpath": f"/form/div[{index}]/input[1]",
                "form": {"formAction": "/register"},
            })
            for index, label in enumerate(("Language", "Citizenship", "Sex"), 1)
        }
        self.assertEqual(len(keys), 3)

    def test_native_select_options_are_preserved_as_complete(self):
        options = [
            {"value": value, "label": label, "selected": value == "en", "disabled": False}
            for value, label in (
                ("ru", "Русский"), ("en", "English"), ("fr", "Français"),
                ("es", "Español"), ("zh", "中文"), ("ar", "العربية"), ("hi", "हिन्दी"),
            )
        ]
        page = FakePage({"fields": [{
            "type": "control", "tag": "select", "inputType": "select-one",
            "name": "language", "label": "Language", "xpath": "/select[1]",
            "visible": True, "classification": "user_field",
            "available_options": options, "options_complete": True,
            "option_extraction_strategy": "native_dom", "attributes": {}, "form": {},
        }], "widgets": [], "scope": {"type": "document"}})
        schema = extract_form_schema(page, "native", "/register")
        self.assertEqual(schema["fields"][0]["available_options"], options)
        self.assertTrue(schema["fields"][0]["options_complete"])

    def test_custom_widget_is_a_first_class_semantic_field(self):
        page = FakePage({
            "fields": [{
                "type": "control", "tag": "input", "inputType": "text", "name": None,
                "label": "Bachelor’s track", "label_source": "component_heading",
                "label_confidence": 0.9, "xpath": "/form/div[1]/input[1]", "visible": True,
                "value_present": False, "attributes": {"placeholder": "Add Subject area"}, "form": {},
            }],
            "widgets": [{
                "widget_key": "/register|multiselect|label:Bachelor’s track|0",
                "controller_xpath": "/form/div[1]/input[1]", "label": "Bachelor’s track",
                "selected_labels": ["Computer and Data Science"],
                "selected_values": [{"label": "Computer and Data Science", "value": "CS"}],
                "semantic_value": ["Computer and Data Science"], "available_options": [],
                "options_complete": "unknown", "option_extraction_strategy": "unknown",
            }], "scope": {"type": "form", "formAction": "/register"},
        })
        schema = extract_form_schema(page, "custom", "/register")
        widget_field = next(field for field in schema["fields"] if field.get("type") == "widget_reference")
        controller = next(field for field in schema["fields"] if field.get("tag") == "input")
        self.assertEqual(widget_field["semantic_label"], "Bachelor’s track")
        self.assertEqual(widget_field["semantic_value"], ["Computer and Data Science"])
        self.assertNotIn("available_options", widget_field)
        self.assertEqual(widget_field["available_options_ref"], widget_field["widget_key"])
        self.assertEqual(controller["classification"], "widget_controller")


class IntegrityTests(unittest.TestCase):
    def test_colliding_field_keys_are_resolved(self):
        fields = [
            {"field_key": "same", "label": "Previous", "xpath": "/button[1]"},
            {"field_key": "same", "label": "Next", "xpath": "/button[2]"},
        ]
        ensure_unique_field_keys(fields)
        self.assertEqual(len({field["field_key"] for field in fields}), 2)

    def test_validation_enforces_one_step_per_semantic_action(self):
        raw = [{"raw_action_id": "raw_000001", "raw_sequence": 1}]
        semantic = [{
            "semantic_action_id": "action_000001", "semantic_sequence": 1,
            "step_id": "step_000001", "raw_action_ids": ["raw_000001"],
            "type": "FILL_FIELD", "outcome": "success",
        }]
        self.assertTrue(validate_recording(raw, semantic, ["step_000001"])["passed"])
        self.assertFalse(validate_recording(raw, semantic, [])["passed"])

    def test_validation_detects_cross_interaction_leak_and_duplicate_options(self):
        raw = [{
            "raw_action_id": "raw_000001", "raw_sequence": 1,
            "interaction_key": "widget:citizenship",
        }]
        semantic = [{
            "semantic_action_id": "action_000001", "semantic_sequence": 1,
            "step_id": "step_000001", "type": "SET_CHECKBOX", "field_key": "privacy",
            "raw_action_ids": ["raw_000001"], "outcome": "success",
            "verification": {"method": "checked_state", "actual": True},
        }]
        schemas = [{"step_id": "step_000001", "widgets": [{
            "widget_key": "citizenship", "available_options": [
                {"label": "English", "value": None}, {"label": " English ", "value": None},
            ],
        }], "fields": []}]
        result = validate_recording(raw, semantic, ["step_000001"], schemas)
        codes = {error["code"] for error in result["errors"]}
        self.assertIn("raw_action_cross_interaction_leak", codes)
        self.assertIn("duplicate_available_option", codes)


class RemainingSemanticRegressionTests(unittest.TestCase):
    def test_citizenship_raw_events_do_not_leak_into_checkbox(self):
        recorder = make_recorder()
        select_widget(
            recorder, label="Citizenship", widget_key="register|multiselect|Citizenship|1",
            option="Afghanistan", start=10,
        )
        checkbox = field_hint(xpath="/form/privacy", name="privacy", label="Privacy", input_type="checkbox")
        recorder._on_event({"type": "pointerdown", "raw_only": True, "timestamp": 20, "field": checkbox})
        recorder._on_event({"type": "click", "timestamp": 21, "field": checkbox,
                            "tag": "INPUT", "inputType": "checkbox"})
        recorder._on_event({"type": "change", "timestamp": 22, "field": checkbox,
                            "tag": "INPUT", "inputType": "checkbox", "checked": True,
                            "immediate_post_state": {"checked": True}})
        self.assertEqual([action["type"] for action in recorder.actions], ["SELECT_OPTION", "SET_CHECKBOX"])
        citizenship_ids, checkbox_ids = (set(action["raw_action_ids"]) for action in recorder.actions)
        self.assertFalse(citizenship_ids & checkbox_ids)
        self.assertTrue(all(
            (raw.get("interaction_key") or "").startswith("widget:")
            for raw in recorder.raw_actions if raw["raw_action_id"] in citizenship_ids
        ))

    def test_sex_selection_is_human_and_verified(self):
        recorder = make_recorder()
        widget_key = "register|multiselect|Sex|2"
        select_widget(recorder, label="Sex", widget_key=widget_key, option="Male", start=10)
        action = recorder.actions[0]
        schema = {
            "timestamp": 20,
            "fields": [{"type": "widget_reference", "field_key": widget_key,
                        "widget_key": widget_key, "classification": "user_field"}],
            "widgets": [{"widget_key": widget_key, "selected_labels": ["Male"]}],
        }
        recorder._apply_action_result(schema, action)
        self.assertEqual(action["type"], "SELECT_OPTION")
        self.assertEqual(action["source"], "human_interaction")
        self.assertEqual(action["outcome"], "success")
        self.assertTrue(action["matcher_training_eligible"])

    def test_widget_state_diff_uses_recent_human_option_evidence(self):
        widget_key = "register|multiselect|Sex|2"
        before_field = {
            "type": "widget_reference", "field_key": widget_key, "widget_key": widget_key,
            "label": "Sex", "semantic_label": "Sex", "classification": "user_field",
            "value_present": False, "semantic_value": [], "selected_values": [],
        }
        recorder = make_recorder([before_field])
        recorder.last_schema.update({"url": "/register", "scope": {"formAction": "/register"}})
        controller = widget_hint("Sex", widget_key, "/form/sex/input")
        selected = {
            "label": "Male", "value": None, "value_source": "unavailable",
            "widget_key": widget_key, "widget_label": "Sex", "controller": controller,
        }
        recorder._on_event({"type": "pointerdown", "raw_only": True, "timestamp": 10,
                            "field": controller, "selected_option": selected})
        candidate_field = {
            **before_field, "value_present": True, "semantic_value": ["Male"],
            "selected_values": [{"label": "Male", "value": None, "value_source": "unavailable"}],
        }
        candidate = {"url": "/register", "timestamp": 11,
                     "scope": {"formAction": "/register"}, "fields": [candidate_field]}
        recorder._record_implicit_changes(candidate, None)
        self.assertEqual(len(recorder.actions), 1)
        self.assertEqual(recorder.actions[0]["type"], "SELECT_OPTION")
        self.assertEqual(recorder.actions[0]["source"], "human_interaction")
        self.assertEqual(recorder.actions[0]["option_label"], "Male")

    def test_three_custom_selects_have_disjoint_interactions(self):
        recorder = make_recorder()
        selections = (
            ("Citizenship", "Afghanistan"),
            ("Sex", "Male"),
            ("Bachelor’s track", "Computer and Data Science"),
        )
        for index, (label, option) in enumerate(selections):
            select_widget(recorder, label=label, widget_key=f"widget-{index}", option=option, start=10 + index * 10)
        self.assertEqual(len(recorder.actions), 3)
        self.assertEqual([action["option_label"] for action in recorder.actions], [item[1] for item in selections])
        raw_sets = [set(action["raw_action_ids"]) for action in recorder.actions]
        self.assertTrue(all(not raw_sets[left] & raw_sets[right]
                            for left in range(3) for right in range(left + 1, 3)))
        self.assertEqual(len({action["parent_interaction_id"] for action in recorder.actions}), 3)

    def test_option_raw_event_keeps_identity_and_value_source(self):
        recorder = make_recorder()
        select_widget(
            recorder, label="Bachelor’s track", widget_key="bachelor", option="Computer and Data Science",
            value="CS", start=10,
        )
        pointer_option = next(raw for raw in recorder.raw_actions
                              if raw["type"] == "pointerdown" and raw.get("selected_option"))
        option = pointer_option["selected_option"]
        self.assertEqual(option["widget_label"], "Bachelor’s track")
        self.assertEqual(option["label"], "Computer and Data Science")
        self.assertEqual(option["value"], "CS")
        self.assertEqual(option["value_source"], "data-value")

    def test_pointerdown_option_then_blur_finalizes_select_without_click(self):
        recorder = make_recorder()
        widget_key = "register|multiselect|Citizenship|1"
        controller = widget_hint("Citizenship", widget_key, "/form/citizenship/input")
        selected = {
            "label": "Afghanistan", "value": "AF", "value_source": "data-value",
            "widget_key": widget_key, "widget_label": "Citizenship", "controller": controller,
        }
        recorder._on_event({"type": "pointerdown", "raw_only": True, "timestamp": 10,
                            "field": controller, "selected_option": selected})
        recorder._on_event({
            "type": "blur", "timestamp": 11, "field": controller,
            "immediate_post_state": {
                "selected_values": [{"label": "Afghanistan", "value": "AF"}],
                "semantic_value": ["Afghanistan"], "value_present": True,
            },
        })
        self.assertEqual(len(recorder.actions), 1)
        self.assertEqual(recorder.actions[0]["type"], "SELECT_OPTION")
        self.assertEqual(recorder.actions[0]["option_label"], "Afghanistan")
        self.assertEqual(set(recorder.actions[0]["raw_action_ids"]), {"raw_000001", "raw_000002"})

    def test_stable_widget_state_verifies_selection_when_blur_state_is_early(self):
        recorder = make_recorder()
        widget_key = "register|multiselect|Language|0"
        action = {
            "type": "SELECT_OPTION", "widget_key": widget_key, "option_label": "Russian",
            "field_key": widget_key, "field": {"classification": "user_field"},
            "raw_action_ids": [], "event_occurred_at": 10,
            "immediate_post_state": {"selected_values": [], "semantic_value": [], "value_present": False},
        }
        schema = {
            "timestamp": 11,
            "fields": [{"type": "widget_reference", "field_key": widget_key,
                        "classification": "user_field"}],
            "widgets": [{
                "widget_key": widget_key,
                "selected_values": [{"label": "Russian", "value": None}],
                "selected_labels": ["Russian"], "semantic_value": ["Russian"],
            }],
        }
        recorder._apply_action_result(schema, action)
        self.assertEqual(action["outcome"], "success")
        self.assertEqual(action["verification"]["method"], "stable_widget_selected_values")
        self.assertEqual(action["after"]["semantic_value"], ["Russian"])
        self.assertTrue(action["matcher_training_eligible"])
        self.assertIsInstance(recorder._signature(schema), Counter)

    def test_widget_open_temporary_empty_state_does_not_create_select(self):
        widget_key = "register|multiselect|Language|0"
        before = {
            "type": "widget_reference", "field_key": widget_key, "widget_key": widget_key,
            "label": "Language", "semantic_label": "Language", "classification": "user_field",
            "value_present": True, "semantic_value": ["English"],
            "selected_values": [{"label": "English", "value": None}],
        }
        recorder = make_recorder([before])
        recorder.last_schema.update({"url": "/register", "scope": {"formAction": "/register"}})
        controller = widget_hint("Language", widget_key, "/form/language/input")
        recorder._on_event({"type": "pointerdown", "raw_only": True, "timestamp": 10, "field": controller})
        candidate = {
            "url": "/register", "timestamp": 11, "scope": {"formAction": "/register"},
            "fields": [{**before, "value_present": False, "semantic_value": [], "selected_values": []}],
        }
        recorder._record_implicit_changes(candidate, None)
        self.assertEqual(recorder.actions, [])

    def test_open_and_close_widget_creates_no_value_action_or_transition(self):
        recorder = make_recorder()
        field = widget_hint("Citizenship", "citizenship", "/form/citizenship/input")
        recorder._on_event({"type": "pointerdown", "raw_only": True, "timestamp": 1, "field": field})
        recorder._on_event({"type": "focus", "timestamp": 2, "field": field})
        recorder._on_event({"type": "blur", "timestamp": 3, "field": field})
        self.assertEqual(recorder.actions, [])

    def test_scope_change_alone_is_not_form_transition(self):
        fields = [{
            "type": "widget_reference", "field_key": "sex", "label": "Sex",
            "classification": "user_field", "visible": True,
            "form": {"formId": "registration", "formAction": "/register"},
        }]
        before = {"url": "/register", "scope": {"type": "document"}, "fields": fields}
        after = {"url": "/register", "scope": {"type": "form", "formId": "registration"}, "fields": fields}
        self.assertIsNone(Recorder.detect_state_transition(before, after))

    def test_real_transition_follows_submit_in_semantic_order(self):
        recorder = make_recorder([{
            "type": "control", "field_key": "email", "label": "Email",
            "classification": "user_field", "visible": True,
            "form": {"formAction": "/register-email"},
        }])
        recorder.last_schema.update({"url": "/register", "scope": {"formAction": "/register-email"}})
        recorder._record_logical_action({
            "type": "SUBMIT_FORM", "form": {"action": "/register-email"},
            "event_occurred_at": 10, "raw_action_ids": [],
        }, {"formAction": "/register-email"})
        candidate = {
            "url": "/register", "timestamp": 11, "scope": {"formAction": "/register"},
            "fields": [
                {"type": "control", "field_key": "name", "label": "Name",
                 "classification": "user_field", "visible": True, "form": {"formAction": "/register"}},
                {"type": "widget_reference", "field_key": "citizenship", "label": "Citizenship",
                 "classification": "user_field", "visible": True, "form": {"formAction": "/register"}},
            ],
        }
        recorder._record_implicit_changes(candidate, recorder.actions[0])
        self.assertEqual([action["type"] for action in recorder.actions], ["SUBMIT_FORM", "FORM_TRANSITION"])
        self.assertEqual([action["semantic_sequence"] for action in recorder.actions], [1, 2])

    def test_async_submit_state_before_next_human_event_is_not_future_state(self):
        recorder = make_recorder()
        recorder.raw_actions = [
            {"raw_action_id": "raw_000001", "raw_sequence": 1, "type": "submit", "timestamp": 100},
            {"raw_action_id": "raw_000002", "raw_sequence": 2, "type": "pointerdown", "timestamp": 200},
        ]
        action = {"type": "SUBMIT_FORM", "event_occurred_at": 100,
                  "raw_action_ids": ["raw_000001"], "immediate_post_state": {}}
        schema = {"timestamp": 150, "fields": [], "widgets": []}
        recorder._apply_action_result(schema, action)
        self.assertFalse(schema["stable_post_state"]["may_include_later_ui_state"])
        self.assertTrue(schema["stable_post_state"]["captured"])

    def test_widget_options_deduplicate_and_remove_status_rows(self):
        widget = {"available_options": [
            {"label": "English", "value": None, "selected": False},
            {"label": " English ", "value": None, "selected": True},
            {"label": "No elements found", "selectable": False},
        ]}
        normalize_widget_options(widget)
        self.assertEqual(widget["available_options"], [{
            "label": "English", "value": None, "value_source": "unavailable",
            "disabled": False, "selected": True,
        }])
        self.assertEqual(widget["excluded_status_row_count"], 1)

    def test_transition_is_page_state_only_for_training(self):
        recorder = make_recorder()
        action = {
            "type": "FORM_TRANSITION", "change_kind": "FORM_TRANSITION",
            "field": {}, "raw_action_ids": [], "event_occurred_at": 10,
            "immediate_post_state": {"before": ["email"], "after": ["registration"]},
        }
        recorder._apply_action_result({"timestamp": 11, "fields": [], "widgets": []}, action)
        self.assertFalse(action["matcher_training_eligible"])
        self.assertTrue(action["page_state_training_eligible"])


if __name__ == "__main__":
    unittest.main()
