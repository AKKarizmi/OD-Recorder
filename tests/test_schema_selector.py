import unittest

from core.recorder import Recorder
from core.schema_extractor import SCHEMA_VERSION, generate_multi_strategy_selector


class SelectorGenerationTests(unittest.TestCase):
    def test_safe_id_creates_css_selector(self):
        selectors = generate_multi_strategy_selector({"tag": "input", "id": "email"})
        self.assertEqual(selectors["id"], "email")
        self.assertEqual(selectors["css"], "#email")

    def test_unsafe_id_does_not_create_invalid_css_selector(self):
        selectors = generate_multi_strategy_selector({"tag": "input", "id": "field:1"})
        self.assertEqual(selectors["id"], "field:1")
        self.assertNotEqual(selectors["css"], "#field:1")

    def test_schema_version_marks_privacy_safe_format(self):
        self.assertEqual(SCHEMA_VERSION, 3)

    def test_event_values_are_removed_and_linked_to_a_schema_field(self):
        recorder = Recorder.__new__(Recorder)
        recorder.actions = []
        recorder.last_schema = {
            "fields": [{"xpath": "/html/body/input[1]", "field_key": "email"}]
        }
        recorder._on_event({
            "type": "input", "xpath": "/html/body/input[1]", "value": "private@example.test",
            "text": "private@example.test", "timestamp": 0,
        })
        event = recorder.actions[0]
        self.assertNotIn("value", event)
        self.assertNotIn("text", event)
        self.assertEqual(event["field_key"], "email")


if __name__ == "__main__":
    unittest.main()
