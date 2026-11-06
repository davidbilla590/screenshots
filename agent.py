"""
agent.py - core engine of the POP screenshot agent.

Flow per publisher URL:
  1. Connect to Browserless (remote Chrome) through Playwright / CDP.
  2. (optional) Find the latest article on the page with Gemini and run a brand-safety check.
  3. Find an ad slot whose size matches the creative (DOM scan -> GPT slot API -> Gemini vision).
  4. Swap the slot content with the client's creative (image or HTML tag).
  5. Screenshot the page, optionally add a URL / timestamp strip, save to disk.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import re
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode, urlparse

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------------------


@dataclass
class Creative:
    name: str
    width: int
    height: int
    kind: str  # "image" | "html"
    data_uri: str = ""  # image creatives
    html: str = ""  # html creatives (full document)
    raw: bytes = b""  # raw image bytes (for previews)


@dataclass
class Result:
    index: int
    order: int = 0
    site: str = ""
    page_url: str = ""
    creative: str = ""
    size: str = ""
    status: str = "PENDING"  # OK | QA_FAIL | NO_SLOT | SKIPPED_BRAND_SAFETY | ERROR
    method: str = ""
    note: str = ""
    file: str = ""
    captured_at: str = ""
    qa_status: str = ""  # PASS | FAIL | NOT_RUN
    qa_report: str = ""  # one line: failed checks (with reason) + passed checks
    qa_fails: int = 0  # number of failed QA checks (used to pick the best fallback screenshot)


@dataclass
class JobConfig:
    browserless_token: str
    gemini_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    region: str = "production-sfo"  # production-sfo | production-lon | production-ams
    stealth: bool = True
    residential_proxy: bool = False
    proxy_country: str = "in"
    session_timeout_s: int = 180
    concurrency: int = 1
    mobile: bool = False
    viewport_w: int = 1366
    viewport_h: int = 768
    locale: str = "en-IN"
    find_latest_article: bool = True
    guidelines: str = ""
    brand_safety: bool = True
    brand_safety_rules: str = ""
    max_article_tries: int = 5  # how many of the latest articles to search for a matching ad slot
    size_tolerance: int = 3
    use_vision_fallback: bool = True
    full_page: bool = False
    add_footer: bool = True
    footer_label: str = ""
    timezone: str = "Asia/Kolkata"
    min_content_pct: int = 60  # screenshot must show real page content over at least this % of its area
    qa_enabled: bool = True  # Gemini vision QA of every screenshot (needs a Gemini key)
    qa_extra_rules: str = ""  # optional extra QA rules appended to the 10 standard ones


LogFn = Callable[[str], None]

DEFAULT_BRAND_SAFETY_RULES = (
    "Adult or sexual content; graphic violence, crime, accidents, death or tragedy; terrorism or war; "
    "hate speech or discrimination; illegal drugs, tobacco, vaping; gambling; heated political controversy; "
    "natural disasters or disease outbreaks; profanity; misinformation; piracy."
)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def short(e, token: str = "") -> str:
    s = str(e).replace("\n", " ")
    if token:
        s = s.replace(token, "***")
    s = re.sub(r"token=[^&\s\"']+", "token=***", s)
    return s[:300]


def normalize_url(u: str) -> str:
    u = (u or "").strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "file"


def now_str(tz_name: str) -> str:
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).strftime("%d %b %Y, %H:%M %Z")


def parse_json_loose(text):
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"(\{.*\}|\[.*\])", t, flags=re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None
    return None


def parse_sites(text: str = "", csv_bytes: Optional[bytes] = None) -> list[str]:
    """Return a de-duplicated list of normalised URLs from pasted text and/or an uploaded CSV."""
    raw: list[str] = []
    if text:
        for line in text.splitlines():
            raw += re.split(r"[\s,;]+", line.strip())
    if csv_bytes:
        content = csv_bytes.decode("utf-8-sig", errors="ignore")
        rows = list(csv.reader(io.StringIO(content)))
        if rows:
            header = [h.strip().lower() for h in rows[0]]
            col, start = 0, 0
            for i, h in enumerate(header):
                if h in ("url", "urls", "site", "sites", "website", "domain", "publisher", "link"):
                    col, start = i, 1
                    break
            for r in rows[start:]:
                if len(r) > col:
                    raw += re.split(r"[\s;]+", r[col].strip())
    out, seen = [], set()
    for s in raw:
        s = s.strip()
        if not s or s.startswith("#"):
            continue
        if not re.match(r"^(https?://)?[\w.-]+\.[A-Za-z]{2,}", s):
            continue
        u = normalize_url(s)
        key = u.rstrip("/").lower()
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


# --------------------------------------------------------------------------------------
# Creatives (images / HTML tags / Google Drive)
# --------------------------------------------------------------------------------------

STANDARD_SIZES = {
    (300, 250), (728, 90), (160, 600), (300, 600), (320, 50), (320, 100), (970, 250), (970, 90),
    (468, 60), (336, 280), (300, 50), (250, 250), (200, 200), (120, 600), (300, 1050), (320, 480),
    (480, 320), (970, 66), (930, 180), (580, 400), (980, 120), (750, 100), (750, 200), (750, 300),
    (240, 400), (180, 150), (125, 125),
}

SIZE_RE = re.compile(r"(?<!\d)(\d{2,4})\s*[xX\u00d7]\s*(\d{2,4})(?!\d)")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
HTML_EXT = {".html", ".htm", ".txt"}


def _valid(w: int, h: int) -> bool:
    return 30 <= w <= 2000 and 30 <= h <= 2000


def size_from_name(text: str) -> Optional[tuple[int, int]]:
    for m in SIZE_RE.finditer(text or ""):
        w, h = int(m.group(1)), int(m.group(2))
        if _valid(w, h):
            return w, h
    return None


def size_from_html(html: str) -> Optional[tuple[int, int]]:
    m = re.search(r"<!--\s*(\d{2,4})\s*[xX\u00d7]\s*(\d{2,4})", html)
    if m and _valid(int(m.group(1)), int(m.group(2))):
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"[?&;]sz=(\d{2,4})x(\d{2,4})", html)
    if m and _valid(int(m.group(1)), int(m.group(2))):
        return int(m.group(1)), int(m.group(2))
    w = re.search(r"width\s*[=:]\s*[\"']?(\d{2,4})", html, re.I)
    h = re.search(r"height\s*[=:]\s*[\"']?(\d{2,4})", html, re.I)
    if w and h and _valid(int(w.group(1)), int(h.group(1))):
        return int(w.group(1)), int(h.group(1))
    return size_from_name(html)


def wrap_html(tag: str) -> str:
    if re.match(r"\s*(<!doctype|<html)", tag, re.I):
        return tag
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;overflow:hidden;background:transparent}</style>"
        "</head><body>" + tag + "</body></html>"
    )


def creative_from_image(path: Path) -> Creative:
    raw = path.read_bytes()
    img = Image.open(io.BytesIO(raw))
    w, h = img.size
    fmt = img.format or "PNG"
    img.close()
    named = size_from_name(path.stem)
    if named:
        w, h = named
    elif (w, h) not in STANDARD_SIZES and (w // 2, h // 2) in STANDARD_SIZES and w % 2 == 0 and h % 2 == 0:
        w, h = w // 2, h // 2  # retina export, e.g. 600x500 -> 300x250
    mime = Image.MIME.get(fmt, "image/png")
    b64 = base64.b64encode(raw).decode("ascii")
    return Creative(name=path.name, width=w, height=h, kind="image", data_uri=f"data:{mime};base64,{b64}", raw=raw)


def creative_from_html_text(name: str, text: str) -> Creative:
    size = size_from_name(name) or size_from_html(text)
    if not size:
        raise ValueError(
            "could not detect the ad size - add a comment like <!-- 300x250 --> at the top of the tag "
            "(or put 300x250 in the file name)"
        )
    return Creative(name=name, width=size[0], height=size[1], kind="html", html=wrap_html(text.strip()))


def creatives_from_html_blocks(text: str) -> tuple[list[Creative], list[str]]:
    out: list[Creative] = []
    warns: list[str] = []
    blocks = [b.strip() for b in re.split(r"^\s*={3,}\s*$", text or "", flags=re.M) if b.strip()]
    for i, b in enumerate(blocks, 1):
        try:
            c = creative_from_html_text(f"tag_{i}", b)
            c.name = f"tag_{i}_{c.width}x{c.height}"
            out.append(c)
        except Exception as e:
            warns.append(f"Pasted tag #{i}: {e}")
    return out, warns


def _download_url(url: str, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = resp.read()
        ctype = (resp.headers.get("Content-Type") or "").lower()
    name = Path(urlparse(url).path).name or "creative"
    if not Path(name).suffix:
        ext = ".gif" if "gif" in ctype else ".jpg" if "jpeg" in ctype else ".webp" if "webp" in ctype else ".png"
        name += ext
    p = dest / safe_name(name)
    p.write_bytes(data)
    return [p]


def download_drive(link: str, dest: Path) -> list[Path]:
    """Download a public Google Drive file / folder (or any direct image URL) into dest."""
    dest.mkdir(parents=True, exist_ok=True)
    link = link.strip()
    if "drive.google.com" not in link and "docs.google.com" not in link:
        return _download_url(link, dest)
    try:
        import gdown

        if "/folders/" in link:
            gdown.download_folder(url=link, output=str(dest), quiet=True, use_cookies=False, remaining_ok=True)
        else:
            gdown.download(url=link, output=str(dest) + "/", quiet=True, fuzzy=True)
    except Exception as e:
        raise RuntimeError(
            f"Could not download '{link}'. Make sure sharing is set to 'Anyone with the link can view'. ({short(e)})"
        )
    files = [p for p in dest.rglob("*") if p.is_file()]
    if not files:
        raise RuntimeError(f"Nothing was downloaded from '{link}'. Check that the link is public.")
    return files


def load_creatives(
    drive_links: str = "",
    uploads: Optional[list[tuple[str, bytes]]] = None,
    html_text: str = "",
) -> tuple[list[Creative], list[str]]:
    creatives: list[Creative] = []
    warnings: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="creatives_"))
    files: list[Path] = []

    links = [ln.strip() for ln in (drive_links or "").splitlines() if ln.strip()]
    for i, link in enumerate(links, 1):
        try:
            files += download_drive(link, work / f"link{i}")
        except Exception as e:
            warnings.append(str(e))

    for name, data in uploads or []:
        p = work / "uploads" / safe_name(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        files.append(p)

    for p in files:
        ext = p.suffix.lower()
        try:
            if ext in IMAGE_EXT:
                creatives.append(creative_from_image(p))
            elif ext in HTML_EXT:
                creatives.append(creative_from_html_text(p.name, p.read_text(encoding="utf-8", errors="ignore")))
            elif ext == ".zip":
                warnings.append(f"{p.name}: HTML5 zip bundles are not supported - use a static image or paste the tag.")
            else:
                warnings.append(f"{p.name}: unsupported file type, skipped.")
        except Exception as e:
            warnings.append(f"{p.name}: {short(e)}")

    if (html_text or "").strip():
        c, w = creatives_from_html_blocks(html_text)
        creatives += c
        warnings += w
    return creatives, warnings


# --------------------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------------------


class Gemini:
    def __init__(self, api_key: str, model: str = "gemini-flash-latest"):
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model = model or "gemini-flash-latest"
        self.last_error = ""

    def ask_json(self, prompt: str, image_png: Optional[bytes] = None, extra_images: Optional[list] = None):
        """Blocking call (run it with asyncio.to_thread). Returns parsed JSON or None."""
        import time

        from google.genai import types

        for attempt in range(2):
            try:
                parts: list = []
                for img in ([image_png] if image_png else []) + list(extra_images or []):
                    parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))
                parts.append(prompt)
                resp = self.client.models.generate_content(
                    model=self.model,
                    contents=parts,
                    config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
                )
                return parse_json_loose(resp.text)
            except Exception as e:
                self.last_error = str(e)[:300]
                if attempt == 0:
                    time.sleep(2)
        return None


# --------------------------------------------------------------------------------------
# JavaScript executed inside the page
# --------------------------------------------------------------------------------------

JS_DISMISS = r"""
() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  let clicked = 0;
  const sels = ['#onetrust-accept-btn-handler', 'button#accept-all', '#didomi-notice-agree-button',
    '.fc-cta-consent', 'button.fc-primary-button', '#truste-consent-button', '.cookie-accept', '.cc-allow',
    '#cookie-accept', 'button[id*="accept" i]', 'button[class*="accept" i]', '[data-testid*="accept" i]'];
  for (const s of sels) {
    try { const el = document.querySelector(s); if (el && vis(el)) { el.click(); clicked++; break; } } catch (e) {}
  }
  if (!clicked) {
    const re = /^(accept( all)?( cookies)?|i agree|agree( (and|&) (continue|proceed|close))?|allow( all)?( cookies)?|got it|ok|okay|yes,? i agree|accept (and|&) continue|i accept|consent|understood)$/i;
    const btns = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'));
    for (const b of btns) {
      const t = (b.innerText || b.value || '').trim();
      if (t && t.length < 30 && re.test(t) && vis(b)) { try { b.click(); clicked++; } catch (e) {} break; }
    }
  }
  return clicked;
}
"""

JS_HIDE_OVERLAYS = r"""
() => {
  const KW = /(consent|cookie|gdpr|privacy|modal|popup|pop-up|overlay|subscribe|newsletter|interstitial|paywall|dialog|lightbox)/i;
  const vw = innerWidth, vh = innerHeight;
  let hidden = 0;
  for (const el of document.querySelectorAll('body *')) {
    let cs; try { cs = getComputedStyle(el); } catch (e) { continue; }
    if (cs.position !== 'fixed') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 30) continue;
    if (el.querySelector('iframe[id^="google_ads_iframe"], ins.adsbygoogle')) continue;
    let cn = el.className; if (cn && typeof cn !== 'string') cn = cn.baseVal || '';
    const sig = (el.id || '') + ' ' + (cn || '') + ' ' + (el.getAttribute('role') || '');
    const txt = (el.innerText || '').slice(0, 300);
    const big = r.width * r.height > 0.15 * vw * vh;
    const looks = KW.test(sig) || el.getAttribute('aria-modal') === 'true' ||
                  (big && /cookie|consent|subscribe|newsletter|sign up|accept all/i.test(txt));
    if (looks) { el.style.setProperty('display', 'none', 'important'); hidden++; }
  }
  document.documentElement.style.setProperty('overflow', 'auto', 'important');
  if (document.body) document.body.style.setProperty('overflow', 'auto', 'important');
  return hidden;
}
"""

JS_LINKS = r"""
() => {
  const host = location.hostname.replace(/^www\./, '');
  const parts = host.split('.');
  let n = 2;
  if (parts.length >= 3 && parts[parts.length - 2].length <= 3 && parts[parts.length - 1].length === 2) n = 3;
  const root = parts.slice(-n).join('.');
  const BAD = /(\/tag\/|\/tags\/|\/category\/|\/categories\/|\/author\/|\/authors\/|\/topic\/|\/topics\/|\/search|\/login|\/signin|\/subscribe|\/about|\/contact|\/privacy|\/terms|\/careers|\/rss|\/feed|\/video|\/videos|\/photos|\/gallery|\/epaper|\.pdf$|\.jpe?g$|\.png$)/i;
  const seen = new Set(); const out = [];
  document.querySelectorAll('a[href]').forEach((a) => {
    if (!/^https?:/i.test(a.href)) return;
    let u; try { u = new URL(a.href); } catch (e) { return; }
    if (!u.hostname.replace(/^www\./, '').endsWith(root)) return;
    if (u.pathname === '/' || u.pathname.length < 8 || BAD.test(u.pathname)) return;
    const text = (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\s+/g, ' ').trim();
    if (text.length < 25) return;
    const clean = u.origin + u.pathname;
    if (seen.has(clean)) return;
    const r = a.getBoundingClientRect(); const cs = getComputedStyle(a);
    if (cs.visibility === 'hidden' || cs.display === 'none') return;
    const card = a.closest('article, li, [class*="card"], [class*="story"], [class*="post"], [class*="item"]');
    let date = '';
    if (card) { const t = card.querySelector('time'); if (t) date = (t.getAttribute('datetime') || t.innerText || '').trim().slice(0, 40); }
    seen.add(clean);
    out.push({href: u.href, text: text.slice(0, 160), date: date, y: Math.round(r.top + window.scrollY)});
  });
  return out.slice(0, 60);
}
"""

JS_PAGE_TEXT = r"""
() => ({
  url: location.href,
  title: document.title || '',
  h1: ((document.querySelector('h1') || {}).innerText || '').slice(0, 300),
  text: ((document.body && document.body.innerText) || '').replace(/\s+/g, ' ').slice(0, 3500)
})
"""

JS_FIND_SLOTS = r"""
([W, H, TOL]) => {
  const AD_SRC = /(doubleclick|googlesyndication|googleadservices|googletagservices|adnxs|adform|criteo|taboola|outbrain|rubiconproject|pubmatic|openx|smartadserver|amazon-adsystem|adsrvr|casalemedia|teads|3lift|sharethrough|adserver|adsystem|moatads|yieldmo|inmobi|mgid|revcontent|indexww|bidswitch|smaato|flashtalking|serving-sys|sizmek|innovid|\/ads?\/|ads\.)/i;
  const AD_WORD = /(^|[^a-z])(ad|ads|adv|advert|advertisement|adslot|adunit|adbox|adwrap|adwrapper|adcontainer|gpt|dfp|mrec|mpu|billboard|leaderboard|sponsored|banner)([^a-z]|$)|div-gpt-ad|google_ads|adsbygoogle|aswift|ad_slot|ad-slot/i;
  const sigOf = (el) => {
    let cn = el.className;
    if (cn && typeof cn !== 'string') cn = cn.baseVal || '';
    return (el.id || '') + ' ' + (cn || '');
  };
  document.querySelectorAll('[data-pop-cand]').forEach((e) => e.removeAttribute('data-pop-cand'));
  const sx = window.scrollX, sy = window.scrollY;
  const all = Array.from(document.querySelectorAll(
    'iframe, ins, div, section, aside, a, img, span, figure, amp-ad, amp-embed, [data-ad-slot], [data-google-query-id]'));
  const cands = [];
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (Math.abs(r.width - W) > TOL || Math.abs(r.height - H) > TOL) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    let score = 0; const hints = [];
    if (el.hasAttribute('data-pop-replaced')) { score += 20; hints.push('replaced'); }
    const tag = el.tagName.toLowerCase();
    const src = (el.getAttribute('src') || '') + ' ' + (el.getAttribute('data-src') || '') + ' ' + (el.getAttribute('name') || '');
    if (tag === 'iframe') { score += 1; if (AD_SRC.test(src)) { score += 4; hints.push('ad-src'); } }
    if (AD_WORD.test(sigOf(el))) { score += 3; hints.push('id/class'); }
    if (el.hasAttribute('data-google-query-id') || el.hasAttribute('data-ad-slot') || el.hasAttribute('data-ad-unit') || el.hasAttribute('data-adunit')) { score += 4; hints.push('ad-attr'); }
    if (el.querySelector('iframe[id^="google_ads_iframe"], ins.adsbygoogle, iframe[src*="doubleclick"], iframe[src*="googlesyndication"], iframe[src*="adform"]')) { score += 4; hints.push('ad-child'); }
    const lab = (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '');
    if (/advert|sponsor/i.test(lab)) { score += 2; hints.push('label'); }
    let p = el.parentElement, depth = 0;
    while (p && depth < 4) {
      if (AD_WORD.test(sigOf(p)) || p.hasAttribute('data-google-query-id')) { score += 2; hints.push('parent'); break; }
      p = p.parentElement; depth++;
    }
    if (score < 3) continue;
    cands.push({el: el, score: score, x: r.left + sx, y: r.top + sy, w: Math.round(r.width), h: Math.round(r.height), tag: tag, hints: hints.join(',')});
  }
  const kept = cands.filter((c) => !cands.some((o) => o !== c && o.el.contains(c.el)));
  kept.sort((a, b) => (b.score - a.score) || (a.y - b.y));
  const out = [];
  kept.slice(0, 8).forEach((c, i) => {
    c.el.setAttribute('data-pop-cand', String(i));
    out.push({idx: i, score: c.score, x: Math.round(c.x), y: Math.round(c.y), w: c.w, h: c.h, tag: c.tag, hints: c.hints, force: false});
  });
  let method = 'dom';
  if (!out.length) {
    try {
      const g = window.googletag;
      if (g && g.pubads) {
        const wh = (z) => {
          if (Array.isArray(z)) return [z[0], z[1]];
          if (z && typeof z.getWidth === 'function') return [z.getWidth(), z.getHeight()];
          if (z && z.width !== undefined) return [z.width, z.height];
          return null;
        };
        const found = [];
        g.pubads().getSlots().forEach((s) => {
          const sizes = (s.getSizes ? s.getSizes() : null) || [];
          if (!sizes.some((z) => { const v = wh(z); return v && v[0] === W && v[1] === H; })) return;
          const el = document.getElementById(s.getSlotElementId());
          if (!el) return;
          const cs = getComputedStyle(el);
          if (cs.display === 'none' || (el.offsetParent === null && cs.position !== 'fixed')) return;
          const r = el.getBoundingClientRect();
          found.push({el: el, x: r.left + sx, y: r.top + sy, w: Math.round(r.width), h: Math.round(r.height)});
        });
        found.sort((a, b) => a.y - b.y);
        found.slice(0, 5).forEach((c, i) => {
          c.el.setAttribute('data-pop-cand', String(i));
          out.push({idx: i, score: 0, x: Math.round(c.x), y: Math.round(c.y), w: c.w, h: c.h, tag: c.el.tagName.toLowerCase(), hints: 'gpt-declared', force: true});
        });
        if (out.length) method = 'gpt';
      }
    } catch (e) {}
  }
  return {method: method, slots: out};
}
"""

_JS_MK = r"""
const mk = async (a) => {
  let node;
  if (a.kind === 'image') {
    node = document.createElement('img');
    node.alt = 'Advertisement';
    node.src = a.dataUri;
    node.setAttribute('width', String(a.w));
    node.setAttribute('height', String(a.h));
    node.style.cssText = 'display:block!important;border:0!important;margin:0!important;padding:0!important;max-width:none!important;width:' + a.w + 'px!important;height:' + a.h + 'px!important;';
    try { await node.decode(); } catch (e) {}
  } else {
    node = document.createElement('iframe');
    node.setAttribute('width', String(a.w));
    node.setAttribute('height', String(a.h));
    node.setAttribute('frameborder', '0');
    node.setAttribute('scrolling', 'no');
    node.style.cssText = 'display:block!important;border:0!important;margin:0!important;padding:0!important;width:' + a.w + 'px!important;height:' + a.h + 'px!important;';
    node.srcdoc = a.html;
  }
  return node;
};
"""

JS_REPLACE = (
    "async (a) => {\n"
    + _JS_MK
    + r"""
  const el = document.querySelector('[data-pop-cand="' + a.idx + '"]');
  if (!el) return {ok: false, reason: 'candidate element vanished'};
  const node = await mk(a);
  let box = el;
  const tag = el.tagName.toLowerCase();
  if (tag === 'iframe' || tag === 'img') {
    box = document.createElement('div');
    box.__popOrigEl = el;
    el.replaceWith(box);
  } else {
    if (box.__popMO) box.__popMO.disconnect();
    if (!box.__popOrigNodes && !box.__popOrigEl) {
      box.__popOrigNodes = Array.from(box.childNodes);
      box.__popOrigStyle = box.getAttribute('style');
    }
  }
  box.replaceChildren(node);
  box.removeAttribute('hidden');
  box.removeAttribute('aria-hidden');
  const S = (k, v) => box.style.setProperty(k, v, 'important');
  S('display', 'block'); S('visibility', 'visible'); S('opacity', '1');
  S('width', a.w + 'px'); S('height', a.h + 'px');
  S('min-width', a.w + 'px'); S('min-height', a.h + 'px');
  S('max-width', a.w + 'px'); S('max-height', a.h + 'px');
  S('overflow', 'hidden'); S('padding', '0'); S('border', '0'); S('background', 'transparent');
  if (a.force) { S('margin-left', 'auto'); S('margin-right', 'auto'); }
  box.setAttribute('data-pop-replaced', '1');
  box.setAttribute('data-pop-cand', String(a.idx));
  const mo = new MutationObserver((muts) => {
    for (const m of muts) m.addedNodes.forEach((n) => { if (n !== node) { try { n.remove(); } catch (e) {} } });
  });
  mo.observe(box, {childList: true});
  box.__popMO = mo;
  box.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = box.getBoundingClientRect();
  return {ok: true, rect: {x: r.x, y: r.y, w: r.width, h: r.height}};
}
"""
)

JS_OVERLAY = (
    "async (a) => {\n"
    + _JS_MK
    + r"""
  const node = await mk(a);
  const box = document.createElement('div');
  const left = a.cx - a.w / 2 + window.scrollX;
  const top = a.cy - a.h / 2 + window.scrollY;
  box.style.cssText = 'position:absolute!important;z-index:2147483647!important;overflow:hidden!important;margin:0!important;padding:0!important;' +
    'left:' + Math.max(0, left) + 'px!important;top:' + Math.max(0, top) + 'px!important;width:' + a.w + 'px!important;height:' + a.h + 'px!important;';
  box.setAttribute('data-pop-replaced', '1');
  box.setAttribute('data-pop-overlay', '1');
  box.appendChild(node);
  document.documentElement.appendChild(box);
  return {ok: true};
}
"""
)

JS_RESTORE = r"""
() => {
  let n = 0;
  document.querySelectorAll('[data-pop-injected], [data-pop-overlay]').forEach((e) => { try { e.remove(); n++; } catch (x) {} });
  document.querySelectorAll('[data-pop-replaced]').forEach((box) => {
    try {
      if (box.__popMO) { box.__popMO.disconnect(); box.__popMO = null; }
      if (box.__popOrigEl) {
        const o = box.__popOrigEl; box.__popOrigEl = null;
        box.replaceWith(o);
      } else if (box.__popOrigNodes) {
        const nodes = box.__popOrigNodes; box.__popOrigNodes = null;
        box.replaceChildren(...nodes);
        const st = box.__popOrigStyle; box.__popOrigStyle = null;
        if (st === null || st === undefined) box.removeAttribute('style'); else box.setAttribute('style', st);
        box.removeAttribute('data-pop-replaced');
      }
      n++;
    } catch (x) {}
  });
  document.querySelectorAll('[data-pop-cand]').forEach((e) => e.removeAttribute('data-pop-cand'));
  return n;
}
"""

# Reads back what is really on screen: is the creative fully visible, is something on top of it, is a video playing?
JS_VERIFY = r"""
async (tryPlay) => {
  const el = document.querySelector('[data-pop-replaced]');
  if (!el) return {found: false};
  el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const vw = innerWidth, vh = innerHeight;
  if (tryPlay) {
    for (const v of document.querySelectorAll('video')) {
      const b = v.getBoundingClientRect();
      if (b.width < 120 || b.height < 70 || b.bottom < 0 || b.top > vh) continue;
      if (!v.paused && !v.muted) continue;
      try { v.muted = false; v.volume = 1; await v.play(); }
      catch (e) { try { v.muted = true; await v.play(); } catch (e2) {} }
    }
    await new Promise((r) => setTimeout(r, 1200));
  }
  const r = el.getBoundingClientRect();
  const ix = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
  const iy = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
  const area = r.width * r.height;
  const visible = area ? (ix * iy) / area : 0;
  let covered = 0, tested = 0;
  for (const f of [[.5, .5], [.1, .1], [.9, .1], [.1, .9], [.9, .9]]) {
    const x = r.left + r.width * f[0], y = r.top + r.height * f[1];
    if (x < 0 || y < 0 || x >= vw || y >= vh) continue;
    tested++;
    const top = document.elementFromPoint(x, y);
    if (top && top !== el && !el.contains(top)) covered++;
  }
  const videos = [];
  document.querySelectorAll('video').forEach((v) => {
    const b = v.getBoundingClientRect();
    if (b.width < 120 || b.height < 70 || b.bottom < 0 || b.top > vh) return;
    videos.push({paused: v.paused, muted: !!(v.muted || v.volume === 0), buffering: !v.paused && v.readyState < 3, ended: v.ended});
  });
  const embeds = document.querySelectorAll('iframe[src*="youtube"], iframe[src*="vimeo"], iframe[src*="dailymotion"]').length;
  return {found: true, visible: visible, covered: covered, tested: tested,
          rect: {x: r.left, y: r.top, w: r.width, h: r.height},
          abs: {x: r.left + window.scrollX, y: r.top + window.scrollY},
          videos: videos, embeds: embeds};
}
"""

VISION_PROMPT = (
    "This is a screenshot of a web page. Detect every display-advertisement slot that is visible "
    "(banner ads, Google ads, sponsored boxes, ad placeholders, boxes labelled 'Advertisement' or 'Ad'). "
    'Return ONLY a JSON array like [{"box_2d": [ymin, xmin, ymax, xmax], "label": "ad"}] with coordinates '
    "normalised to 0-1000. Return [] if there are none. Do not include navigation, article photos or logos."
)

# --------------------------------------------------------------------------------------
# Footer strip (URL + timestamp)
# --------------------------------------------------------------------------------------


def _font(size: int = 14):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while len(text) > 4 and draw.textlength(text + "...", font=font) > max_w:
        text = text[:-1]
    return text + "..."


def add_footer(png: bytes, page_url: str, stamp: str, size: str, label: str) -> bytes:
    im = Image.open(io.BytesIO(png)).convert("RGB")
    bar = 34
    out = Image.new("RGB", (im.width, im.height + bar), (24, 24, 27))
    out.paste(im, (0, 0))
    d = ImageDraw.Draw(out)
    f = _font(14)
    right = f"{stamp}  |  ad {size}" + (f"  |  {label}" if label else "")
    rw = d.textlength(right, font=f)
    d.text((im.width - rw - 10, im.height + 9), right, fill=(200, 200, 205), font=f)
    left = _fit(d, page_url, f, max(50, im.width - rw - 40))
    d.text((10, im.height + 9), left, fill=(240, 240, 240), font=f)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------------------
# Browser helpers
# --------------------------------------------------------------------------------------


def ws_url(cfg: JobConfig) -> str:
    q = {"token": cfg.browserless_token, "timeout": str(int(cfg.session_timeout_s) * 1000)}
    if cfg.stealth:
        q["stealth"] = "true"
    if cfg.residential_proxy:
        q["proxy"] = "residential"
        if cfg.proxy_country:
            q["proxyCountry"] = cfg.proxy_country.lower()
    return f"wss://{cfg.region}.browserless.io?{urlencode(q)}"


async def connect_with_retry(pw, cfg: JobConfig, L: LogFn):
    last = None
    attempt = 0
    clamped = False
    while attempt < 3:
        attempt += 1
        try:
            return await pw.chromium.connect_over_cdp(ws_url(cfg), timeout=45000)
        except Exception as e:
            last = e
            # Browserless rejects a timeout above the plan limit: "...between 1 and 120,000..."
            m = re.search(r"between 1 and ([\d,]+)", str(e))
            if m and not clamped:
                limit_ms = int(m.group(1).replace(",", ""))
                cfg.session_timeout_s = max(10, limit_ms // 1000)
                clamped = True
                attempt -= 1
                L(f"plan limit reached - session timeout lowered to {cfg.session_timeout_s}s")
                continue
            L(f"Browserless connect failed (attempt {attempt}/3): {short(e, cfg.browserless_token)}")
            await asyncio.sleep(3 * attempt)
    raise RuntimeError(f"Could not connect to Browserless: {short(last, cfg.browserless_token)}")


async def new_context(browser, cfg: JobConfig):
    kwargs = dict(
        viewport={"width": cfg.viewport_w, "height": cfg.viewport_h},
        device_scale_factor=1,
        locale=cfg.locale,
        user_agent=MOBILE_UA if cfg.mobile else DESKTOP_UA,
        bypass_csp=True,
        ignore_https_errors=True,
    )
    if cfg.mobile:
        kwargs.update(is_mobile=True, has_touch=True)
    try:
        return await browser.new_context(**kwargs)
    except Exception:
        if browser.contexts:
            return browser.contexts[0]
        raise


async def safe_goto(page, url: str, L: LogFn):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        if "Timeout" in str(e):
            L("navigation is slow - continuing with the partially loaded page")
        else:
            raise


async def lazy_scroll(page, cfg: JobConfig, steps: int = 4):
    try:
        total = await page.evaluate(
            "() => Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)"
        )
        for i in range(steps):
            y = min(int((i + 1) * cfg.viewport_h * 0.9), int(total))
            await page.evaluate("(y) => window.scrollTo(0, y)", y)
            await page.wait_for_timeout(350)
        await page.evaluate("() => window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)
    except Exception:
        pass


async def prepare_page(page, cfg: JobConfig, scroll: bool = True):
    try:
        await page.wait_for_load_state("load", timeout=6000)
    except Exception:
        pass
    await page.wait_for_timeout(1000)
    for _ in range(2):
        try:
            n = await page.evaluate(JS_DISMISS)
        except Exception:
            n = 0
        await page.wait_for_timeout(800)
        if not n:
            break
    try:
        await page.evaluate(JS_HIDE_OVERLAYS)
    except Exception:
        pass
    if scroll:
        await lazy_scroll(page, cfg)


# --------------------------------------------------------------------------------------
# Agent steps: article choice, brand safety, slot replacement
# --------------------------------------------------------------------------------------


async def get_links(page) -> list:
    try:
        return await page.evaluate(JS_LINKS)
    except Exception:
        return []


async def pick_articles(cfg: JobConfig, gem: Optional[Gemini], site_url: str, links: list, L: LogFn, want: int = 10) -> list[str]:
    """Choose candidate article URLs. Runs OUTSIDE any browser session (Gemini time is not billed as session time)."""
    if not links:
        L("no article links found - using the given URL")
        return [site_url]
    picks: list[int] = []
    n_pick = max(5, min(30, int(want)))
    if gem:
        listing = "\n".join(
            f"[{i}] {l['text']} | {urlparse(l['href']).path} | {l.get('date', '')}" for i, l in enumerate(links)
        )
        prompt = (
            "You help capture ad-placement screenshots on publisher sites. Below are links from a publisher page "
            "(index, headline, url path, date if known). Pick the individual ARTICLE pages (not sections, tags, "
            "videos, galleries or the homepage) that are most likely the LATEST published stories. Prefer the "
            "newest date when dates exist, otherwise the most prominent top-of-page stories.\n"
            f"Extra instructions from the campaign manager: {cfg.guidelines or 'none'}\n\n"
            f"{listing}\n\n"
            f'Return ONLY JSON: {{"picks": [indices, newest/best first, max {n_pick}]}}'
        )
        data = await asyncio.to_thread(gem.ask_json, prompt)
        if isinstance(data, dict) and isinstance(data.get("picks"), list):
            for v in data["picks"]:
                try:
                    i = int(v)
                except Exception:
                    continue
                if 0 <= i < len(links) and i not in picks:
                    picks.append(i)
        elif gem.last_error:
            L(f"Gemini article pick failed, using heuristic ({gem.last_error[:120]})")
    if not picks:
        slugged = [i for i, l in enumerate(links) if len([s for s in urlparse(l["href"]).path.split("/") if s]) >= 2]
        picks = (slugged or list(range(len(links))))[:n_pick]
    # Top up with the remaining article-like links so there are always enough articles to try if a slot is missing
    if len(picks) < n_pick:
        slugged = [i for i, l in enumerate(links) if len([s for s in urlparse(l["href"]).path.split("/") if s]) >= 2]
        for i in slugged + list(range(len(links))):
            if i not in picks:
                picks.append(i)
            if len(picks) >= n_pick:
                break
    urls: list[str] = []
    for i in picks[:n_pick]:
        if links[i]["href"] not in urls:
            urls.append(links[i]["href"])
    L(f"article candidates: {len(urls)}")
    return urls or [site_url]


async def get_page_info(page) -> dict:
    return await page.evaluate(JS_PAGE_TEXT)


async def brand_safety_verdict(cfg: JobConfig, gem: Gemini, info: dict) -> dict:
    rules = cfg.brand_safety_rules.strip() or DEFAULT_BRAND_SAFETY_RULES
    prompt = (
        "You are a brand-safety reviewer for programmatic advertising. Decide whether it is SAFE to show a brand "
        "advertisement next to this web page.\n"
        f"Unsafe / sensitive categories for this brand: {rules}\n\n"
        f"URL: {info.get('url')}\nTitle: {info.get('title')}\nHeading: {info.get('h1')}\n"
        f"Text excerpt: {info.get('text')}\n\n"
        'Return ONLY JSON: {"safe": true or false, "category": "<category or none>", "reason": "<max 20 words>"}'
    )
    data = await asyncio.to_thread(gem.ask_json, prompt)
    if isinstance(data, dict) and "safe" in data:
        return data
    return {"safe": True, "reason": "brand-safety check unavailable (Gemini error) - NOT verified", "unchecked": True}


async def vision_place(page, gem: Gemini, cfg: JobConfig, c: Creative, L: LogFn) -> Optional[dict]:
    vw, vh = cfg.viewport_w, cfg.viewport_h
    try:
        total = await page.evaluate(
            "() => Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)"
        )
    except Exception:
        total = vh
    positions = [0]
    y = int(vh * 0.9)
    while y < total - vh * 0.3 and len(positions) < 4:
        positions.append(y)
        y += int(vh * 0.9)
    tol_w, tol_h = max(12, c.width * 0.12), max(12, c.height * 0.12)
    for sy in positions:
        await page.evaluate("(y) => window.scrollTo(0, y)", sy)
        await page.wait_for_timeout(700)
        shot = await page.screenshot(type="png")
        data = await asyncio.to_thread(gem.ask_json, VISION_PROMPT, shot)
        if not isinstance(data, list):
            continue
        best, best_err = None, 1e9
        for b in data:
            try:
                ymin, xmin, ymax, xmax = [float(v) for v in b["box_2d"]]
            except Exception:
                continue
            x0, x1 = xmin / 1000 * vw, xmax / 1000 * vw
            y0, y1 = ymin / 1000 * vh, ymax / 1000 * vh
            w, h = x1 - x0, y1 - y0
            if abs(w - c.width) <= tol_w and abs(h - c.height) <= tol_h:
                err = abs(w - c.width) + abs(h - c.height)
                if err < best_err:
                    best, best_err = ((x0 + x1) / 2, (y0 + y1) / 2), err
        if best:
            arg = {
                "cx": best[0], "cy": best[1], "w": c.width, "h": c.height,
                "kind": c.kind, "dataUri": c.data_uri, "html": c.html,
            }
            res = await page.evaluate(JS_OVERLAY, arg)
            if res and res.get("ok"):
                L("slot located visually (Gemini) - creative overlaid")
                return {"method": "vision-overlay", "hints": "gemini-vision"}
    await page.evaluate("() => window.scrollTo(0, 0)")
    return None


async def place_creative(page, cfg: JobConfig, gem: Optional[Gemini], c: Creative, L: LogFn,
                         allow_vision: bool = True) -> Optional[dict]:
    found, slots = {}, []
    for attempt in range(2):
        try:
            found = await page.evaluate(JS_FIND_SLOTS, [c.width, c.height, cfg.size_tolerance])
        except Exception as e:
            L(f"slot scan error: {short(e)}")
            found = {}
        slots = (found or {}).get("slots", [])
        if slots:
            break
        if attempt == 0:
            L(f"no {c.width}x{c.height} slot yet - waiting for ads to load")
            await page.wait_for_timeout(3500)
    if slots:
        s = slots[0]
        arg = {
            "idx": s["idx"], "w": c.width, "h": c.height, "kind": c.kind,
            "dataUri": c.data_uri, "html": c.html, "force": bool(s.get("force")),
        }
        res = await page.evaluate(JS_REPLACE, arg)
        if res and res.get("ok"):
            return {"method": found.get("method", "dom"), "hints": s.get("hints", ""), "count": len(slots)}
        L(f"replace failed: {(res or {}).get('reason', 'unknown')}")
    if allow_vision and cfg.use_vision_fallback and gem:
        return await vision_place(page, gem, cfg, c, L)
    return None


# --------------------------------------------------------------------------------------
# QA of the final screenshot (10 guidelines, judged by Gemini vision + DOM facts)
# --------------------------------------------------------------------------------------

QA_NAMES = {1: "Safety", 2: "Dates", 3: "Website", 4: "Creative", 5: "Layout", 6: "Authenticity",
            7: "Quality", 8: "Popups", 9: "Video", 10: "Language", 11: "Content"}

QA_GUIDELINES = """\
1. Safety: No crime, violence, sexual, alcohol, drugs, death, hate, or inappropriate/negative content.
2. Dates: No past, expired, or outdated dates shown as current.
3. Website: Looks safe, legitimate, and realistic; no suspicious or broken elements.
4. Creative: Correct ad, branding, text, images, and CTA; no missing/cropped elements.
5. Layout: Proper alignment, spacing, and sizing; no overlap or clipping.
6. Authenticity: Looks like a genuine live website, not a mockup.
7. Quality: Sharp, clear, clean, and high-quality; no visual artifacts.
8. Popups: No unwanted popups, banners, alerts, or overlays.
9. Video: If present, video must be playing and unmuted; not paused, muted, or buffering. Otherwise N/A.
10. Language: If content is in a local language (e.g., Tagalog), translate/check it if needed. Flag only if the content is inappropriate, misleading, incorrect, or inconsistent with the screenshot. Otherwise LOOKS FINE.
11. Content: The screenshot must show real page content (headline, article text, images) over at least 60-70% of its area - not a mostly blank, empty, still-loading or error page.
"""


def _to_png(raw: bytes, max_side: int = 1400) -> Optional[bytes]:
    try:
        im = Image.open(io.BytesIO(raw))
        im.seek(0)
        im = im.convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def crop_region(png: bytes, x: float, y: float, w: float, h: float, pad: int = 24, scale: int = 2) -> Optional[bytes]:
    """2x close-up of the ad placement so Gemini can read the text / CTA and spot cropping."""
    try:
        im = Image.open(io.BytesIO(png)).convert("RGB")
        box = (max(0, int(x - pad)), max(0, int(y - pad)), min(im.width, int(x + w + pad)), min(im.height, int(y + h + pad)))
        if box[2] - box[0] < 10 or box[3] - box[1] < 10:
            return None
        c = im.crop(box)
        c = c.resize((c.width * scale, c.height * scale), Image.LANCZOS)
        buf = io.BytesIO()
        c.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def content_coverage(png: bytes, cell: int = 20) -> float:
    """% of the screenshot (in 20px cells) that holds something other than the plain page background."""
    try:
        from PIL import ImageChops
        im = Image.open(io.BytesIO(png)).convert("L")
        hist = im.histogram()
        bg = max(range(256), key=lambda v: hist[v])
        mask = ImageChops.difference(im, Image.new("L", im.size, bg)).point(lambda v: 255 if v > 24 else 0)
        small = mask.resize((max(1, im.width // cell), max(1, im.height // cell)), Image.BOX)
        px = list(small.getdata())
        return round(100.0 * sum(1 for v in px if v > 10) / max(1, len(px)), 1)
    except Exception:
        return 100.0


def parse_qa(data) -> Optional[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
        return None
    checks: dict[int, tuple[str, str]] = {}
    for it in data["checks"]:
        try:
            i = int(it.get("id"))
        except Exception:
            continue
        if i not in QA_NAMES:
            continue
        st = str(it.get("status", "")).strip().upper()
        if st in ("PASS", "OK", "LOOKS FINE", "YES"):
            st = "PASS"
        elif st in ("FAIL", "FAILED", "NO"):
            st = "FAIL"
        else:
            st = "N/A"
        checks[i] = (st, str(it.get("note", "")).replace("\n", " ")[:140])
    if not checks:
        return None
    for i in QA_NAMES:
        checks.setdefault(i, ("N/A", "not assessed"))
    return {"checks": checks, "summary": str(data.get("summary", ""))[:200]}


def apply_dom_facts(qa: dict, facts: dict, full_page: bool, min_content: float = 60.0) -> None:
    """Hard facts read from the page override the model: they are measured, not guessed."""
    cp = facts.get("content_pct")
    if cp is not None and cp < min_content:
        qa["checks"][11] = ("FAIL", f"only {cp:.0f}% of the screenshot shows page content (need {min_content:.0f}%+)")
    if not facts.get("found"):
        return
    if facts.get("covered"):
        qa["checks"][5] = ("FAIL", "another element is drawn on top of the creative")
    vis = facts.get("visible", 1)
    if not full_page and vis < 0.95:
        qa["checks"][4] = ("FAIL", f"only {int(vis * 100)}% of the creative is inside the screenshot")


def finish_qa(qa: dict) -> dict:
    qa["failed"] = sorted(i for i, (s, _) in qa["checks"].items() if s == "FAIL")
    qa["status"] = "FAIL" if qa["failed"] else "PASS"
    return qa


def qa_report_text(qa: dict) -> str:
    ch = qa["checks"]
    parts = []
    fails = [f"{i} {QA_NAMES[i]}: {ch[i][1] or 'failed'}" for i in sorted(ch) if ch[i][0] == "FAIL"]
    if fails:
        parts.append("FAILED -> " + " | ".join(fails))
    passed = [QA_NAMES[i] for i in sorted(ch) if ch[i][0] == "PASS"]
    parts.append("passed: " + (", ".join(passed) or "-"))
    na = [QA_NAMES[i] for i in sorted(ch) if ch[i][0] == "N/A"]
    if na:
        parts.append("n/a: " + ", ".join(na))
    if qa.get("summary"):
        parts.append(qa["summary"])
    return " ; ".join(parts)


async def qa_review(cfg: JobConfig, gem: Gemini, c: Creative, png: bytes, facts: dict, info: dict,
                    L: LogFn, timeout: float) -> Optional[dict]:
    close = None
    if facts.get("found"):
        box = facts["abs"] if cfg.full_page else facts["rect"]
        r = facts["rect"]
        close = crop_region(png, box["x"], box["y"], r["w"], r["h"])
    ref = _to_png(c.raw) if (c.kind == "image" and c.raw) else None
    extra, desc = [], "Image 1 = the full screenshot of the page. "
    n = 2
    if close:
        extra.append(close)
        desc += f"Image {n} = 2x close-up of the ad placement. "
        n += 1
    if ref:
        extra.append(ref)
        desc += f"Image {n} = the ORIGINAL creative supplied by the client (what the ad must look like). "
    else:
        desc += "The creative is an HTML tag, so there is no reference file; judge it from the close-up. "
    prompt = (
        "You are a strict QA reviewer of ad-placement proof screenshots. The screenshot must show a real news/article "
        "page with the client's banner placed on it.\n"
        f"{desc}\n"
        f"Today's date: {now_str(cfg.timezone)}\n"
        f"Page URL: {info.get('url', '')}\nPage title: {info.get('title', '')}\nHeadline: {info.get('h1', '')}\n"
        f"Page text excerpt: {(info.get('text') or '')[:1500]}\n"
        f"Creative: {c.name}, expected size {c.width}x{c.height}\n"
        f"Measured facts from the browser (reliable): creative visible {int(facts.get('visible', 0) * 100)}%, "
        f"elements covering it: {facts.get('covered', 0)}, videos in view: {json.dumps(facts.get('videos', []))}, "
        f"embedded video iframes: {facts.get('embeds', 0)}, "
        f"measured page-content coverage: {facts.get('content_pct', '?')}% (minimum {cfg.min_content_pct}%)\n\n"
        "Check the screenshot against these 11 guidelines:\n"
        f"{QA_GUIDELINES}\n"
        f"{('Extra rules: ' + cfg.qa_extra_rules.strip()) if cfg.qa_extra_rules.strip() else ''}\n"
        "Judging notes:\n"
        "- Judge only what is visible. FAIL means a clear violation, not a doubt.\n"
        "- 1 and 3: also FAIL if the page is not an article (error/404, login wall, blank page, bare homepage).\n"
        "- 2: FAIL only for dates that are expired or clearly outdated but presented as current (offers, events, 'today', "
        "'live', news dated long ago). A normal recent publish date is fine.\n"
        "- 4: compare against the original creative: same ad, branding, text, images and CTA, nothing missing or cropped.\n"
        "- 9: status N/A if there is no video. Otherwise PASS only if it is playing and unmuted.\n"
        "- 11: FAIL if real page content covers clearly less than the minimum % of the screenshot (mostly blank/white, skeleton "
        "loaders, error page).\n"
        "- 10: if the content is not English, translate it mentally. FAIL only if inappropriate, misleading, incorrect or "
        "inconsistent with the screenshot, otherwise PASS (note 'LOOKS FINE').\n\n"
        'Return ONLY JSON: {"checks":[{"id":1,"status":"PASS|FAIL|N/A","note":"<max 15 words>"}, ... ids 1 to 11 ...],'
        '"summary":"<max 25 words>"}'
    )
    try:
        data = await asyncio.wait_for(asyncio.to_thread(gem.ask_json, prompt, png, extra), timeout=timeout)
    except Exception as e:
        L(f"QA call failed: {short(e)}")
        return None
    qa = parse_qa(data)
    if qa is None and gem.last_error:
        L(f"QA unavailable ({gem.last_error[:100]})")
    return qa


async def verify_placement(page, try_play: bool = False) -> dict:
    try:
        return await page.evaluate(JS_VERIFY, bool(try_play)) or {}
    except Exception:
        return {}


async def shoot_and_review(page, cfg: JobConfig, gem: Optional[Gemini], c: Creative, L: LogFn, deadline: float) -> dict:
    """Screenshot + QA. If QA flags something fixable (popup, cropped/blurry render) it fixes it and retries once."""
    png, qa, facts = b"", None, {}
    for attempt in range(2):
        facts = await verify_placement(page, try_play=True)
        if facts.get("found") and (facts.get("covered") or (not cfg.full_page and facts.get("visible", 1) < 0.95)):
            try:
                await page.evaluate(JS_HIDE_OVERLAYS)
            except Exception:
                pass
            await page.wait_for_timeout(500)
            facts = await verify_placement(page)
        png = await page.screenshot(type="png", full_page=cfg.full_page, timeout=40000)
        facts["content_pct"] = content_coverage(png)
        qa = None
        remaining = deadline - time.monotonic()
        if cfg.qa_enabled and gem and remaining > 25:
            try:
                info = await get_page_info(page)
            except Exception:
                info = {"url": page.url}
            qa = await qa_review(cfg, gem, c, png, facts, info, L, timeout=min(45.0, remaining - 12))
            if qa:
                apply_dom_facts(qa, facts, cfg.full_page, cfg.min_content_pct)
                finish_qa(qa)
        if qa is None and facts["content_pct"] < cfg.min_content_pct:  # content rule is measured, needs no AI
            qa = {"checks": {i: ("N/A", "AI QA not run") for i in QA_NAMES}, "summary": ""}
            apply_dom_facts(qa, facts, cfg.full_page, cfg.min_content_pct)
            finish_qa(qa)
        if qa is None or qa["status"] == "PASS" or attempt == 1:
            break
        fixable = set(qa["failed"]) & {4, 5, 7, 8, 11}
        if not fixable or deadline - time.monotonic() < 40:
            break
        L("QA flagged " + ", ".join(QA_NAMES[i] for i in sorted(fixable)) + " - fixing and retrying once")
        if 8 in fixable:
            try:
                await page.evaluate(JS_DISMISS)
                await page.evaluate(JS_HIDE_OVERLAYS)
            except Exception:
                pass
        await page.wait_for_timeout(1500)
    return {"png": png, "qa": qa, "facts": facts}


# --------------------------------------------------------------------------------------
# Per-site workflow
#
# A Browserless session has a hard time limit (your plan: 120 s). So the work is split:
#   1. discovery session  (short)  : open the homepage, collect links, close.
#   2. Gemini picks articles       : runs with NO browser session open.
#   3. capture session(s)          : open ONE article, brand-safety check runs in parallel while
#                                    the page prepares, then swap + screenshot every creative.
#                                    If time runs low, a fresh session continues with the rest.
# --------------------------------------------------------------------------------------


async def close_quietly(browser):
    try:
        await browser.close()
    except Exception:
        pass


async def discover_articles(pw, cfg: JobConfig, gem: Optional[Gemini], site_url: str, L: LogFn, want: int = 10) -> list[str]:
    browser = await connect_with_retry(pw, cfg, L)
    links: list = []
    try:
        ctx = await new_context(browser, cfg)
        page = await ctx.new_page()
        await safe_goto(page, site_url, L)
        await page.wait_for_timeout(2000)
        links = await get_links(page)
        if len(links) < 5:
            await page.wait_for_timeout(2500)
            links = await get_links(page)
    finally:
        await close_quietly(browser)
    return await pick_articles(cfg, gem, site_url, links, L, want)


SESSION_DEAD = re.compile(r"closed|disconnected|connection|target page", re.I)


async def capture_session(pw, cfg: JobConfig, gem: Optional[Gemini], idx: int, host: str, article_url: str,
                          todo: list, out_dir: Path, L: LogFn, check_safety: bool) -> dict:
    """One browser session on one article. todo = [(order, Creative), ...].
    Only a real ad slot of the creative's size is used (NO_SLOT if there is none) - nothing is ever created on the page."""
    out = {"rows": [], "todo": list(todo), "unsafe": False, "reason": "", "unchecked": False, "stop": False}
    browser = await connect_with_retry(pw, cfg, L)
    deadline = time.monotonic() + cfg.session_timeout_s - 10  # Browserless kills the session at the limit
    try:
        ctx = await new_context(browser, cfg)
        page = await ctx.new_page()
        await safe_goto(page, article_url, L)
        await page.wait_for_timeout(1200)

        safety_task = None
        if check_safety and gem:
            try:
                info = await get_page_info(page)
                safety_task = asyncio.create_task(brand_safety_verdict(cfg, gem, info))
            except Exception as e:
                L(f"could not read page text for brand safety: {short(e)}")
                out["unchecked"] = True

        await prepare_page(page, cfg, scroll=True)  # Gemini works on the safety check meanwhile

        if safety_task is not None:
            try:
                verdict = await asyncio.wait_for(safety_task, timeout=max(5.0, deadline - time.monotonic() - 30))
            except Exception:
                verdict = {"safe": True, "unchecked": True}
            if verdict.get("unchecked"):
                out["unchecked"] = True
                L("brand safety could not be verified (continuing)")
            elif not verdict.get("safe", True):
                out["unsafe"] = True
                out["reason"] = f"{verdict.get('category', '')}: {verdict.get('reason', '')}".strip()
                L(f"brand-safety FAIL ({out['reason']})")
                return out

        shown_url = page.url
        tag = hashlib.md5(article_url.encode()).hexdigest()[:5]
        L(f"capturing on {shown_url}")
        while out["todo"]:
            k, c = out["todo"][0]
            remaining = deadline - time.monotonic()
            if remaining < 15:
                L("session time almost used up - continuing in a fresh session")
                break
            row = Result(index=idx, order=k, site=article_url, page_url=shown_url,
                         creative=c.name, size=f"{c.width}x{c.height}")
            try:
                try:  # remove the banner of the previous creative so every screenshot shows ONE creative only
                    await page.evaluate(JS_RESTORE)
                except Exception:
                    pass
                placed = await place_creative(page, cfg, gem, c, L, allow_vision=(remaining > 45))
                if not placed:
                    row.status = "NO_SLOT"
                    row.note = f"No {c.width}x{c.height} ad slot found on this page"
                else:
                    await page.wait_for_timeout(900)
                    shot = await shoot_and_review(page, cfg, gem, c, L, deadline)
                    png, qa, facts = shot["png"], shot["qa"], shot["facts"]
                    stamp = now_str(cfg.timezone)
                    if cfg.add_footer:
                        png = add_footer(png, shown_url, stamp, f"{c.width}x{c.height}", cfg.footer_label)
                    fname = f"{idx:03d}_{safe_name(host)}_{c.width}x{c.height}_{k}_{tag}.png"
                    (out_dir / fname).write_bytes(png)
                    row.method = placed.get("method", "")
                    row.file = str(out_dir / fname)
                    row.captured_at = stamp
                    row.note = f"matched via {placed.get('hints', '') or placed.get('method', '')}"
                    if qa is None:
                        row.status, row.qa_status = "OK", "NOT_RUN"
                        if not (cfg.qa_enabled and gem):
                            row.note += " | QA not run (needs a Gemini key)"
                        else:
                            row.note += " | QA could not be completed"
                    else:
                        row.qa_report = qa_report_text(qa)
                        row.qa_fails = len(qa["failed"])
                        if qa["status"] == "PASS":
                            row.status, row.qa_status = "OK", "PASS"
                        else:
                            row.status, row.qa_status = "QA_FAIL", "FAIL"
                            L("QA FAIL: " + ", ".join(QA_NAMES[i] for i in qa["failed"]))
                    if qa is not None and qa["status"] == "PASS":
                        L(f"saved {fname} (QA pass)")
                    else:
                        L(f"saved {fname}")
            except Exception as e:
                if SESSION_DEAD.search(str(e)):
                    L("browser session ended early - continuing in a fresh session")
                    break
                row.status = "ERROR"
                row.note = short(e, cfg.browserless_token)
            out["rows"].append(row)
            out["todo"].pop(0)
            if row.status == "OK":  # this article is now used: every other creative must go to a DIFFERENT article
                out["stop"] = True
                break
        return out
    finally:
        await close_quietly(browser)


