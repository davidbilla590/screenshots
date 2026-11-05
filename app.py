"""
app.py - Streamlit front-end for the POP screenshot agent.
Run locally:  streamlit run app.py
"""
import os
import tempfile
from pathlib import Path

import streamlit as st

import agent

st.set_page_config(page_title="POP Screenshot Agent", page_icon="📸", layout="wide")


def get_secret(*names: str) -> str:
    """Read a key from environment variables or Streamlit secrets (first one that exists)."""
    for n in names:
        v = os.environ.get(n, "")
        if v:
            return v
    for n in names:
        try:
            v = st.secrets[n]
            if v:
                return str(v)
        except Exception:
            continue
    return ""


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("🔑 Keys")
    bl_key = st.text_input(
        "Browserless API token",
        value=get_secret("BROWSERLESS_API_KEY", "BROWSERLESS_TOKEN", "BROWSERLESS_API_TOKEN"),
        type="password",
    )
    gem_key = st.text_input("Gemini API key", value=get_secret("GEMINI_API_KEY", "GOOGLE_API_KEY"), type="password")
    gem_model = st.text_input("Gemini model", value="gemini-flash-latest")

    st.header("🌐 Browser")
    region = st.selectbox("Browserless region", ["production-sfo", "production-lon", "production-ams"], index=1)
    device = st.radio("Device", ["Desktop (1366x768)", "Mobile (390x844)"], horizontal=True)
    stealth = st.checkbox("Stealth mode", value=True)
    use_proxy = st.checkbox("Residential proxy (uses more Browserless units)", value=False)
    proxy_country = st.text_input("Proxy country (ISO code)", value="in", disabled=not use_proxy)
    timeout_s = st.slider("Session timeout per site (sec)", 60, 600, 180, step=30,
                          help="Free Browserless plans cap sessions at ~60 s - lower this if connections get rejected.")
    concurrency = st.slider("Parallel sites", 1, 5, 1, help="Must not exceed your Browserless plan's concurrency.")
    tol = st.slider("Ad-size tolerance (px)", 0, 10, 3)

# ------------------------------------------------------------------ header
st.title("📸 POP Screenshot Agent")
st.caption("Swap the live ad slot on publisher pages with your client's creative of the same size and capture the screenshot - in bulk.")

# ------------------------------------------------------------------ step 1: creatives
st.subheader("1 · Client creatives")
c1, c2 = st.columns(2)
with c1:
    drive_links = st.text_area(
        "Google Drive links - file or folder (one per line)",
        height=110,
        placeholder="https://drive.google.com/drive/folders/...\nhttps://drive.google.com/file/d/.../view",
        help="Sharing must be 'Anyone with the link can view'. Name files with the size, e.g. McD_300x250.png",
    )
    uploads = st.file_uploader("...or upload creatives", type=["png", "jpg", "jpeg", "gif", "webp", "html", "htm", "txt"],
                               accept_multiple_files=True)
with c2:
    html_tags = st.text_area(
        "...or paste HTML tags",
        height=240,
        placeholder="<!-- 300x250 -->\n<script src=\"...\"></script>\n===\n<!-- 728x90 -->\n<iframe ...></iframe>",
        help="Separate several tags with a line containing ===. Put the size in a comment (<!-- 300x250 -->). "
             "Images are the most reliable for POP; live ad tags may render blank outside the ad server.",
    )

if st.button("Load creatives"):
    with st.spinner("Loading creatives..."):
        loaded, warns = agent.load_creatives(drive_links, [(f.name, f.getvalue()) for f in (uploads or [])], html_tags)
    st.session_state["creatives"] = loaded
    st.session_state["creative_warnings"] = warns

creatives = st.session_state.get("creatives", [])
for w in st.session_state.get("creative_warnings", []):
    st.warning(w)
if creatives:
    st.success(f"{len(creatives)} creative(s) loaded")
    cols = st.columns(min(4, len(creatives)))
    for i, c in enumerate(creatives):
        with cols[i % len(cols)]:
            if c.kind == "image" and c.raw:
                st.image(c.raw, caption=f"{c.name} - {c.width}x{c.height}", width=min(c.width, 220))
            else:
                st.code(f"{c.name}\n{c.width}x{c.height} (HTML tag)")

# ------------------------------------------------------------------ step 2: sites
st.subheader("2 · Publisher URLs")
s1, s2 = st.columns([3, 2])
with s1:
    sites_text = st.text_area("One URL per line", height=150, placeholder="https://www.example.com\nhttps://www.another-site.com/news")
with s2:
    csv_up = st.file_uploader("...or upload a CSV (column 'url' or first column)", type=["csv", "txt"])
sites = agent.parse_sites(sites_text, csv_up.getvalue() if csv_up else None)
st.caption(f"{len(sites)} URL(s) detected")

