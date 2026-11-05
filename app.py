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

async def capture_tear_sheet(url