async def run_url(pw, cfg: JobConfig, gem: Optional[Gemini], idx: int, host: str, url: str, todo: list,
                  out_dir: Path, L: LogFn, check_safety: bool):
    """All pending creatives on ONE article (several browser sessions if the time limit requires it)."""
    rows: list[Result] = []
    todo = list(todo)
    unsafe, reason, unchecked, sessions, stopped = False, "", False, 0, False
    while todo and sessions < 4:
        sessions += 1
        try:
            res = await capture_session(pw, cfg, gem, idx, host, url, todo, out_dir, L,
                                        check_safety=(check_safety and sessions == 1))
        except Exception as e:
            L(f"capture failed: {short(e, cfg.browserless_token)}")
            for k, c in todo:
                rows.append(Result(index=idx, order=k, site=url, page_url=url, creative=c.name,
                                   size=f"{c.width}x{c.height}", status="ERROR", note=short(e, cfg.browserless_token)))
            todo = []
            break
        if res["unsafe"]:
            unsafe, reason = True, res["reason"]
            break
        unchecked = unchecked or res["unchecked"]
        rows += res["rows"]
        todo = res["todo"]
        if res.get("stop"):
            stopped = True
            break
    if not unsafe and not stopped:
        for k, c in todo:  # could not be finished within the available session time
            rows.append(Result(index=idx, order=k, site=url, page_url=url, creative=c.name,
                               size=f"{c.width}x{c.height}", status="ERROR",
                               note=f"Not finished within {cfg.session_timeout_s}s browser sessions - rerun this site"))
    return rows, unsafe, reason, unchecked


