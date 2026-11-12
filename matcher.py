"""
Matching logic: links each scraped Bellhaven location to the right CRM account
and classifies the relationship so the reviewer knows exactly what to approve.

Match types
-----------
CONFIDENT_MATCH   – name + address both align strongly; no changes needed
NEEDS_FIX_NAME    – found the right account but name in CRM is outdated/wrong
NEEDS_FIX_PARENT  – right facility, but its parent_id doesn't point to Bellhaven
NEEDS_FIX_BOTH    – name AND parent both need updating
NEW_ACCOUNT       – no CRM record found; needs to be created
ORPHANED          – CRM account exists under Bellhaven parent but not on website
                    (may be inactive, sold, or renamed)
"""

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from rapidfuzz import fuzz, process

logger = logging.getLogger(__name__)

# --- Match type constants -------------------------------------------
CONFIDENT_MATCH = "CONFIDENT_MATCH"
NEEDS_FIX_NAME = "NEEDS_FIX_NAME"
NEEDS_FIX_PARENT = "NEEDS_FIX_PARENT"
NEEDS_FIX_BOTH = "NEEDS_FIX_BOTH"
NEW_ACCOUNT = "NEW_ACCOUNT"
ORPHANED = "ORPHANED"

# Thresholds (tuned by experimentation)
NAME_EXACT_THRESHOLD = 92   # fuzzy name score for confident name match
NAME_FUZZY_THRESHOLD = 70   # fuzzy name score for "probably same" match
ZIP_WEIGHT = 30             # bonus applied when zip codes match


@dataclass
class MatchResult:
    match_type: str
    scraped: Optional[dict] = None          # scraped location dict
    crm_account: Optional[dict] = None      # matched CRM account (if any)
    bellhaven_parent_id: Optional[str] = None
    name_score: float = 0.0
    address_score: float = 0.0
    issues: list[str] = field(default_factory=list)
    proposed_changes: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


# ------------------------------------------------------------------ #
# Normalisation helpers
# ------------------------------------------------------------------ #

