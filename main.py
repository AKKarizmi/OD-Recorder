import argparse
import os
import sys

from config import BROWSER_CHANNEL
from core.csv_loader import load_universities
from core.profile_manager import ensure_recorder_user_data_dir
from core.recorder import Recorder


def main():
    parser = argparse.ArgumentParser(description="Recorder for Autofill system")
    parser.add_argument("--csv", default="dataset.csv", help="Path to dataset.csv")
    args = parser.parse_args()

    universities = load_universities(args.csv)
    if not universities:
        print("No universities found.")
        sys.exit(1)

    for idx, uni in enumerate(universities):
        print(f"{idx}: {uni['University Name']} ({uni.get('Portal Link', 'unknown')})")
    
    while True:
        try:
            choice = input("Select university index: ").strip()
            uni_index = int(choice)
            if 0 <= uni_index < len(universities):
                university = universities[uni_index]
                break
            print(f"Please enter a number between 0 and {len(universities) - 1}.")
        except ValueError:
            print("Invalid input. Please enter a number.")

    categories = ["AUTH", "Registration", "Application", "Data Entry"]
    for idx, category in enumerate(categories):
        print(f"{idx}: {category}")
    
    while True:
        try:
            choice = input("Select category index: ").strip()
            cat_index = int(choice)
            if 0 <= cat_index < len(categories):
                category = categories[cat_index]
                break
            print(f"Please enter a number between 0 and {len(categories) - 1}.")
        except ValueError:
            print("Invalid input. Please enter a number.")

    try:
        user_data_dir = ensure_recorder_user_data_dir(
            browser_channel=BROWSER_CHANNEL
        )
        existing_profile = os.path.isfile(os.path.join(user_data_dir, "Local State"))
        print(f"Using Recorder's dedicated Chrome profile: {user_data_dir}")
        if existing_profile:
            print("Saved Recorder browser login state will be reused.")
        else:
            print(
                "This is a separate browser profile. Sign in to the portal in the "
                "Chrome window; the login will be retained for later recordings."
            )

        Recorder(
            university,
            category,
            user_data_dir,
        ).start()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Recorder could not start: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
