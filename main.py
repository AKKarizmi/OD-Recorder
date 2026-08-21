import argparse
import os
import sys

from config import BROWSER_CHANNEL
from core.profile_manager import ensure_recorder_user_data_dir, cleanup_old_profiles
from core.recorder import Recorder


# Target portal configuration - Open Doors Global University only
TARGET_PORTAL = {
    "University Name": "Open Doors Global University",
    "portal_url": "https://od.globaluni.ru"
    # "portal_url": "https://russia-edu.minobrnauki.gov.ru/"
}
# SUPPORTED_DOMAINS = {"od.globaluni.ru"}
# SUPPORTED_DOMAINS = {"russia-edu.minobrnauki.gov.ru/"}


def main():
    parser = argparse.ArgumentParser(description="Recorder for Autofill system")
    parser.add_argument(
        "--cleanup-profiles",
        action="store_true",
        help="Remove old browser profiles instead of starting recording"
    )
    parser.add_argument(
        "--older-than",
        type=int,
        default=30,
        metavar="DAYS",
        help="When using --cleanup-profiles, remove profiles older than this many days (default: 30)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --cleanup-profiles, show what would be deleted without actually deleting"
    )
    parser.add_argument(
        "--university-url",
        type=str,
        default=None,
        help="Override the default portal URL with a custom one"
    )
    args = parser.parse_args()

    # Handle profile cleanup command
    if args.cleanup_profiles:
        removed = cleanup_old_profiles(
            older_than_days=args.older_than,
            dry_run=args.dry_run
        )
        if args.dry_run:
            print(f"\n[DRY RUN] Would remove {len(removed)} old profile(s)")
        else:
            print(f"\nRemoved {len(removed)} old profile(s)")
        sys.exit(0)

    # Use hardcoded target portal or override from command line
    university = TARGET_PORTAL.copy()
    if args.university_url:
        university["portal_url"] = args.university_url
    
    # Validate portal URL domain
    # from urllib.parse import urlparse
    # parsed = urlparse(university["portal_url"])
    # if parsed.netloc not in SUPPORTED_DOMAINS:
    #     print(f"Error: Only portals under {SUPPORTED_DOMAINS} are supported.")
    #     print(f"Got: {university['portal_url']}")
    #     sys.exit(1)
    
    print(f"Target Portal: {university['University Name']}")
    print(f"Portal URL: {university['portal_url']}")

    categories = ["Overview of the website", "Login", "Registration", "Portofolio Data Entry", "Portofolio Tests"]
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
