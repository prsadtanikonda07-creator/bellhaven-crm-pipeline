# Bellhaven CRM Ownership Pipeline

A daily pipeline that keeps Bellhaven Senior Living's CRM accounts in sync with their public website. It scrapes every community listing, matches each one to the right CRM account, surfaces discrepancies for a human reviewer, and writes approved changes back to the CRM.

---

## Quick start (5 minutes)

```bash
# 1. Clone / download this folder
cd bellhaven

# 2. Install dependencies (Python 3.10+)
pip install -r requirements.txt

# 3. Run the pipeline and open the review app
python run.py

# → open http://localhost:5000 in your browser
```

The pipeline runs first (~1-2 min), then the review app starts automatically.

---

## What the pipeline does

1. **Scrapes** every Bellhaven community from `https://analyst-assessment-production.up.railway.app`  
   Extracts: name, address, city, state, zip, care offerings.  
   Also follows community links on the homepage and other site pages, so a community announced there (e.g. a "New this year" notice) is picked up before it is added to the paginated directory.

2. **Fetches** all accounts from the CRM sandbox API.

3. **Finds the Bellhaven parent account** — the top-level org all facility accounts should hang off.

4. **Matches** each scraped location to its CRM account using fuzzy name matching + address/zip scoring. Classifies each match:

   | Type | Meaning |
   |------|---------|
   | ✅ Confident Match | Name + address align strongly; nothing to change |
   | ✏️ Name Fix | Right account, but name in CRM is outdated |
   | 🔗 Parent Fix | Right account, but `parent_id` doesn't point to Bellhaven |
   | ⚠️ Name + Parent Fix | Both name and parent need updating |
   | 🆕 New Account | No CRM record found; needs to be created |
   | 👻 Orphaned | In CRM under Bellhaven, but not on website |

5. **Applies CHOW logic** to any reparenting proposals (see below).

6. **Persists proposals** to `pipeline.db` (SQLite). Already-decided proposals are never re-proposed — re-runs are safe.

---

## Review app

```
http://localhost:5000
```

| Page | Purpose |
|------|---------|
| `/` | Dashboard — proposal counts by type |
| `/proposals` | Full list; filterable by type or status |
| `/proposals/<id>` | Detail — evidence, proposed changes, approve/reject |

For each proposal you can:
- **Approve** → writes the change to the CRM immediately, records the decision  
- **Reject** → records the rejection, no CRM write  

Decisions are stored in `pipeline.db`. Re-running the pipeline after decisions are recorded will never re-queue those items.

---

## CHOW (Change of Ownership) logic

When an account needs a new parent, the pipeline checks `lifetime_revenue` and `outstanding_ar`:

**Both > 0 (CHOW path):**
1. Leave the old account untouched.
2. Create a new account under the correct parent.
3. Set `chow_current_account` on the old account → new account's ID.

**Otherwise (direct re-parent):**  
Patch `parent_id` on the existing account.

The review UI shows a yellow warning when the CHOW path applies, so reviewers always see the reason.

---

## Daily schedule

**GitHub Actions** (`.github/workflows/daily_pipeline.yml`)  
Runs at 7:00 AM ET every day. The SQLite database is cached between runs so decisions persist.

**Cron alternative** (run locally or on a server):
```cron
# 7:00 AM ET every day
0 12 * * *  cd /path/to/bellhaven && python pipeline.py >> logs/pipeline.log 2>&1
```

---

## Project structure

```
bellhaven/
├── run.py              # Entry point: pipeline + app
├── pipeline.py         # Orchestration (scrape → match → persist)
├── scraper.py          # Bellhaven website scraper
├── crm_client.py       # CRM API wrapper (auth, pagination, CRUD)
├── matcher.py          # Fuzzy matching + classification logic
├── chow.py             # CHOW rule implementation
├── db.py               # SQLite persistence (proposals + decisions)
├── app.py              # Flask review app
├── requirements.txt
├── pipeline.db         # Created on first run (gitignored in prod)
└── .github/
    └── workflows/
        └── daily_pipeline.yml
```

---

## Configuration

The CRM API token is read from the `CRM_TOKEN` environment variable and is never committed:

```bash
export CRM_TOKEN=<your token>            # macOS / Linux
$env:CRM_TOKEN = "<your token>"          # Windows PowerShell
```

In GitHub Actions it comes from the `CRM_TOKEN` repository secret. `BASE_URL` is at the top of `crm_client.py`.

---

## Design decisions

**Why SQLite for decisions?**  
Lightweight, zero-dependency, works on a laptop and in GitHub Actions with artifact caching. The decision log is the source of truth for idempotency — same proposal ID (hash of scraped name + CRM id) on every run.

**Why fuzzy matching over exact matching?**  
CRM data is messy. "Bellhaven at Oak Park" vs "Bellhaven Oak Park Senior Living" are the same place. `token_sort_ratio` + `token_set_ratio` handles word-order variation; zip code boosting handles near-duplicate names in different cities.

**Why keep CONFIDENT_MATCH proposals?**  
Visibility. Reviewers can verify the pipeline's confident calls, not just its uncertain ones. Approving a confident match writes nothing to the CRM — it's just an acknowledgment.

**Why not auto-approve confident matches?**  
Human-in-the-loop is explicit in the spec: "Nothing writes without approval." A reviewer can approve batches quickly, but the approval step is never skipped.
