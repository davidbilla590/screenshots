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
import io
import json
import re
import sys
import tempfile
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
    status: str = "PENDING"  # OK | NO_SLOT | SKIPPED_BRAND_SAFETY | ERROR
    method: str = ""
    note: str = ""
    file: str = ""
    captured_at: str = ""


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
    max_article_tries: int = 3
    size_tolerance: int = 3
    use_vision_fallback: bool = True
    full_page: bool = False
    add_footer: bool = True
    footer_label: str = ""
    timezone: str = "Asia/Kolkata"


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

    def ask_json(self, prompt: str, image_png: Optional[bytes] = None):
        """Blocking call (run it with asyncio.to_thread). Returns parsed JSON or None."""
        import time

        from google.genai import types

        for attempt in range(2):
            try:
                parts: list = []
                if image_png:
                    parts.append(types.Part.from_bytes(data=image_png, mime_type="image/png"))
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
    el.replaceWith(box);
  } else if (box.__popMO) {
    box.__popMO.disconnect();
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
  box.appendChild(node);
  document.documentElement.appendChild(box);
  return {ok: true};
}
"""
)

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
    for attempt in range(1, 4):
        try:
            return await pw.chromium.connect_over_cdp(ws_url(cfg), timeout=45000)
        except Exception as e:
            last = e
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
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        if "Timeout" in str(e):
            L("navigation is slow - continuing with the partially loaded page")
        else:
            raise


async def lazy_scroll(page, cfg: JobConfig, steps: int = 6):
    try:
        total = await page.evaluate(
            "() => Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)"
        )
        for i in range(steps):
            y = min(int((i + 1) * cfg.viewport_h * 0.9), int(total))
            await page.evaluate("(y) => window.scrollTo(0, y)", y)
            await page.wait_for_timeout(450)
        await page.evaluate("() => window.scrollTo(0, 0)")
        await page.wait_for_timeout(600)
    except Exception:
        pass


async def prepare_page(page, cfg: JobConfig, scroll: bool = True):
    try:
        await page.wait_for_load_state("load", timeout=12000)
    except Exception:
        pass
    await page.wait_for_timeout(1500)
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


async def pick_articles(page, cfg: JobConfig, gem: Optional[Gemini], site_url: str, L: LogFn) -> list[str]:
    try:
        links = await page.evaluate(JS_LINKS)
    except Exception:
        links = []
    if not links:
        L("no article links found - using the given URL")
        return [site_url]
    picks: list[int] = []
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
            'Return ONLY JSON: {"picks": [indices, best first, max 5]}'
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
        picks = (slugged or list(range(len(links))))[:5]
    urls: list[str] = []
    for i in picks[:5]:
        if links[i]["href"] not in urls:
            urls.append(links[i]["href"])
    L(f"article candidates: {len(urls)}")
    return urls or [site_url]


async def check_brand_safety(page, cfg: JobConfig, gem: Gemini) -> dict:
    info = await page.evaluate(JS_PAGE_TEXT)
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


async def place_creative(page, cfg: JobConfig, gem: Optional[Gemini], c: Creative, L: LogFn) -> Optional[dict]:
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
    if cfg.use_vision_fallback and gem:
        return await vision_place(page, gem, cfg, c, L)
    return None


# --------------------------------------------------------------------------------------
# Per-site workflow
# --------------------------------------------------------------------------------------


async def process_site(pw, cfg: JobConfig, gem: Optional[Gemini], idx: int, site: str,
                       creatives: list[Creative], out_dir: Path, log: LogFn) -> list[Result]:
    site_url = normalize_url(site)
    host = urlparse(site_url).netloc.replace("www.", "")

    def L(msg: str):
        log(f"[{idx}] {host}: {msg}")

    rows: list[Result] = []
    browser = await connect_with_retry(pw, cfg, L)
    try:
        ctx = await new_context(browser, cfg)
        page = await ctx.new_page()
        L("opening page")
        await safe_goto(page, site_url, L)

        candidates = [site_url]
        loaded = None
        if cfg.find_latest_article:
            await prepare_page(page, cfg, scroll=False)
            candidates = await pick_articles(page, cfg, gem, site_url, L)
        else:
            await prepare_page(page, cfg, scroll=True)
            loaded = site_url

        chosen, notes = None, []
        unchecked = False
        for url in candidates[: max(1, cfg.max_article_tries)]:
            if loaded != url:
                L(f"opening {url}")
                await safe_goto(page, url, L)
                await prepare_page(page, cfg, scroll=True)
                loaded = url
            if cfg.brand_safety and gem:
                verdict = await check_brand_safety(page, cfg, gem)
                if verdict.get("unchecked"):
                    unchecked = True
                elif not verdict.get("safe", True):
                    reason = f"{verdict.get('category', '')}: {verdict.get('reason', '')}"
                    L(f"brand-safety FAIL ({reason}) - trying next article")
                    notes.append(f"{url} -> {reason}")
                    continue
            chosen = url
            break

        if not chosen:
            rows.append(Result(index=idx, site=site_url, page_url=site_url, status="SKIPPED_BRAND_SAFETY",
                               note="; ".join(notes)[:300] or "no safe article found"))
            return rows

        article_url = page.url
        L(f"capturing on {article_url}")
        for k, c in enumerate(creatives, 1):
            row = Result(index=idx, order=k, site=site_url, page_url=article_url,
                         creative=c.name, size=f"{c.width}x{c.height}")
            try:
                placed = await place_creative(page, cfg, gem, c, L)
                if not placed:
                    row.status = "NO_SLOT"
                    row.note = f"No {c.width}x{c.height} ad slot found on this page"
                else:
                    await page.wait_for_timeout(900)
                    png = await page.screenshot(type="png", full_page=cfg.full_page, timeout=40000)
                    stamp = now_str(cfg.timezone)
                    if cfg.add_footer:
                        png = add_footer(png, article_url, stamp, f"{c.width}x{c.height}", cfg.footer_label)
                    fname = f"{idx:03d}_{safe_name(host)}_{c.width}x{c.height}_{k}.png"
                    (out_dir / fname).write_bytes(png)
                    row.status = "OK"
                    row.method = placed.get("method", "")
                    row.file = str(out_dir / fname)
                    row.captured_at = stamp
                    row.note = f"matched via {placed.get('hints', '') or placed.get('method', '')}"
                    if unchecked:
                        row.note += " | brand safety NOT verified"
                    L(f"saved {fname}")
            except Exception as e:
                row.status = "ERROR"
                row.note = short(e, cfg.browserless_token)
            rows.append(row)
    finally:
        try:
            await browser.close()
        except Exception:
            pass
    return rows


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

    async with async_playwright() as pw:

        async def worker(i: int, site: str):
            nonlocal done
            async with sem:
                try:
                    rows = await process_site(pw, cfg, gem, i, site, creatives, out_dir, log)
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
    fields = ["index", "site", "page_url", "creative", "size", "status", "method", "captured_at", "file", "note"]
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
