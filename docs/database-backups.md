# Production database backups

TheoryLabs must not depend on a free database host as the only copy of its historical match data.

## Current backup design

The workflow at `.github/workflows/db-backup.yml` creates a portable Postgres custom-format dump of the production database, encrypts it, performs a full restore into an ephemeral Postgres instance, and only then uploads the encrypted file as a GitHub Actions artifact.

The production database is **read only** during the backup. The workflow does not mutate production data.

### Required repository secrets

- `NEON_DATABASE_URL` — the production Neon Postgres URL. Live ingest, read-only production diagnostics, and backups use this secret.
- `DATABASE_URL` — temporarily retained as the legacy Render Postgres rollback/migration-source URL. It is not the active production destination after the Neon cutover.
- `DB_BACKUP_PASSPHRASE` — a long random passphrase used only to encrypt/decrypt database backups.

Do not put any database URL or backup passphrase in the repository, issues, logs, or chat. Store the backup passphrase in a password manager. Losing it makes the encrypted backups unusable.

The workflow refuses to run if the passphrase is shorter than 24 characters.

## What gets uploaded

Only:

- `theorylabs-<UTC timestamp>.dump.enc`
- its SHA-256 checksum

The unencrypted `.dump` exists only on GitHub's ephemeral runner and is deleted at the end of the job.

Encryption is OpenSSL AES-256-CBC with PBKDF2-SHA256 and 250,000 iterations.

## Verification

A backup is uploaded only after all of these succeed:

1. `pg_dump` completes.
2. The encrypted file decrypts with the configured passphrase.
3. The decrypted bytes exactly match the original dump.
4. `pg_restore` restores the dump into a fresh temporary Postgres database.

This tests that the backup is actually restorable, not merely that a file was created.

## Retention

GitHub Free includes 500 MB of Actions artifact storage. The scheduled workflow therefore keeps each weekly artifact for only 8 days.

GitHub Actions is the short-term safety copy, not the long-term archive.

After the first successful backup, download one encrypted copy and keep it somewhere independent of Render and GitHub. A later step may mirror encrypted dumps to a separate object store such as Cloudflare R2.

## Manual backup

GitHub:

1. Open **Actions**.
2. Choose **Encrypted Postgres backup**.
3. Choose **Run workflow**.
4. Select **main**.
5. Run it.
6. Open the completed run and confirm the restore verification passed.
7. Download the `theorylabs-postgres-backup-...` artifact.

Do not delete the old production database until a backup has passed and the replacement database has been independently checked.

## Decrypting a backup

Given `theorylabs-YYYYMMDDTHHMMSSZ.dump.enc`:

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -iter 250000 -md sha256 \
  -pass env:DB_BACKUP_PASSPHRASE \
  -in theorylabs-YYYYMMDDTHHMMSSZ.dump.enc \
  -out theorylabs.dump
```

Then restore to an empty Postgres database:

```bash
pg_restore --no-owner --no-acl \
  --dbname="$TARGET_DATABASE_URL" \
  theorylabs.dump
```

For a migration, verify row counts, schema/content fingerprints, and application health against the target before changing which secret production workflows consume. The legacy Render database should remain untouched as a rollback snapshot until Neon has been operating successfully.
