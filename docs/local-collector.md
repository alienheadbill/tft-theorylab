# Collect TheoryLabs data on your own computer (free)

TheoryLabs can collect its ranked TFT data on an ordinary Windows, macOS or
Linux computer, for $0. Everything is stored in **one file on your computer**
(a SQLite database). You do **not** need Postgres, Docker, Neon, Render or any
other cloud service.

**The only thing to install is Python 3.11 or newer.** Everything else comes
with TheoryLabs.

## The easy way: the TheoryLabs Collector app (Windows)

After the one-time setup below, you never need a terminal:

1. **Double-click `TheoryLabs Collector.cmd`** in the TheoryLabs folder. Your browser opens the dashboard at `http://127.0.0.1:8765/`; the small black window closes by itself.
2. **Riot development key:** paste a fresh key into the hidden box and click **Save key**, then **Check key**. Keys expire about every 24 hours. The key is never shown again, and it only goes into `.env` on this computer.
3. **Click _Collect Data_.** The large status at the top of the page always says what is happening:
   - **READY**: you can collect.
   - **NOT READY**: says why, for example no key, or the pause TheoryLabs makes around every patch change (see "Patch changes").
   - **COLLECTING**: shows the current step, elapsed time, Riot requests so far and rate-limit events.
   - **COMPLETE**: shows new matches, current-patch matches, boards and validation.
   - **FAILED**: gives a plain-language reason and the step that failed. It stays until you retry or click **OK**.

   A collection takes about 10–25 minutes. You can close or reload the page; it keeps going. If the page says the app was **restarted after this page was opened**, click **Reload the page**.
4. **Read the result:** new matches, already-stored matches skipped, totals, Riot requests, validation, Discovery, backup and report file.
5. **When you want a website snapshot, click _Create Public Snapshot_.** It writes a sanitized, verified file in `data/local/public/`. Nothing is uploaded or deployed.
6. **When you're done,** click **Stop the collector app** at the top of the page, or double-click `Stop TheoryLabs Collector.cmd`.

Double-clicking `TheoryLabs Collector.cmd` again while the app is running simply reopens the dashboard; it never starts a second copy. Only one collection or snapshot can run at a time, and the app refuses to stop while one is running.

**What it is.** A small, separate program that runs **only on this computer**. It listens on 127.0.0.1, so other devices on your network cannot reach it. It is not part of the public website and is never deployed. It uses exactly the same collector as `tftlab local-collect`, so the CLI below keeps working and both share one lock: a collection started in a terminal blocks the app's buttons, and the other way round.

