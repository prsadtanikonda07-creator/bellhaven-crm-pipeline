"""
CHOW (Change of Ownership) logic.

SOP rule (verbatim from assessment):
  When an account needs to move to a different parent, check lifetime_revenue
  and outstanding_ar first.

  - If the account has revenue history AND outstanding AR > 0:
      Do NOT re-parent the existing account.
      Instead: leave the old account exactly as-is, create a NEW account under
      the correct parent, and set chow_current_account on the OLD account to the
      new account's id.

  - Otherwise (no revenue history, or outstanding_ar == 0):
      Re-parent the existing account directly.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def chow_required(account: dict) -> bool:
    """
    Returns True if the CHOW path must be used (preserve old, create new).

    Both conditions must be true:
      1. lifetime_revenue is set and > 0
      2. outstanding_ar is set and > 0
    """
    revenue = _to_float(account.get("lifetime_revenue"))
    ar = _to_float(account.get("outstanding_ar"))
    return (revenue is not None and revenue > 0) and (ar is not None and ar > 0)


def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def describe_chow(account: dict) -> str:
    """Human-readable explanation of the CHOW decision for the review UI."""
    revenue = _to_float(account.get("lifetime_revenue"))
    ar = _to_float(account.get("outstanding_ar"))

    if chow_required(account):
        return (
            f"⚠️  CHOW required — account has lifetime revenue "
            f"${revenue:,.2f} and outstanding AR ${ar:,.2f}. "
            f"Old account must be preserved; a new account will be created under "
            f"the Bellhaven parent and chow_current_account will be set on the old record."
        )
    elif revenue and revenue > 0:
        return (
            f"Direct re-parent allowed — account has lifetime revenue "
            f"${revenue:,.2f} but outstanding AR is "
            f"{'$0' if ar == 0 else 'not set'}. No CHOW needed."
        )
    else:
        return "Direct re-parent allowed — no revenue history on this account."
