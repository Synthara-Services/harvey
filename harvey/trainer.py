"""Trainer — give Harvey a URL and it learns everything about the product.

Uses Cloudflare Browser Rendering /crawl API for deep site crawling.
Falls back to manual scraping if no Cloudflare credentials are configured.
"""

import asyncio
import copy
import difflib
import logging
import os
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from harvey.brain import Brain
from harvey.config import BrainConfig, ConfigError, _find_config_file, load_brain_config
from harvey.state import StateManager

logger = logging.getLogger("harvey.trainer")

from harvey.paths import PROJECT_ROOT, SKILLS_DIR, USER_ROOT

# Settings training does not learn, used only on a first run with no config
# at all. They mirror the shipped harvey.yaml.
DEFAULT_CHANNELS = {
    "email": {
        "enabled": True,
        "provider": "gmail",
        "max_daily_sends": 50,
        "require_approval": True,
        "send_to_risky": False,
        "max_bounce_rate": 0.05,
        "signature": "",
        "opt_out_line": "",
    },
    "linkedin": {
        "enabled": True,
        "max_daily_connections": 20,
        "max_daily_messages": 10,
    },
}
DEFAULT_USAGE = {
    "max_daily_claude_percent": 80,
    "heartbeat_interval_minutes": 15,
    "quiet_hours": {"start": "22:00", "end": "07:00", "timezone": "America/New_York"},
}
# Persona values in the shipped harvey.yaml. Training fills company and
# tone from the site only while they still hold these; the sender's name,
# role, email and LinkedIn always come from the config.
TEMPLATE_PERSONA = {
    "name": "Harvey",
    "company": "Your Company",
    "email": "harvey@yourcompany.com",
    "tone": "professional, consultative, confident",
}


# The shipped template. The setup wizard trains into it directly; it is not
# a hand-edited config, so it is not protected like harvey.local.yaml.
TEMPLATE_CONFIG = PROJECT_ROOT / "harvey.yaml"


def _changed_keys(old: dict, new: dict) -> list[str]:
    """Dotted section.key names whose values differ between two configs."""
    changed = []
    for section in dict.fromkeys([*old, *new]):
        a, b = old.get(section), new.get(section)
        if isinstance(a, dict) and isinstance(b, dict):
            changed += [
                f"{section}.{key}" for key in dict.fromkeys([*a, *b])
                if a.get(key) != b.get(key)
            ]
        elif a != b:
            changed.append(section)
    return changed


def _settings_in_effect(config_path: Path) -> dict:
    """The raw settings a training run keeps.

    The file being written when it exists (a re-train), else the config
    Harvey loads today, else nothing (a first run gets the defaults above).
    Raises ConfigError for a file that exists but can't be read, so training
    never overwrites a config it could not understand.
    """
    path = config_path
    if not path.exists():
        try:
            path = Path(_find_config_file())
        except ConfigError:
            return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"Could not read {path}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping.")
    return data


def _normalize_start_url(url: str) -> str:
    """Accept 'acmecorp.com' as well as 'https://acmecorp.com'."""
    url = url.strip()
    if url and not urlparse(url).scheme:
        url = f"https://{url}"
    return url

# Fallback: pages to try if Cloudflare crawl is unavailable
FALLBACK_PATHS = [
    "/", "/about", "/about-us", "/pricing", "/features", "/product",
    "/products", "/solutions", "/services", "/how-it-works", "/why-us",
    "/case-studies", "/customers", "/testimonials", "/faq", "/contact",
    "/team", "/our-team", "/blog", "/resources", "/integrations",
]

# Cloudflare API
CF_API_BASE = "https://api.cloudflare.com/client/v4/accounts"

# How long to poll before giving up (seconds)
CRAWL_TIMEOUT = 600  # 10 minutes
POLL_INTERVAL = 10   # check every 10 seconds


