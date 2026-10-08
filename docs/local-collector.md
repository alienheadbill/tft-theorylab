# Collect TheoryLabs data on your own computer (free)

TheoryLabs can collect its ranked TFT data on an ordinary Windows, macOS or
Linux computer, for $0. Everything is stored in **one file on your computer**
(a SQLite database). You do **not** need Postgres, Docker, Neon, Render or any
other cloud service.

**The only thing to install is Python 3.11 or newer.** Everything else comes
with TheoryLabs.

## The short version (every day)

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

   From then on you can simply double-click `scripts\local-collect.cmd`.

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
| **TheoryLabs does not currently have a verified collection window** | The current patch's exact start/end has not been verified and registered yet. TheoryLabs never guesses patch boundaries. Wait for a TheoryLabs update that adds the new patch window, update your copy (see "Updating TheoryLabs"), then run again. Nothing was collected. |
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
- **the patch:** the current trusted patch, and whether the current time is inside it;
- **the Riot key:** whether it is set.

It never prints the key, player identifiers or match identifiers.

## Making a public website snapshot: `tftlab local-snapshot`

The raw database is private: it contains Riot's raw match data and the list of sampled players. The website must only ever get a **sanitized snapshot**:

```powershell
.\.venv\Scripts\tftlab local-snapshot
```

This uses TheoryLabs' verified exporter. It:

- checks that the data really came from Riot collection (demo data is refused);
- removes raw payloads, player identifiers and the collection ledger;
- replaces match ids with opaque ones;
- precomputes Discovery;
- verifies the result twice.

By default it exports the current patch; use `--balance-window 18.3` for another stored, verified patch. The output goes to `data/local/public/`:

- `theorylabs-public-snapshot.sqlite3.gz`: **the file to publish**, when you decide to;
- `theorylabs-public-snapshot.sqlite3`: the same snapshot, uncompressed;
- `theorylabs-public-snapshot.sqlite3.manifest.json`: counts, provenance and SHA-256 checksums.

The command prints the match and board counts, file sizes and SHA-256 checksums. **Nothing is uploaded or deployed automatically.** Publishing is a separate, deliberate step (see the README, "Public website snapshots").

## Where everything is stored

| Path | What | Share it? |
|---|---|---|
| `.env` | Your Riot key and settings | **Never** |
| `data/local/theorylabs.sqlite3` | The raw collector database (private) | **No**: it contains player identifiers |
| `data/local/backups/theorylabs-<UTC time>.sqlite3` | Pre-run backups (newest 7 kept) | No |
| `data/local/reports/local-collect-<UTC time>.json` | Each run's report (counts only, no ids or key) | Yes, if the maintainer asks |
| `data/local/public/theorylabs-public-snapshot.sqlite3.gz` | Sanitized public snapshot | Yes, this is the publishable file |

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

TheoryLabs only collects inside **verified** patch windows. They are listed in the code (`UNREAL_PATCH_REGISTRY` in `src/tftlab/unreal_patch.py`), each with its source. When the current window ends, `local-collect` stops with the "does not currently have a verified collection window" message until the next window is verified and added. This is deliberate: Riot's match data currently masks the exact patch version, so TheoryLabs refuses to guess boundaries rather than mix two patches.

## Updating TheoryLabs

- **GitHub Desktop or git:** pull the latest `main` (`git pull`). Your `.env` and `data/local/` are not touched.
- **ZIP download:** download and extract the new ZIP into a new folder, then move your `.env` file and the whole `data\local` folder from the old folder into the new one. Then run the setup steps 4 and onwards in the new folder (`local-init` will leave your files unchanged).

After updating, running `.\.venv\Scripts\pip install -e .` again is harmless and picks up any new requirements.

## Options (rarely needed)

```text
tftlab local-collect [--db PATH] [--keep-backups 7]
                     [--challenger-seeds 15] [--grandmaster-seeds 15] [--master-seeds 20]
                     [--diamond-seeds 25] [--platinum-seeds 25] [--matches-per-seed 10]
tftlab local-status [--db PATH] [--check-riot]
tftlab local-snapshot [--db PATH] [--out PATH] [--balance-window W ...]
tftlab local-init
tftlab local-set-key
```

The defaults are the bounded settings that worked in production. Collection is always bounded and limited to the current verified patch; there is no "collect everything" mode here.

## Limitations

- **Development keys expire about every 24 hours.** A key that expires mid-run stops that run safely (see above).
- **No automatic scheduling.** You run the command when you want to. A Windows Task Scheduler or cron schedule makes sense later, once there is a Riot production key that does not expire daily.
- **Sleep and network.** The computer must stay awake and online during a run. An interrupted run is safe to repeat.
- **Windows PowerShell script policy** may block `.ps1` files. Use `scripts\local-collect.cmd` or the `-ExecutionPolicy Bypass` line above.
- **One collection at a time** per database. A second one is refused while the first runs.
