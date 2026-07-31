# Off-server backup (restic → Backblaze B2)

Encrypted, off-server backups of the staging database, the per-tenant
object-storage volume, and the **non-secret host configuration** — the
`/etc/caddy/Caddyfile` (which exists nowhere else), the installed `career-*`
systemd units, and a manifest of versions, unit states and env KEY NAMES.
restic encrypts **client-side** (AES-256), so B2 only ever stores ciphertext.
`.env` files are never backed up — secrets stay on the server, which means a
rebuild is blocked on the owner: see `docs/RUNBOOK-DISASTER-RECOVERY.md`.
Whitepaper §15.14; design in `docs/PLAN.md` P-B.5.

Three snapshot tags, each addressable by a stable name:

```bash
restic dump --tag db     latest career_staging.dump    # the database
restic restore --tag storage latest --target /tmp/x    # tenant files
restic dump --tag config latest career-config.tar.gz   # Caddyfile + units + manifest
```

## One-time setup

1. **Backblaze B2** (web): create a **private** bucket; add a lifecycle rule to
   keep prior/hidden versions ~30 days (delete protection); create an
   **application key restricted to that bucket** with read+write. Copy the
   `keyID` and `applicationKey`.
2. **Server config** (secrets live outside the repo):
   ```bash
   mkdir -p /root/.config/career-backup && chmod 700 /root/.config/career-backup
   cp ops/backup/backup.env.example /root/.config/career-backup/backup.env
   chmod 600 /root/.config/career-backup/backup.env
   # edit it: set RESTIC_REPOSITORY=b2:<bucket>:staging, B2_ACCOUNT_ID (keyID),
   # B2_ACCOUNT_KEY (applicationKey), and a strong RESTIC_PASSWORD.
   ```
   **Store `RESTIC_PASSWORD` in your password manager too** — without it the
   backups cannot be decrypted.
3. **Install the timers** (all units now live in one place, `ops/systemd/` —
   the old per-area copies under `ops/backup/systemd/` and `ops/engine/systemd/`
   had already drifted, missing the `OnFailure=` alert line, so installing from
   them produced a stack whose failures were silent):
   ```bash
   cp ops/systemd/career-backup.* ops/systemd/career-restore-test.* \
      ops/systemd/career-alert@.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now career-backup.timer career-restore-test.timer
   ```

## Run manually

```bash
ops/backup/backup.sh        # nightly backup (init on first run)
ops/backup/restore-test.sh  # restore latest into a scratch DB, verify, drop
```

## Retention

7 daily + 4 weekly + 6 monthly snapshots (`restic forget --prune`).

## Production

At the C9 Saudi-server migration, use a **separate** bucket, key, and repo
password for production — the two environments share nothing.