class CloudflareCrawler:
    """Cloudflare Browser Rendering /crawl API client."""

    def __init__(self, account_id: str, api_token: str):
        self.account_id = account_id
        self.api_token = api_token
        self.base_url = f"{CF_API_BASE}/{account_id}/browser-rendering/crawl"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    async def crawl(
        self,
        url: str,
        max_pages: int = 100,
        depth: int = 5,
        render: bool = True,
    ) -> dict[str, str]:
        """Crawl a website and return {url: markdown_content} for all pages.

        Uses Cloudflare's /crawl endpoint which:
        - Renders JavaScript (SPAs, dynamic content)
        - Discovers pages via sitemaps + links
        - Returns content as clean markdown
        - Respects robots.txt
        - Can crawl up to 100,000 pages
        """
        async with httpx.AsyncClient(timeout=60, headers=self.headers) as client:
            # 1. Start the crawl job
            payload = {
                "url": url,
                "limit": max_pages,
                "depth": depth,
                "formats": ["markdown"],
                "render": render,
                "source": "all",
                "options": {
                    "includeSubdomains": False,
                    "includeExternalLinks": False,
                },
                "rejectResourceTypes": ["image", "media", "font", "stylesheet"],
            }

            logger.info(f"Starting Cloudflare crawl: {url} (max {max_pages} pages)")
            resp = await client.post(self.base_url, json=payload)

            if resp.status_code != 200:
                logger.error(f"Cloudflare crawl start failed: {resp.status_code} {resp.text}")
                return {}

            result = resp.json()
            if not result.get("success"):
                logger.error(f"Cloudflare crawl error: {result}")
                return {}

            job_id = result.get("result")
            if not job_id:
                logger.error("No job ID returned from Cloudflare crawl")
                return {}

            logger.info(f"Crawl job started: {job_id}")

            # 2. Poll for completion
            pages = await self._poll_job(client, job_id)
            return pages

    async def _poll_job(self, client: httpx.AsyncClient, job_id: str) -> dict[str, str]:
        """Poll a crawl job until complete, then collect all pages."""
        start_time = time.time()
        pages: dict[str, str] = {}

        while time.time() - start_time < CRAWL_TIMEOUT:
            # Check status with minimal data
            resp = await client.get(
                f"{self.base_url}/{job_id}",
                params={"limit": 1},
            )

            if resp.status_code != 200:
                logger.error(f"Poll failed: {resp.status_code}")
                break

            data = resp.json().get("result", {})
            status = data.get("status", "unknown")
            total = data.get("total", 0)
            finished = data.get("finished", 0)

            elapsed = int(time.time() - start_time)
            print(f"\r      Crawling... {finished}/{total} pages ({elapsed}s)", end="", flush=True)

            if status in ("completed", "cancelled_due_to_timeout", "cancelled_due_to_limits"):
                print()  # newline after progress
                if status != "completed":
                    logger.warning(f"Crawl ended with status: {status}")
                break

            if status == "errored":
                logger.error("Crawl job errored")
                print()
                break

            await asyncio.sleep(POLL_INTERVAL)

        # 3. Collect all results with pagination
        pages = await self._collect_results(client, job_id)
        return pages

    async def _collect_results(
        self, client: httpx.AsyncClient, job_id: str
    ) -> dict[str, str]:
        """Paginate through all crawl results and collect markdown content."""
        pages: dict[str, str] = {}
        cursor = None

        while True:
            params = {"limit": 50, "status": "completed"}
            if cursor:
                params["cursor"] = cursor

            resp = await client.get(
                f"{self.base_url}/{job_id}",
                params=params,
            )

            if resp.status_code != 200:
                break

            data = resp.json().get("result", {})
            records = data.get("records", [])

            for record in records:
                url = record.get("url", "")
                markdown = record.get("markdown", "")
                if url and markdown:
                    pages[url] = markdown

            # Check for next page
            next_cursor = data.get("cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        return pages


class FallbackCrawler:
    """Recursive HTTP crawler for when Cloudflare is not configured.

    Starts with priority pages, then follows every internal link it finds
    on each crawled page until it hits the max_pages limit.
    """

    async def crawl(self, base_url: str, max_pages: int = 100) -> dict[str, str]:
        """Recursively crawl a website by following internal links."""
        pages: dict[str, str] = {}
        visited: set[str] = set()
        domain = urlparse(base_url).netloc

        # Queue starts with priority paths, then discovered links
        queue: list[str] = [urljoin(base_url, p) for p in FALLBACK_PATHS]

        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            while queue and len(pages) < max_pages:
                url = queue.pop(0)

                # Normalize and skip if already visited
                clean_url = self._normalize_url(url)
                if clean_url in visited:
                    continue
                visited.add(clean_url)

                # Fetch the page
                html, text = await self._fetch_page(client, url)
                if not text:
                    continue

                pages[url] = text
                print(f"\r      Scraped {len(pages)} pages...", end="", flush=True)

                # Discover new links from this page and add to queue
                if html:
                    new_links = self._extract_links(html, base_url, domain)
                    for link in new_links:
                        norm = self._normalize_url(link)
                        if norm not in visited and link not in queue:
                            queue.append(link)

        print()  # newline after progress
        return pages

    async def _fetch_page(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, str]:
        """Fetch a page. Returns (raw_html, clean_text)."""
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return "", ""

            content_type = resp.headers.get("content-type", "")
            if "text/html" not in content_type:
                return "", ""

            raw_html = resp.text
            soup = BeautifulSoup(raw_html, "html.parser")

            # Remove noise elements
            for tag in soup(["script", "style", "nav", "footer", "noscript", "iframe"]):
                tag.decompose()

            text = soup.get_text(separator="\n", strip=True)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return raw_html, "\n".join(lines)

        except Exception as e:
            logger.debug(f"Failed to fetch {url}: {e}")
            return "", ""

    def _extract_links(self, html: str, base_url: str, domain: str) -> list[str]:
        """Extract all internal links from an HTML page."""
        soup = BeautifulSoup(html, "html.parser")
        links = []

        for a in soup.find_all("a", href=True):
            href = a["href"]

            # Skip anchors, mailto, tel, javascript
            if href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue

            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)

            # Only internal links
            if parsed.netloc == domain and parsed.scheme in ("http", "https"):
                # Strip query params and fragments for cleaner crawling
                clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                # Skip file downloads
                if not any(clean.endswith(ext) for ext in (".pdf", ".zip", ".png", ".jpg", ".svg", ".css", ".js")):
                    if clean not in links:
                        links.append(clean)

        return links

    def _normalize_url(self, url: str) -> str:
        """Normalize a URL for dedup (strip trailing slash, query, fragment)."""
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        return f"{parsed.scheme}://{parsed.netloc}{path}"


