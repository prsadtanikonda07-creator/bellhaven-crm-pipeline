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
from datetime import date

from matcher import (
    Matcher, MatchResult, NEW_ACCOUNT, CONFIDENT_MATCH, NEEDS_FIX_PARENT, NEEDS_FIX_BOTH, ORPHANED,
    _crm_name, _crm_parent, _same_street, _zip_match, is_parent_account, is_superseded,
)
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

    matches: list[tuple[dict, dict]] = []   # (scraped location, matched CRM account)
    for loc in scraped_locations:
        loc_dict = loc.to_dict()
        result = matcher.match_location(loc_dict)

        # Track matched CRM ids (to identify orphans later)
        if result.crm_account:
            matched_crm_ids.add(result.crm_account.get("id", ""))
            matches.append((loc_dict, result.crm_account))

        # Nothing to change at a location a reviewer already decided on: after
        # an approved CHOW or new account it matches a different id, which must
        # not come back as a fresh proposal.
        if result.match_type == CONFIDENT_MATCH and database.location_reviewed(loc_dict["name"], db_path):
            summary["skipped_already_decided"] += 1
            continue

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
    duplicate_ids |= _detect_stale_same_address(all_accounts, matches, duplicate_ids, proposal_ids, summary, db_path)
    # A duplicate already has its own proposal; don't also call it orphaned.
    matched_crm_ids |= duplicate_ids

    # ------------------------------------------------------------------ #
    # 7. Orphaned accounts
    # ------------------------------------------------------------------ #
    logger.info("=== STEP 7: Finding orphaned accounts ===")
    orphans = matcher.find_orphaned_accounts(matched_crm_ids)
    orphans += _find_unparented_bellhaven(all_accounts, matched_crm_ids, bellhaven_parent_id)
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
        if _crm_parent(acc) != bellhaven_parent_id or is_superseded(acc):
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
                "note": (
                    f"Duplicate of {_crm_name(survivor)} ({survivor.get('id')}): same name and city "
                    f"under Bellhaven. Marked by the Bellhaven sync pipeline {date.today().isoformat()}."
                ),
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


def _detect_stale_same_address(
    all_accounts: list[dict],
    matches: list[tuple[dict, dict]],
    already_proposed: set[str],
    proposal_ids: list[str],
    summary: dict,
    db_path,
) -> set[str]:
    """
    Other CRM accounts at the exact street + ZIP of a matched Bellhaven location
    are left over from a previous operator (e.g. "Harborview Shores of Erie" at
    Bellhaven Shores of Erie's address). One building, one facility: propose
    marking each a duplicate of the matched account. If the stale record has
    billing history (revenue AND outstanding AR), don't deactivate it; flag it
    Needs Review for the billing team instead, mirroring the CHOW SOP.
    """
    summary.setdefault("duplicates", 0)
    flagged: set[str] = set()
    for loc, survivor in matches:
        for acc in all_accounts:
            acc_id = acc.get("id", "")
            if (
                acc_id == survivor.get("id")
                or acc_id in already_proposed or acc_id in flagged
                or is_parent_account(acc) or is_superseded(acc)
                or not _zip_match(loc.get("zip_code", ""), acc.get("billing_zip", ""))
                or not _same_street(loc.get("address", ""), acc.get("billing_street", ""))
            ):
                continue
            flagged.add(acc_id)
            billing = (acc.get("lifetime_revenue") or 0) > 0 and (acc.get("outstanding_ar") or 0) > 0
            where = f"{loc['address']}, {loc['city']} {loc['state']}"
            if billing:
                proposed = {
                    "status": "Needs Review",
                    "note": (
                        f"Same address as {loc['name']} ({survivor.get('id')}), {where}, but has billing "
                        f"history, so not deactivated. Billing team to confirm before marking duplicate."
                    ),
                }
            else:
                proposed = {
                    "duplicate_of_account": survivor.get("id"),
                    "status": "Inactive",
                    "note": (
                        f"Stale record from a previous operator: same address as {loc['name']} "
                        f"({survivor.get('id')}), {where}. Marked by the Bellhaven sync pipeline "
                        f"{date.today().isoformat()}."
                    ),
                }
            issues = [
                f"Same street address and ZIP as '{loc['name']}' ({where}), which is matched to "
                f"'{_crm_name(survivor)}' ({survivor.get('id')}). Current parent: "
                f"{acc.get('parent_name') or 'none'}."
            ]
            pid = database.upsert_proposal(
                match_type="DUPLICATE",
                scraped=None,
                crm_account=acc,
                issues=issues,
                proposed_changes=proposed,
                evidence={
                    "crm_name": _crm_name(acc),
                    "crm_account_id": acc_id,
                    "survivor_id": survivor.get("id"),
                    "survivor_name": _crm_name(survivor),
                    "shared_address": where,
                },
                db_path=db_path,
            )
            if pid:
                proposal_ids.append(pid)
                summary["duplicates"] += 1
                logger.info("Stale record: '%s' (%s) shares the address of '%s'",
                            _crm_name(acc), acc_id, loc["name"])
    return flagged


def _find_unparented_bellhaven(
    all_accounts: list[dict], matched_ids: set[str], bellhaven_parent_id: Optional[str]
) -> list[MatchResult]:
    """Bellhaven-branded accounts with no parent at all that match no website
    location. Not ours to re-parent on absence of evidence, but worth a flag."""
    found = []
    for acc in all_accounts:
        if (
            "bellhaven" not in _crm_name(acc).lower()
            or is_parent_account(acc) or is_superseded(acc)
            or _crm_parent(acc) or acc.get("id") in matched_ids
        ):
            continue
        found.append(MatchResult(
            match_type=ORPHANED,
            crm_account=acc,
            bellhaven_parent_id=bellhaven_parent_id,
            issues=[
                "Bellhaven-branded account with no parent, and not listed on the Bellhaven website. "
                "Not re-parented without evidence it is a current Bellhaven community."
            ],
            proposed_changes={
                "status": "Needs Review",
                "note": (
                    f"Bellhaven-branded but has no parent account and is not on the Bellhaven website "
                    f"as of {date.today().isoformat()}. Confirm current owner before assigning a parent."
                ),
            },
            evidence={
                "crm_name": _crm_name(acc),
                "crm_status": acc.get("status", ""),
                "crm_account_id": acc.get("id"),
            },
        ))
    return found


if __name__ == "__main__":
    run()
