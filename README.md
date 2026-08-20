# OD-Recorder

Human Interaction Recorder for the Open Doors website. It collects safe form
metadata and interaction flow to support a separately built autofill system.

## Data policy

New recordings intentionally exclude typed values, password values, hidden
tokens, and generic page text. They are local operational output and are
ignored by Git. Existing tracked recordings must be removed from Git tracking
and reviewed separately before publication.

## Recorded metadata

Each schema snapshot contains control labels, IDs/names, type, visibility,
constraints, autocomplete hints, form information, selected state, and native
select options. Interaction logs contain event type and field location, but no
user-entered value.

## Run

Install the requirements, ensure Google Chrome is installed, then run:

```powershell
python main.py
```

The recorder opens an isolated Chrome profile. Log in and navigate before
pressing Enter to begin; those preparatory actions are not recorded.
