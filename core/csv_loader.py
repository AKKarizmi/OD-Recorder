import csv
import sys
from typing import Any


def csv_universities(csv_path: str) -> list[dict[str, Any]]:
    """Load universities from CSV file with proper error handling."""
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as handle:
            return list(csv.DictReader(handle))
    except FileNotFoundError:
        print(f"Error: CSV file not found: {csv_path}")
        print("Please ensure the dataset.csv file exists in the current directory.")
        sys.exit(1)
    except PermissionError:
        print(f"Error: Permission denied reading: {csv_path}")
        sys.exit(1)
    except UnicodeDecodeError as exc:
        print(f"Error: Unable to decode CSV file {csv_path}: {exc}")
        print("Ensure the file is encoded in UTF-8.")
        sys.exit(1)
    except csv.Error as exc:
        print(f"Error: Malformed CSV file {csv_path}: {exc}")
        sys.exit(1)


load_universities = csv_universities