**Patch information.** The "Your data" panel shows the current trusted patch, the next scheduled patch with its date (from Riot's official patch schedule), and how fresh that schedule is: **Riot — checked today**, or **cached — refresh failed** when Riot's page could not be reached and the saved copy is used. Its source, fingerprints and window rule are under **Technical details**.

**Where its files are.**
- `data/local/collector-ui.log` is the app's log (keys are scrubbed).
- `data/local/.collector-ui.json` records the running app's port and its private control token, so the stop launcher can find it. The file is deleted when the app stops.

**macOS / Linux:** run `./scripts/collector-ui.sh` (or `tftlab collector-ui`). It stays attached to the terminal; stop it with Ctrl+C or the stop button.

## The short version with the command line (every day)

1. Get a fresh Riot development key from the [Riot Developer Portal](https://developer.riotgames.com/). Development keys stop working after about 24 hours.
2. Save it: `tftlab local-set-key`, then paste it. The key is hidden while you type.
3. Run **one command**:
   - Windows: double-click `scripts\local-collect.cmd`, or in PowerShell run `.\scripts\local-collect.ps1`.
   - macOS/Linux: `./scripts/local-collect.sh`.
   - Or, in any terminal in the TheoryLabs folder: `tftlab local-collect`.
4. Read the report at the end. It says **SUCCESS** in green when everything worked.

A run usually takes about 10–25 minutes, because TheoryLabs deliberately stays well inside Riot's rate limits. Keep the computer awake and online until it finishes. If a run is interrupted (sleep, Wi-Fi drop, Ctrl+C), nothing is damaged: just run it again.

The rest of this page explains each part.

## One-time setup

### Windows

1. **Install Python** 3.11 or newer from [python.org](https://www.python.org/downloads/). On the first installer screen, tick **"Add python.exe to PATH"**.
2. **Get TheoryLabs.** Either:
   - use [GitHub Desktop](https://desktop.github.com/) to clone the repository (easiest to update later); or
   - on GitHub, click **Code → Download ZIP** and extract it, for example to `Documents\tft-theorylab`.
3. **Open PowerShell in that folder.** In File Explorer, open the folder, then right-click on empty space and choose **Open in Terminal** (Windows 11). On Windows 10, choose **File → Open Windows PowerShell**.
4. **Create the private Python environment and install TheoryLabs.** Copy and paste these two lines; the second takes a minute:

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\pip install -e .
   ```

5. **Create the local folders and your settings file:**

   ```powershell
   .\.venv\Scripts\tftlab local-init
   ```

   This creates `data\local\` and a `.env` file (a copy of `.env.example`). It never overwrites a `.env` or database you already have.

6. **Save your Riot key:**

   ```powershell
   .\.venv\Scripts\tftlab local-set-key
   ```

   Paste the key and press Enter. Nothing appears while you paste; that is intentional, so the key never shows on screen or in the command history. (Alternatively, run `notepad .env` and replace `RGAPI-your-key-here` on the `RIOT_API_KEY=` line.)

7. **Region.** `.env` already says `TFT_PLATFORM=na1` and `TFT_REGION=americas`, which is correct for North America. Leave them alone unless you collect another region.

8. **Check everything:**

   ```powershell
   .\.venv\Scripts\tftlab local-status
   ```

9. **Collect:**

   ```powershell
   .\.venv\Scripts\tftlab local-collect
   ```

   From then on you can simply double-click **`TheoryLabs Collector.cmd`** (the dashboard; see "The easy way" above), or `scripts\local-collect.cmd` for the plain terminal version.

> If PowerShell says "running scripts is disabled on this system" for `.\scripts\local-collect.ps1`, use the `.cmd` file instead, or run
> `powershell -ExecutionPolicy Bypass -File .\scripts\local-collect.ps1`.
> The `.\.venv\Scripts\tftlab ...` commands are not affected.

### macOS / Linux

```bash
cd ~/path/to/tft-theorylab
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/tftlab local-init
.venv/bin/tftlab local-set-key
.venv/bin/tftlab local-status
./scripts/local-collect.sh
```

(If `python3 --version` shows something older than 3.11, install a newer Python from python.org or your package manager first.)

> Tip: after `.\.venv\Scripts\Activate.ps1` (Windows) or `source .venv/bin/activate` (macOS/Linux), you can type just `tftlab ...`. This page writes the full `.venv` path so that it works without activating.

## What `tftlab local-collect` does

The steps run in order. If any check fails, the command stops **before** changing anything and tells you what to do.

1. **Checks:**
   - the database is a file on this computer (cloud database addresses are refused);
   - there is enough free disk space;
   - the Riot region settings;
   - the current UTC time;
   - the **current verified patch window**;
   - the Riot key is set and **still valid** (one small request to Riot);
   - CommunityDragon champion cost data is reachable;
   - what is already stored.
2. **Backup:** a safe copy of the database is saved in `data/local/backups/` (not on the very first run, when there is nothing to back up yet).
3. **Collection:** the same bounded collection TheoryLabs used in production:
   - seed players: 15 Challenger, 15 Grandmaster, 20 Master, 25 Diamond and 25 Platinum;
   - their 10 most recent ranked games, **current patch only**;
   - players not sampled before are used first;
   - games already stored are skipped;
   - Riot's rate limits are respected automatically.
4. **Validation:** the existing data-integrity checks for the current patch.
5. **Discovery preparation:** precomputes the Discovery analytics for the current patch only, so the website is fast.
6. **Report:** a plain summary on screen. It is also saved as JSON in `data/local/reports/`.

The command never uses `DATABASE_URL`, never contacts Neon or any other cloud database, and never uploads anything.

### Collecting more: maximum mode

The default run is a small sample: about 300 Riot requests in 10–25 minutes. To collect as much of the current patch as a time budget allows, run:

```powershell
.\.venv\Scripts\tftlab local-collect --collection-mode maximum
```

- It reads every Challenger, Grandmaster and Master player and every Diamond and Platinum division, breadth-first: players never sampled before come first, spread across every rank band, so stopping early still covers every rank.
- For each player it reads their whole current-patch history, not only the last 10 games. A game already stored is never fetched again.
- It stops cleanly at the first budget reached: `--max-duration-minutes` (default 45), `--max-requests` (default 2,400) or `--max-match-fetches` (default 2,000). The report says which one.
- Everything else is the same as a normal run: the checks, the backup, the same rate limits (at most 10 requests every 10 seconds with a development key), validation and Discovery preparation. It uses the same code as the `--collection-mode maximum` option of `tftlab ingest-riot` (README, "Maximum collection mode and Riot rate limits").
- If the key expires during a run, the matches stored so far are kept, and the next run carries on with the players it did not finish.

### Reading the report

| Line | Meaning |
|---|---|
| Balance window | The patch being collected and its verified time window |
| Raw database | Where the data is (private, stays on this computer) |
| Patch matches before this run / New matches inserted / Already stored | Growth from this run; duplicates are skipped, never stored twice |
| Patch matches now / Boards | Totals for the current patch (8 boards per match) |
| Seeds sampled | Players used per rank group (selected/requested) |
| Riot requests / Rate-limit events | How many requests were made and how often Riot asked TheoryLabs to slow down (handled automatically) |
| Validation | `passed`, `passed (warnings: ...)` or `FAILED` |
| Discovery preparation | `prepared`, `already up to date` or `FAILED` |
| Backup before this run | The backup file made before collecting |
| Ready to create a public snapshot | Whether `tftlab local-snapshot` can be run now |

Warnings such as "board(s) Riot sent without units" or "id(s) not in CommunityDragon yet" are known, harmless situations, not failures.

## When something goes wrong

| What you see | What it means / what to do |
|---|---|
| **Riot development key is expired (or invalid)…** | The key expired (about every 24 hours). Get a new one in the Developer Portal, run `tftlab local-set-key`, run again. Nothing was collected. |
| **RIOT_API_KEY is not set** | Run `tftlab local-set-key` (or edit `.env`). Make sure you are in the TheoryLabs folder. |
| **TheoryLabs does not currently have a trusted collection window.** … starts trusting patch 18.5 at … | The normal pause around a patch change (see "Patch changes"). Run again after the time given; nothing needs updating. Nothing was collected. |
| … **balance update (18.6B)** … collection pauses across it | Riot scheduled a mid-patch balance update. Collection resumes automatically when the next patch's window starts (the time is in the message). |
| … **could not read Riot's official patch schedule and has no usable saved copy** | A new patch needs Riot's schedule, and neither Riot's page nor a saved copy was available. Check the internet connection; it is tried again when the app starts and before each collection. |
| … **found the next scheduled patch but cannot create a trusted collection window safely yet** | Riot's schedule is too old to trust for a new patch (not checked for 48 hours), or Riot moved a date in a way that conflicts with a window already used. Get online so the schedule refreshes; if it persists, ask the maintainer (the details are in `tftlab local-patch-schedule`). |
| **Collection stopped early … The local database is still valid** | The connection or the key failed mid-run. Matches saved before the failure are kept. The run is marked incomplete and does not count for player rotation, so the next run simply retries. Fix the cause (new key, internet) and run again. |
| **Collection completed, but validation found severe integrity problems** | Nothing was deleted. Do **not** make a public snapshot. Ask the maintainer to look at the saved report and `tftlab validate-live-data --db data/local/theorylabs.sqlite3`. |
| **Another collection is already running** | Wait for it to finish. If the computer restarted mid-run, the file named in the message can be deleted (stale locks are cleared automatically after 6 hours). |
| **Not enough free disk space** | Free some space, or keep fewer backups with `tftlab local-collect --keep-backups 3`. |
| **CommunityDragon … could not be reached** | A free game-data service TheoryLabs uses for champion costs is down. Try again later. Nothing was collected. |

The exit code is 0 only for SUCCESS, so scripts can tell success from failure.

## How much data do we have? `tftlab local-status`

`local-status` works offline: it does not contact Riot or anything else (add `--check-riot` if you want it to test the key too). It shows:

- **the database:** where it is, its file size, total matches and boards, matches per patch (balance window), and the latest game time;
- **collection runs:** how many completed and how many are incomplete, plus the size of the player-sampling ledger;
- **prepared Discovery:** its status for the current and recent patches;
- **backups:** the latest local backup;
- **the patch:** the current trusted patch, whether the current time is inside it, the next scheduled patch, any upcoming balance update, and how fresh the saved patch schedule is (it reads the saved copy only);
- **the Riot key:** whether it is set.

It never prints the key, player identifiers or match identifiers.

## Making a public website snapshot: `tftlab local-snapshot`

The raw database is private: it contains Riot's raw match data and the list of sampled players. The website must only ever get a **sanitized snapshot**:

```powershell
.\.venv\Scripts\tftlab local-snapshot
```

This uses TheoryLabs' verified exporter. It:

- checks that the data really came from Riot collection (demo data is refused), and that every exported match is linked to a completed collection run (a match stored only by a failed or unfinished run blocks the export until a later completed run finds it again);
- removes raw payloads, player identifiers and the collection ledger;
- replaces match ids with opaque ones;
- precomputes Discovery;
- verifies the result twice.

By default it exports the current patch; use `--balance-window 18.3` for another stored, verified patch. The output goes to `data/local/public/`:

- `theorylabs-public-snapshot.sqlite3.gz`: **the file to publish**, when you decide to;
- `theorylabs-public-snapshot.sqlite3`: the same snapshot, uncompressed;
- `theorylabs-public-snapshot.sqlite3.manifest.json`: counts, provenance and SHA-256 checksums.

The command prints the match and board counts, file sizes and SHA-256 checksums. **Nothing is uploaded or deployed.** Publishing is a separate step: `tftlab local-publish` (see "Publishing"), or by hand (see the README, "Public website snapshots").

## Publishing: `tftlab local-publish`

`local-publish` does the whole publishing job: it makes the snapshot, publishes it as a GitHub Release, points the Render website at it, deploys, and checks the live site. Its guarantee: a failed or unclear publication leaves the website on the snapshot it served before, never deletes a release that might be needed to recover, and refuses to publish when it cannot be sure the collected data is complete.

Run it by hand for now. Running it unattended (on a schedule) is for after these safeguards have been reviewed and a manual publication has gone through them.

**One-time setup:**
1. `gh auth login` (the GitHub CLI), so releases are created as you.
2. In the Render dashboard, **Account Settings → API Keys → Create API Key**, then run `.\.venv\Scripts	ftlab local-set-render-key` and paste it (hidden, saved only in `.env`).
3. Add your website's address to `.env`: `TFT_PUBLIC_SITE_URL=https://<your-service>.onrender.com`.
4. Try it without uploading anything: `.\.venv\Scripts	ftlab local-publish --dry-run`.

**Before anything is changed.** It publishes only when all of these hold; otherwise it stops (or skips) and the website keeps the snapshot it has:
- **The right code:** this folder is exactly `origin/main` with no local changes, so the snapshot's analytics match the code the website runs (`git pull` keeps it there).
- **A successful collection:** the newest collection report that changed data is readable and says `success`. A run stopped before collecting (`preflight_failed`, for example an expired key) changed nothing and is passed over. Any other outcome blocks. So does a report that can't be read or has no outcome, because that collection may have changed data and then crashed.
- **A trusted current patch:** not the pause around a patch change.
- **Enough data for a new patch:** when the website doesn't serve the current patch yet, the current patch alone needs at least **200 matches**. Matches from the previous patch included alongside never count towards it.
- **Enough new data:** otherwise there must be at least **200 new matches**, compared patch by patch with what the website reports (`/api/balance-windows`). `--force` skips this check and the new-patch one, and nothing else.
- **A known way back:** a read-only Render check finds exactly one web service at `TFT_PUBLIC_SITE_URL` and reads its current `TFT_SNAPSHOT_URL` and `TFT_SNAPSHOT_SHA256`. Both must be present and well-formed, and the website must report the snapshot it serves now (`/api/source`). This is the snapshot it would roll back to. Without one, it refuses to start.
- **A verified snapshot:** the exporter checks every match, including that **every exported match** is linked to a completed collection run (see "Making a public website snapshot"). If a collection is running, it skips with "Collection is still running; publication was skipped." and changes nothing.

**What it publishes.** The current patch. While the current patch has fewer than 1,000 matches, the previous patch is included as well.

**Then:**
1. It creates a new GitHub Release (`public-snapshot-<patch>-<date>`, `-r2` and so on if that tag exists) with exactly the `.gz` and its manifest, and checks both uploaded completely. If not, Render is never touched. The release is kept whatever happens later, for diagnosis or a retry.
2. It sets `TFT_SNAPSHOT_URL` and `TFT_SNAPSHOT_SHA256` on Render (nothing else) and starts a deploy.
3. It counts as published only when the deploy is `live`, `/api/source` reports the new snapshot, and `/api/discovery` serves the newest patch from prepared data (`prepared.status = current`).

**If anything fails once Render has been changed**, it rolls back and checks the rollback:
- It puts back whichever of the two settings differ from the previous values.
- If the new deploy went live, or timed out and might still go live, it cancels the new deploy (when one might still be running), starts a deploy of the previous snapshot and waits for it to be live.
- The rollback counts as **verified** only when Render shows the previous values again and `/api/source` reports the previous snapshot.
- Otherwise the report says **HIGH SEVERITY: … the rollback could not be verified** and that manual Render inspection is needed. It never claims the site was restored without checking.

**Old releases** are deleted only after a fully verified publication. Only releases this command published are deleted (their notes start with "Published automatically by `tftlab local-publish`."), keeping the newest 5. The new release and the release the website served before are never deleted. If the previous release can't be identified from its URL, nothing is deleted. Releases published any other way (by hand, or by the release workflow) are never deleted. A failed cleanup is a warning; it never undoes a good publication.

Each run saves a report as `data/local/reports/local-publish-<UTC time>.json`, with the outcome, every step, the previous snapshot it would roll back to, the release, the deploy ids and any rollback result. The Render key is only sent to api.render.com and is never printed or written into a report. Exit code 0 means published or skipped (nothing worth publishing, or a collection was running); 1 means something failed, and the message says what was and wasn't changed.

The old cloud workflow `live-ingest.yml` no longer runs on a schedule (see the README, "Former scheduled collection").

## Where everything is stored

| Path | What | Share it? |
|---|---|---|
| `.env` | Your Riot key and settings | **Never** |
| `data/local/theorylabs.sqlite3` | The raw collector database (private) | **No**: it contains player identifiers |
| `data/local/backups/theorylabs-<UTC time>.sqlite3` | Pre-run backups (newest 7 kept) | No |
| `data/local/reports/local-collect-<UTC time>.json` | Each run's report (counts only, no ids or key) | Yes, if the maintainer asks |
| `data/local/public/theorylabs-public-snapshot.sqlite3.gz` | Sanitized public snapshot | Yes, this is the publishable file |
| `data/local/patch-calendar.json` | Saved copy of Riot's public patch schedule, with its source, fetch time and fingerprints | Yes |
| `data/local/patch-calendar-check.json` | When Riot's schedule was last checked and whether that worked | Yes |
| `data/local/patch-windows-frozen.json` | The patch windows already used for collection. **Never edit or delete it**: stored matches were classified with these windows | Yes |
| `data/local/backups/patch-windows/patch-windows-frozen-<UTC time>-<n>.json` | Automatic copies of that record, one each time it changes (newest 30 kept). The newest good copy is the reference: if the record goes missing, is damaged, or differs from that copy (for example after a hand edit), the collector restores the copy before collecting and keeps the replaced file aside as `patch-windows-frozen.json.damaged-<time>` | Yes |

`.env` and everything under `data/local/` are excluded from git, so they cannot be committed by accident.

**Disk use.** Measured with realistic test data, the raw database grows by about **40 MB per 1,000 matches**; real Riot payloads may be somewhat larger. One run typically adds a few hundred new matches. With 7 backups, the backups folder uses about 7× the database size. Use `--keep-backups N` (at least 1) if you need less.

**Using a different location.** Set `TFT_LOCAL_DB_PATH=D:\TheoryLabs\theorylabs.sqlite3` in `.env`, or pass `--db PATH` to the `local-*` commands. Backups, reports and snapshots then go next to it. Database URLs (`postgres://…`) are always refused.

## Backups and restoring

Before every run (except the very first), the database is copied with SQLite's own backup mechanism. This is safe even while the database is in use, unlike copying the file by hand. The copy is integrity-checked, and only then are backups beyond the newest 7 deleted. The active database is never deleted.

To restore a backup:

1. Make sure no TheoryLabs command is running.
2. Rename `data/local/theorylabs.sqlite3` to `theorylabs-broken.sqlite3`. Delete any `theorylabs.sqlite3-wal` / `-shm` files next to it.
3. Copy the chosen file from `data/local/backups/` to `data/local/theorylabs.sqlite3`.
4. Run `tftlab local-status` to check.

## Patch changes

TheoryLabs only collects inside a **trusted patch window**, because Riot's match data currently masks the exact patch version and TheoryLabs refuses to mix two patches. New patches no longer need a TheoryLabs update:

- **Riot's official schedule.** The collector reads Riot's public "Patch Schedule - Teamfight Tactics" support article (one HTTPS request, no key, at most once an hour: when the app starts and before a collection). The dashboard and `local-status` only read the saved copy. `tftlab local-patch-schedule` shows what it saw; add `--refresh` to check Riot now.
- **The rule.** A patch scheduled on Pacific date D, followed in the schedule by an entry on date N, is trusted from **00:00 Pacific Time on D+1** until **17:00 Pacific Time on N−2**. Daylight saving is handled by the time-zone database. Around every patch change about two days of matches stay unclassified; that is deliberate. This rule reproduces the 18.3 and 18.4 windows exactly. 18.2, 18.3 and 18.4 stay fixed in the code.
- **Example.** 18.4's window ends Oct 19, 17:00 PT. 18.5 (Oct 21) is trusted from Oct 22, 00:00 PT to Nov 2, 17:00 PT (18.6 is Nov 4). Collection says NOT READY in between and resumes by itself.
- **Balance updates** such as **18.6B** are not new client patches. Since TheoryLabs cannot separate matches before and after one, collection pauses from two days before it until the next patch's window starts (for 18.6B: Nov 9, 17:00 PT until 19.1's window on Dec 2).
- **Frozen once used.** The first collection in a new window records it in `data/local/patch-windows-frozen.json`, with a copy in `data/local/backups/patch-windows/`. From then on it never moves, even if Riot later edits the schedule. If Riot moves the next patch *earlier* than a window already in use allows, collection pauses rather than changing history.
- **If the record is lost or changed.** The collector restores it from the newest good copy, never by working it out again, and never accepts an edited record as new history. If no good copy is left and stored matches were classified with a window that is now unknown, collection pauses until the record is restored by hand.
- **No schedule, no new window.** If Riot's page cannot be read or looks different than expected, the last good copy is kept. A new window is only started from a copy checked within the last 48 hours. The last patch Riot lists has no window until Riot lists the one after it.

## Updating TheoryLabs

- **GitHub Desktop or git:** pull the latest `main` (`git pull`). Your `.env` and `data/local/` are not touched.
- **ZIP download:** download and extract the new ZIP into a new folder, then move your `.env` file and the whole `data\local` folder from the old folder into the new one. Then run the setup steps 4 and onwards in the new folder (`local-init` will leave your files unchanged).

After updating, run `.\.venv\Scripts\pip install -e .` again: it is harmless and picks up new requirements. (On Windows this adds the `tzdata` package, the time-zone data the patch calendar needs; without it new patch windows are not created and the dashboard says so.)

## Options (rarely needed)

```text
tftlab local-collect [--db PATH] [--keep-backups 7]
                     [--challenger-seeds 15] [--grandmaster-seeds 15] [--master-seeds 20]
                     [--diamond-seeds 25] [--platinum-seeds 25] [--matches-per-seed 10]
                     [--collection-mode bounded|maximum]
                     [--max-duration-minutes 45] [--max-requests 2400] [--max-match-fetches 2000]
tftlab local-status [--db PATH] [--check-riot]
tftlab local-patch-schedule [--db PATH] [--refresh] [--json]
tftlab local-snapshot [--db PATH] [--out PATH] [--balance-window W ...]
tftlab local-publish [--db PATH] [--dry-run] [--force]
tftlab local-set-render-key
tftlab local-init
tftlab local-set-key
```

The defaults are the bounded settings that worked in production. Collection is always limited to the current verified patch. Maximum mode (above) is opt-in and always stops at its budgets; there is no unlimited mode.

## Limitations

- **Development keys expire about every 24 hours.** A key that expires mid-run stops that run safely (see above).
- **No automatic scheduling.** You run the command when you want to. A Windows Task Scheduler or cron schedule makes sense later, once there is a Riot production key that does not expire daily.
- **Sleep and network.** The computer must stay awake and online during a run. An interrupted run is safe to repeat.
- **Windows PowerShell script policy** may block `.ps1` files. Use `scripts\local-collect.cmd` or the `-ExecutionPolicy Bypass` line above.
- **One collection at a time** per database. A second one is refused while the first runs.
- **Patch dates come from Riot's schedule, not from the matches.** If Riot ships a patch more than about two days off its scheduled date without updating the page, a window can still contain a few matches from the wrong side of the change. Riot says it posts an alert on that page for delays within 48 hours; TheoryLabs does not read such alerts yet.
