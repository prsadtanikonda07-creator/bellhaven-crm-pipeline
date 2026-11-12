"""
Main pipeline orchestration.

Run order
---------
1. Scrape all Bellhaven communities from the website.
2. Fetch all accounts from the CRM.
3. Find the Bellhaven parent account.
4. For each scraped location, run the matcher.
5. Find orphaned CRM accounts (under Bellhaven, not on website).
6. Apply CHOW logic to any reparenting proposals.
7. Persist proposals to SQLite (skipping already-decided ones).

Re-runs are safe: proposals with existing decisions are never re-proposed.
"""

import logging
import sys
from pathlib import Path
from typing import Optional

from crm_client import CRMClient
from scraper import scrape_bellhaven
from matcher import Matcher, NEW_ACCOUNT, CONFIDENT_MATCH, NEEDS_FIX_PARENT, NEEDS_FIX_BOTH
from chow import chow_required, describe_chow
import db as database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")


def run(site: str = "https://analyst-assessment-production.up.railway.app",
        db_path: Optional[Path] = None) -> dict:
    """
    Execute the full pipeline.  Returns a summary dict.
    """
    if db_path is None:
        db_path = database.DB_PATH

    database.init_db(db_path)
    crm = CRMClient()

    # ------------------------------------------------------------------ #
    # 1. Scrape website
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 1: Scraping Bellhaven website ===")
    scraped_locations = scrape_bellhaven(site)
    logger.info("Scraped %d locations", len(scraped_locations))

    if not scraped_locations:
        logger.error("No locations found — check the scraper against the live site.")
        sys.exit(1)

    # ------------------------------------------------------------------ #
    # 2. Fetch all CRM accounts
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 2: Fetching CRM accounts ===")
    all_accounts = crm.get_all_accounts()
    logger.info("Fetched %d CRM accounts", len(all_accounts))

    # ------------------------------------------------------------------ #
    # 3. Find Bellhaven parent account
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 3: Finding Bellhaven parent account ===")
    matcher = Matcher(all_accounts)
    parent_account = matcher.find_bellhaven_parent()

    if parent_account:
        bellhaven_parent_id = parent_account.get("id")
        logger.info(
            "Bellhaven parent: '%s'  (id=%s)",
            parent_account.get("name"), bellhaven_parent_id,
        )
        # Re-instantiate matcher with parent id known
        matcher = Matcher(all_accounts, bellhaven_parent_id)
    else:
        bellhaven_parent_id = None
        logger.warning("Could not find Bellhaven parent account in CRM")

    # ------------------------------------------------------------------ #
    # 4. Match each scraped location
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 4: Matching scraped locations to CRM accounts ===")
    matched_crm_ids: set[str] = set()
    proposal_ids: list[str] = []
    summary = {
        "total_scraped": len(scraped_locations),
        "confident_match": 0,
        "needs_fix": 0,
        "new_account": 0,
        "orphaned": 0,
        "skipped_already_decided": 0,
        "chow_cases": 0,
    }

    for loc in scraped_locations:
        loc_dict = loc.to_dict()
        result = matcher.match_location(loc_dict)

        # Track matched CRM ids (to identify orphans later)
        if result.crm_account:
            matched_crm_ids.add(result.crm_account.get("id", ""))

        # Apply CHOW logic to reparenting proposals
        chow_note = ""
        if result.match_type in (NEEDS_FIX_PARENT, NEEDS_FIX_BOTH) and result.crm_account:
            if chow_required(result.crm_account):
                summary["chow_cases"] += 1
                chow_note = describe_chow(result.crm_account)
                # Flag in proposed_changes so the app knows to use CHOW path
                result.proposed_changes["_chow"] = True
                result.issues.insert(0, chow_note)
            else:
                chow_note = describe_chow(result.crm_account)
                result.evidence["chow_note"] = chow_note

        result.evidence["chow_note"] = chow_note if chow_note else "N/A"

        pid = database.upsert_proposal(
            match_type=result.match_type,
            scraped=result.scraped,
            crm_account=result.crm_account,
            issues=result.issues,
            proposed_changes=result.proposed_changes,
            evidence=result.evidence,
            db_path=db_path,
        )

        if pid is None:
            summary["skipped_already_decided"] += 1
            logger.debug("Skipped (already decided): %s", loc_dict.get("name"))
        else:
            proposal_ids.append(pid)
            if result.match_type == CONFIDENT_MATCH:
                summary["confident_match"] += 1
            elif result.match_type == NEW_ACCOUNT:
                summary["new_account"] += 1
            else:
                summary["needs_fix"] += 1

    # ------------------------------------------------------------------ #
    # 5. Duplicate detection
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 5: Detecting duplicate accounts ===")
    duplicate_ids = _detect_duplicates(all_accounts, bellhaven_parent_id, proposal_ids, summary, db_path)
    # A duplicate already has its own proposal; don't also call it orphaned.
    matched_crm_ids |= duplicate_ids

    # ------------------------------------------------------------------ #
    # 7. Orphaned accounts
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 7: Finding orphaned accounts ===")
    orphans = matcher.find_orphaned_accounts(matched_crm_ids)
    logger.info("Found %d orphaned accounts", len(orphans))

    for result in orphans:
        pid = database.upsert_proposal(
            match_type=result.match_type,
            scraped=result.scraped,
            crm_account=result.crm_account,
            issues=result.issues,
            proposed_changes=result.proposed_changes,
            evidence=result.evidence,
            db_path=db_path,
        )
        if pid is None:
            summary["skipped_already_decided"] += 1
        else:
            proposal_ids.append(pid)
            summary["orphaned"] += 1

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    logger.info("=== PIPELINE COMPLETE ===")
    logger.info("  Scraped locations :  %d", summary["total_scraped"])
    logger.info("  Confident matches :  %d", summary["confident_match"])
    logger.info("  Need fixes        :  %d", summary["needs_fix"])
    logger.info("  New accounts      :  %d", summary["new_account"])
    logger.info("  Orphaned          :  %d", summary["orphaned"])
    logger.info("  CHOW cases        :  %d", summary["chow_cases"])
    logger.info("  Duplicates        :  %d", summary.get("duplicates", 0))
    logger.info("  Already decided   :  %d", summary["skipped_already_decided"])
    logger.info("  New proposals     :  %d", len(proposal_ids))

    return summary