def brain_section(brain_config: BrainConfig) -> dict:
    """The ``brain:`` block to write back, so retraining keeps the choice."""
    section = {"provider": brain_config.provider}
    if brain_config.model:
        section["model"] = brain_config.model
    if brain_config.reasoning_effort:
        section["reasoning_effort"] = brain_config.reasoning_effort
    return section


class Trainer:
    def __init__(self, brain_config: BrainConfig | None = None):
        # Without an explicit choice, train with whatever provider the
        # existing config selects (Claude when none does).
        self.brain_config = brain_config or load_brain_config()
        self.state = StateManager()
        self.brain = Brain(self.state, self.brain_config)
        self.scraped_pages: dict[str, str] = {}
        self.skills_dir = SKILLS_DIR
        # What this run wrote, and what it proposed next to existing files.
        self.written: list[Path] = []
        self.proposed: list[tuple[Path, Path]] = []

    def _write_or_propose(self, path: Path, content: str, protect: bool = True):
        """Write a generated file, or propose it next to the existing one.

        An existing file may carry hand edits and comments (a re-train), so
        it is never overwritten: the new version goes to <name>.proposed, to
        merge by hand. A first training writes directly.
        """
        if not (protect and path.exists()):
            path.write_text(content, encoding="utf-8")
            self.written.append(path)
            return
        if path.read_text(encoding="utf-8", errors="replace") == content:
            return  # nothing new to propose
        proposed = path.with_name(path.name + ".proposed")
        proposed.write_text(content, encoding="utf-8")
        self.proposed.append((path, proposed))

    async def train(
        self,
        url: str,
        output_path: str = "harvey.local.yaml",
        max_pages: int = 100,
    ):
        """Train Harvey on a product website.

        1. Deep crawl the site (Cloudflare /crawl or fallback)
        2. Analyze all content with the configured brain
        3. Extract product info, ICP, objections, competitive intel
        4. Generate config + product knowledge skill. Files that already
           exist are never overwritten: the new versions go to *.proposed.
        """
        load_dotenv()
        await self.state.init_db()

        url = _normalize_start_url(url)
        parsed = urlparse(url)
        domain = parsed.netloc
        if not domain:
            print(f"\nERROR: '{url}' doesn't look like a valid URL.")
            print("Try something like: https://yourcompany.com")
            return None
        base_url = f"{parsed.scheme}://{domain}"

        # Relative paths resolve next to harvey.local.yaml (the project root
        # unless sandboxed), so training works from any directory.
        config_path = Path(output_path)
        if not config_path.is_absolute():
            config_path = USER_ROOT / config_path
        # Read the settings to keep before spending any crawl or model calls.
        try:
            settings = _settings_in_effect(config_path)
        except ConfigError as e:
            print(f"\nERROR: {e}")
            print("Fix or move that file, then train again. Nothing was changed.")
            return None

        logger.info(f"Training Harvey on {base_url}...")
        print(f"\n{'='*60}")
        print(f"  Harvey Training Mode")
        print(f"  Learning from: {base_url}")
        print(f"{'='*60}\n")

        # Step 1: Deep crawl
        cf_account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
        cf_api_token = os.getenv("CLOUDFLARE_API_TOKEN", "")

        if cf_account_id and cf_api_token:
            print(f"[1/6] Deep crawling with Cloudflare (up to {max_pages} pages)...")
            crawler = CloudflareCrawler(cf_account_id, cf_api_token)
            self.scraped_pages = await crawler.crawl(
                base_url, max_pages=max_pages, depth=10
            )
        else:
            print("[1/6] Crawling website (add CLOUDFLARE_ACCOUNT_ID and")
            print("      CLOUDFLARE_API_TOKEN to .env for deep JS-rendered crawling)...")
            crawler = FallbackCrawler()
            self.scraped_pages = await crawler.crawl(base_url, max_pages=max_pages)

        if not self.scraped_pages:
            print("\nERROR: Could not scrape any pages. Check the URL.")
            return None

        print(f"      Done! Crawled {len(self.scraped_pages)} pages.\n")

        # Step 2: Extract product information
        print("[2/6] Analyzing product information...")
        product_info = await self._extract_product_info()
        if not product_info:
            print("ERROR: Could not extract product information.")
            return None
        print(f"      Found: {product_info.get('product_name', 'Unknown Product')}\n")

        # Step 3: Identify ICP
        print("[3/6] Identifying ideal customer profile...")
        icp_info = await self._extract_icp()
        print(f"      Target: {', '.join(icp_info.get('industries', ['Unknown']))}\n")

        # Step 4: Competitive analysis
        print("[4/6] Analyzing competitive landscape...")
        competitive_intel = await self._extract_competitive_intel(product_info)
        print(f"      Identified {len(competitive_intel.get('competitors', []))} competitors.\n")

        # Step 5: Generate objection responses
        print("[5/6] Generating objection handling playbook...")
        objections = await self._generate_objections(product_info, competitive_intel)
        print(f"      Prepared {len(objections)} objection responses.\n")

        # Step 6: Generate config + skills
        print("[6/6] Building configuration and product knowledge...")
        config = self._build_config(product_info, icp_info, objections, settings)

        self._write_or_propose(
            config_path,
            yaml.dump(config, default_flow_style=False, sort_keys=False),
            protect=config_path.resolve() != TEMPLATE_CONFIG.resolve(),
        )

        # Generate product knowledge skill
        await self._generate_product_knowledge(product_info, icp_info, competitive_intel)

        # Generate competitive battle cards skill
        if competitive_intel.get("competitors"):
            await self._generate_battle_cards(product_info, competitive_intel)

        print(f"\n{'='*60}")
        print(f"  Training complete!")
        print(f"{'='*60}")
        print(f"\n  Product:      {config['product']['name']}")
        print(f"  Company:      {config['persona']['company']}")
        print(f"  ICP titles:   {', '.join(config['icp']['titles'])}")
        print(f"  Industries:   {', '.join(config['icp']['industries'])}")
        print(f"  Pages crawled: {len(self.scraped_pages)}")
        self._print_files()
        persona = config["persona"]
        print(f"\n  Sender kept as configured: {persona['name'] or '(no name)'}"
              f" <{persona['email'] or 'no email'}>, provider "
              f"{config['channels'].get('email', {}).get('provider', '?')}.")
        if persona["name"] in ("", TEMPLATE_PERSONA["name"]) or persona["email"] in (
            "", TEMPLATE_PERSONA["email"]
        ):
            print(f"  Set persona name and email in {config_path} before sending.")
        print(f"\n  Review {config_path} and adjust as needed.")
        print(f"  Then run: harvey run\n")

        return config

    def _print_files(self):
        """What this run wrote, and what it proposed and why."""
        def show(path: Path) -> str:
            try:
                return str(path.relative_to(PROJECT_ROOT))
            except ValueError:
                return str(path)

        if self.written:
            print(f"\n  Files written:")
            for path in self.written:
                print(f"    - {show(path)}")
        if not self.proposed:
            return
        print(f"\n  Proposed (your files were left exactly as they are):")
        for live, proposed in self.proposed:
            old = live.read_text(encoding="utf-8", errors="replace").splitlines()
            new = proposed.read_text(encoding="utf-8").splitlines()
            diff = list(difflib.unified_diff(old, new, lineterm="", n=0))
            added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
            removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
            print(f"    - {show(proposed)}  (+{added} -{removed} lines)")
            if live.suffix in (".yaml", ".yml"):
                try:
                    keys = _changed_keys(
                        yaml.safe_load("\n".join(old)) or {},
                        yaml.safe_load("\n".join(new)) or {},
                    )
                    if keys:
                        print(f"      changed: {', '.join(keys)}")
                except yaml.YAMLError:
                    pass
            print(f"      compare: diff -u {show(live)} {show(proposed)}")
        print(f"  Nothing in effect changed. Merge what you want by hand.")

    def _get_all_content(self, max_chars: int = 60000) -> str:
        """Combine all scraped pages into one string for analysis."""
        all_content = ""
        for url, content in self.scraped_pages.items():
            path = urlparse(url).path or "/"
            # Truncate individual pages to keep total manageable
            page_content = content[:8000] if len(content) > 8000 else content
            all_content += f"\n\n--- PAGE: {path} ---\n{page_content}"
            if len(all_content) > max_chars:
                break
        return all_content

    async def _extract_product_info(self) -> dict:
        """Ask Claude to extract product information from all crawled content."""
        all_content = self._get_all_content()

        prompt = f"""Analyze this entire website and extract comprehensive product/company information.

WEBSITE CONTENT ({len(self.scraped_pages)} pages):
{all_content}

Extract the following and return as JSON:
{{
  "company_name": "the company name",
  "product_name": "the main product or service name",
  "product_description": "2-3 sentence description of what the product does and who it's for",
  "pricing": "pricing info if available (tiers, starting price, etc.), otherwise 'Contact for pricing'",
  "key_benefits": ["benefit 1", "benefit 2", "benefit 3", "benefit 4", "benefit 5"],
  "features": ["feature 1", "feature 2", "feature 3", "feature 4", "feature 5", "feature 6"],
  "target_audience": "who this product is for — be specific about roles and company types",
  "value_proposition": "the core value proposition in one sentence",
  "differentiators": ["what makes this different from alternatives — point 1", "point 2"],
  "competitors": ["competitor 1", "competitor 2", "competitor 3"],
  "social_proof": ["any case studies, testimonials, notable customers, or stats mentioned"],
  "integrations": ["tools and platforms this integrates with"],
  "use_cases": ["specific use case 1", "use case 2", "use case 3"],
  "tone": "describe the brand's communication tone in 3-4 words"
}}

Be thorough — you have access to the full website. Extract everything relevant."""

        result = await self.brain.think_json(prompt, session_id="harvey-trainer")
        return result if isinstance(result, dict) else {}

    async def _extract_icp(self) -> dict:
        """Ask Claude to determine the ideal customer profile from all content."""
        all_content = self._get_all_content(max_chars=40000)

        prompt = f"""Based on this entire website, determine the Ideal Customer Profile (ICP).
Look at case studies, testimonials, pricing tiers, feature descriptions, and target messaging
to understand exactly who this product is built for.

WEBSITE CONTENT ({len(self.scraped_pages)} pages):
{all_content}

Return as JSON:
{{
  "industries": ["industry 1", "industry 2", "industry 3"],
  "company_size": "employee range like '10-200 employees'",
  "titles": ["decision maker title 1", "title 2", "title 3", "title 4"],
  "geography": ["country or region 1", "region 2"],
  "buyer_persona": "A detailed description of the ideal buyer — their role, daily challenges, goals, and what success looks like for them",
  "pain_points": ["specific pain point 1", "pain point 2", "pain point 3", "pain point 4"],
  "buying_triggers": ["trigger event that makes them ready to buy 1", "trigger 2", "trigger 3"],
  "disqualifiers": ["signals that someone is NOT a good fit 1", "disqualifier 2"]
}}

For any city in "geography", always include the state/province and country if
the name could plausibly refer to more than one place — write "Solon, Ohio"
not "Solon" (there's also a Solon in France), "Springfield, Illinois" not
"Springfield" (30+ US cities share that name). These strings get geocoded
automatically for local-business discovery, and an unqualified name silently
resolves to the wrong place on Earth instead of failing loudly.

"titles" MUST be literal job titles exactly as they'd appear on a business
card or LinkedIn profile ("Owner", "CEO", "President", "Plant Manager",
"Practice Manager") — never a customer-segment or persona label ("Business
Owner Segment", "Decision Maker", "Healthcare Professional"). Harvey matches
a found contact's title against this list by substring, so a descriptive
label instead of a real title means every real contact silently fails to
match and gets discarded. If the buyer is an INDIVIDUAL bought into by
persona rather than a role at a target company (e.g. this product targets
physicians, business owners, or executives directly as people, not a
"Director of X" at a company you sell software to), list every real job
title that persona plausibly holds — a "business owner" persona should
produce ["Owner", "President", "CEO", "Founder", "Managing Partner"], a
"physician" persona should produce ["Physician", "MD", "Doctor"], and so on.
Prefer more titles over fewer; a missed real title costs a contact forever,
an extra one costs nothing.

Be specific — don't guess generically. Base this on what the website actually says about their customers."""

        result = await self.brain.think_json(prompt, session_id="harvey-trainer")
        return result if isinstance(result, dict) else {
            "industries": ["Technology"],
            "company_size": "10-200 employees",
            "titles": ["VP", "Director", "Head of"],
            "geography": ["United States"],
        }

    async def _extract_competitive_intel(self, product_info: dict) -> dict:
        """Extract competitive positioning and differentiators."""
        all_content = self._get_all_content(max_chars=40000)

        prompt = f"""Analyze this website for competitive intelligence.
The product is: {product_info.get('product_name', '')}
Known competitors: {', '.join(product_info.get('competitors', []))}

WEBSITE CONTENT:
{all_content}

Look for comparison pages, "why us" messaging, feature differentiation,
and any mentions of competitors or alternatives.

Return as JSON:
{{
  "competitors": [
    {{
      "name": "Competitor Name",
      "how_we_win": "What makes our product better than this competitor",
      "their_weakness": "Known limitations or complaints about this competitor",
      "migration_angle": "Why someone would switch from them to us"
    }}
  ],
  "positioning": "How this product positions itself in the market — one paragraph",
  "unique_strengths": ["strength that no competitor has 1", "strength 2"],
  "common_alternatives": ["what prospects do instead of buying this — DIY, spreadsheets, etc."]
}}

If the website doesn't mention specific competitors, infer the most likely ones based on the product category."""

        result = await self.brain.think_json(prompt, session_id="harvey-trainer")
        return result if isinstance(result, dict) else {"competitors": []}

    async def _generate_objections(self, product_info: dict, competitive_intel: dict) -> dict:
        """Generate objection handling responses based on product + competitive intel."""
        competitors = competitive_intel.get("competitors", [])
        competitor_names = [c.get("name", "") for c in competitors if isinstance(c, dict)]

        prompt = f"""You are a world-class B2B sales trainer. Based on this product and its competitive
landscape, generate the most common objections a prospect would raise, with killer responses.

Product: {product_info.get('product_name', '')}
Description: {product_info.get('product_description', '')}
Pricing: {product_info.get('pricing', '')}
Key Benefits: {', '.join(product_info.get('key_benefits', []))}
Competitors: {', '.join(competitor_names)}
Differentiators: {', '.join(product_info.get('differentiators', []))}

Generate 8-12 objections covering:
- Price/budget concerns
- Existing solutions / competitor loyalty
- Timing / priority
- Implementation / switching costs
- Trust / credibility
- Need / relevance

Each response should be 1-2 sentences, consultative (not defensive), and end with
a question or redirect that keeps the conversation going.

Return as a JSON object where keys are the objection and values are the response."""

        result = await self.brain.think_json(prompt, session_id="harvey-trainer")
        return result if isinstance(result, dict) else {}

    async def _generate_product_knowledge(
        self, product_info: dict, icp_info: dict, competitive_intel: dict
    ):
        """Generate comprehensive product knowledge skill file."""
        product_name = product_info.get("product_name", "the product")

        knowledge = f"""# Product Knowledge: {product_name}

## What We Sell
{product_info.get('product_description', '')}

## Core Value Proposition
{product_info.get('value_proposition', '')}

## Key Benefits
{chr(10).join('- ' + b for b in product_info.get('key_benefits', []))}

## Features
{chr(10).join('- ' + f for f in product_info.get('features', []))}

## Use Cases
{chr(10).join('- ' + u for u in product_info.get('use_cases', []))}

## Pricing
{product_info.get('pricing', 'Contact for pricing')}

## Target Audience
{product_info.get('target_audience', '')}

## Ideal Buyer Persona
{icp_info.get('buyer_persona', '')}

## Integrations
{chr(10).join('- ' + i for i in product_info.get('integrations', []))}

## What Makes Us Different
{chr(10).join('- ' + d for d in product_info.get('differentiators', []))}

## Social Proof & Case Studies
{chr(10).join('- ' + s for s in product_info.get('social_proof', []))}

## Prospect Pain Points
{chr(10).join('- ' + p for p in icp_info.get('pain_points', []))}

## Buying Triggers
When these events happen, prospects are most likely to buy:
{chr(10).join('- ' + t for t in icp_info.get('buying_triggers', []))}

## Disqualifiers
Do NOT pursue prospects who:
{chr(10).join('- ' + d for d in icp_info.get('disqualifiers', []))}

## Market Positioning
{competitive_intel.get('positioning', '')}

## Tone & Voice
{product_info.get('tone', 'professional, consultative, confident')}
"""

        self._write_or_propose(self.skills_dir / "product_knowledge.md", knowledge)

    async def _generate_battle_cards(self, product_info: dict, competitive_intel: dict):
        """Generate competitive battle cards as a skill file."""
        competitors = competitive_intel.get("competitors", [])
        if not competitors:
            return

        product_name = product_info.get("product_name", "Our Product")

        cards = f"""# Competitive Battle Cards: {product_name}

Use these when a prospect mentions a competitor or asks "how are you different?"

## Market Positioning
{competitive_intel.get('positioning', '')}

## Our Unique Strengths
{chr(10).join('- ' + s for s in competitive_intel.get('unique_strengths', []))}

## Common Alternatives (Non-Competitors)
What prospects do instead of buying a solution like ours:
{chr(10).join('- ' + a for a in competitive_intel.get('common_alternatives', []))}

---
"""

        for comp in competitors:
            if not isinstance(comp, dict):
                continue
            name = comp.get("name", "Unknown")
            cards += f"""
## vs. {name}

**How we win:** {comp.get('how_we_win', 'N/A')}

**Their weakness:** {comp.get('their_weakness', 'N/A')}

**Migration angle:** {comp.get('migration_angle', 'N/A')}

**When a prospect says "We use {name}":**
"That's a solid tool. The teams that've moved to {product_name} from {name} tell us the biggest difference is {comp.get('how_we_win', 'the experience')}. Worth a quick comparison?"

---
"""

        self._write_or_propose(self.skills_dir / "competitive_intel.md", cards)

    def _build_config(
        self,
        product_info: dict,
        icp_info: dict,
        objections: dict,
        settings: dict | None = None,
    ) -> dict:
        """The config to write: what training learned, on top of ``settings``.

        Training learns the product, the ICP and the objections. The sender
        (persona), channels and usage stay as the user configured them, so
        a re-train never changes who emails go out from or how.
        """
        settings = copy.deepcopy(settings or {})

        persona = {
            "name": "", "company": "", "role": "", "email": "", "linkedin": "",
            "tone": TEMPLATE_PERSONA["tone"],
        }
        persona.update(settings.get("persona") or {})
        learned = {
            "company": product_info.get("company_name", ""),
            "tone": product_info.get("tone", ""),
        }
        for key, value in learned.items():
            if value and persona.get(key) in ("", None, TEMPLATE_PERSONA[key]):
                persona[key] = value

        product = settings.get("product") or {}
        product.update({
            "name": product_info.get("product_name", "Your Product"),
            "description": product_info.get("product_description", ""),
            "pricing": product_info.get("pricing", "Contact for pricing"),
            "key_benefits": product_info.get("key_benefits", []),
            "objection_responses": objections,
        })

        icp = settings.get("icp") or {}
        icp.update({
            "industries": icp_info.get("industries", ["Technology"]),
            "company_size": icp_info.get("company_size", "10-200 employees"),
            "titles": icp_info.get("titles", ["VP", "Director"]),
            "geography": icp_info.get("geography", ["United States"]),
        })

        config = {
            "persona": persona,
            "product": product,
            "icp": icp,
            "channels": settings.get("channels") or copy.deepcopy(DEFAULT_CHANNELS),
            "usage": settings.get("usage") or copy.deepcopy(DEFAULT_USAGE),
            "brain": brain_section(self.brain_config),
        }
        # Anything else the user configured stays as it was.
        for key, value in settings.items():
            config.setdefault(key, value)
        return config


async def run_training(url: str, output: str = "harvey.local.yaml", max_pages: int = 100):
    """Entry point for training."""
    trainer = Trainer()
    await trainer.train(url, output, max_pages=max_pages)


def main():
    """CLI entry point for training."""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m harvey.trainer <website-url> [max-pages]")
        print("")
        print("Examples:")
        print("  python -m harvey.trainer https://acmecorp.com")
        print("  python -m harvey.trainer https://acmecorp.com 200")
        print("")
        print("For deep JS-rendered crawling, add to .env:")
        print("  CLOUDFLARE_ACCOUNT_ID=your_account_id")
        print("  CLOUDFLARE_API_TOKEN=your_api_token")
        print("")
        print("Without Cloudflare credentials, Harvey will use basic HTTP")
        print("scraping (no JavaScript rendering, limited page discovery).")
        sys.exit(1)

    url = _normalize_start_url(sys.argv[1])

    max_pages = 100
    if len(sys.argv) > 2:
        try:
            max_pages = max(1, int(sys.argv[2]))
        except ValueError:
            print(f"Invalid max-pages value: {sys.argv[2]!r} (expected a number)")
            sys.exit(1)

    asyncio.run(run_training(url, max_pages=max_pages))


if __name__ == "__main__":
    main()