# ------------------------------------------------------------------ step 3: guidelines
st.subheader("3 · Capture guidelines")
g1, g2 = st.columns(2)
with g1:
    find_latest = st.checkbox("URLs are homepages / sections - open the latest article", value=True)
    guidelines = st.text_area("Guidelines for choosing the page", height=100,
                              placeholder="e.g. Latest article only. Prefer lifestyle, food or tech stories. Avoid sports and politics.")
    max_tries = st.number_input("Max articles to try per site", 1, 8, 3)
with g2:
    brand_safety = st.checkbox("Run a brand-safety check (needs Gemini)", value=True)
    bs_rules = st.text_area("Brand-safety rules", height=100, placeholder="Leave blank to use the standard list "
                            "(adult, violence, tragedy, hate, drugs, gambling, politics...)")
    vision = st.checkbox("Use Gemini vision as a fallback to find ad slots", value=True)

# ------------------------------------------------------------------ step 4: output
st.subheader("4 · Output")
o1, o2, o3, o4 = st.columns(4)
with o1:
    full_page = st.checkbox("Full-page screenshot", value=False, help="Off = viewport screenshot with the ad centred.")
with o2:
    add_footer = st.checkbox("Add URL + timestamp strip", value=True)
with o3:
    footer_label = st.text_input("Footer label (optional)", placeholder="e.g. Creative simulated on live page")
with o4:
    tz = st.text_input("Timezone", value="Asia/Kolkata")

# ------------------------------------------------------------------ run
ready = bool(bl_key and creatives and sites)
if not bl_key:
    st.info("Add your Browserless token in the sidebar.")
elif not creatives:
    st.info("Load at least one creative (step 1).")
elif not sites:
    st.info("Add at least one publisher URL (step 2).")

if st.button("▶ Run agent", type="primary", disabled=not ready):
    mobile = device.startswith("Mobile")
    cfg = agent.JobConfig(
        browserless_token=bl_key.strip(),
        gemini_key=gem_key.strip(),
        gemini_model=gem_model.strip() or "gemini-flash-latest",
        region=region,
        stealth=stealth,
        residential_proxy=use_proxy,
        proxy_country=proxy_country.strip(),
        session_timeout_s=int(timeout_s),
        concurrency=int(concurrency),
        mobile=mobile,
        viewport_w=390 if mobile else 1366,
        viewport_h=844 if mobile else 768,
        find_latest_article=find_latest,
        guidelines=guidelines,
        brand_safety=brand_safety,
        brand_safety_rules=bs_rules,
        max_article_tries=int(max_tries),
        size_tolerance=int(tol),
        use_vision_fallback=vision,
        full_page=full_page,
        add_footer=add_footer,
        footer_label=footer_label.strip(),
        timezone=tz.strip() or "Asia/Kolkata",
    )
    out_dir = Path(tempfile.mkdtemp(prefix="pop_"))
    progress = st.progress(0.0, text="Starting...")
    log_box = st.empty()
    lines: list = []

    def log(msg: str):
        lines.append(msg)
        log_box.code("\n".join(lines[-20:]), language=None)

    def on_progress(done: int, total: int):
        progress.progress(done / total, text=f"{done}/{total} sites finished")

    try:
        results = agent.run_job_sync(cfg, sites, creatives, out_dir, log, on_progress)
        zip_bytes, csv_bytes = agent.build_outputs(results, out_dir)
        st.session_state["results"] = results
        st.session_state["zip"] = zip_bytes
        st.session_state["csv"] = csv_bytes
        progress.progress(1.0, text="Done")
    except Exception as e:
        st.error(f"Run failed: {agent.short(e, bl_key)}")

# ------------------------------------------------------------------ results
results = st.session_state.get("results")
if results:
    st.subheader("Results")
    ok = [r for r in results if r.status == "OK"]
    m1, m2, m3 = st.columns(3)
    m1.metric("Screenshots", len(ok))
    m2.metric("No matching slot", sum(r.status == "NO_SLOT" for r in results))
    m3.metric("Skipped / errors", sum(r.status in ("ERROR", "SKIPPED_BRAND_SAFETY") for r in results))

    st.dataframe(
        [
            {
                "site": r.site, "page": r.page_url, "creative": r.creative, "size": r.size,
                "status": r.status, "method": r.method, "note": r.note,
            }
            for r in results
        ],
        use_container_width=True,
    )
    d1, d2 = st.columns(2)
    d1.download_button("⬇ Download screenshots (ZIP)", st.session_state["zip"], file_name="pop_screenshots.zip",
                       mime="application/zip")
    d2.download_button("⬇ Download report (CSV)", st.session_state["csv"], file_name="pop_report.csv", mime="text/csv")

    gcols = st.columns(2)
    for i, r in enumerate(ok):
        with gcols[i % 2]:
            if r.file and Path(r.file).exists():
                st.image(r.file, caption=f"{r.site} - {r.size}")
