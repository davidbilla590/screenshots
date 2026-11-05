import streamlit as st
import requests
import io
import base64
from PIL import Image
from google import genai

st.set_page_config(page_title="POP Tear Sheet Generator", layout="wide")
st.title("Ad Replacement & POP Generator")

with st.sidebar:
    st.header("API Configuration")
    browserless_key = st.text_input("Browserless API Key", type="password")
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
    Evaluate if this article content violates brand safety guidelines:
    Guidelines: {guidelines}
    Article Text: {headline_and_text}

    Reply ONLY in this format:
    SAFE: [YES or NO]
    REASON: [Brief 1-sentence reason]
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
        Inspect this full screenshot of a webpage:
        1. Confirm if a {ad_size_str} ad creative is cleanly visible and properly positioned.
        2. Ensure it does not look clipped, broken, or overlapping article text.
        Return: 'VERIFIED' or 'ISSUE DETECTED' along with a brief explanation.
        """
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt, image]
        )
        return response.text
    except Exception as e:
        return f"Vision QA skipped: {e}"

def run_browserless_capture(url, w, h, creative_html, token):
    # Encode creative payload to avoid syntax errors inside the JavaScript string
    b64_creative = base64.b64encode(creative_html.encode('utf-8')).decode('utf-8')

    # Evaluates inside Browserless /scrape endpoint
    scrape_script = f"""
    new Promise(resolve => {{
        const width = {w};
        const height = {h};
        const adHtml = decodeURIComponent(escape(atob('{b64_creative}')));

        // Dismiss cookie banners if present
        for (const sel of ["button:has-text('Accept')", "button:has-text('Agree')", "#onetrust-accept-btn-handler"]) {{
            const btn = document.querySelector(sel);
            if (btn) btn.click();
        }}

        // Locate matching ad container
        const elements = Array.from(document.querySelectorAll('div, iframe, ins, aside, [id*="ad"], [class*="ad"]'));
        let target = null;
        for (const el of elements) {{
            const rect = el.getBoundingClientRect();
            if (Math.abs(rect.width - width) <= 25 && Math.abs(rect.height - height) <= 25) {{
                target = el;
                break;
            }}
        }}

        let injected = false;
        if (target) {{
            const wrapper = document.createElement('div');
            wrapper.style.width = width + 'px';
            wrapper.style.height = height + 'px';
            wrapper.style.overflow = 'hidden';
            wrapper.style.display = 'flex';
            wrapper.style.alignItems = 'center';
            wrapper.style.justifyContent = 'center';
            wrapper.innerHTML = adHtml;

            target.innerHTML = '';
            target.appendChild(wrapper);
            target.scrollIntoView({{ behavior: 'instant', block: 'center' }});
            injected = true;
        }}

        const pageTitle = document.title || '';
        const bodyText = (document.querySelector('article') || document.body || {{}}).innerText || '';
        const summary = (pageTitle + ' ' + bodyText).slice(0, 1000);

        setTimeout(() => {{
            resolve({{ injected, summary }});
        }}, 2000);
    }})
    """

    endpoint = f"https://production-sfo.browserless.io/scrape?token={token}&stealth=true"

    payload = {
        "url": url,
        "gotoOptions": {
            "waitUntil": "domcontentloaded",
            "timeout": 45000
        },
        "viewport": {
            "width": 1440,
            "height": 900
        },
        "waitForTimeout": 3000,
        "screenshot": {
            "type": "png",
            "fullPage": False
        },
        "elements": [
            {
                "selector": "body",
                "timeout": 5000
            }
        ],
        "eval": scrape_script
    }

    try:
        response = requests.post(
            endpoint,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=60
        )

        if response.status_code != 200:
            return None, "", f"Browserless error ({response.status_code}): {response.text}"

        res_data = response.json()
        
        # Extract screenshot data
        screenshot_b64 = res_data.get("screenshot")
        if not screenshot_b64:
            return None, "", "No screenshot was returned by the browser session."

        raw_bytes = base64.b64decode(screenshot_b64)

        eval_result = res_data.get("eval", {})
        page_summary = eval_result.get("summary", "")
        
        return raw_bytes, page_summary, None

    except requests.exceptions.RequestException as e:
        return None, "", f"Network connection error: {str(e)}"

if st.button("Generate Proof of Performance", type="primary"):
    if not browserless_key or not gemini_key:
        st.error("Please provide both Browserless and Gemini API keys in the sidebar.")
    elif not page_url or not creative_input:
        st.error("Please provide both the publisher URL and the creative tag/URL.")
    else:
        with st.spinner("Injecting ad and capturing tear sheet..."):
            client = genai.Client(api_key=gemini_key)

            if input_type == "Image URL":
                formatted_creative = f'<img src="{creative_input}" style="width:{target_w}px;height:{target_h}px;object-fit:contain;display:block;margin:auto;" />'
            else:
                formatted_creative = f'<iframe srcdoc="{creative_input}" width="{target_w}" height="{target_h}" frameborder="0" scrolling="no" style="border:none;overflow:hidden;"></iframe>'

            screenshot_bytes, page_text, err = run_browserless_capture(
                page_url, target_w, target_h, formatted_creative, browserless_key
            )

            if err:
                st.error(f"Capture failed: {err}")
            else:
                safety_result = evaluate_brand_safety(client, page_text, brand_guidelines)
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
