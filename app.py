import streamlit as st
import asyncio
import io
from PIL import Image
from playwright.async_api import async_playwright
from google import genai

st.set_page_config(page_title="POP Tear Sheet Generator", layout="wide")
st.title("Ad Replacement & POP Generator")

with st.sidebar:
    st.header("API Configuration")
    browserless_key = st.text_input("Browserless API Key", type="password")
    browserless_region = st.selectbox(
        "Browserless Fleet",
        ["production-sfo.browserless.io", "production-lon.browserless.io", "chrome.browserless.io"]
    )
    gemini_key = st.text_input("Gemini API Key", type="password")

col1, col2 = st.columns(2)
with col1:
    page_url = st.text_input("Target Publisher URL", placeholder="https://example.com/article-123")
    ad_size = st.selectbox("Ad Slot Dimension", ["300x250", "728x90", "300x600", "970x250", "320x50"])
    target_w, target_h = map(int, ad_size.split("x"))

with col2:
    input_type = st.radio("Creative Format", ["Image URL", "HTML / 3rd-Party Script Tag"])
    if input_type == "Image URL":
        creative_input = st.text_input("Client Image URL", placeholder="https://domain.com/banner.jpg")
    else:
        creative_input = st.text_area("Client Ad Tag (HTML/JS)", placeholder="<iframe>...</iframe>")

brand_guidelines = st.text_input("Brand Safety Rules", value="No war, violence, crime, plane crashes, or controversial politics.")

def evaluate_brand_safety(gemini_client, headline_and_text, guidelines):
    prompt = f"""
    Evaluate if this article violates brand safety:
    Guidelines: {guidelines}
    Article: {headline_and_text}

    Reply in format:
    SAFE: [YES/NO]
    REASON: [Brief explanation]
    """
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"Safety check skipped: {e}"

def perform_vision_qa(gemini_client, image_bytes, ad_size_str):
    try:
        image = Image.open(io.BytesIO(image_bytes))
        prompt = f"""
        Inspect this webpage screenshot:
        1. Is the {ad_size_str} ad creative cleanly visible and properly positioned?
        2. Does it avoid overlapping text or looking broken?
        Return: 'VERIFIED' or 'ISSUE DETECTED' with brief commentary.
        """
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt, image]
        )
        return response.text
    except Exception as e:
        return f"Vision QA skipped: {e}"

async def capture_tear_sheet(url, w, h, creative_html, host, token):
    # Pass timeout and stealth parameters to the remote CDP endpoint
    endpoint_url = f"wss://{host}?token={token}&stealth=true&timeout=60000&--disable-gpu=true"

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(endpoint_url)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout(35000)

        try:
            # Block heavy tracking media/fonts that trigger remote browser memory crashes
            await page.route("**/*", lambda route: (
                route.abort() if route.request.resource_type in ["media", "font"] else route.continue_()
            ))

            # Navigate using domcontentloaded to avoid waiting on endless trackers
            await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            await asyncio.sleep(3)

            # Dismiss cookie banners if present
            for selector in ["button:has-text('Accept')", "button:has-text('Agree')", "#onetrust-accept-btn-handler"]:
                try:
                    if await page.is_visible(selector, timeout=1000):
                        await page.click(selector)
                        break
                except Exception:
                    pass

            # Safe extraction of text
            try:
                page_summary = await page.evaluate("() => document.title + ' ' + (document.body ? document.body.innerText.slice(0, 1000) : '')")
            except Exception:
                page_summary = "Article summary unavailable."

            # Inject the creative into matching slot
            injection_success = await page.evaluate("""
                ({ width, height, html }) => {
                    const elements = Array.from(document.querySelectorAll('div, iframe, ins, aside'));
                    let target = null;
                    for (const el of elements) {
                        const r = el.getBoundingClientRect();
                        if (Math.abs(r.width - width) <= 15 && Math.abs(r.height - height) <= 15) {
                            target = el;
                            break;
                        }
                    }
                    if (!target) return false;

                    const container = document.createElement('div');
                    container.style.width = width + 'px';
                    container.style.height = height + 'px';
                    container.style.overflow = 'hidden';
                    container.style.display = 'flex';
                    container.style.alignItems = 'center';
                    container.style.justifyContent = 'center';
                    container.innerHTML = html;

                    target.innerHTML = '';
                    target.appendChild(container);
                    target.scrollIntoView({ behavior: 'instant', block: 'center' });
                    return true;
                }
            """, {"width": w, "height": h, "html": creative_html})

            if not injection_success:
                await browser.close()
                return None, page_summary, f"No ad container near {w}x{h} was detected on this page."

            await asyncio.sleep(2)
            screenshot_bytes = await page.screenshot(full_page=False)
            await browser.close()
            return screenshot_bytes, page_summary, None

        except Exception as e:
            try:
                await browser.close()
            except Exception:
                pass
            return None, "", str(e)

if st.button("Generate Proof of Performance", type="primary"):
    if not browserless_key or not gemini_key:
        st.error("Please provide both Browserless and Gemini API keys in the sidebar.")
    elif not page_url or not creative_input:
        st.error("Please provide both the publisher URL and the creative tag/URL.")
    else:
        with st.spinner("Connecting to cloud browser and generating screenshot..."):
            client = genai.Client(api_key=gemini_key)

            if input_type == "Image URL":
                formatted_creative = f'<img src="{creative_input}" style="width:{target_w}px;height:{target_h}px;object-fit:contain;display:block;margin:auto;" />'
            else:
                formatted_creative = f'<iframe srcdoc="{creative_input}" width="{target_w}" height="{target_h}" frameborder="0" scrolling="no" style="border:none;overflow:hidden;"></iframe>'

            screenshot_bytes, page_summary, err = asyncio.run(
                capture_tear_sheet(page_url, target_w, target_h, formatted_creative, browserless_region, browserless_key)
            )

            if err:
                st.error(f"Capture failed: {err}")
            else:
                safety_result = evaluate_brand_safety(client, page_summary, brand_guidelines)
                vision_result = perform_vision_qa(client, screenshot_bytes, ad_size)

                st.subheader("Result & Quality Check")
                r1, r2 = st.columns(2)
                with r1:
                    st.info(f"**Brand Safety Check:**\n{safety_result}")
                with r2:
                    st.info(f"**Visual QA:**\n{vision_result}")

                st.image(screenshot_bytes, caption=f"Injected {ad_size} POP Screenshot", use_container_width=True)
                st.download_button(
                    label="Download POP Screenshot",
                    data=screenshot_bytes,
                    file_name="proof_of_performance.png",
                    mime="image/png"
                )