def _detect_duplicates(
    all_accounts: list[dict],
    bellhaven_parent_id: Optional[str],
    proposal_ids: list[str],
    summary: dict,
    db_path,
) -> set[str]:
    """
    Find accounts under the Bellhaven parent that share the same name AND city.
    Proposes marking the lower-quality record as a duplicate of the other.
    Returns the ids of the records proposed as duplicates.
    """
    from collections import defaultdict
    from matcher import _crm_name, _crm_parent

    duplicate_ids: set[str] = set()
    if not bellhaven_parent_id:
        return duplicate_ids

    # Group Bellhaven accounts by normalised (name, city) key
    groups: dict = defaultdict(list)
    for acc in all_accounts:
        if _crm_parent(acc) != bellhaven_parent_id:
            continue
        name_key = _crm_name(acc).lower().strip()
        city_key = (acc.get("billing_city") or acc.get("city") or "").lower().strip()
        if name_key:
            groups[(name_key, city_key)].append(acc)

    summary.setdefault("duplicates", 0)

    for (name_key, city_key), group in groups.items():
        if len(group) < 2:
            continue

        # Sort: prefer account with more data filled in (use id as stable tiebreaker)
        group.sort(key=lambda a: (
            -len([v for v in a.values() if v]),   # more fields filled = better
            a.get("id", ""),
        ))
        survivor = group[0]
        for loser in group[1:]:
            duplicate_ids.add(loser.get("id", ""))
            issues = [
                f"Duplicate of '{_crm_name(survivor)}' (id={survivor.get('id')}). "
                f"Recommend setting duplicate_of_account={survivor.get('id')} "
                f"and status=Inactive on this record."
            ]
            proposed = {
                "duplicate_of_account": survivor.get("id"),
                "status": "Inactive",
            }
            pid = database.upsert_proposal(
                match_type="DUPLICATE",
                scraped=None,
                crm_account=loser,
                issues=issues,
                proposed_changes=proposed,
                evidence={
                    "crm_name": _crm_name(loser),
                    "crm_account_id": loser.get("id"),
                    "survivor_id": survivor.get("id"),
                    "survivor_name": _crm_name(survivor),
                    "city": city_key,
                },
                db_path=db_path,
            )
            if pid:
                proposal_ids.append(pid)
                summary["duplicates"] += 1
                logger.info(
                    "Duplicate: '%s' (%s) → survivor is %s",
                    _crm_name(loser), loser.get("id"), survivor.get("id"),
                )
    return duplicate_ids


if __name__ == "__main__":
    run()