def article_key(u: str) -> str:
    return u.split("#")[0].split("?")[0].rstrip("/").lower()


async def process_site(pw, cfg: JobConfig, gem: Optional[Gemini], idx: int, site: str,
                       creatives: list[Creative], out_dir: Path, log: LogFn,
                       used_articles: Optional[set] = None) -> list[Result]:
    """
    For every creative the agent walks through the latest articles until it gets a screenshot that passes QA:
      walk through the site's articles, real ad slot only (next article if no slot / QA fails / error)
      EVERY creative gets its OWN article: an article that already produced a screenshot is never used again
      (not for another creative, and not for another row of the same website in this job).
    A creative with no real slot on any checked article gets NO_SLOT and NO screenshot.
    """
    site_url = normalize_url(site)
    host = urlparse(site_url).netloc.replace("www.", "")
    t0 = time.monotonic()

    def L(msg: str):
        log(f"[{idx}] {host} +{int(time.monotonic() - t0)}s: {msg}")

    used = used_articles if used_articles is not None else set()
    # enough articles for every creative to get its own, plus spares for articles without a slot
    want = min(30, max(int(cfg.max_article_tries), 3 * len(creatives)))
    if cfg.find_latest_article:
        L(f"looking for the latest articles (up to {want})")
        candidates = await discover_articles(pw, cfg, gem, site_url, L, want)
    else:
        candidates = [site_url]
    candidates = [u for u in candidates if article_key(u) not in used][:want]
    if not candidates:
        return [Result(index=idx, site=site_url, page_url=site_url, status="ERROR",
                       note="every article found on this site was already used for another screenshot")]

    accepted: dict[int, Result] = {}  # creative order -> screenshot that passed
    fallback: dict[int, Result] = {}  # creative order -> best screenshot that failed QA
    problem: dict[int, Result] = {}  # creative order -> last NO_SLOT / ERROR row
    all_files: set[str] = set()
    safe_urls: list[str] = []
    notes: list[str] = []
    unchecked = False
    pending = list(enumerate(creatives, 1))

    def absorb(rows: list[Result]):
        for r in rows:
            if r.file:
                all_files.add(r.file)
            if r.status == "OK":
                accepted.setdefault(r.order, r)
            elif r.status == "QA_FAIL":
                cur = fallback.get(r.order)
                if cur is None or r.qa_fails < cur.qa_fails:
                    fallback[r.order] = r
            else:
                problem[r.order] = r

    # ---- natural slots, article by article --------------------------------------------------------------------
    for n, url in enumerate(candidates, 1):
        if not pending:
            break
        if article_key(url) in used:
            continue
        rows, unsafe, reason, unc = await run_url(pw, cfg, gem, idx, host, url, pending, out_dir, L,
                                                  check_safety=cfg.brand_safety)
        if unsafe:
            notes.append(f"{url} -> {reason}")
            L(f"article {n}/{len(candidates)} not brand-safe - trying the next article")
            continue
        unchecked = unchecked or unc
        safe_urls.append(url)
        absorb(rows)
        if any(r.status == "OK" for r in rows):
            used.add(article_key(url))  # one article = one screenshot
        pending = [(k, c) for k, c in pending if k not in accepted]
        if pending:
            L(f"article {n}/{len(candidates)}: {len(pending)} creative(s) still without a passing screenshot"
              + (" - trying the next article" if n < len(candidates) else ""))

    if not safe_urls:
        return [Result(index=idx, site=site_url, page_url=site_url, status="SKIPPED_BRAND_SAFETY",
                       note=("; ".join(notes))[:300] or "no safe article found")]

    # ---- final rows ------------------------------------------------------------------------------------------------
    final: list[Result] = []
    for k, c in enumerate(creatives, 1):
        r = accepted.get(k)
        fb = fallback.get(k)
        if r is None and fb is not None and article_key(fb.site) not in used:
            r = fb
            used.add(article_key(fb.site))
        if r is None:
            r = problem.get(k) or Result(index=idx, order=k, site=site_url, page_url=site_url, creative=c.name,
                                         size=f"{c.width}x{c.height}", status="ERROR", note="not processed")
            if r.status == "NO_SLOT":
                r.note = f"No {c.width}x{c.height} ad slot on any of the {len(safe_urls)} latest article(s) checked"
        final.append(r)

    keep = {r.file for r in final if r.file}
    for f in all_files - keep:  # screenshots of rejected attempts
        try:
            Path(f).unlink()
        except Exception:
            pass
    if unchecked:
        for r in final:
            if r.file:
                r.note += " | brand safety NOT verified"
    ok = sum(1 for r in final if r.status == "OK")
    L(f"done: {ok}/{len(final)} screenshot(s) passed QA, {sum(1 for r in final if r.status == 'QA_FAIL')} flagged by QA")
    return final


