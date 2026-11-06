# OD-Recorder

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Human Interaction Recorder for university portal websites. Collects safe form metadata and interaction flow to support a separately built autofill system.

## Features

- **Privacy-Aware Recording**: Captures safe form values, including pre-filled values, while excluding passwords and security-sensitive fields
- **Dynamic Schema Detection**: Automatically detects form changes, new fields, and visibility updates
- **Raw + Semantic Actions**: Coalesces low-level events into replay-oriented actions such as `FILL_FIELD`, `SET_CHECKBOX`, `SELECT_OPTION`, `CLICK_ACTION`, `SUBMIT_FORM`, and `STATE_CHANGE`
- **Action-Owned State**: Saves immediate event-time state separately from the stable DOM and verifies selection outcomes
- **Rendered Page Content**: Captures post-login headings, profile details, statuses, tables, links, and image metadata outside form controls
- **Integrity Validation**: Checks IDs, sequences, action/step cardinality, widget references, and field-key uniqueness before final save
- **Session Management**: Persists cookies, tokens, and browser state across sessions
- **Multi-Platform**: Supports Windows, macOS, and Linux with Google Chrome
- **Profile Isolation**: Uses dedicated Chrome profiles to avoid interfering with daily browsing
- **Auto-Recovery**: Periodic auto-save prevents data loss on crashes

## Data Policy

**Recordings capture:**
- Ordinary field values, including pre-filled values and personal data needed by the autofill workflow

**Recordings intentionally exclude:**
- Password values
- Hidden fields and security values such as CSRF tokens, session tokens, authorization values, and API keys
- Generic page text content

Recordings are stored locally as operational output and are ignored by Git.

## Recorded Metadata

Each schema snapshot contains:
- Control labels, IDs, and names
- Field types and visibility state
- Input constraints (min/max length, patterns)
- Autocomplete hints and aria-labels
- Form information and selected states
- Native select dropdown options
- Compact custom-widget state, controller identity, and explicit submit-control intent
- Raw values and privacy-safe fingerprints for populated, non-secret controls

Interaction logs contain:
- Logical action type and semantic field/widget identity
- Stable raw-action, semantic-action, interaction, and step IDs
- Explicit per-field/widget interaction sessions are also saved in `interactions.json`
- Success, failed, or unverified outcome with verification evidence
- Uniqueness-validated selectors as supporting metadata
- Timestamps and safe captured values; excluded sensitive values are not stored

## Installation

### Prerequisites

1. **Python 3.10 or higher** - [Download Python](https://www.python.org/downloads/)
2. **Google Chrome** (stable channel) - [Download Chrome](https://www.google.com/chrome/)
3. **Playwright** - Installed automatically via pip

### Setup

```bash
# Clone or navigate to the repository
cd /path/to/od-recorder

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

## Usage

### Basic Recording

```bash
python main.py
```

The recorder will:
1. Display available recording categories
2. Open an isolated Chrome profile
3. Prompt you to log in and navigate to the target section
4. Begin recording after you press Enter

### Command-Line Options

```bash
# Start recording with default settings
python main.py

# Use a custom portal URL
python main.py --university-url "https://example.edu/portal"

# Clean up old browser profiles (older than 30 days)
python main.py --cleanup-profiles

# Preview what would be deleted (dry run)
python main.py --cleanup-profiles --dry-run

# Remove profiles older than 60 days
python main.py --cleanup-profiles --older-than 60
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PORTAL_BROWSER_CHANNEL` | Chrome channel (`chrome`, `chrome-beta`) | `chrome` |
| `PORTAL_HEADLESS` | Run browser in headless mode | Auto-detect |
| `PORTAL_RECORDER_USER_DATA_DIR` | Custom profile directory path | Platform-specific |
| `CI` | CI environment flag (enables headless) | `false` |
| `PORTAL_DEBUG_CAPTURE_VALUES` | Store raw values for fields not classified as secrets or PII | `false` |
| `PORTAL_CAPTURE_SENSITIVE_VALUES` | Explicitly store password/token-like values in plain JSON | `false` |

## Output Structure

Recordings are saved to `recordings/sessions/<category>/<university>_<session_id>/`:

```
recordings/sessions/
└── Registration/
    └── Open_Doors_Global_University_a1b2c3d4/
        ├── actions.json          # Interaction event log
        ├── step_00_initial_load.json
        ├── step_01_nav_1.json
        ├── step_02_dynamic_2.json
        └── ...                   # Schema snapshots per step
```

Every step schema contains a document-level `page_content` snapshot independent
of form scope. `actions.json` and `semantic_actions.json` also expose a
deduplicated `page_content` timeline. Records contain a stable `content_key`,
semantic kind, text, selector/XPath, frame and section context, plus structured
table, label/value, link, or image metadata where applicable. Content changes
are marked as `initial`, `added`, `changed`, or `removed`.

### Actions JSON Format

```json
{
  "session_id": "a1b2c3d4",
  "category": "Registration",
  "university": {"University Name": "...", "portal_url": "..."},
  "start_time": "2024-01-15T10:30:00+00:00",
  "end_time": "2024-01-15T10:35:00+00:00",
  "recording_duration_seconds": 300,
  "total_actions": 42,
  "total_steps": 8,
  "actions": [
    {
      "type": "FILL_FIELD",
      "semantic_action_id": "action_000001",
      "semantic_sequence": 1,
      "step_id": "step_000001",
      "raw_action_ids": ["raw_000001", "raw_000002"],
      "field_key": "main|form_id:registration|input|email|name:email|email",
      "value_fingerprint": "fnv1a32:1234abcd:18",
      "outcome": "success",
      "event_occurred_at": 1705315800123,
      "recorded_at": "2024-01-15T10:30:00.123+00:00"
    }
  ]
}
```

## Configuration

Create a `config.json` file for custom settings:

```json
{
  "categories": ["AUTH", "Registration", "Application", "Data Entry"],
  "browser_channel": "chrome",
  "headless": false,
  "debug_capture_values": false,
  "capture_sensitive_values": false
}
```

## Security Considerations

- Sensitive fields (passwords, credit cards, API keys) are automatically redacted
- Setting `capture_sensitive_values` (or `PORTAL_CAPTURE_SENSITIVE_VALUES`) to true disables that protection and writes sensitive values to plain JSON
- Session tokens and cookies stay in the local Recorder profile/session cache; protect that directory as account-sensitive data
- Profile directories use restrictive permissions (0o700)
- No data is transmitted externally - all recordings are local

## Troubleshooting

### "Could not open the dedicated Recorder Chrome profile"

- Close any other Chrome windows using the Recorder profile
- Ensure no other Recorder instance is running
- Try cleaning up old profiles: `python main.py --cleanup-profiles`

### "Navigation timed out"

- The portal may be slow to respond; the browser remains open for manual inspection
- Check your network connection
- Verify the portal URL is accessible

### Browser doesn't launch

- Ensure Google Chrome (not Chromium) is installed
- On Linux: `sudo apt install google-chrome-stable`
- On macOS: Download from [google.com/chrome](https://www.google.com/chrome/)
- On Windows: Install from the official Chrome installer

## Development

### Running Tests

```bash
pytest tests/
```

### Code Style

This project follows PEP 8 guidelines. Type hints are used throughout.

## License

MIT License - see LICENSE file for details.

## Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request with tests

---

**Note**: This tool is designed for educational and testing purposes. Always obtain proper authorization before recording interactions with any website.