_STRIP_WORDS = re.compile(
    r"\b(the|a|an|at|of|and|at|llc|inc|ltd|corp|senior|living|community|"
    r"communities|care|center|centre|facility|facilities|homes?|village|"
    r"gardens?|estates?|manor|place|court|apartments?|residences?|assisted|"
    r"independent|memory|skilled|nursing|rehabilitation)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name)
    name = _PUNCT.sub(" ", name)
    name = _STRIP_WORDS.sub(" ", name)
    name = _SPACES.sub(" ", name).strip().lower()
    return name


def normalize_address(addr: str) -> str:
    """Lowercase, remove punctuation, normalise direction abbreviations."""
    if not addr:
        return ""
    addr = addr.lower()
    # Expand common street-type abbreviations for consistency
    replacements = {
        r"\bst\.?\b": "street",
        r"\bave\.?\b": "avenue",
        r"\bblvd\.?\b": "boulevard",
        r"\bdr\.?\b": "drive",
        r"\brd\.?\b": "road",
        r"\bln\.?\b": "lane",
        r"\bct\.?\b": "court",
        r"\bpl\.?\b": "place",
        r"\bnw\b": "northwest",
        r"\bne\b": "northeast",
        r"\bsw\b": "southwest",
        r"\bse\b": "southeast",
        r"\bn\b": "north",
        r"\bs\b": "south",
        r"\be\b": "east",
        r"\bw\b": "west",
    }
    addr = _PUNCT.sub(" ", addr)
    for pattern, replacement in replacements.items():
        addr = re.sub(pattern, replacement, addr)
    return _SPACES.sub(" ", addr).strip()


def _light_name(name: str) -> str:
    """Case/spacing-insensitive, but keeps every word, so 'of' vs 'at' or
    'Centre' vs 'Center' still count as a difference worth fixing."""
    name = unicodedata.normalize("NFKC", name or "")
    name = re.sub(r"[‐-―]", "-", name)
    return _SPACES.sub(" ", name).strip().casefold()


def _street_number(addr: str) -> str:
    m = re.match(r"\s*(\d+)", addr or "")
    return m.group(1) if m else ""


def _same_street(scraped_street: str, crm_street: str) -> bool:
    """Same house number and near-identical street after normalisation."""
    na, nb = normalize_address(scraped_street), normalize_address(crm_street)
    if not na or not nb:
        return False
    num_a, num_b = _street_number(na), _street_number(nb)
    if num_a and num_b and num_a != num_b:
        return False
    return fuzz.token_sort_ratio(na, nb) >= 85


def _zip_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return a[:5].strip() == b[:5].strip()


def _name_similarity(a: str, b: str) -> float:
    """
    Returns 0-100 similarity.  Uses token_sort_ratio so word-order
    differences ("Bellhaven at Oak Park" vs "Oak Park by Bellhaven") score high.
    """
    na = normalize_name(a)
    nb = normalize_name(b)
    if not na or not nb:
        return 0.0
    scores = [
        fuzz.token_sort_ratio(na, nb),
        fuzz.token_set_ratio(na, nb),
        fuzz.partial_ratio(na, nb),
    ]
    return max(scores)


def _address_similarity(scraped_addr: str, crm_addr: str) -> float:
    na = normalize_address(scraped_addr)
    nb = normalize_address(crm_addr)
    if not na or not nb:
        return 0.0
    return max(
        fuzz.token_sort_ratio(na, nb),
        fuzz.partial_ratio(na, nb),
    )


# ------------------------------------------------------------------ #
# CRM account helpers
# ------------------------------------------------------------------ #

def _full_crm_address(acc: dict) -> str:
    parts = [
        acc.get("address", "") or acc.get("street", "") or acc.get("billing_street", ""),
        acc.get("city", "") or acc.get("billing_city", ""),
        acc.get("state", "") or acc.get("billing_state", ""),
        acc.get("zip", "") or acc.get("zip_code", "") or acc.get("billing_zip", ""),
    ]
    return " ".join(p for p in parts if p)


def _crm_zip(acc: dict) -> str:
    return (
        acc.get("zip", "")
        or acc.get("zip_code", "")
        or acc.get("billing_zip", "")
        or acc.get("postal_code", "")
    )


def _crm_name(acc: dict) -> str:
    return acc.get("name", "") or acc.get("account_name", "")


def _crm_parent(acc: dict) -> Optional[str]:
    return acc.get("parent_id") or acc.get("parent_account_id")


def is_superseded(acc: dict) -> bool:
    """Already resolved by a past decision: a duplicate pointing at its survivor,
    or an old account preserved for billing after a change of ownership."""
    return bool(acc.get("duplicate_of_account") or acc.get("chow_current_account"))


def is_parent_account(acc: dict) -> bool:
    return "(parent account)" in _crm_name(acc).lower()


# ------------------------------------------------------------------ #
# Core matcher
# ------------------------------------------------------------------ #

class Matcher:
    def __init__(self, all_accounts: list[dict], bellhaven_parent_id: Optional[str] = None):
        self.all_accounts = all_accounts
        self.bellhaven_parent_id = bellhaven_parent_id

        # Pre-build normalised name index for fast lookups
        self._name_index: dict[str, dict] = {}
        for acc in all_accounts:
            norm = normalize_name(_crm_name(acc))
            if norm:
                self._name_index[norm] = acc

        # Accounts already under Bellhaven parent
        self.bellhaven_accounts: list[dict] = []
        if bellhaven_parent_id:
            self.bellhaven_accounts = [
                a for a in all_accounts
                if _crm_parent(a) == bellhaven_parent_id
                or (a.get("id") == bellhaven_parent_id)  # exclude parent itself
            ]
            # Remove parent account from list
            self.bellhaven_accounts = [
                a for a in self.bellhaven_accounts
                if a.get("id") != bellhaven_parent_id
            ]

    def find_bellhaven_parent(self) -> Optional[dict]:
        """Find the parent Bellhaven account in the CRM."""
        # Try exact name search first. The CRM labels parent orgs with a
        # "(Parent Account)" suffix, e.g. "Bellhaven Senior Living (Parent Account)".
        for acc in self.all_accounts:
            name = _crm_name(acc).lower().replace("(parent account)", "").strip()
            if name in ("bellhaven senior living", "bellhaven"):
                parent_type = acc.get("type", "") or acc.get("account_type", "")
                parent_flag = acc.get("is_parent") or acc.get("parent_account")
                if parent_type or parent_flag or not acc.get("parent_id"):
                    return acc

        # Fuzzy fallback
        candidates = [
            a for a in self.all_accounts
            if "bellhaven" in _crm_name(a).lower()
        ]
        for acc in candidates:
            if not _crm_parent(acc):  # top-level accounts have no parent
                return acc

        # Last resort: pick highest fuzzy match
        if candidates:
            return candidates[0]
        return None

    def match_location(self, loc_dict: dict) -> MatchResult:
        """
        Find the best CRM account for a scraped location and return a MatchResult.
        """
        name = loc_dict.get("name", "")
        address = loc_dict.get("address", "")
        city = loc_dict.get("city", "")
        state = loc_dict.get("state", "")
        zip_code = loc_dict.get("zip_code", "")
        scraped_full_addr = f"{address} {city} {state} {zip_code}".strip()

        # --- Candidate selection -------------------------------------
        # Address is the identity of a facility; names drift (renames, CHOW).
        # A candidate must share the location (same ZIP or city) AND either
        # the street address or a strong name match. Name alone across cities
        # is never enough: "Bellhaven of Kettering" is not "Bellhaven of Marion".
        candidates: list[tuple[tuple, dict]] = []
        for acc in self.all_accounts:
            if is_parent_account(acc) or is_superseded(acc):
                continue
            same_place = _zip_match(zip_code, _crm_zip(acc)) or (
                city and city.casefold() == (acc.get("billing_city") or acc.get("city") or "").casefold()
            )
            if not same_place:
                continue
            crm_street = acc.get("billing_street") or acc.get("address") or acc.get("street") or ""
            street_ok = _same_street(address, crm_street)
            exact_name = _light_name(name) == _light_name(_crm_name(acc))
            name_sim = _name_similarity(name, _crm_name(acc))
            # Without a street match, accept a name match only if it is exact,
            # or the CRM has no real street to contradict it (e.g. a PO Box).
            # Fuzzy subset scores are too generous ("Union Square" fits inside
            # "Bellhaven at Union Square") to override a different street.
            if not street_ok and not (
                exact_name or (name_sim >= NAME_EXACT_THRESHOLD and not _street_number(crm_street))
            ):
                continue
            rank = (
                street_ok,
                exact_name,
                name_sim,
                _crm_parent(acc) == self.bellhaven_parent_id,
                not acc.get("duplicate_of_account"),
                # same tiebreak as duplicate detection, so the survivor wins
                len([v for v in acc.values() if v]),
            )
            candidates.append((rank, acc))

        candidates.sort(key=lambda x: x[1].get("id", ""))   # ties: lowest id first
        candidates.sort(key=lambda x: x[0], reverse=True)    # stable, so ties keep id order

        if not candidates:
            # No CRM account found anywhere
            return MatchResult(
                match_type=NEW_ACCOUNT,
                scraped=loc_dict,
                bellhaven_parent_id=self.bellhaven_parent_id,
                issues=["No matching CRM account found — needs to be created"],
                evidence={
                    "scraped_name": name,
                    "scraped_address": scraped_full_addr,
                },
            )

        _, best_acc = candidates[0]
        acc_id = best_acc.get("id")
        acc_name = _crm_name(best_acc)
        acc_parent = _crm_parent(best_acc)
        acc_full_addr = _full_crm_address(best_acc)

        name_score = _name_similarity(name, acc_name)
        addr_score = _address_similarity(scraped_full_addr, acc_full_addr)

        # --- Determine what's wrong -----------------------------------
        issues = []
        proposed_changes = {}

        # The website is the source of truth for the name: any wording
        # difference (of/at, Center/Centre) is a fix, not a fuzzy match.
        name_ok = _light_name(name) == _light_name(acc_name)
        parent_ok = (acc_parent == self.bellhaven_parent_id) if self.bellhaven_parent_id else True

        if not name_ok:
            issues.append(
                f"Name mismatch: CRM has '{acc_name}', website shows '{name}' "
                f"(similarity {name_score:.0f}%)"
            )
            proposed_changes["name"] = name

        if not parent_ok:
            current_parent_name = "none" if not acc_parent else acc_parent
            issues.append(
                f"Wrong parent: account parent_id is '{current_parent_name}', "
                f"should be Bellhaven ({self.bellhaven_parent_id})"
            )
            proposed_changes["parent_id"] = self.bellhaven_parent_id

        # Care types / status
        crm_status = best_acc.get("status", "Active")
        if crm_status and crm_status.lower() == "inactive":
            issues.append("Account is marked Inactive in CRM but appears active on website")
            proposed_changes["status"] = "Active"

        # Determine match type
        if not issues:
            match_type = CONFIDENT_MATCH
        elif not name_ok and not parent_ok:
            match_type = NEEDS_FIX_BOTH
        elif not name_ok:
            match_type = NEEDS_FIX_NAME
        else:
            match_type = NEEDS_FIX_PARENT

        return MatchResult(
            match_type=match_type,
            scraped=loc_dict,
            crm_account=best_acc,
            bellhaven_parent_id=self.bellhaven_parent_id,
            name_score=name_score,
            address_score=addr_score,
            issues=issues,
            proposed_changes=proposed_changes,
            evidence={
                "scraped_name": name,
                "scraped_address": scraped_full_addr,
                "crm_name": acc_name,
                "crm_address": acc_full_addr,
                "crm_parent_id": acc_parent,
                "crm_status": crm_status,
                "name_similarity": round(name_score, 1),
                "address_similarity": round(addr_score, 1),
                "crm_account_id": acc_id,
            },
        )

    def find_orphaned_accounts(self, matched_crm_ids: set[str]) -> list[MatchResult]:
        """
        Return MatchResults for CRM accounts under Bellhaven that have no
        corresponding scraped location.  These may be closed, sold, or renamed.
        """
        orphans = []
        for acc in self.bellhaven_accounts:
            acc_id = acc.get("id")
            if acc_id in matched_crm_ids:
                continue
            if acc.get("id") == self.bellhaven_parent_id or is_superseded(acc):
                continue

            orphans.append(MatchResult(
                match_type=ORPHANED,
                crm_account=acc,
                bellhaven_parent_id=self.bellhaven_parent_id,
                issues=[
                    "Account exists under Bellhaven in CRM but not found on website. "
                    "May be closed, sold, or renamed. Consider marking Inactive or Needs Review."
                ],
                proposed_changes={
                    "status": "Needs Review",
                    "note": (
                        f"Listed under Bellhaven Senior Living but not on the Bellhaven website "
                        f"as of {date.today().isoformat()}. May be closed, sold, or renamed; "
                        f"confirm ownership before changing the parent."
                    ),
                },
                evidence={
                    "crm_name": _crm_name(acc),
                    "crm_address": _full_crm_address(acc),
                    "crm_status": acc.get("status", ""),
                    "crm_account_id": acc_id,
                },
            ))
        return orphans
