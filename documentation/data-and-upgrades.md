# Data and upgrades

## Where Tureen keeps your data

Tureen keeps conversations, drafts, decision and image sessions, host records, and the knowledge library in one SQLite database:

- Flatpak: `~/.var/app/io.github.jackrabbithanna.Tureen/data/tureen/tureen.db`
- Native install: `~/.local/share/tureen/tureen.db`

API keys are not in the database; they are in the desktop keyring (see [API keys](hosts-and-providers.md#api-keys)). Knowledge imports keep extracted text, not copies of the original files.

Tureen does not read data from Gnollama, its former name. Gnollama's database and saved keys are left where they were.

## Database upgrades

The current release uses database schema version 16, and preserves existing documents, embeddings, collection memberships, chat settings, and historical citations.

Upgrades run automatically on startup, with progress and a recoverable error screen. If you close Tureen during an upgrade, it waits for the database operation to finish.

- **Backups.** Before upgrading an existing database, Tureen saves a consistent, timestamped backup beside it, named like `tureen.db.pre-v16-<timestamp>.bak`, and reports its location. Keep that file until you have verified the upgrade; then it can be archived or removed.
- **Safety.** Failed migrations roll back, and incompatible or damaged vectors are never silently discarded.
- **Versions.** Databases from newer app versions are refused. Don't open an upgraded database with an older app version.
- **Size.** SQLite reuses pages freed by a migration, so the database file may not shrink right away.

### Schema history

| Version | Change |
| --- | --- |
| 7 | Moves existing vectors to sqlite-vec without contacting any server: it copies and verifies their IDs, dimensions, and bytes before removing the old vector column |
| 8 | Adds collections and membership without copying or rebuilding vectors; existing documents appear under **Ungrouped Documents**, and existing chat selections are preserved |
| 9 | Adds web-source provenance without reindexing existing documents |
| 10 | Adds cloud host types and keyring references, without storing API keys in the database |
| 11 | Adds stable message identifiers, managed drafts, full-text search, and comparison runs, preserving message attachments and metadata |
| 14 | Adds saved decision runs without changing existing conversations |
| 15 | Adds saved image runs |
| 16 | Adds image documents to the knowledge library |

Versions 15 and 16 don't change existing chats or documents. In a benchmark with 50,000 vectors, the one-time version 7 migration and backup took about 68 seconds.
