import streamlit as st
import asyncio
import io
import base64
from PIL import Image
from playwright.async_api import async_playwright
from google import genai

st.set_page_config(page_title="POP Tear Sheet Generator", layout="wide")
st.title("Ad Replacement & POP Generator")

# Sidebar: Credentials
with st.sidebar:
    st.header("API Configuration")
    browserless_key = st.text_input("Browserless API Key", type="password")
    gemini_key = st.text_input("Gemini API Key", type="password")

# Form Inputs
col1, col2 = st.columns(2)
with col1:
    page_url = st.text_input("Target Publisher URL", placeholder="https://example.com/article-123")
    ad_size = st.selectbox("Ad Slot Dimension", ["300x250", "728x90", "300x600", "970x250", "320x50"])
    target_w, target_h = map(int, ad_size.split("x"))

with col2:
    input_type = st.radio("Creative Format", ["Image URL", "HTML / 3rd-Party Script Tag"])
    if input_type == "Image URL":
        creative_input = st.text_input("Client Image URL (e.g. McDonald's 300x250)", placeholder="https://domain.com/banner.jpg")
    else:
        creative_input = st.text_area("Client Ad Tag (HTML/JS)", placeholder="<iframe>...</iframe> or <script>...</script>")

brand_guidelines = st.text_input("Brand Safety Rules", value="No war, violence, crime, plane crashes, or controversial politics.")

# Helpers
def evaluate_brand_safety(gemini_client, headline_and_text, guidelines):
    prompt = f"""
    Evaluate if this article content violates brand safety guidelines.
    Guidelines: {guidelines}
    Article Excerpt: {headline_and_text}

    Reply ONLY in this format:
    SAFE: [YES or NO]
    REASON: [Brief 1-sentence reason]
    """
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text

def perform_vision_qa(gemini_client, image_bytes, ad_size_str):
    image = Image.open(io.BytesIO(image_bytes))
    prompt = f"""
    Inspect this full screenshot of a webpage. 
    1. Confirm if a {ad_size_str} ad creative is cleanly visible and aligned in the layout.
    2. Ensure it does not look clipped, broken, or overlapping text.
    Return: 'VERIFIED' or 'ISSUE DETECTED' along with a brief explanation.
    """
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt, image]
    )
    return response.text

async def capture_tear_sheet(url, w, h, creative_html, token):
    # Connect to Browserless cloud instance via WebSocket
    endpoint_url = f"wss://chrome.browserless.io?token={token}"
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(endpoint_url)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        # Navigate
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3) # Wait for client-side ad scripts to fire

        # Extract basic page text for safety analysis
        page_summary = await page.evaluate("() => document.title + ' ' + (document.querySelector('article, main, body')?.innerText.slice(0, 1500) || '')")

        # DOM Replacement: Search for matching dimensions and replace
        injection_success = await page.evaluate("""
            ({ width, height, html }) => {
                const candidates = Array.from(document.querySelectorAll('div, iframe, ins'));
                let target = null;
                for (const el of candidates) {
                    const rect = el.getBoundingClientRect();
                    if (Math.abs(rect.width - width) <= 6 && Math.abs(rect.height - height) <= 6) {
                        target = el;
                        break;
                    }
                }
                if (!target) return false;

                const wrapper = document.createElement('div');
                wrapper.style.width = width + 'px';
                wrapper.style.height = height + 'px';
                wrapper.style.overflow = 'hidden';
                wrapper.style.display = 'flex';
                wrapper.style.alignItems = 'center';
                wrapper.style.justifyContent = 'center';
                wrapper.innerHTML = html;

                target.innerHTML = '';
                target.appendChild(wrapper);
                target.scrollIntoView({ behavior: 'instant', block: 'center' });
                return true;
            }
        """, {"width": w, "height": h, "html": creative_html})

        if not injection_success:
            await browser.close()
            return None, page_summary, "No ad slot found matching dimensions."

        await asyncio.sleep(2) # Allow image/script to render
        screenshot_bytes = await page.screenshot(full_page=False)
        await browser.close()
        return screenshot_bytes, page_summary, None

# Trigger Generation
if st.button("Generate Proof of Performance", type="primary"):
    if not browserless_key or not gemini_key:
        st.error("Please provide both Browserless and Gemini API keys in the sidebar.")
    elif not page_url or not creative_input:
        st.error("Please provide both the publisher URL and the creative tag/URL.")
    else:
        with st.spinner("Processing tear sheet in cloud browser..."):
            client = genai.Client(api_key=gemini_key)
            
            # Format creative payload
            if input_type == "Image URL":
                formatted_creative = f'<img src="{creative_input}" style="width:{target_w}px;height:{target_h}px;object-fit:contain;display:block;margin:auto;" />'
            else:
                formatted_creative = f'<iframe srcdoc="{creative_input}" width="{target_w}" height="{target_h}" frameborder="0" scrolling="no" style="border:none;overflow:hidden;"></iframe>'

            # Run browser task
            screenshot_bytes, page_summary, err = asyncio.run(
                capture_tear_sheet(page_url, target_w, target_h, formatted_creative, browserless_key)
            )

            if err:
                st.error(f"Capture failed: {err}")
            else:
                # 1. Brand Safety Evaluation
                safety_result = evaluate_brand_safety(client, page_summary, brand_guidelines)
                
                # 2. Vision QA Inspection
                vision_result = perform_vision_qa(client, screenshot_bytes, ad_size)

                st.subheader("Result & Quality Check")
                res_col1, res_col2 = st.columns(2)
                with res_col1:
                    st.info(f"**Brand Safety Check:**\n{safety_result}")
                with res_col2:
                    st.info(f"**Visual QA:**\n{vision_result}")

                st.image(screenshot_bytes, caption=f"Injected {ad_size} POP Screenshot", use_container_width=True)
                
                # Download Button
                st.download_button(
                    label="Download POP Screenshot",
                    data=screenshot_bytes,
                    file_name="proof_of_performance.png",
                    mime="image/png"
                )