CONFLUENCE CLOUD TO SERVER COPY
================================

Copies one Cloud page and its descendant pages onto an existing Server or
Data Center page. Dry-run by default. Python 3.10+.

    py -m pip install requests playwright

The destination page must already exist. The script creates the Cloud root
as a child of that page. It does not replace the destination page. Cloud
folders become ordinary pages. Keep the state file between runs so a second
run updates those pages instead of creating copies.

Put this in .env next to migrate.py. Do not commit .env or the session file.

    SOURCE_URL=https://yoursite.atlassian.net/wiki
    SOURCE_EMAIL=cloud-user@company.com
    SOURCE_TOKEN=atlassian-api-token
    SOURCE_PAGE_ID=123456789
    TARGET_URL=https://confluence.example.com
    TARGET_PARENT_ID=987654321
    EXPECT_SPACE=SPACEKEY
    MIGRATION_NAME=project-name

SOURCE_PAGE_ID is the Cloud page to copy, including that page.
TARGET_PARENT_ID is the existing Server page that will contain the copy.
EXPECT_SPACE is optional. When set, the run stops if that parent is in a
different space. MIGRATION_NAME names the state, report, and browser session
files. Use a different name for each tree you copy.

The same values can be passed as flags, which override .env:

    py migrate.py --source-url https://yoursite.atlassian.net/wiki --source-page 123 --target-url https://confluence.example.com --target-parent 456 --name project-name

Cloud uses SOURCE_EMAIL and SOURCE_TOKEN. The Server login is a browser
session: the script opens Edge, you sign in, and it saves
<name>-session.json. TARGET_EMAIL and TARGET_TOKEN are not used.

From this folder:

    py migrate.py
    py migrate.py --apply

Review <name>-report.json before --apply. Optional: --skip-attachments

LIMITATIONS
- Macros, embedded apps, comments, history, and permissions may not match.
- Cloud user mentions and profile macros become the text "Cloud user".
- Attachment files already on the destination are left in place.
- This copies. The Cloud pages stay where they are.
- If a page is created and the run stops before the state file is saved,
  the next run stops on that duplicate instead of writing over it.