async def run_job(cfg: JobConfig, sites: list[str], creatives: list[Creative], out_dir: Path,
                  log: Optional[LogFn] = None, progress: Optional[Callable[[int, int], None]] = None) -> list[Result]:
    from playwright.async_api import async_playwright

    log = log or print
    out_dir.mkdir(parents=True, exist_ok=True)
    gem: Optional[Gemini] = None
    if cfg.gemini_key:
        try:
            gem = Gemini(cfg.gemini_key, cfg.gemini_model)
        except Exception as e:
            log(f"Gemini disabled: {short(e)}")
    else:
        log("No Gemini key - using heuristics only (no brand-safety check, no vision fallback)")

    sem = asyncio.Semaphore(max(1, int(cfg.concurrency)))
    results: list[Result] = []
    done = 0
    used_by_host: dict[str, set] = {}  # articles already screenshotted, per website (shared by all rows of the job)
    host_locks: dict[str, asyncio.Lock] = {}

    async with async_playwright() as pw:

        async def worker(i: int, site: str):
            nonlocal done
            hkey = urlparse(normalize_url(site)).netloc.replace("www.", "").lower()
            lock = host_locks.setdefault(hkey, asyncio.Lock())  # rows of the same website run one after another
            async with sem, lock:
                try:
                    rows = await process_site(pw, cfg, gem, i, site, creatives, out_dir, log,
                                              used_articles=used_by_host.setdefault(hkey, set()))
                except Exception as e:
                    log(f"[{i}] {site}: ERROR {short(e, cfg.browserless_token)}")
                    rows = [Result(index=i, site=site, status="ERROR", note=short(e, cfg.browserless_token))]
            results.extend(rows)
            done += 1
            if progress:
                progress(done, len(sites))

        await asyncio.gather(*(worker(i, s) for i, s in enumerate(sites, 1)))

    results.sort(key=lambda r: (r.index, r.order))
    return results


def run_job_sync(*args, **kwargs) -> list[Result]:
    """Entry point for Streamlit / scripts."""
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    return asyncio.run(run_job(*args, **kwargs))


def build_outputs(results: list[Result], out_dir: Path) -> tuple[bytes, bytes]:
    """Write report.csv + a ZIP of all screenshots. Returns (zip_bytes, csv_bytes)."""
    fields = ["index", "site", "page_url", "creative", "size", "status", "qa_status", "qa_report", "method",
              "captured_at", "file", "note"]
    csv_path = out_dir / "report.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            row = {k: getattr(r, k) for k in fields}
            row["file"] = Path(r.file).name if r.file else ""
            w.writerow(row)
    zip_path = out_dir / "pop_screenshots.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for r in results:
            if r.file and Path(r.file).exists():
                z.write(r.file, Path(r.file).name)
        z.write(csv_path, "report.csv")
    return zip_path.read_bytes(), csv_path.read_bytes()
