"""
Bellhaven Senior Living website scraper — rewritten for the actual site structure.

Site structure (confirmed by inspection):
  Homepage:  /
  Index:     /communities          (paginated: ?page=1, ?page=2, ?page=3)
  Detail:    /communities/<slug>

Detail page fields (confirmed example):
  Name:          <h1>Bellhaven of Maplewood</h1>
  Address label: "Address"  → "210 Orchard Lane"
  City/State/Zip: "Maplewood, OH 44280"
  Care Offerings: "Assisted Living"
  Administrator:  "Nadia Duval"
  Phone:          "(614) 250-9447"
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_SITE = "https://analyst-assessment-production.up.railway.app"
COMMUNITIES_PATH = "/communities"

CITY_STATE_ZIP_RE = re.compile(
    r"^([A-Za-z\s.'-]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$"
)


@dataclass
class ScrapedLocation:
    name: str
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    care_types: list[str] = field(default_factory=list)
    administrator: str = ""
    phone: str = ""
    source_url: str = ""

    def full_address(self) -> str:
        return f"{self.address}, {self.city}, {self.state} {self.zip_code}".strip(", ")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "zip_code": self.zip_code,
            "care_types": self.care_types,
            "administrator": self.administrator,
            "phone": self.phone,
            "source_url": self.source_url,
        }


def _get(url: str, session: requests.Session) -> Optional[BeautifulSoup]:
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            logger.warning("Attempt %d failed for %s: %s", attempt + 1, url, e)
            time.sleep(1.5 ** attempt)
    return None


def _parse_detail_page(soup: BeautifulSoup, url: str) -> Optional[ScrapedLocation]:
    """
    Parse a community detail page.

    The page renders labeled fields like:
      dt/dd pairs, or divs with a label span + value span.
    We collect ALL text nodes and find the values after each known label.
    """
    # Name from <h1>
    h1 = soup.find("h1")
    if not h1:
        logger.warning("No <h1> on %s", url)
        return None
    name = h1.get_text(strip=True)

    loc = ScrapedLocation(name=name, source_url=url)

    # Strategy: find every visible text node and pair labels with their values.
    # The site uses a simple definition-list or labeled-field pattern.
    # We walk through <dt>/<dd> pairs, or any element whose text is a known label.

    # First try <dl> / <dt> / <dd>
    dl = soup.find("dl")
    if dl:
        dts = dl.find_all("dt")
        dds = dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label = dt.get_text(strip=True).lower()
            # separator keeps <br>-split lines apart ("street<br>City, ST zip")
            value = dd.get_text("\n", strip=True)
            _apply_field(loc, label, value)

    # Fallback: scan every element for known label patterns
    # (handles tables, divs with label+value spans, etc.)
    if not loc.address:
        full_text = soup.get_text(separator="\n", strip=True)
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]
        _parse_lines(loc, lines)

    return loc


def _apply_field(loc: ScrapedLocation, label: str, value: str):
    if "address" in label:
        # Value might be "210 Orchard Lane\nMaplewood, OH 44280"
        parts = [p.strip() for p in value.replace(",", ", ").splitlines() if p.strip()]
        for part in parts:
            m = CITY_STATE_ZIP_RE.match(part)
            if m:
                loc.city = m.group(1).strip()
                loc.state = m.group(2).upper()
                loc.zip_code = m.group(3)
            elif part and not loc.address:
                loc.address = part
    elif "care" in label:
        # One badge per offering (one line each); "&" is part of a name
        # like "Short-Term Rehabilitation & Nursing", not a separator.
        loc.care_types = [v.strip() for v in re.split(r"[,\n]", value) if v.strip()]
    elif "administrator" in label or "admin" in label:
        loc.administrator = value
    elif "phone" in label or "tel" in label:
        loc.phone = value


def _parse_lines(loc: ScrapedLocation, lines: list[str]):
    """
    Fallback: scan text lines for labeled content.
    Handles layouts where the label and value end up on separate lines.
    """
    LABELS = {
        "address": "address",
        "care offerings": "care",
        "care type": "care",
        "administrator": "administrator",
        "phone": "phone",
    }
    i = 0
    while i < len(lines):
        line_lower = lines[i].lower()
        matched_label = None
        for label_text, field_type in LABELS.items():
            if lines[i].lower().startswith(label_text):
                matched_label = field_type
                # Value might be on same line (after label) or next line
                after_label = lines[i][len(label_text):].strip()
                if after_label:
                    _store_value(loc, field_type, after_label, lines, i)
                elif i + 1 < len(lines):
                    _store_value(loc, field_type, lines[i + 1], lines, i + 1)
                break
        # Also try to find city/state/zip pattern on any line
        if not loc.city:
            m = CITY_STATE_ZIP_RE.match(lines[i])
            if m:
                loc.city = m.group(1).strip()
                loc.state = m.group(2).upper()
                loc.zip_code = m.group(3)
        i += 1

    # Last resort for address: look for a line starting with a number
    if not loc.address:
        for line in lines:
            if re.match(r"^\d+\s+\w", line) and not CITY_STATE_ZIP_RE.match(line):
                loc.address = line
                break


def _store_value(loc: ScrapedLocation, field_type: str, value: str, lines: list, idx: int):
    if field_type == "address":
        m = CITY_STATE_ZIP_RE.match(value)
        if m:
            loc.city = m.group(1).strip()
            loc.state = m.group(2).upper()
            loc.zip_code = m.group(3)
        else:
            if re.match(r"^\d+\s+\w", value):
                loc.address = value
                # Next line might be city/state/zip
                if idx + 1 < len(lines):
                    m2 = CITY_STATE_ZIP_RE.match(lines[idx + 1])
                    if m2:
                        loc.city = m2.group(1).strip()
                        loc.state = m2.group(2).upper()
                        loc.zip_code = m2.group(3)
    elif field_type == "care":
        loc.care_types = [v.strip() for v in re.split(r"[,&]", value) if v.strip()]
    elif field_type == "administrator":
        loc.administrator = value
    elif field_type == "phone":
        loc.phone = value


def _collect_community_links(session: requests.Session) -> list[str]:
    """
    Scrape all 3 pages of the community index and return all detail-page URLs.
    """
    links = []
    page = 1
    while True:
        url = f"{BASE_SITE}{COMMUNITIES_PATH}?page={page}"
        logger.info("Fetching community index page %d: %s", page, url)
        soup = _get(url, session)
        if not soup:
            logger.error("Could not fetch community index page %d", page)
            break

        # Every community is an <h3> containing an <a>
        for h3 in soup.find_all("h3"):
            a = h3.find("a", href=True)
            if a and "/communities/" in a["href"]:
                full_url = urljoin(BASE_SITE, a["href"])
                if full_url not in links:
                    links.append(full_url)

        # Check for "Next →" pagination link
        next_link = soup.find("a", string=lambda s: s and "next" in s.lower())
        if not next_link:
            break
        page += 1
        time.sleep(0.3)

    return links


def scrape_bellhaven(site: str = BASE_SITE) -> list[ScrapedLocation]:
    """
    Scrape all Bellhaven communities. Returns a list of ScrapedLocation objects.
    """
    session = requests.Session()
    session.headers["User-Agent"] = "BellhavenPipeline/1.0 (assessment)"

    community_urls = _collect_community_links(session)
    logger.info("Found %d community pages to scrape", len(community_urls))

    locations = []
    for url in community_urls:
        logger.info("Scraping: %s", url)
        soup = _get(url, session)
        if not soup:
            logger.warning("Skipped (fetch failed): %s", url)
            continue
        loc = _parse_detail_page(soup, url)
        if loc:
            locations.append(loc)
            logger.debug(
                "  %s | %s, %s %s | %s",
                loc.name, loc.city, loc.state, loc.zip_code, loc.care_types,
            )
        time.sleep(0.3)

    logger.info("Scraping complete — %d locations collected", len(locations))
    return locations
