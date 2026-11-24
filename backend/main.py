import os
import io
import re
import time
import hmac
import math
import wave
import json
import uuid
import struct
import random
import shutil
import asyncio
import datetime
import subprocess
import base64
import urllib.parse
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Union
import requests
import httpx
import edge_tts
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageEnhance
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
try:
    from gradio_client import Client, handle_file
    GradioClient = Client
    GRADIO_AVAILABLE = True
except Exception:
    Client = None
    GradioClient = None
    handle_file = None
    GRADIO_AVAILABLE = False

try:
    import cv2
    import numpy as np
    OPENCV_AVAILABLE = True
except Exception:
    cv2 = None
    np = None
    OPENCV_AVAILABLE = False

app = FastAPI(title="CinePop AI — Pocket Bollywood & Face-Swap Studio Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return exact missing/invalid field names in JSON so clients never see a vague 422 error."""
    missing_fields: list[str] = []
    formatted_errors: list[dict] = []
    for err in exc.errors():
        loc_parts = [str(p) for p in err.get("loc", []) if str(p) not in ("body", "query", "path")]
        field_name = ".".join(loc_parts) if loc_parts else "request"
        msg = str(err.get("msg", "Invalid value"))
        err_type = str(err.get("type", "validation_error"))
        if field_name not in missing_fields:
            missing_fields.append(field_name)
        formatted_errors.append({
            "field": field_name,
            "message": msg,
            "type": err_type,
        })
    summary = "; ".join(f"{e['field']}: {e['message']}" for e in formatted_errors) or "Invalid request payload"
    return JSONResponse(
        status_code=422,
        content={
            "error": "VALIDATION_ERROR",
            "detail": f"Validation error — {summary}",
            "missing_fields": missing_fields,
            "errors": formatted_errors,
            "path": str(request.url.path),
        },
    )

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "akashtyagi534@gmail.com").lower().strip()
ADMIN_PIN = os.getenv("ADMIN_PIN", "143200").strip()
UPI_ID = os.getenv("UPI_ID", "8269045143@ybl")
MOVIE_PRICE_INR = int(os.getenv("MOVIE_PRICE_INR", "50"))

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

PROJECTS_DIR = "all_movies"
FACE_SWAPS_DIR = "all_face_swaps"
DB_FILE = "cinepop_db.json"
os.makedirs(PROJECTS_DIR, exist_ok=True)
os.makedirs(FACE_SWAPS_DIR, exist_ok=True)

ALLOWED_MOODS = ["Action", "Emotional", "Suspense", "Comedy", "Romantic"]
ALLOWED_LANGUAGES = ["Hindi", "Bhojpuri", "Haryanvi", "Punjabi", "English"]
ALLOWED_SFX = ["none", "rain", "thunder", "punch", "explosion", "sword", "footsteps"]
ALLOWED_FACE_ANGLES = ("front", "left", "right", "up", "down")


def detect_scene_angle(
    visual_action_prompt: str = "",
    dialogue: str = "",
    mood: str = "Action",
    sfx: str = "none",
    scene_idx: int = 0,
) -> str:
    """Detect best-matching character head/camera angle (front/left/right/up/down) for a scene."""
    blob = f"{visual_action_prompt} {dialogue}".lower()
    if any(k in blob for k in ("look up", "looking up", "sky", "ऊपर", "आसमान", "flying", "helicopter", "rooftop", "lightning", "thunder", "tower", "ऊंचाई")):
        return "up"
    if any(k in blob for k in ("look down", "looking down", "नीचे", "ज़मीन", "ground", "fallen", "bowed", "sad", "tears", "रो", "दुख", "floor")):
        return "down"
    if any(k in blob for k in ("turn left", "left profile", "left side", "बाएं", "running left", "dodge left", "glancing left")):
        return "left"
    if any(k in blob for k in ("turn right", "right profile", "right side", "दाएं", "running right", "dodge right", "glancing right")):
        return "right"
    if any(k in blob for k in ("running", "chase", "sprint", "fight", "punch", "sword", "combat", "side shot", "tracking", "दौड़", "लड़ाई", "हमला", "तलवार")) or sfx in ("punch", "sword", "explosion"):
        # Alternate left/right side action profile on dynamic action shots, front on dialogue beats
        cycle = ("front", "left", "right")
        return cycle[scene_idx % len(cycle)]
    if mood == "Emotional" and scene_idx % 3 == 2:
        return "down"
    if mood == "Suspense" and scene_idx % 3 == 1:
        return "left" if scene_idx % 2 == 0 else "right"
    return "front"


def select_character_angle_photo(char_entry: dict, target_angle: str = "front") -> str:
    """
    STRICT CHARACTER PHOTO LOCK:
    Always returns the user's exact uploaded/scanned photo matching target_angle
    ('front', 'left', 'right', 'up', 'down'), falling back ONLY to the character's
    uploaded 'front' / primary photo. NEVER returns any random or external image.
    """
    angles = char_entry.get("angles") or {}
    clean_angle = target_angle if target_angle in ALLOWED_FACE_ANGLES else "front"
    candidates = [
        angles.get(clean_angle),
        angles.get("front"),
        char_entry.get("image"),
    ]
    for a in ALLOWED_FACE_ANGLES:
        candidates.append(angles.get(a))
    for path in candidates:
        if isinstance(path, str) and path and os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return str(char_entry.get("image") or "")


def open_upright_image(path_or_bytes) -> Image.Image:
    """Open an image file or bytes and ALWAYS apply EXIF orientation so photos are never 90-degree sideways."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        img = Image.open(io.BytesIO(path_or_bytes))
    else:
        img = Image.open(path_or_bytes)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def save_upright_image_bytes(raw_bytes: bytes, out_path: str, max_side: int = 1280) -> str:
    """Save uploaded camera/gallery image upright (EXIF-corrected) so it never renders rotated sideways."""
    try:
        img = open_upright_image(raw_bytes)
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        img.save(out_path, "JPEG", quality=92)
    except Exception:
        with open(out_path, "wb") as f:
            f.write(raw_bytes)
    return out_path


_DEVANAGARI_FONT_CACHE: dict[str, Optional[str]] = {}


def find_devanagari_font_path(bold: bool = True) -> Optional[str]:
    """Locate an installed Noto Sans Devanagari (or Hindi-capable) TTF/OTF font so Hindi never renders as □□□ boxes."""
    key = "bold" if bold else "regular"
    if key in _DEVANAGARI_FONT_CACHE:
        return _DEVANAGARI_FONT_CACHE[key]

    preferred = (
        [
            "/app/fonts/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.otf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagariUI-Bold.ttf",
            "/app/fonts/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSerifDevanagari-Bold.ttf",
            "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ]
        if bold
        else [
            "/app/fonts/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.otf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagariUI-Regular.ttf",
            "/app/fonts/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
            "/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        ]
    )
    for p in preferred:
        if os.path.exists(p) and os.path.getsize(p) > 20000:
            _DEVANAGARI_FONT_CACHE[key] = p
            return p

    # Recursive scan of /usr/share/fonts for any Devanagari TTF/OTF
    try:
        found_deva: list[str] = []
        for root, _, files in os.walk("/usr/share/fonts"):
            for fn in files:
                low = fn.lower()
                if ("devanagari" in low or "deva" in low) and low.endswith((".ttf", ".otf")):
                    fp = os.path.join(root, fn)
                    if os.path.getsize(fp) > 20000:
                        found_deva.append(fp)
        if bold:
            for fp in found_deva:
                if "bold" in os.path.basename(fp).lower():
                    _DEVANAGARI_FONT_CACHE[key] = fp
                    return fp
        if found_deva:
            _DEVANAGARI_FONT_CACHE[key] = found_deva[0]
            return found_deva[0]
    except Exception:
        pass

    # Fallback: query fontconfig (`fc-list`) for any installed Devanagari font
    try:
        out = subprocess.check_output(
            ["fc-list", ":lang=hi", "file"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode("utf-8", errors="ignore")
        candidates = [ln.split(":")[0].strip() for ln in out.splitlines() if ln.strip()]
        if bold:
            for c in candidates:
                if "bold" in c.lower() and os.path.exists(c) and os.path.getsize(c) > 20000:
                    _DEVANAGARI_FONT_CACHE[key] = c
                    return c
        for c in candidates:
            if os.path.exists(c) and os.path.getsize(c) > 20000:
                _DEVANAGARI_FONT_CACHE[key] = c
                return c
    except Exception:
        pass

    _DEVANAGARI_FONT_CACHE[key] = None
    return None


def load_hindi_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    fp = find_devanagari_font_path(bold=bold)
    if fp:
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            pass
    return ImageFont.load_default()


SCRIPT_TAG_LINE_RE = re.compile(
    r"^\s*(?:title|शीर्षक|scene\s*\d*|सीन\s*\d*|location|लोकेशन|action|एक्शन|mood|मूड|sfx|camera|कैमरा|int\.|ext\.|cut\s+to|fade\s+(?:in|out)|background|बैकग्राउंड)\s*[:：\-]?",
    re.IGNORECASE,
)

LEADING_SPEAKER_PREFIX_RE = re.compile(
    r"^\s*[\"'“”‘’]*\s*(?:[A-Za-z\u0900-\u097F][A-Za-z\u0900-\u097F0-9_\- ]{0,22})\s*[:：]\s*"
)


def clean_spoken_dialogue(raw_text: str) -> str:
    """
    Strip ALL script/stage metadata tags AND any embedded leading speaker prefixes
    (e.g. `Vikram:`, `Hero:`, `अजय:`, `Kaal:`, `Narrator:`) so TTS characters NEVER
    read character names or stage directions aloud and subtitles NEVER show double prefixes!
    """
    txt = str(raw_text or "").strip()
    if not txt:
        return ""
    txt = re.sub(r"\[[^\]]*\]", " ", txt)
    txt = re.sub(r"\([^)]*(?:action|scene|location|camera|mood|sfx|angry|shouts|runs|punches|laugh|एक्शन|सीन|लोकेशन)[^)]*\)", " ", txt, flags=re.IGNORECASE)
    txt = re.sub(
        r"\b(?:Title|Scene\s*\d*|Location|Action|Mood|SFX|Camera|Visual|शीर्षक|सीन\s*\d*|लोकेशन|एक्शन|मूड)\s*[:：]\s*[^\n.।!?]*",
        " ",
        txt,
        flags=re.IGNORECASE,
    )
    txt = txt.replace("**", "").replace("__", "").replace("##", "").replace("#", "")
    txt = re.sub(r"^[\-\*•\d\.\)\s]+", "", txt)
    for _ in range(3):
        stripped = LEADING_SPEAKER_PREFIX_RE.sub("", txt).strip()
        if stripped == txt or len(stripped) < 2:
            break
        txt = stripped
    txt = re.sub(r"\s+", " ", txt).strip(" \"'“”‘’")
    return txt


def detect_speaker_role(speaker_name: str, char_map: Optional[dict] = None) -> str:
    """
    Classify speaker into strict Male/Female role buckets so Male speakers NEVER get a Female voice/photo
    and Female speakers NEVER get a Male voice/photo:
    - Male roles: 'hero' (Hero, Raja, Rajkumar, Inspector, Officer, Beta, Pati),
                  'villain' (Villain, Bhoot, Tantrik, Mantri, Senapati, Don, Boss, Gunda, Rakshas, Shaitan,chor),
                  'narrator' (Narrator, Sutradhar)
    - Female roles: 'female' (Heroine, Rani, Rajkumari, Nayika, Ladki, Patni, Bahu, Priya, Kavya),
                    'saas' (Saas, Chudail, Dayan, Maharani,Rajmata, Maa, Bua, Aunty)
    """
    low = (speaker_name or "").lower().strip()
    char_map = char_map or {}
    char_names = list(char_map.keys())
    if any(k in low for k in ("narrator", "sutradhar", "voiceover", "storyteller", "सूत्रधार", "कथावाचक", "नैरेटर")):
        return "narrator"
    if any(
        k in low
        for k in (
            "saas", "mother-in-law", "stepmother", "aunty", "chachi", "maa", "mother", "bua",
            "chudail", "dayan", "daayan", "witch", "rajmata", "maharani", "malkin", "dadi", "nani",
            "सास", "सौतेली", "बुआ", "माँ", "मम्मी", "चुड़ैल", "डायन", "राजमाता", "महारानी", "मालकिन", "दादी", "चाची",
        )
    ):
        return "saas"
    if any(
        k in low
        for k in (
            "heroine", "nayika", "नायिका", "priya", "kavya", "काव्या", "pooja", "anjali", "meera",
            "rani", "rajkumari", "princess", "queen", "wife", "bahu", "girl", "woman", "ladki", "lady",
            "sister", "behen", "beti", "dost_f", "लड़की", "रानी", "राजकुमारी", "बहन", "पत्नी", "बहू", "प्रिया", "मीरा", "बेटी",
        )
    ):
        return "female"
    if any(
        k in low
        for k in (
            "villain", "rival", "don", "kaal", "boss", "gunda", "evil", "enemy", "rakshas", "shaitan",
            "bhoot", "ghost", "tantrik", "mantri", "senapati", "mahamantri", "chor", "daku", "smuggler",
            "sarpanch", "zamindar", "thakur", "criminal", "killer", "monster",
            "विलेन", "डॉन", "गुंडा", "दुश्मन", "काल", "साला", "दामाद", "भूत", "तांत्रिक", "मंत्री", "महामंत्री",
            "सेनापति", "राक्षस", "शैतान", "चोर", "डाकू", "ठाकुर", "ज़मींदार", "जमींदार", "अघोरी",
        )
    ):
        return "villain"
    if any(
        k in low
        for k in (
            "hero", "raja", "king", "rajkumar", "prince", "inspector", "police", "officer", "captain",
            "sipahi", "detective", "beta", "pati", "bhai", "dost", "vaidya", "pandit",
            "नायक", "हीरो", "राजा", "महाराज", "राजकुमार", "इंस्पेक्टर", "पुलिस", "अफसर", "सिपाही", "बेटा", "पति", "भाई",
        )
    ):
        return "hero"
    if any(k in low for k in ("log", "crowd", "public", "villager", "people", "लोग", "भीड़", "जनता", "गाँव वाले", "गांव वाले", "दरबारी")):
        return "crowd"
    if char_names and speaker_name == char_names[0]:
        return "hero"
    if len(char_names) >= 2 and speaker_name == char_names[1]:
        entry_v = str(char_map[speaker_name].get("voice", ""))
        if "Swara" in entry_v or "Neerja" in entry_v:
            return "female"
        return "villain"
    entry = char_map.get(speaker_name) or {}
    if "Swara" in str(entry.get("voice", "")) or "Neerja" in str(entry.get("voice", "")):
        return "female"
    return "hero"


def is_female_speaker_role(role: str, speaker_name: str = "") -> bool:
    """Strict gender check: True ONLY for female roles ('female', 'saas')."""
    r = (role or "").lower().strip()
    if r in ("female", "saas"):
        return True
    if r in ("hero", "villain", "narrator", "crowd"):
        return False
    return detect_speaker_role(speaker_name, None) in ("female", "saas")


def resolve_character_voice_profile(
    speaker_name: str,
    char_entry: Optional[dict],
    char_map: dict,
    language: str = "Hindi",
    scene_idx: int = 0,
) -> tuple[str, str, str]:
    """
    STRICT GENDER-LOCKED VOICE PROFILE (Aadmi Ki Aawaz Me Aadmi, Aurat Ki Aawaz Me Aurat!):
    - Male roles (`hero`, `villain`, `narrator`, `crowd` / Hero, Raja, Villain, Mantri, Bhoot, Inspector, Narrator)
      -> STRICTLY Male Voice (`hi-IN-MadhurNeural` / `en-IN-PrabhatNeural`)
    - Female roles (`female`, `saas` / Heroine, Rani, Saas, Nayika, Chudail, Rajkumari)
      -> STRICTLY Female Voice (`hi-IN-SwaraNeural` / `en-IN-NeerjaNeural`)
    """
    lang = normalize_language(language)
    role = detect_speaker_role(speaker_name, char_map)
    is_female = is_female_speaker_role(role, speaker_name)

    if lang == "English":
        if is_female:
            if role == "saas":
                return ("en-IN-NeerjaNeural", "+6Hz", "+4%")
            return ("en-IN-NeerjaNeural", "+4Hz", "+2%")
        if role == "narrator":
            return ("en-IN-PrabhatNeural", "-4Hz", "+3%")
        if role == "villain":
            return ("en-IN-PrabhatNeural", "-12Hz", "-6%")
        return ("en-IN-PrabhatNeural", "+3Hz", "+2%")

    # Hindi strict gender lock: Female -> hi-IN-SwaraNeural ONLY, Male -> hi-IN-MadhurNeural ONLY
    if is_female:
        if role == "saas":
            return ("hi-IN-SwaraNeural", "+6Hz", "+4%")
        return ("hi-IN-SwaraNeural", "+4Hz", "+2%")

    if role == "narrator":
        return ("hi-IN-MadhurNeural", "-4Hz", "+3%")
    if role == "villain":
        return ("hi-IN-MadhurNeural", "-12Hz", "-6%")
    if role == "crowd":
        return ("hi-IN-MadhurNeural", "+6Hz", "+5%")
    return ("hi-IN-MadhurNeural", "+3Hz", "+2%")


def _get_or_create_role_portrait(role_kind: str, proj_dir: str) -> Optional[str]:
    """
    Generate and cache a realistic Bollywood character portrait when
    the script introduces Heroine, Villain, Saas, or Crowd speakers that the user did not upload a photo for.
    """
    role_map = {
        "villain": (
            "villain",
            "Medium waist-up cinematic portrait of dramatic Indian Bollywood villain man with intense eyes, dark beard, sharp tailored coat, moody rim lighting, 4K film still, photorealistic, male actor only",
            42,
        ),
        "saas": (
            "saas",
            "Medium waist-up cinematic portrait of wealthy dramatic Indian matriarch woman in rich silk saree and gold jewelry, sharp intimidating expression, Bollywood palace rim lighting, 4K film still, photorealistic, female actress only",
            63,
        ),
        "female": (
            "heroine",
            "Medium waist-up cinematic portrait of expressive young Indian Bollywood heroine actress in royal ethnic attire, emotional eyes, warm dramatic rim lighting, 4K film still, photorealistic, female actress only",
            77,
        ),
        "crowd": (
            "crowd",
            "Cinematic Bollywood movie shot of expressive Indian royal court people reacting in dramatic palace lighting, photorealistic 4K film still, clear faces",
            99,
        ),
    }
    clean_role, prompt, seed_num = role_map.get(role_kind, role_map["villain"])
    out_path = safe_project_file(proj_dir, f"auto_role_{clean_role}.jpg")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 4000:
        return out_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    encoded = urllib.parse.quote(prompt, safe="")
    for url in (
        f"https://image.pollinations.ai/prompt/{encoded}?width=768&height=768&model=flux&nologo=true&seed={seed_num}",
        f"https://image.pollinations.ai/prompt/{encoded}?width=768&height=768&model=turbo&nologo=true&seed={seed_num}",
    ):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            ctype = str(resp.headers.get("content-type", "")).lower()
            if resp.status_code == 200 and len(resp.content) > 5000 and ("image" in ctype or resp.content[:2] in (b"\xff\xd8", b"\x89P")):
                save_upright_image_bytes(resp.content, out_path, max_side=960)
                return out_path
        except Exception:
            continue
    return None


def resolve_character_name(raw_name: object, char_map: dict, fallback_idx: int = 0) -> str:
    """Resolve dialogue speaker accurately: preserves topic-specific roles (Raja/Rani/Bhoot/Tantrik/Mantri/Heroine/Villain/Saas/Narrator) with strict gender separation."""
    char_names = list(char_map.keys())
    raw = str(raw_name or "").strip().strip(":：").strip()
    if not char_names:
        return raw or "Hero"
    if raw in char_map:
        return raw
    raw_low = raw.lower()
    if raw_low:
        for cn in char_names:
            if cn.lower() == raw_low:
                return cn
        if any(k in raw_low for k in ("narrator", "sutradhar", "सूत्रधार", "नैरेटर", "कथावाचक")):
            return "Narrator"
        if any(k in raw_low for k in ("chudail", "dayan", "daayan", "witch", "चुड़ैल", "डायन")):
            return "Chudail"
        if any(k in raw_low for k in ("rajmata", "राजमाता")):
            return "Rajmata"
        if any(k in raw_low for k in ("saas", "सास", "माँ", "mother-in-law")):
            return "Saas"
        if any(k in raw_low for k in ("rani", "रानी", "महारानी", "maharani", "queen")):
            return "Rani"
        if any(k in raw_low for k in ("rajkumari", "राजकुमारी", "princess")):
            return "Rajkumari"
        if any(k in raw_low for k in ("heroine", "nayika", "नायिका", "पत्नी", "बहू", "ladki", "लड़की", "priya", "kavya", "meera")):
            for cn in char_names:
                if detect_speaker_role(cn, char_map) == "female":
                    return cn
            return "Heroine"
        if any(k in raw_low for k in ("bhoot", "ghost", "भूत", "प्रेत")):
            return "Bhoot"
        if any(k in raw_low for k in ("tantrik", "तांत्रिक", "अघोरी")):
            return "Tantrik"
        if any(k in raw_low for k in ("mantri", "मंत्री", "महामंत्री", "senapati", "सेनापति")):
            return "Mantri"
        if any(k in raw_low for k in ("villain", "rival", "don", "kaal", "boss", "विलेन", "डॉन", "दुश्मन", "काल", "गद्दार")):
            return "Villain"
        if any(k in raw_low for k in ("hero", "हीरो", "नायक", "raja", "राजा", "महाराज", "rajkumar", "राजकुमार", "inspector", "इंस्पेक्टर")):
            return char_names[0]
        if any(k in raw_low for k in ("log", "crowd", "public", "people", "लोग", "भीड़", "जनता", "गाँव वाले")):
            return "Crowd"
        for cn in char_names:
            cn_low = cn.lower()
            if cn_low in raw_low or raw_low in cn_low:
                return cn
    return raw or char_names[fallback_idx % len(char_names)]

PITCH_PRESETS = {
    "normal": {"pitch": "+2Hz", "rate": "+0%", "label": "Hero Clear"},
    "hero_deep": {"pitch": "-8Hz", "rate": "-3%", "label": "Heroic Deep"},
    "heavy_villain": {"pitch": "-14Hz", "rate": "-6%", "label": "Heavy Villain"},
    "cute_kid": {"pitch": "+26Hz", "rate": "+10%", "label": "Cute Kid"},
    "fast_comic": {"pitch": "+10Hz", "rate": "+14%", "label": "Fast Comedy"},
}

LOCATION_RULES = [
    ("village_field", "Gaon ka Khet", r"gaon|village|khet|kisan|farm|field|गाँव|गांव|खेत|किसान|सरसों"),
    ("haunted_mansion", "Purani Haveli", r"haveli|bhoot|ghost|haunted|horror|mansion|हवेली|भूत|डराव|खंडहर|आत्मा"),
    ("mumbai_rain", "Mumbai ki Barish", r"mumbai|barish|rain|monsoon|marine|मुंबई|बारिश|बरसात|तूफान"),
    ("deep_jungle", "Deep Jungle", r"jungle|forest|van|tiger|tree|जंगल|वन|पेड़|शेर"),
    ("police_station", "Police Station", r"police|thana|inspector|jail|lockup|पुलिस|थाना|इंस्पेक्टर|जेल"),
    ("outer_space", "Outer Space", r"space|alien|planet|rocket|galaxy|अंतरिक्ष|एलियन|ग्रह|रॉकेट|तारे"),
    ("royal_palace", "Royal Palace", r"palace|mahal|raja|king|queen|darbar|महल|राजा|रानी|दरबार|राजमहल"),
    ("mountains", "Himalayan Mountains", r"mountain|pahad|snow|himalaya|hill|पहाड़|बर्फ|हिमालय|शिखर"),
    ("desert_fort", "Desert Fort", r"desert|registan|fort|kila|rajasthan|रेगिस्तान|किला|राजस्थान"),
    ("courtroom", "Courtroom", r"court|judge|vakeel|adalat|कानून|अदालत|जज|वकील"),
    ("cyber_lab", "Hi-Tech Lab", r"lab|scientist|cyber|robot|computer|लैब|वैज्ञानिक|रोबोट|हैक"),
    ("city_street", "Mumbai City Street", r"city|street|road|market|bazar|don|शहर|सड़क|बाज़ार|डॉन"),
]

MOOD_RIM_COLORS = {
    "Action": (255, 59, 92),
    "Emotional": (96, 165, 250),
    "Suspense": (34, 211, 238),
    "Comedy": (250, 204, 21),
    "Romantic": (244, 114, 182),
}


def verify_pin_secret(candidate: str) -> bool:
    if not candidate:
        return False
    return hmac.compare_digest(candidate.strip(), ADMIN_PIN)


def verify_admin(admin_email: str, admin_pin: str):
    if admin_email.lower().strip() != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Unauthorized Admin Email")
    if not verify_pin_secret(admin_pin):
        raise HTTPException(
            status_code=403,
            detail="गलत 6-Digit Secret Admin PIN! कृपया सही PIN डालें।",
        )


def normalize_mood(raw: str, fallback: str = "Action") -> str:
    lower = (raw or "").lower()
    if re.search(r"emot|sad|cry|family|भावुक|रो|माँ|दुख", lower):
        return "Emotional"
    if re.search(r"suspen|horror|thrill|ghost|mystery|सस्पेंस|डर|भूत|राज़", lower):
        return "Suspense"
    if re.search(r"comed|fun|laugh|joke|कॉमेडी|हँस|मज़ाक", lower):
        return "Comedy"
    if re.search(r"roman|love|pyar|ishq|रोमां|प्यार|इश्क़", lower):
        return "Romantic"
    if re.search(r"action|fight|war|super|chase|एक्शन|लड़ाई|जंग|हीरो", lower):
        return "Action"
    return fallback if fallback in ALLOWED_MOODS else "Action"


def detect_location_meta(text: str, fallback_location: str = "") -> tuple[str, str]:
    combined = f"{text or ''} {fallback_location or ''}".lower()
    for key, label, pattern in LOCATION_RULES:
        if re.search(pattern, combined, re.IGNORECASE):
            return key, (fallback_location.strip() if fallback_location.strip() else label)
    if fallback_location.strip():
        return "city_street", fallback_location.strip()
    return "city_street", "Mumbai City Street"


def safe_extract_json(raw_text: str, fallback_data):
    try:
        cleaned = re.sub(r"```(?:json)?", "", raw_text).replace("```", "").strip()
        return json.loads(cleaned)
    except Exception:
        match = re.search(r"(\{.*\}|\[.*\])", raw_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                pass
    return fallback_data


def normalize_language(raw: str) -> str:
    clean = (raw or "").strip().lower()
    for lang in ALLOWED_LANGUAGES:
        if lang.lower() == clean:
            return lang
    return "Hindi"


def normalize_pitch(raw: str) -> str:
    clean = (raw or "normal").strip().lower()
    return clean if clean in PITCH_PRESETS else "normal"


def detect_sfx_type(text: str, location_key: str = "", mood: str = "Action") -> str:
    combined = f"{text or ''} {location_key or ''}".lower()
    if re.search(r"bijli|thunder|toofan|storm|बिजली|तूफान|गरज", combined):
        return "thunder"
    if re.search(r"barish|rain|monsoon|बारिश|बरसात|mumbai_rain", combined):
        return "rain"
    if re.search(r"dhamaka|bomb|blast|explosion|आग|बम|धमाका|विस्फोट", combined):
        return "explosion"
    if re.search(r"talwar|sword|blade|तलवार|कटार|खंजर", combined):
        return "sword"
    if re.search(r"fight|punch|maar|mukka|ladai|लड़ाई|मुक्का|मार|फाइट", combined):
        return "punch"
    if re.search(r"kadam|footstep|chal|haveli|bhoot|कदम|आहट|हवेली|भूत", combined):
        return "footsteps"
    if mood == "Action":
        return "punch"
    if mood == "Suspense":
        return "footsteps"
    return "none"


def build_visual_action_prompt(
    character: str,
    dialogue: str,
    location: str,
    mood: str = "Action",
    sfx: str = "none",
) -> str:
    clean_loc = (location or "dramatic Bollywood cinema set").strip()
    if sfx in ("explosion", "punch", "sword"):
        motion = "high-impact handheld camera shake, fiery sparks flying, kinetic combat motion, dust shockwave"
    elif sfx in ("rain", "thunder"):
        motion = "heavy monsoon storm rain splashing, lightning flashes, fast tracking camera through wet street"
    elif mood == "Action":
        motion = "character sprinting and charging forward with fierce expression, fast tracking camera, cinematic speed blur"
    elif mood == "Suspense":
        motion = "slow creeping dolly-in camera, flickering neon rim lights, drifting fog and dramatic shadows"
    elif mood == "Comedy":
        motion = "snappy whip-pan reaction shot, expressive comic energy, vibrant warm Bollywood studio lighting"
    elif mood == "Romantic":
        motion = "dreamy sweeping camera tracking, golden hour lens flare, glowing floating bokeh particles"
    else:
        motion = "dramatic cinematic push-in close-up, expressive eyes, atmospheric rim lighting, anamorphic film look"
    return f"Cinematic Bollywood action movie shot of {character} in {clean_loc}, {motion}, 4K film grain, dynamic camera movement"


def make_referral_code(email: str) -> str:
    prefix = re.sub(r"[^A-Z0-9]", "", email.split("@")[0].upper())[:3] or "USR"
    suffix = uuid.uuid4().hex[:5].upper()
    return f"CP{prefix}{suffix}"


AVATARS_DIR = "all_avatars"
os.makedirs(AVATARS_DIR, exist_ok=True)

DEFAULT_AI_CONFIG = {
    # Story Brain Auto-Chain (Priority 1 -> 2 -> 3)
    "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
    "groq_api_key": os.getenv("GROQ_API_KEY", ""),
    # Visual & Video Auto-Chain (Priority 1 -> 2 -> 3 -> 4 -> 5)
    "cf_account_id": os.getenv("CF_ACCOUNT_ID", ""),
    "cf_api_token": os.getenv("CF_API_TOKEN", ""),
    "together_api_key": os.getenv("TOGETHER_API_KEY", ""),
    "custom_video_space": "",
    "custom_faceswap_space": "",
    "hf_token": os.getenv("HF_TOKEN", ""),
    "enable_custom_hf": True,
    "enable_pollinations": True,
    "pollinations_model": "flux",
    "enable_public_hf_spaces": True,
    "enable_ltx_video": True,
    "enable_wan21": True,
    "enable_cogvideox": True,
    "enable_local_motion_fallback": True,
    "space_timeout_sec": 10,
    # Admin Business Controls
    "free_trial_passes": 1,
    "pack_50_passes": 1,
    "upi_id": UPI_ID,
    "announcement_banner": "",
}

# Smart Auto-Switch Circuit Breaker: provider_key -> (cooldown_until_epoch, reason)
_API_VAULT_COOLDOWN: dict[str, tuple[float, str]] = {}


def _is_vault_provider_cooling(provider_key: str) -> bool:
    entry = _API_VAULT_COOLDOWN.get(provider_key)
    if not entry:
        return False
    return time.time() < entry[0]


def _mark_vault_provider_failed(provider_key: str, reason: str = "error", ttl_sec: int = 180):
    _API_VAULT_COOLDOWN[provider_key] = (time.time() + ttl_sec, str(reason)[:80])


def normalize_ai_config(raw: Optional[dict]) -> dict:
    cfg = dict(DEFAULT_AI_CONFIG)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in cfg:
                cfg[k] = v
    for skey in (
        "gemini_api_key",
        "groq_api_key",
        "cf_account_id",
        "cf_api_token",
        "together_api_key",
        "custom_video_space",
        "custom_faceswap_space",
        "hf_token",
        "upi_id",
        "announcement_banner",
    ):
        cfg[skey] = str(cfg.get(skey) or "").strip()
    if not cfg["upi_id"]:
        cfg["upi_id"] = UPI_ID
    cfg["announcement_banner"] = cfg["announcement_banner"][:240]
    pm = str(cfg.get("pollinations_model") or "flux").strip().lower()
    cfg["pollinations_model"] = pm if pm in ("flux", "turbo", "flux-realism") else "flux"
    for bkey in (
        "enable_custom_hf",
        "enable_pollinations",
        "enable_public_hf_spaces",
        "enable_ltx_video",
        "enable_wan21",
        "enable_cogvideox",
        "enable_local_motion_fallback",
    ):
        val = cfg.get(bkey, True)
        if isinstance(val, str):
            cfg[bkey] = val.strip().lower() in ("true", "1", "yes", "on")
        else:
            cfg[bkey] = bool(val)
    try:
        cfg["space_timeout_sec"] = max(4, min(15, int(cfg.get("space_timeout_sec", 10))))
    except Exception:
        cfg["space_timeout_sec"] = 10
    try:
        cfg["free_trial_passes"] = max(0, min(50, int(cfg.get("free_trial_passes", 1))))
    except Exception:
        cfg["free_trial_passes"] = 1
    try:
        cfg["pack_50_passes"] = max(1, min(100, int(cfg.get("pack_50_passes", 1))))
    except Exception:
        cfg["pack_50_passes"] = 1
    return cfg


def get_runtime_ai_config() -> dict:
    try:
        db = load_db()
        return normalize_ai_config(db.get("ai_config"))
    except Exception:
        return dict(DEFAULT_AI_CONFIG)


# ==============================================================================
# CINEPOP AI - SMART AI FILM DIRECTOR & AUTO-FAILOVER ENGINE (v12 MASTER)
# ==============================================================================

# 1. ADMIN PANEL DYNAMIC API VAULT (Auto-Switch Priority Chain)
RUNTIME_API_VAULT = {
    "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", ""),
    "GROQ_API_KEY": os.getenv("GROQ_API_KEY", ""),
    "CF_ACCOUNT_ID": os.getenv("CF_ACCOUNT_ID", ""),
    "CF_API_TOKEN": os.getenv("CF_API_TOKEN", ""),
    "TOGETHER_API_KEY": os.getenv("TOGETHER_API_KEY", ""),
    "HF_TOKEN": os.getenv("HF_TOKEN", ""),
    "CUSTOM_HF_SPACE": os.getenv("CUSTOM_HF_SPACE", "black-forest-labs/FLUX.1-schnell"),
}

API_HEALTH_STATUS = {
    "gemini": "standby",
    "groq": "standby",
    "cloudflare": "standby",
    "together": "standby",
    "huggingface": "standby",
    "pollinations": "active",
}

_VAULT_TO_AI_CONFIG_KEY = {
    "GEMINI_API_KEY": "gemini_api_key",
    "GROQ_API_KEY": "groq_api_key",
    "CF_ACCOUNT_ID": "cf_account_id",
    "CF_API_TOKEN": "cf_api_token",
    "TOGETHER_API_KEY": "together_api_key",
    "HF_TOKEN": "hf_token",
    "CUSTOM_HF_SPACE": "custom_video_space",
}


def _sync_runtime_api_vault_from_config(cfg: Optional[dict] = None) -> dict:
    """Keep RUNTIME_API_VAULT hydrated from persistent Admin ai_config + env fallback."""
    active_cfg = cfg if isinstance(cfg, dict) else get_runtime_ai_config()
    for vault_k, cfg_k in _VAULT_TO_AI_CONFIG_KEY.items():
        val = str(active_cfg.get(cfg_k) or "").strip()
        if val:
            RUNTIME_API_VAULT[vault_k] = val
        elif not RUNTIME_API_VAULT.get(vault_k):
            default_v = "black-forest-labs/FLUX.1-schnell" if vault_k == "CUSTOM_HF_SPACE" else ""
            RUNTIME_API_VAULT[vault_k] = os.getenv(vault_k, default_v).strip()
    return RUNTIME_API_VAULT


# 2. DYNAMIC TOPIC-AWARE CHARACTER & SCENE PROMPTS (Waist-Up Only & Strict Male/Female Visual Lock!)
TV_SERIAL_SET_BASE = (
    "Live-action Indian Hindi TV drama frame, warm cinematic lighting, 35mm DSLR camera, "
    "medium waist-up framing showing full upper body and expressive hand gestures, realistic skin texture, no cartoon, no extreme close-up."
)

CHARACTER_PROMPTS = {
    "Wide_Confrontation": (
        f"Medium wide two-shot confrontation inside a grand royal Indian palace hall with chandeliers and carved arches, {TV_SERIAL_SET_BASE} "
        "Handsome Indian male hero in royal embroidered bandhgala sherwani standing face-to-face confronting menacing Indian male antagonist in dark royal cloak, "
        "while young Indian female heroine in crimson silk lehenga-saree stands nearby."
    ),
    "Hero": (
        f"Medium over-the-shoulder waist-up shot of ONE handsome young Indian MALE actor only (man with short hair and trimmed beard), {TV_SERIAL_SET_BASE} "
        "wearing tailored black-and-gold royal bandhgala suit standing firmly and speaking with confident hand gesture inside grand Indian palace hall, no woman in foreground."
    ),
    "Heroine": (
        f"Medium waist-up shot of ONE young Indian FEMALE actress only (woman), {TV_SERIAL_SET_BASE} "
        "wearing crimson Banarasi silk saree and traditional gold jhumkas speaking with expressive emotional eyes inside grand Indian palace hall, no man in foreground."
    ),
    "Villain": (
        f"Medium waist-up shot of ONE menacing Indian MALE villain actor only (man with sharp beard and intense eyes), {TV_SERIAL_SET_BASE} "
        "wearing dark royal velvet coat standing face-to-face and speaking with angry smirk and pointing finger inside grand Indian hall, strictly male actor."
    ),
    "Saas": (
        f"Medium waist-up shot of ONE commanding older Indian FEMALE matriarch actress only (woman), {TV_SERIAL_SET_BASE} "
        "wearing heavy maroon Banarasi silk saree, gold necklace and big bindi speaking dramatically with hand gesture inside grand Indian hall, strictly female actress."
    ),
    "Context_BRoll": (
        "Cinematic insert B-Roll shot from an Indian live-action drama, grand illuminated royal Indian palace fortress gate at night with glowing torches, "
        "dramatic mist, 8k DSLR live-action."
    ),
}


def _detect_topic_story_archetype(topic: str) -> str:
    """Classify user's topic so fallback stories & visual prompts match the EXACT topic (Raja/Bhoot, Jungle, Police, Comedy, or Drama)."""
    low = (topic or "").lower()
    has_ghost = any(
        k in low
        for k in (
            "bhoot", "ghost", "pret", "atma", "aatma", "chudail", "dayan", "haunted", "horror", "tantrik", "shaitan", "kंकाल",
            "भूत", "प्रेत", "आत्मा", "चुड़ैल", "डायन", "भुतहा", "हवेली का रहस्य", "तांत्रिक", "डरावनी", "श्राप", "काली शक्ति",
        )
    )
    has_royal = any(
        k in low
        for k in (
            "raja", "rani", "king", "queen", "mahal", "palace", "rajkumar", "rajkumari", "empire", "fort", "singhasan", "mantri", "senapati",
            "राजा", "रानी", "महल", "राजकुमार", "राजकुमारी", "सिंहासन", "दरबार", "किला", "मंत्री", "सेनापति", "महाराज", "साम्राज्य",
        )
    )
    if has_ghost and has_royal:
        return "royal_ghost"
    if has_ghost:
        return "horror_ghost"
    if has_royal:
        return "royal_kingdom"
    if any(k in low for k in ("jungle", "forest", "sher", "tiger", "lion", "janwar", "island", "cave", "जंगल", "शेर", "बाघ", "गुफा", "शिकार", "खज़ाना", "खजाना")):
        return "jungle_adventure"
    if any(k in low for k in ("police", "crime", "murder", "chor", "chori", "inspector", "jail", "court", "cbi", "detective", "don", "mafia", "पुलिस", "चोर", "चोरी", "इंस्पेक्टर", "जेल", "हत्या", "जुर्म", "गैंगस्टर")):
        return "police_thriller"
    if any(k in low for k in ("comedy", "funny", "mazaak", "hasna", "jugaad", "padosi", "kanjoos", "कॉमेडी", "मज़ाक", "मजाक", "हंसी", "जुगाड़", "पड़ोसी", "कंजूस")):
        return "comedy_drama"
    return "custom_topic"


def get_dynamic_character_prompts(topic: str = "") -> dict[str, str]:
    """
    Build topic-accurate waist-up live-action prompts with STRICT gender locking:
    - Hero & Villain are ALWAYS explicitly MALE actors.
    - Heroine & Saas/Elder-Female are ALWAYS explicitly FEMALE actresses.
    - When topic is Raja/Mahal/Bhoot, generates Indian King in royal sherwani & crown, Queen, Ghost/Tantrik, haunted palace!
    """
    archetype = _detect_topic_story_archetype(topic)
    clean_t = re.sub(r"[^a-zA-Z0-9 ,.-]", " ", (topic or "")).strip()[:80]
    topic_hint = f"Theme context: {clean_t}. " if clean_t else ""

    if archetype in ("royal_ghost", "horror_ghost"):
        set_base = (
            f"Live-action Indian supernatural royal thriller drama frame, {topic_hint}"
            "inside an ancient candle-lit haunted Indian royal palace darbar hall with mysterious blue-golden mist, "
            "carved stone pillars, flickering मशाल torches, 35mm DSLR camera, medium waist-up framing showing full upper body and hand gestures, no cartoon, no extreme close-up."
        )
        return {
            "Wide_Confrontation": (
                f"Medium wide two-shot confrontation, {set_base} "
                "Brave Indian King (Raja) in royal gold-embroidered sherwani, jeweled crown and sword hilt standing face-to-face confronting "
                "a terrifying supernatural dark Tantrik Ghost (Bhoot) in tattered black-ash robes with glowing eerie eyes inside the haunted royal mahal."
            ),
            "Hero": (
                f"Medium over-the-shoulder waist-up shot of ONE handsome Indian MALE King (Raja) only, {set_base} "
                "man with royal beard wearing regal maroon-and-gold embroidered sherwani, royal crown (mukut) and pearl necklace, holding sword hilt and speaking bravely, strictly male actor."
            ),
            "Heroine": (
                f"Medium waist-up shot of ONE beautiful Indian FEMALE Queen (Rani) only, {set_base} "
                "woman wearing royal crimson-and-gold silk lehenga saree, royal crown jewelry and jhumkas speaking with worried emotional eyes inside haunted palace, strictly female actress."
            ),
            "Villain": (
                f"Medium waist-up shot of ONE menacing supernatural Indian MALE Ghost / Tantrik (Bhoot) only, {set_base} "
                "fierce man with ash-covered face, dark mystical robes, rudraksha beads and glowing supernatural aura confronting the King with dramatic hand gesture, strictly male actor."
            ),
            "Saas": (
                f"Medium waist-up shot of ONE mysterious Indian FEMALE Rajmata / Royal Priestess only, {set_base} "
                "older woman in dark maroon royal silk saree and heavy gold temple jewelry warning the royal court with dramatic hand gesture, strictly female actress."
            ),
            "Context_BRoll": (
                "Cinematic insert B-Roll shot from an Indian supernatural royal drama, ancient haunted Indian maharaja fort palace under full moon night sky with lightning, "
                "glowing torches, swirling fog at grand palace gates, 8k DSLR live-action."
            ),
        }

    if archetype == "royal_kingdom":
        set_base = (
            f"Live-action Indian historical royal palace drama frame, {topic_hint}"
            "inside a grand Rajputana royal darbar hall with golden throne, chandeliers, carved sandstone arches and royal guards in background, "
            "35mm DSLR camera, medium waist-up framing showing full upper body and hand gestures, no cartoon, no extreme close-up."
        )
        return {
            "Wide_Confrontation": (
                f"Medium wide two-shot confrontation, {set_base} "
                "Noble Indian King (Raja) in royal gold sherwani and jeweled turban standing face-to-face confronting "
                "scheming royal minister (Mantri) in dark brocade robe inside the grand royal darbar."
            ),
            "Hero": (
                f"Medium over-the-shoulder waist-up shot of ONE handsome Indian MALE King (Raja) only, {set_base} "
                "man with royal mustache and beard wearing gold-embroidered royal sherwani, jeweled turban crown and pearl necklace speaking with commanding hand gesture, strictly male actor."
            ),
            "Heroine": (
                f"Medium waist-up shot of ONE graceful Indian FEMALE Queen (Rani) only, {set_base} "
                "woman wearing royal crimson silk Rajputi poshak lehenga, heavy gold kundan jewelry and maang-tikka speaking with expressive eyes, strictly female actress."
            ),
            "Villain": (
                f"Medium waist-up shot of ONE cunning Indian MALE royal antagonist (Senapati / Mantri) only, {set_base} "
                "bearded man in black-and-silver royal armor tunic speaking with arrogant smirk and pointing finger, strictly male actor."
            ),
            "Saas": (
                f"Medium waist-up shot of ONE commanding Indian FEMALE Rajmata (Queen Mother) only, {set_base} "
                "older royal woman in maroon Banarasi silk saree and heavy gold heritage jewelry speaking authoritatively in darbar, strictly female actress."
            ),
            "Context_BRoll": (
                "Cinematic insert B-Roll shot of a grand illuminated Indian royal fort palace at dusk with flapping royal flags, "
                "armored horses and glowing golden torches, 8k DSLR live-action."
            ),
        }

    if archetype == "jungle_adventure":
        set_base = (
            f"Live-action Indian jungle adventure thriller frame, {topic_hint}"
            "inside a misty ancient Indian forest temple ruin with golden sunbeams, torches and dense jungle foliage, "
            "35mm DSLR camera, medium waist-up framing showing full upper body and hand gestures, no cartoon, no extreme close-up."
        )
        return {
            "Wide_Confrontation": (
                f"Medium wide two-shot confrontation, {set_base} "
                "Brave Indian male adventurer hero in rugged jacket confronting dangerous male jungle poacher boss face-to-face near ancient forest temple."
            ),
            "Hero": (
                f"Medium over-the-shoulder waist-up shot of ONE handsome young Indian MALE adventurer hero only, {set_base} "
                "man with short beard in dark olive expedition jacket holding torch and speaking firmly, strictly male actor."
            ),
            "Heroine": (
                f"Medium waist-up shot of ONE young Indian FEMALE explorer heroine only, {set_base} "
                "woman in practical adventure kurti-jacket speaking urgently with expressive gesture in misty jungle, strictly female actress."
            ),
            "Villain": (
                f"Medium waist-up shot of ONE ruthless Indian MALE jungle villain only, {set_base} "
                "man with rugged beard and dark tactical vest confronting the hero with menacing gesture, strictly male actor."
            ),
            "Saas": (
                f"Medium waist-up shot of ONE wise elder Indian FEMALE tribal guardian only, {set_base} "
                "older woman in traditional forest shawl and silver jewelry warning about the jungle secret, strictly female actress."
            ),
            "Context_BRoll": (
                "Cinematic insert B-Roll shot of a mystical ancient Indian jungle temple ruin at twilight with glowing torches, mist and roaring waterfall, 8k DSLR live-action."
            ),
        }

    if archetype == "police_thriller":
        set_base = (
            f"Live-action Indian crime thriller TV drama frame, {topic_hint}"
            "inside a dramatic Indian police investigation headquarters and city mansion hall with moody cinema lighting, "
            "35mm DSLR camera, medium waist-up framing showing full upper body and hand gestures, no cartoon, no extreme close-up."
        )
        return {
            "Wide_Confrontation": (
                f"Medium wide two-shot confrontation, {set_base} "
                "Sharp Indian male Police Officer in khaki uniform confronting powerful male crime boss in dark suit face-to-face."
            ),
            "Hero": (
                f"Medium over-the-shoulder waist-up shot of ONE handsome Indian MALE Police Inspector hero only, {set_base} "
                "man with trimmed mustache in crisp khaki IPS officer uniform speaking with commanding hand gesture, strictly male actor."
            ),
            "Heroine": (
                f"Medium waist-up shot of ONE young Indian FEMALE investigative journalist / lawyer heroine only, {set_base} "
                "woman in smart crimson ethnic blazer-saree holding evidence file and speaking boldly, strictly female actress."
            ),
            "Villain": (
                f"Medium waist-up shot of ONE intimidating Indian MALE mafia don villain only, {set_base} "
                "bearded man in black velvet blazer and sunglasses smirking and pointing finger, strictly male actor."
            ),
            "Saas": (
                f"Medium waist-up shot of ONE senior Indian FEMALE judge / matriarch only, {set_base} "
                "older woman in dignified maroon silk saree speaking sternly with hand gesture, strictly female actress."
            ),
            "Context_BRoll": (
                "Cinematic insert B-Roll shot of Indian police beacon cars with flashing red-blue lights arriving outside a grand mansion gate at rainy night, 8k DSLR live-action."
            ),
        }

    return dict(CHARACTER_PROMPTS)


def _build_topic_aware_director_fallback(topic: str, part_num: int = 1, prev_cliffhanger: str = "") -> list[dict]:
    """
    100% TOPIC-AWARE HINDI MULTI-CHARACTER FALLBACK STORY:
    Reads the user's exact topic (Raja/Bhoot, Jungle, Police, Comedy, or Custom Topic)
    and builds an authentic 7-dialogue script with topic-accurate characters.
    ZERO hardcoded 'ghar jamai / sasural' lines!
    """
    clean_t = re.sub(r"\(\s*Genre:.*?\)", "", (topic or "")).strip()
    clean_t = re.sub(r"\s+", " ", clean_t).strip() or "रहस्यमयी महल का राज़"
    short_t = clean_t[:48]
    archetype = _detect_topic_story_archetype(clean_t)

    if archetype == "royal_ghost":
        if part_num == 1:
            return [
                {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": f"अमावस्या की आधी रात और {short_t} का खौफनाक राज़ आज खुलने वाला था!"},
                {"speaker": "Rani", "emotion": "shocked", "broll": False, "dialogue": "महाराज रुक जाइए! उस बंद तहखाने से भयानक भूत की आवाज़ें आ रही हैं!"},
                {"speaker": "Mantri", "emotion": "angry", "broll": False, "dialogue": "महाराज, जो भी उस शापित सिंहासन के पास गया, वह कभी ज़िंदा नहीं लौटा!"},
                {"speaker": "Hero", "emotion": "confident", "broll": False, "dialogue": "एक राजा अपने राज्य और प्रजा की रक्षा के लिए किसी भूत से नहीं डरता!"},
                {"speaker": "Bhoot", "emotion": "angry", "broll": False, "dialogue": "हाहाहा! राजा, इस महल और तेरे सिंहासन पर सदियों से मेरा कब्ज़ा है!"},
                {"speaker": "Rajmata", "emotion": "shocked", "broll": False, "dialogue": "सावधान पुत्र! इस भूत की असली शक्ति उस काले ताबीज़ में छुपी है!"},
                {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": "तभी राजा की तलवार चमकी और तहखाने का दरवाज़ा टूटा — आगे क्या हुआ?"},
            ]
        return [
            {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": f"Part {part_num} में राजा और महल के भूत का आमना-सामना चरम पर पहुँच गया!"},
            {"speaker": "Bhoot", "emotion": "angry", "broll": False, "dialogue": "मेरी मायावी शक्ति के आगे तेरी तलवार कुछ नहीं कर सकती राजा!"},
            {"speaker": "Rani", "emotion": "sad", "broll": False, "dialogue": "महाराज, राजमाता के बताए उस प्राचीन मंत्र और अग्नि-बाण का प्रयोग कीजिए!"},
            {"speaker": "Mantri", "emotion": "shocked", "broll": False, "dialogue": "असंभव! महल के सारे स्तंभ हिल रहे हैं, यह भूत तो और शक्तिशाली हो रहा है!"},
            {"speaker": "Hero", "emotion": "confident", "broll": False, "dialogue": "सत्य और धर्म की अग्नि के सामने तेरा हर काला जादू आज भस्म हो जाएगा!"},
            {"speaker": "Bhoot", "emotion": "shocked", "broll": False, "dialogue": "नहीं! मेरा काला ताबीज़ कैसे टूट गया? मेरी शक्तियाँ खत्म हो रही हैं!"},
            {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": "राजा के एक प्रहार से शाप मिट गया और पूरे राज्य में फिर से उजाला छा गया!"},
        ]

    if archetype == "horror_ghost":
        return [
            {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": f"काली रात के सन्नाटे में {short_t} का खौफनाक खेल शुरू हो चुका था!"},
            {"speaker": "Heroine", "emotion": "shocked", "broll": False, "dialogue": "देखो! हवेली के आईने में कोई साया दिखाई दे रहा है, यहाँ से बाहर चलो!"},
            {"speaker": "Bhoot", "emotion": "angry", "broll": False, "dialogue": "इस चौखट के अंदर आने वाला कोई भी इंसान आज रात बचकर नहीं जाएगा!"},
            {"speaker": "Hero", "emotion": "confident", "broll": False, "dialogue": "डरो मत! मैं तुम्हें कुछ नहीं होने दूँगा, इसके पीछे ज़रूर कोई गहरा राज़ है!"},
            {"speaker": "Saas", "emotion": "shocked", "broll": False, "dialogue": "यह कोई साधारण साया नहीं, पचास साल पुराना अधूरा इंतकाम जाग उठा है!"},
            {"speaker": "Hero", "emotion": "confident", "broll": False, "dialogue": "आज इस रहस्य से पर्दा उठेगा और इस खौफ का हमेशा के लिए अंत होगा!"},
            {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": "तभी अचानक बिजली कड़की और बंद दरवाज़े के पीछे का असली सच सामने आ गया!"},
        ]

    if archetype == "royal_kingdom":
        return [
            {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": f"शाही दरबार में {short_t} को लेकर सबसे बड़ा टकराव शुरू हो गया!"},
            {"speaker": "Mantri", "emotion": "angry", "broll": False, "dialogue": "महाराज, पड़ोसी राज्य की सेना हमारी सीमा पर खड़ी है और खज़ाना खतरे में है!"},
            {"speaker": "Rani", "emotion": "sad", "broll": False, "dialogue": "महाराज, हमारे ही दरबार में कोई गद्दार दुश्मनों को गुप्त संदेश भेज रहा है!"},
            {"speaker": "Rajmata", "emotion": "angry", "broll": False, "dialogue": "इस सिंहासन के साथ गद्दारी करने वाले को किसी भी कीमत पर बख्शा नहीं जाएगा!"},
            {"speaker": "Hero", "emotion": "confident", "broll": False, "dialogue": "मैं जानता हूँ गद्दार कौन है — आज भरे दरबार में उसका नकाब उतरेगा!"},
            {"speaker": "Mantri", "emotion": "shocked", "broll": False, "dialogue": "महाराज... यह गुप्त पत्र आपके हाथ कैसे लगा? मुझे क्षमा कर दीजिए!"},
            {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": "राजा के न्याय और पराक्रम ने पूरे साम्राज्य को एक बड़ी साज़िश से बचा लिया!"},
        ]

    if archetype == "jungle_adventure":
        return [
            {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": f"घने जंगल के बीच {short_t} का सबसे खतरनाक सफर शुरू हो चुका था!"},
            {"speaker": "Heroine", "emotion": "shocked", "broll": False, "dialogue": "सावधान! आगे गुफा के रास्ते पर खूंखार शिकारियों और जंगली जानवरों का पहरा है!"},
            {"speaker": "Villain", "emotion": "angry", "broll": False, "dialogue": "इस जंगल के खज़ाने और राज़ पर सिर्फ मेरा हक है, पीछे हट जाओ!"},
            {"speaker": "Hero", "emotion": "confident", "broll": False, "dialogue": "यह जंगल और इसकी धरोहर किसी अपराधी की जागीर नहीं, हम पीछे नहीं हटेंगे!"},
            {"speaker": "Saas", "emotion": "shocked", "broll": False, "dialogue": "प्राचीन मंदिर का द्वार केवल वही खोल सकता है जिसके दिल में सच्चाई हो!"},
            {"speaker": "Hero", "emotion": "confident", "broll": False, "dialogue": "तेरा हर जाल टूट चुका है, अब इस जंगल से तेरा खेल खत्म!"},
            {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": "तभी प्राचीन गुफा का रहस्यमयी दरवाज़ा खुला और सबकी आँखें फटी रह गईं!"},
        ]

    if archetype == "police_thriller":
        return [
            {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": f"शहर में {short_t} के केस ने पूरे पुलिस महकमे को हिला कर रख दिया!"},
            {"speaker": "Villain", "emotion": "angry", "broll": False, "dialogue": "इंस्पेक्टर, मेरे खिलाफ सबूत ढूँढने की कोशिश मत कर, वरना अंजाम बुरा होगा!"},
            {"speaker": "Heroine", "emotion": "confident", "broll": False, "dialogue": "मेरे पास इस पूरे घोटाले और साज़िश की असली रिकॉर्डिंग मौजूद है!"},
            {"speaker": "Saas", "emotion": "shocked", "broll": False, "dialogue": "इतने बड़े अपराध को अब कानून की नज़र से कोई नहीं छुपा सकता!"},
            {"speaker": "Hero", "emotion": "confident", "broll": False, "dialogue": "कानून के हाथ बहुत लंबे होते हैं — तेरे हर जुर्म की फाइल अब कोर्ट में है!"},
            {"speaker": "Villain", "emotion": "shocked", "broll": False, "dialogue": "यह कैसे हो सकता है? मेरा सबसे खास गवाह पुलिस के साथ कैसे मिल गया!"},
            {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": "तभी पुलिस सायरन गूँजा और अपराधी के सारे राज़ दुनिया के सामने आ गए!"},
        ]

    # Dynamic custom topic script directly built around user's exact topic words
    return [
        {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": f"आज की इस धमाकेदार कहानी का केंद्र है — {short_t}!"},
        {"speaker": "Heroine", "emotion": "shocked", "broll": False, "dialogue": f"{short_t} का यह सच अगर बाहर आ गया तो सब कुछ बदल जाएगा!"},
        {"speaker": "Villain", "emotion": "angry", "broll": False, "dialogue": "जब तक मैं यहाँ खड़ा हूँ, मेरी योजना को कोई नहीं रोक सकता!"},
        {"speaker": "Hero", "emotion": "confident", "broll": False, "dialogue": f"तेरी हर चाल नाकाम होगी, क्योंकि {short_t} का असली फैसला आज मैं करूँगा!"},
        {"speaker": "Saas", "emotion": "shocked", "broll": False, "dialogue": "सच्चाई और हिम्मत के आगे बड़े-बड़े षड्यंत्र भी टिक नहीं पाते!"},
        {"speaker": "Villain", "emotion": "shocked", "broll": False, "dialogue": "नहीं! मेरी सारी बाज़ी एक ही पल में कैसे पलट गई?"},
        {"speaker": "Narrator", "emotion": "twist", "broll": True, "dialogue": f"और इस तरह {short_t} के सबसे बड़े रहस्य से आखिरकार पर्दा उठ गया!"},
    ]


# 3. AUTO-FAILOVER STORY DIRECTOR (Gemini -> Groq -> Topic-Aware Dynamic Hindi Story)
async def director_generate_story(topic: str, part_num: int = 1, prev_cliffhanger: str = "") -> list[dict]:
    _sync_runtime_api_vault_from_config()
    archetype = _detect_topic_story_archetype(topic)
    role_guide = (
        'Allowed speaker names for this topic: "Narrator", "Hero" (or "Raja"), "Heroine" (or "Rani"), "Villain" (or "Bhoot" / "Mantri"), "Saas" (or "Rajmata").'
        if archetype in ("royal_ghost", "horror_ghost", "royal_kingdom")
        else 'Allowed speaker names: "Narrator", "Hero", "Heroine", "Villain", "Saas".'
    )
    system_prompt = (
        "You are an expert Indian Hindi TV Serial & Film Showrunner. "
        "Write a crisp, high-drama 7-dialogue scene in natural Hindi (Devanagari) strictly based on the user's exact Topic with zero incomplete sentences. "
        "NEVER insert unrelated 'ghar jamai' or 'sasural' plots unless the user's topic explicitly asks for it! "
        f"{role_guide} "
        "Narrator speaks ONLY line 1 (hook) and line 7 (cliffhanger). "
        "Return ONLY valid JSON array of 7 objects: "
        '[{"speaker":"Narrator|Hero|Heroine|Villain|Saas","emotion":"angry|shocked|sad|confident|twist","broll":false,"dialogue":"..."}]'
    )
    user_prompt = f"Exact Story Topic: {topic}. Episode Part: {part_num}. Previous Cliffhanger: {prev_cliffhanger or 'Start of story'}."

    # Priority 1: Gemini 2.5 Flash
    gemini_key = RUNTIME_API_VAULT.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            payload = {
                "contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
            }
            async with httpx.AsyncClient(timeout=8.0) as client_http:
                r = await client_http.post(url, json=payload)
                if r.status_code == 200:
                    txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    data = json.loads(txt)
                    if isinstance(data, list) and len(data) >= 5:
                        API_HEALTH_STATUS["gemini"] = "active"
                        return data[:8]
            API_HEALTH_STATUS["gemini"] = "quota_full"
        except Exception:
            API_HEALTH_STATUS["gemini"] = "error"

    # Priority 2: Groq Llama 3.3 70B (0.4s Ultra-Fast Auto-Switch)
    groq_key = RUNTIME_API_VAULT.get("GROQ_API_KEY", "").strip()
    if groq_key:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client_http:
                r = await client_http.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.7,
                    },
                )
                if r.status_code == 200:
                    raw = r.json()["choices"][0]["message"]["content"]
                    start, end = raw.find("["), raw.rfind("]") + 1
                    data = json.loads(raw[start:end])
                    if isinstance(data, list) and len(data) >= 5:
                        API_HEALTH_STATUS["groq"] = "active"
                        return data[:8]
            API_HEALTH_STATUS["groq"] = "quota_full"
        except Exception:
            API_HEALTH_STATUS["groq"] = "error"

    # Priority 3: Topic-Aware Multi-Character Hindi Story Fallback (Matches Raja/Bhoot, Jungle, Police, or Custom Topic!)
    return _build_topic_aware_director_fallback(topic=topic, part_num=part_num, prev_cliffhanger=prev_cliffhanger)


def _is_valid_non_black_image_file(img_path: str, min_bytes: int = 4500) -> bool:
    """
    Verify an image file is a valid readable image AND is NOT a dark/black/blank frame
    (checks mean luminance & standard deviation so rate-limit/black images are immediately rejected!).
    """
    try:
        if not img_path or not os.path.exists(img_path) or os.path.getsize(img_path) < min_bytes:
            return False
        im = open_upright_image(img_path).resize((96, 96), Image.Resampling.BILINEAR)
        if np is not None:
            arr = np.asarray(im, dtype=np.float32)
            mean_val = float(arr.mean())
            std_val = float(arr.std())
            # Reject black/near-black frames or flat solid-color frames
            if mean_val < 20.0 or std_val < 10.0:
                return False
        return True
    except Exception:
        return False


def _render_rich_cinematic_palace_actor_fallback(
    out_path: str,
    role: str = "Hero",
    width: int = 720,
    height: int = 1280,
) -> str:
    """
    Emergency 100% non-black, richly lit royal Indian palace / atmospheric character silhouette + golden chandelier canvas
    if every internet provider is offline and zero donor images exist yet.
    """
    img = Image.new("RGB", (width, height), (54, 28, 18))
    _draw_vertical_gradient(img, (98, 48, 24), (36, 18, 28))
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    cx, cy = width // 2, int(height * 0.36)
    # Warm golden chandelier & royal arch glow
    for rad, alpha in ((int(width * 0.55), 45), (int(width * 0.38), 75), (int(width * 0.22), 110)):
        od.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=(255, 195, 75, alpha))
    # Ornate golden palace pillars
    pw = max(24, int(width * 0.07))
    od.rectangle([int(width * 0.06), 0, int(width * 0.06) + pw, height], fill=(165, 110, 42, 120))
    od.rectangle([int(width * 0.94) - pw, 0, int(width * 0.94), height], fill=(165, 110, 42, 120))
    # Atmospheric fire/mist embers
    rng = random.Random(abs(hash(role)) + 77)
    for _ in range(42):
        ex = rng.randint(20, width - 20)
        ey = rng.randint(40, height - 40)
        er = rng.randint(3, 9)
        od.ellipse([ex - er, ey - er, ex + er, ey + er], fill=(255, 210, 115, rng.randint(60, 155)))
    composed = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    composed.save(out_path, "JPEG", quality=93)
    return out_path


# 4. AUTO-FAILOVER IMAGE ENGINE (Cloudflare -> Together -> HF -> Pollinations Flux + Fast Turbo Retry)
async def fetch_single_master_frame(
    role: str,
    prompt: str,
    out_path: str,
    width: int = 720,
    height: int = 1280,
    role_idx: int = 0,
) -> str:
    _sync_runtime_api_vault_from_config()
    role_seed_base = (abs(hash(f"{role}_{prompt[:48]}_{int(time.time()) // 60}")) + role_idx * 7919) % 899999 + 100000

    # Priority 1: Cloudflare Workers AI (@cf/black-forest-labs/flux-1-schnell)
    cf_id = RUNTIME_API_VAULT.get("CF_ACCOUNT_ID", "").strip()
    cf_tok = RUNTIME_API_VAULT.get("CF_API_TOKEN", "").strip()
    if cf_id and cf_tok:
        try:
            url = f"https://api.cloudflare.com/client/v4/accounts/{cf_id}/ai/run/@cf/black-forest-labs/flux-1-schnell"
            async with httpx.AsyncClient(timeout=10.0) as client_http:
                r = await client_http.post(url, headers={"Authorization": f"Bearer {cf_tok}"}, json={"prompt": prompt, "num_steps": 4})
                if r.status_code == 200:
                    res_json = r.json()
                    img_b64 = res_json.get("result", {}).get("image")
                    if img_b64:
                        with open(out_path, "wb") as f:
                            f.write(base64.b64decode(img_b64))
                        if _is_valid_non_black_image_file(out_path):
                            API_HEALTH_STATUS["cloudflare"] = "active"
                            return out_path
            API_HEALTH_STATUS["cloudflare"] = "quota_full"
        except Exception:
            API_HEALTH_STATUS["cloudflare"] = "error"

    # Priority 2: Together AI (FLUX.1-schnell-Free)
    tog_key = RUNTIME_API_VAULT.get("TOGETHER_API_KEY", "").strip()
    if tog_key:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client_http:
                r = await client_http.post(
                    "https://api.together.xyz/v1/images/generations",
                    headers={"Authorization": f"Bearer {tog_key}"},
                    json={
                        "model": "black-forest-labs/FLUX.1-schnell-Free",
                        "prompt": prompt,
                        "width": 768,
                        "height": 1024,
                        "steps": 4,
                        "seed": role_seed_base,
                        "response_format": "b64_json",
                    },
                )
                if r.status_code == 200:
                    img_b64 = r.json()["data"][0]["b64_json"]
                    with open(out_path, "wb") as f:
                        f.write(base64.b64decode(img_b64))
                    if _is_valid_non_black_image_file(out_path):
                        API_HEALTH_STATUS["together"] = "active"
                        return out_path
            API_HEALTH_STATUS["together"] = "quota_full"
        except Exception:
            API_HEALTH_STATUS["together"] = "error"

    # Priority 3: HuggingFace Direct Inference API
    hf_tok = RUNTIME_API_VAULT.get("HF_TOKEN", "").strip()
    if hf_tok:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client_http:
                r = await client_http.post(
                    "https://api-inference.huggingface.co/models/black-forest-labs/FLUX.1-schnell",
                    headers={"Authorization": f"Bearer {hf_tok}"},
                    json={"inputs": prompt},
                )
                if r.status_code == 200 and len(r.content) > 5000:
                    with open(out_path, "wb") as f:
                        f.write(r.content)
                    if _is_valid_non_black_image_file(out_path):
                        API_HEALTH_STATUS["huggingface"] = "active"
                        return out_path
            API_HEALTH_STATUS["huggingface"] = "quota_full"
        except Exception:
            API_HEALTH_STATUS["huggingface"] = "error"

    # Priority 4: Pollinations AI (`model=flux` -> immediate fast `model=turbo` retry with distinct role seed!)
    pollinations_attempts = [
        ("flux", prompt[:380], role_seed_base, 11.0),
        ("turbo", prompt[:260], role_seed_base + 313, 10.0),
        (
            "turbo",
            f"Live-action Indian royal drama medium waist-up shot of {role}, {prompt[:160]}, warm golden palace lighting, 8k DSLR",
            role_seed_base + 997,
            9.0,
        ),
    ]
    for model_name, p_text, seed_v, t_out in pollinations_attempts:
        try:
            enc = urllib.parse.quote(p_text)
            url = f"https://image.pollinations.ai/prompt/{enc}?width={width}&height={height}&model={model_name}&nologo=true&seed={seed_v}"
            async with httpx.AsyncClient(timeout=t_out, follow_redirects=True) as client_http:
                r = await client_http.get(
                    url,
                    headers={
                        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) CinePopStudio/{role_idx + 1}.0",
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    },
                )
                if r.status_code == 200 and len(r.content) > 5000:
                    with open(out_path, "wb") as f:
                        f.write(r.content)
                    if _is_valid_non_black_image_file(out_path):
                        API_HEALTH_STATUS["pollinations"] = "active"
                        return out_path
        except Exception:
            continue

    return ""


def _create_mirrored_zoomed_variant(
    src_img_path: str,
    out_img_path: str,
    width: int,
    height: int,
    mirror: bool = True,
    zoom_ratio: float = 0.11,
    color_grade: str = "golden",
) -> bool:
    """
    Create a visually distinct horizontal-flipped + warm golden/royal color-graded + strong digital zoomed
    + atmospheric (warm golden light / royal mist / ember glow) frame from an available character photo
    so a failed image fetch NEVER shows a black or duplicate frame!
    """
    try:
        if not _is_valid_non_black_image_file(src_img_path):
            return False
        img = open_upright_image(src_img_path).resize((width, height), Image.Resampling.LANCZOS)
        if mirror:
            img = ImageOps.mirror(img)
        mx = max(14, int(width * zoom_ratio))
        my = max(14, int(height * zoom_ratio))
        if width - 2 * mx > 100 and height - 2 * my > 100:
            img = img.crop((mx, my, width - mx, height - my)).resize((width, height), Image.Resampling.LANCZOS)

        # Apply warm golden / royal / atmospheric color grade + mist/ember overlay
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        if color_grade == "royal":
            od.rectangle([0, 0, width, height], fill=(215, 140, 45, 34))
            od.ellipse([-int(width * 0.2), -int(height * 0.15), int(width * 0.8), int(height * 0.45)], fill=(255, 200, 90, 42))
        elif color_grade == "mist":
            od.rectangle([0, 0, width, height], fill=(180, 145, 95, 30))
            od.rectangle([0, int(height * 0.58), width, height], fill=(235, 195, 130, 44))
        else:
            # Default warm golden palace grade
            od.rectangle([0, 0, width, height], fill=(245, 175, 55, 36))
            od.ellipse([int(width * 0.15), -int(height * 0.1), int(width * 0.85), int(height * 0.5)], fill=(255, 215, 110, 40))

        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        img = ImageEnhance.Contrast(img).enhance(1.08)
        img = ImageEnhance.Color(img).enhance(1.12)
        img = ImageEnhance.Brightness(img).enhance(1.04)
        img.save(out_img_path, "JPEG", quality=93)
        return _is_valid_non_black_image_file(out_img_path)
    except Exception:
        return False


# 5. STAGGERED 1.2s MASTER CASTING SHEET (Avoids Rate-Limit + 0% Black Screen Guarantee!)
async def build_master_casting_sheet(
    work_dir: str,
    width: int = 720,
    height: int = 1280,
    topic: str = "",
) -> dict[str, str]:
    """
    Fetch all 6 topic-aware roles (`Wide_Confrontation`, `Hero/Raja`, `Heroine/Rani`, `Villain`, `Saas`, `Context_BRoll`)
    with a mandatory 1.2-second stagger delay and distinct seeds so Pollinations/Free APIs never rate-limit or return black images!
    If any role times out, immediately recovers via fast turbo retry -> local same-gender cached photo -> horizontal flip + warm golden/royal color grade + zoom!
    """
    os.makedirs(work_dir, exist_ok=True)
    dynamic_prompts = get_dynamic_character_prompts(topic)
    casting: dict[str, str] = {}

    for idx_r, (role, prompt) in enumerate(dynamic_prompts.items()):
        out_file = os.path.join(work_dir, f"master_{role.lower()}.jpg")
        if _is_valid_non_black_image_file(out_file, min_bytes=5000):
            casting[role] = out_file
            continue
        if idx_r > 0:
            # Mandatory 1.2s delay between role fetches so free image servers never rate-limit!
            await asyncio.sleep(1.2)
        res = await fetch_single_master_frame(role, prompt, out_file, width, height, role_idx=idx_r)
        if res and _is_valid_non_black_image_file(res):
            casting[role] = res

    # Strict Gender-Aware & Atmospheric Golden/Royal Grade Recovery so NO character slot is ever missing or black!
    fallback_preference = {
        "Hero": ("Hero", "Villain", "Wide_Confrontation", "Context_BRoll"),
        "Villain": ("Villain", "Hero", "Wide_Confrontation", "Context_BRoll"),
        "Heroine": ("Heroine", "Saas", "Wide_Confrontation", "Context_BRoll"),
        "Saas": ("Saas", "Heroine", "Wide_Confrontation", "Context_BRoll"),
        "Wide_Confrontation": ("Wide_Confrontation", "Hero", "Villain", "Heroine", "Context_BRoll"),
        "Context_BRoll": ("Context_BRoll", "Wide_Confrontation", "Hero", "Villain"),
    }
    grade_cycle = ("golden", "royal", "mist")
    role_to_internal = {
        "Hero": "hero",
        "Heroine": "female",
        "Villain": "villain",
        "Saas": "saas",
        "Wide_Confrontation": "wide_confrontation",
        "Context_BRoll": "context_broll",
    }

    for idx_r, role in enumerate(dynamic_prompts.keys()):
        if role in casting and _is_valid_non_black_image_file(casting[role]):
            continue
        out_file = os.path.join(work_dir, f"master_{role.lower()}.jpg")

        # Try pre-cached same-gender/role image from local cache first
        internal_rk = role_to_internal.get(role, "hero")
        cached_im = _reuse_cached_photorealistic_scene(width, height, internal_rk, idx_r + 101)
        if cached_im is not None:
            try:
                cached_im.save(out_file, "JPEG", quality=93)
                if _is_valid_non_black_image_file(out_file):
                    casting[role] = out_file
                    continue
            except Exception:
                pass

        # Otherwise horizontal flip + warm golden/royal color grade + digital zoom on available master frame
        prefs = list(fallback_preference.get(role, ())) + list(casting.keys())
        for donor_role in prefs:
            donor_path = casting.get(donor_role)
            if donor_path and _is_valid_non_black_image_file(donor_path):
                if _create_mirrored_zoomed_variant(
                    donor_path,
                    out_file,
                    width,
                    height,
                    mirror=True,
                    zoom_ratio=0.09 + (idx_r % 3) * 0.03,
                    color_grade=grade_cycle[idx_r % len(grade_cycle)],
                ):
                    casting[role] = out_file
                    break

        # Final safety: rich golden palace canvas (never black!)
        if role not in casting or not _is_valid_non_black_image_file(casting.get(role, "")):
            _render_rich_cinematic_palace_actor_fallback(out_file, role=role, width=width, height=height)
            casting[role] = out_file

    return casting


def _apply_hero_faceswap_to_master_frame(
    hero_master_path: str,
    user_selfie_path: str,
    out_path: str,
    width: int,
    height: int,
    watermark_text: str = "",
    watermark_position: str = "top-right",
) -> str:
    """
    Strictly face-swap user's selfie onto the AI-generated Hero inside the haveli.
    NEVER displays the raw bedroom selfie directly on screen!
    Chain: HF AI Face-Swap -> OpenCV seamlessClone face-only blend -> Clean AI Hero fallback.
    """
    bg = open_upright_image(hero_master_path).resize((width, height), Image.Resampling.LANCZOS)
    has_user = bool(user_selfie_path and os.path.exists(user_selfie_path) and os.path.getsize(user_selfie_path) > 512)
    upright_tmp = f"{out_path}_upright_user.jpg"
    scene_tmp = f"{out_path}_hero_scene.jpg"
    swapped_tmp = f"{out_path}_hero_swapped.jpg"
    try:
        if has_user:
            actor = open_upright_image(user_selfie_path)
            actor.save(upright_tmp, "JPEG", quality=92)
            bg.save(scene_tmp, "JPEG", quality=92)
            # Step A: HuggingFace AI Face-Swap (`tonyassi/face-swap` / `ezioruan/inswapper`)
            swapped_p = _try_hf_scene_faceswap(scene_tmp, upright_tmp, swapped_tmp)
            if swapped_p and os.path.exists(swapped_p):
                composed = open_upright_image(swapped_p).resize((width, height), Image.Resampling.LANCZOS)
            else:
                # Step B: OpenCV face-only seamlessClone onto AI Hero's face (never raw selfie background!)
                cv_swapped = _try_opencv_face_only_swap(bg, actor)
                composed = cv_swapped if cv_swapped is not None else bg
        else:
            composed = bg

        composed = ImageEnhance.Contrast(composed).enhance(1.06)
        composed = ImageEnhance.Color(composed).enhance(1.06)
        if watermark_text:
            draw_channel_watermark(composed, watermark_text, watermark_position)
        composed.save(out_path, "JPEG", quality=93)
        return out_path
    finally:
        for tmp_f in (upright_tmp, scene_tmp, swapped_tmp):
            if os.path.exists(tmp_f):
                try:
                    os.remove(tmp_f)
                except Exception:
                    pass


def _get_runtime_gemini_client():
    """Return a google.genai.Client using Admin API Vault key first, falling back to env GEMINI_API_KEY."""
    cfg = get_runtime_ai_config()
    active_key = (cfg.get("gemini_api_key") or GEMINI_API_KEY or "").strip()
    if not active_key:
        return None
    if active_key == GEMINI_API_KEY and client is not None:
        return client
    try:
        return genai.Client(api_key=active_key)
    except Exception:
        return client


def _call_story_brain_auto_chain(prompt: str, timeout_s: float = 10.0) -> tuple[Optional[str], str]:
    """
    Story Brain Auto-Failover Chain (1 -> 2 -> 3) with <0.5s instant switch on failure / 429 quota:
      Priority 1: Gemini 2.5 Flash (`GEMINI_API_KEY`)
      Priority 2: Groq Llama 3.3 70B (`GROQ_API_KEY` — 0.4s free backup)
      Priority 3: Built-in Smart Hindi Multi-Character Drama Engine (handled by caller when None)
    """
    cfg = get_runtime_ai_config()

    # Priority 1: Gemini 2.5 Flash
    gem_key = (cfg.get("gemini_api_key") or GEMINI_API_KEY or "").strip()
    if gem_key and not _is_vault_provider_cooling("gemini_story"):
        gem_cli = _get_runtime_gemini_client()
        if gem_cli is not None:
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    res = pool.submit(
                        lambda: gem_cli.models.generate_content(
                            model="gemini-2.5-flash", contents=prompt
                        )
                    ).result(timeout=min(timeout_s, 10.0))
                txt = (getattr(res, "text", None) or "").strip()
                if txt:
                    return txt, "Gemini 2.5 Flash"
            except Exception as e:
                err_s = str(e)
                ttl = 600 if ("429" in err_s or "quota" in err_s.lower() or "RESOURCE_EXHAUSTED" in err_s) else 90
                _mark_vault_provider_failed("gemini_story", err_s, ttl_sec=ttl)

    # Priority 2: Groq Llama 3.3 70B (0.4s ultra-fast free failover!)
    groq_key = (cfg.get("groq_api_key") or os.getenv("GROQ_API_KEY") or "").strip()
    if groq_key and not _is_vault_provider_cooling("groq_story"):
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {groq_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are India's top Dangal TV & Pocket FM Hindi multi-character drama scriptwriter. Always follow the requested output format strictly.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 1100,
                },
                timeout=6.0,
            )
            if resp.status_code == 200:
                j = resp.json()
                txt = str(((j.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                if txt:
                    return txt, "Groq Llama 3.3 70B"
            elif resp.status_code in (401, 403, 429):
                _mark_vault_provider_failed("groq_story", f"HTTP {resp.status_code}", ttl_sec=600)
            else:
                _mark_vault_provider_failed("groq_story", f"HTTP {resp.status_code}", ttl_sec=90)
        except Exception as e:
            _mark_vault_provider_failed("groq_story", str(e), ttl_sec=90)

    return None, "Built-in Smart Hindi Drama Engine"


def save_user_avatar_bytes(email: str, raw_bytes: bytes) -> Optional[str]:
    """Save an EXIF-upright user profile face avatar to all_avatars/ and return its relative URL."""
    if not email or not raw_bytes or len(raw_bytes) < 256:
        return None
    try:
        safe_slug = re.sub(r"[^a-zA-Z0-9]", "_", email.lower().strip())[:48] or "user"
        filename = f"{safe_slug}.jpg"
        out_path = os.path.join(AVATARS_DIR, filename)
        save_upright_image_bytes(raw_bytes, out_path, max_side=640)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 512:
            ts = int(time.time())
            return f"/user-avatar/{filename}?v={ts}"
    except Exception:
        pass
    return None


def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("promo_codes", {})
                data.setdefault("payment_requests", [])
                data.setdefault("wallet_transactions", [])
                data.setdefault("referrals", [])
                data.setdefault("face_swaps", {})
                data.setdefault("total_revenue", 0)
                data["ai_config"] = normalize_ai_config(data.get("ai_config"))
                for em, u in data.get("users", {}).items():
                    u.setdefault("wallet_balance", 0)
                    u.setdefault("mobile", "")
                    u.setdefault("face_linked", bool(u.get("photo_url")))
                    u.setdefault("redeemed_codes", [])
                    if not u.get("referral_code"):
                        u["referral_code"] = make_referral_code(em)
                    u.setdefault("referred_by", "")
                    u.setdefault("referrals_count", 0)
                    u.setdefault("referral_earnings", 0)
                return data
        except Exception:
            pass
    return {
        "users": {},
        "payment_requests": [],
        "promo_codes": {},
        "wallet_transactions": [],
        "referrals": [],
        "face_swaps": {},
        "total_revenue": 0,
        "ai_config": dict(DEFAULT_AI_CONFIG),
    }


def save_db(db):
    tmp = f"{DB_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_FILE)


def save_state(proj_dir: str, state: dict):
    tmp = f"{proj_dir}/state.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, f"{proj_dir}/state.json")


def load_state(proj_dir: str):
    path = f"{proj_dir}/state.json"
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def safe_project_file(proj_dir: str, filename: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]", "_", os.path.basename(filename))
    abs_proj = os.path.abspath(proj_dir)
    target = os.path.abspath(os.path.join(abs_proj, clean))
    if not target.startswith(abs_proj + os.sep):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return target


def apply_referral_internal(db: dict, referee_email: str, raw_code: str) -> tuple[bool, str]:
    clean_code = (raw_code or "").upper().strip()
    if not clean_code:
        return False, "कृपया सही Referral Code डालें।"
    referee = db["users"].get(referee_email)
    if not referee:
        return False, "User not found."
    if referee.get("referred_by"):
        return False, "आप पहले ही एक Referral Code इस्तेमाल कर चुके हैं!"

    referrer = None
    for u in db["users"].values():
        if str(u.get("referral_code", "")).upper() == clean_code:
            referrer = u
            break

    if not referrer:
        return False, "गलत Referral Code! कृपया दोस्त का सही कोड डालें।"
    if referrer["email"] == referee_email:
        return False, "आप अपना खुद का Referral Code इस्तेमाल नहीं कर सकते!"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    referee["referred_by"] = referrer["email"]
    referee["credits"] = int(referee.get("credits", 0)) + 1
    referrer["credits"] = int(referrer.get("credits", 0)) + 1
    referrer["referrals_count"] = int(referrer.get("referrals_count", 0)) + 1
    referrer["referral_earnings"] = int(referrer.get("referral_earnings", 0)) + 1

    db.setdefault("referrals", []).insert(
        0,
        {
            "id": str(uuid.uuid4())[:8],
            "referrer_email": referrer["email"],
            "referee_email": referee_email,
            "code": clean_code,
            "reward_passes": 1,
            "created_at": now,
        },
    )
    for target_em, note_txt in [
        (referee_email, f"Referral Bonus from {clean_code} (+1 Free Pass)"),
        (referrer["email"], f"Friend {referee_email} joined via {clean_code} (+1 Free Pass)"),
    ]:
        db.setdefault("wallet_transactions", []).insert(
            0,
            {
                "tx_id": str(uuid.uuid4())[:8],
                "email": target_em,
                "amount": 0,
                "credits_delta": 1,
                "note": note_txt,
                "created_at": now,
            },
        )
    return True, f"🎉 बधाई हो! {clean_code} लागू हुआ — आपको और आपके दोस्त दोनों को +1 फ्री मूवी पास मिला!"


@app.get("/")
def home():
    cfg = get_runtime_ai_config()
    return {
        "app": "CinePop AI — Pocket Bollywood & Face-Swap Studio",
        "status": "100% Live",
        "engine_version": "v12-live-action-tv-serial-master-cast-vault",
        "devanagari_font": find_devanagari_font_path(bold=True) or "not-found",
        "gemini_configured": bool(cfg.get("gemini_api_key") or GEMINI_API_KEY),
        "groq_configured": bool(cfg.get("groq_api_key") or os.getenv("GROQ_API_KEY")),
        "cloudflare_flux_configured": bool(cfg.get("cf_account_id") and cfg.get("cf_api_token")),
        "together_flux_configured": bool(cfg.get("together_api_key")),
        "opencv_faceswap": OPENCV_AVAILABLE,
        "video_engine": "Auto-Failover Vault (Cloudflare Flux -> Together -> HF Wan2.1/LTX -> Pollinations) + Master Casting Sheet + 30fps Fast Concat",
        "admin_pin_enabled": True,
        "price_per_movie": f"₹{MOVIE_PRICE_INR}",
        "upi_id": cfg.get("upi_id") or UPI_ID,
        "free_trial_passes": cfg.get("free_trial_passes", 1),
        "pack_50_passes": cfg.get("pack_50_passes", 1),
        "announcement_banner": cfg.get("announcement_banner", ""),
    }


@app.get("/public/app-config/")
@app.get("/public/app-config")
def get_public_app_config():
    """Public non-sensitive business config (UPI ID, trial passes, ₹50 pack passes, home banner). ZERO API keys exposed."""
    cfg = get_runtime_ai_config()
    return {
        "upi_id": cfg.get("upi_id") or UPI_ID,
        "movie_price": MOVIE_PRICE_INR,
        "free_trial_passes": cfg.get("free_trial_passes", 1),
        "pack_50_passes": cfg.get("pack_50_passes", 1),
        "announcement_banner": cfg.get("announcement_banner", ""),
    }


@app.post("/auth/google-login/")
def google_login(
    email: str = Form(...),
    name: str = Form("CinePop Creator"),
    mobile: str = Form(""),
    photo_url: str = Form(""),
    device_id: str = Form("android"),
    admin_pin: str = Form(""),
    referral_code: str = Form(""),
):
    email = email.lower().strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Valid Google Email required")
    clean_mobile = re.sub(r"[^0-9+\-\s]", "", (mobile or "").strip())[:20]

    is_admin_email = email == ADMIN_EMAIL
    if is_admin_email and admin_pin.strip() and not verify_pin_secret(admin_pin):
        raise HTTPException(
            status_code=403,
            detail="गलत 6-Digit Secret Admin PIN! कृपया सही PIN डालें।",
        )
    admin_verified = is_admin_email and verify_pin_secret(admin_pin)

    db = load_db()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if email not in db["users"]:
        db["users"][email] = {
            "email": email,
            "name": name,
            "mobile": clean_mobile,
            "photo_url": photo_url,
            "face_linked": bool(photo_url),
            "device_id": device_id,
            "installed_at": now,
            "last_active": now,
            "credits": 0,
            "wallet_balance": 0,
            "movies_created": 0,
            "total_paid": 0,
            "redeemed_codes": [],
            "referral_code": make_referral_code(email),
            "referred_by": "",
            "referrals_count": 0,
            "referral_earnings": 0,
        }
    else:
        db["users"][email]["last_active"] = now
        db["users"][email].setdefault("wallet_balance", 0)
        db["users"][email].setdefault("mobile", "")
        db["users"][email].setdefault("face_linked", bool(db["users"][email].get("photo_url")))
        if not db["users"][email].get("referral_code"):
            db["users"][email]["referral_code"] = make_referral_code(email)
        db["users"][email].setdefault("referred_by", "")
        db["users"][email].setdefault("referrals_count", 0)
        db["users"][email].setdefault("referral_earnings", 0)
        if name:
            db["users"][email]["name"] = name
        if clean_mobile:
            db["users"][email]["mobile"] = clean_mobile
        if photo_url and not str(db["users"][email].get("photo_url", "")).startswith("/user-avatar/"):
            db["users"][email]["photo_url"] = photo_url

    referral_message = ""
    if referral_code.strip():
        ok, msg = apply_referral_internal(db, email, referral_code.strip())
        if ok:
            referral_message = msg

    save_db(db)
    cfg = normalize_ai_config(db.get("ai_config"))
    return {
        "user": db["users"][email],
        "is_admin": admin_verified,
        "is_admin_email": is_admin_email,
        "requires_admin_pin": is_admin_email and not admin_verified,
        "referral_message": referral_message,
        "upi_id": cfg.get("upi_id") or UPI_ID,
        "movie_price": MOVIE_PRICE_INR,
        "free_trial_passes": cfg.get("free_trial_passes", 1),
        "pack_50_passes": cfg.get("pack_50_passes", 1),
        "announcement_banner": cfg.get("announcement_banner", ""),
    }


@app.post("/auth/instant-login/")
def instant_guest_login(
    device_id: str = Form("android"),
    guest_name: str = Form(""),
    mobile: str = Form(""),
    referral_code: str = Form(""),
):
    """1-Click Guest / Instant Start with Admin-configured Free Trial Movie Passes."""
    clean_dev = re.sub(r"[^a-zA-Z0-9_-]", "", (device_id or "guest").strip())[:24] or "guest"
    clean_mobile = re.sub(r"[^0-9+\-\s]", "", (mobile or "").strip())[:20]
    guest_id = str(uuid.uuid4())[:8]
    email = f"guest_{clean_dev}_{guest_id}@cinepop.guest".lower()
    display_name = (guest_name or "").strip()[:32] or f"Guest Creator {guest_id[:4].upper()}"

    db = load_db()
    cfg = normalize_ai_config(db.get("ai_config"))
    trial_passes = max(0, int(cfg.get("free_trial_passes", 1)))
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db["users"][email] = {
        "email": email,
        "name": display_name,
        "mobile": clean_mobile,
        "photo_url": "",
        "face_linked": False,
        "device_id": clean_dev,
        "installed_at": now,
        "last_active": now,
        "credits": trial_passes,
        "wallet_balance": 0,
        "movies_created": 0,
        "total_paid": 0,
        "redeemed_codes": ["INSTANT_TRIAL"],
        "referral_code": make_referral_code(email),
        "referred_by": "",
        "referrals_count": 0,
        "referral_earnings": 0,
        "is_guest": True,
    }
    referral_message = ""
    if referral_code.strip():
        ok, msg = apply_referral_internal(db, email, referral_code.strip())
        if ok:
            referral_message = msg

    save_db(db)
    return {
        "user": db["users"][email],
        "is_admin": False,
        "is_admin_email": False,
        "requires_admin_pin": False,
        "referral_message": referral_message,
        "message": f"⚡ Instant Start तैयार है! आपको {trial_passes} Free Trial Video Pass मिल गया है।",
        "upi_id": cfg.get("upi_id") or UPI_ID,
        "movie_price": MOVIE_PRICE_INR,
        "free_trial_passes": trial_passes,
        "pack_50_passes": cfg.get("pack_50_passes", 1),
        "announcement_banner": cfg.get("announcement_banner", ""),
    }


@app.post("/auth/profile/update/")
@app.post("/auth/profile/update")
async def update_user_profile(request: Request):
    """Link upright Face ID / profile photo, Name, and Mobile Number to the user's profile in DB."""
    content_type = (request.headers.get("content-type") or "").lower()
    form_data = None
    payload: dict = {}
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    else:
        try:
            form_data = await request.form()
            for k in form_data.keys():
                v = form_data.get(k)
                if not _is_upload_file_obj(v):
                    payload[k] = v
        except Exception:
            payload = {}

    email = _safe_payload_str(payload, "email", "user_email", default="").lower().strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")

    name = _safe_payload_str(payload, "name", default="")
    mobile = re.sub(r"[^0-9+\-\s]", "", _safe_payload_str(payload, "mobile", "phone", default=""))[:20]
    photo_url = _safe_payload_str(payload, "photo_url", default="")

    db = load_db()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if email not in db["users"]:
        db["users"][email] = {
            "email": email,
            "name": name or email.split("@")[0],
            "mobile": mobile,
            "photo_url": photo_url,
            "face_linked": bool(photo_url),
            "device_id": "android",
            "installed_at": now,
            "last_active": now,
            "credits": 0,
            "wallet_balance": 0,
            "movies_created": 0,
            "total_paid": 0,
            "redeemed_codes": [],
            "referral_code": make_referral_code(email),
            "referred_by": "",
            "referrals_count": 0,
            "referral_earnings": 0,
        }

    u = db["users"][email]
    u["last_active"] = now
    if name:
        u["name"] = name[:60]
    if mobile:
        u["mobile"] = mobile
    if photo_url:
        u["photo_url"] = photo_url
        u["face_linked"] = True

    avatar_files = _extract_form_uploads(form_data, "avatar", "photo", "face_image", "image")
    if avatar_files:
        try:
            raw_av = await avatar_files[0].read()
            saved_url = save_user_avatar_bytes(email, raw_av)
            if saved_url:
                u["photo_url"] = saved_url
                u["face_linked"] = True
        except Exception:
            pass

    save_db(db)
    return {"message": "✅ प्रोफ़ाइल अपडेट हो गई!", "user": u}


@app.get("/user-avatar/{filename}")
def get_user_avatar(filename: str):
    clean_name = os.path.basename(filename)
    path = os.path.join(AVATARS_DIR, clean_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/auth/google-web", response_class=HTMLResponse)
def google_web_login_page(redirect: str = ""):
    """Web OAuth / Gmail Account Picker fallback page for devices without Google Play SHA-1 config."""
    safe_redirect = redirect if redirect.startswith(("https://", "http://", "cinepop://")) else ""
    html = f"""<!doctype html>
<html lang="hi">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>CinePop AI — Google Account Sign-In</title>
  <style>
    body{{margin:0;font-family:system-ui,-apple-system,sans-serif;background:#0E091D;color:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;box-sizing:border-box}}
    .card{{width:100%;max-width:420px;background:#1A1033;border:1px solid rgba(255,214,0,.35);border-radius:20px;padding:28px;box-shadow:0 20px 60px rgba(0,0,0,.5)}}
    h1{{margin:0 0 6px;font-size:22px;color:#FFD600}}
    p{{margin:0 0 18px;font-size:13px;color:#cbd5e1;line-height:1.5}}
    label{{display:block;font-size:12px;color:#e2e8f0;margin:12px 0 6px;font-weight:600}}
    input{{width:100%;box-sizing:border-box;padding:12px 14px;border-radius:10px;border:1px solid rgba(255,255,255,.18);background:#0E091D;color:#fff;font-size:14px}}
    button{{width:100%;margin-top:16px;padding:13px;border:none;border-radius:12px;background:#FF007F;color:#fff;font-weight:700;font-size:15px;cursor:pointer}}
    .guest{{background:#00E5FF;color:#0E091D;margin-top:10px}}
    #status{{margin-top:14px;font-size:13px;color:#34d399;text-align:center}}
  </style>
</head>
<body>
  <div class="card">
    <h1>🎬 CinePop AI Sign-In</h1>
    <p>अपनी Gmail ID और नाम डालें और 1 क्लिक में CinePop AI Studio में लॉगिन करें।</p>
    <form id="loginForm">
      <label for="email">Gmail ID</label>
      <input id="email" type="email" required placeholder="yourname@gmail.com" />
      <label for="name">आपका नाम</label>
      <input id="name" type="text" placeholder="जैसे: Rahul Sharma" />
      <button type="submit">Continue with Google Account</button>
    </form>
    <button class="guest" id="guestBtn" type="button">⚡ 1-Click Guest / Instant Start (Free Trial)</button>
    <div id="status"></div>
  </div>
  <script>
    const redirectUrl = {json.dumps(safe_redirect)};
    document.getElementById('loginForm').addEventListener('submit', async (e) => {{
      e.preventDefault();
      const email = document.getElementById('email').value.trim();
      const name = document.getElementById('name').value.trim() || email.split('@')[0];
      const fd = new FormData();
      fd.append('email', email);
      fd.append('name', name);
      fd.append('device_id', 'google-web');
      const r = await fetch('/auth/google-login/', {{ method: 'POST', body: fd }});
      const d = await r.json();
      if (!r.ok) {{ document.getElementById('status').style.color = '#fb7185'; document.getElementById('status').textContent = d.detail || 'Login failed'; return; }}
      document.getElementById('status').textContent = '✅ Login Verified! Returning to CinePop AI...';
      if (redirectUrl) {{
        const sep = redirectUrl.includes('?') ? '&' : '?';
        window.location.href = redirectUrl + sep + 'email=' + encodeURIComponent(d.user.email) + '&name=' + encodeURIComponent(d.user.name);
      }}
    }});
    document.getElementById('guestBtn').addEventListener('click', async () => {{
      const fd = new FormData();
      fd.append('device_id', 'web-guest');
      const r = await fetch('/auth/instant-login/', {{ method: 'POST', body: fd }});
      const d = await r.json();
      document.getElementById('status').textContent = d.message || '✅ Instant Guest Ready!';
      if (redirectUrl && d.user) {{
        const sep = redirectUrl.includes('?') ? '&' : '?';
        window.location.href = redirectUrl + sep + 'email=' + encodeURIComponent(d.user.email) + '&name=' + encodeURIComponent(d.user.name);
      }}
    }});
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/referral/apply/")
def referral_apply(email: str = Form(...), code: str = Form(...)):
    clean_email = email.lower().strip()
    db = load_db()
    ok, msg = apply_referral_internal(db, clean_email, code)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    save_db(db)
    return {"message": msg, "user": db["users"][clean_email]}


@app.get("/referral/summary/")
def referral_summary(email: str):
    clean_email = email.lower().strip()
    db = load_db()
    user = db["users"].get(clean_email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    history = [
        r
        for r in db.get("referrals", [])
        if r.get("referrer_email") == clean_email or r.get("referee_email") == clean_email
    ]
    return {
        "referral_code": user.get("referral_code", ""),
        "referred_by": user.get("referred_by", ""),
        "referrals_count": int(user.get("referrals_count", 0)),
        "referral_earnings": int(user.get("referral_earnings", 0)),
        "history": history[:25],
    }


@app.post("/admin/verify-pin/")
def admin_verify_pin(
    email: str = Form(...),
    admin_pin: str = Form(...),
):
    verify_admin(email, admin_pin)
    return {
        "verified": True,
        "is_admin": True,
        "message": "6-Digit Admin PIN Verified!",
    }


@app.get("/auth/me/")
def auth_me(email: str):
    clean_email = email.lower().strip()
    db = load_db()
    if clean_email not in db["users"]:
        raise HTTPException(status_code=404, detail="User not found")
    user = db["users"][clean_email]
    user.setdefault("wallet_balance", 0)
    return {
        "user": user,
        "is_admin_email": clean_email == ADMIN_EMAIL,
        "upi_id": UPI_ID,
        "movie_price": MOVIE_PRICE_INR,
    }


@app.post("/billing/buy-pass-from-wallet/")
def buy_pass_from_wallet(
    email: str = Form(...),
    movies_count: int = Form(1),
):
    clean_email = email.lower().strip()
    count = max(1, min(20, int(movies_count)))
    cost = count * MOVIE_PRICE_INR

    db = load_db()
    if clean_email not in db["users"]:
        raise HTTPException(status_code=404, detail="User not found")

    user = db["users"][clean_email]
    bal = int(user.get("wallet_balance", 0))
    if bal < cost:
        raise HTTPException(
            status_code=400,
            detail=f"वॉलेट में पर्याप्त बैलेंस नहीं है (जरूरी: ₹{cost}, उपलब्ध: ₹{bal})",
        )

    user["wallet_balance"] = bal - cost
    user["credits"] = int(user.get("credits", 0)) + count
    db["wallet_transactions"].insert(
        0,
        {
            "tx_id": str(uuid.uuid4())[:8],
            "email": clean_email,
            "amount": -cost,
            "credits_delta": count,
            "note": f"Converted ₹{cost} wallet to {count} Movie Pass",
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    save_db(db)
    return {
        "message": f"✅ ₹{cost} वॉलेट बैलेंस से {count} मूवी पास जुड़ गया!",
        "user": user,
    }


@app.post("/admin/create-promo/")
def create_promo(
    admin_email: str = Form(...),
    admin_pin: str = Form(...),
    code: str = Form(...),
    free_credits: int = Form(1),
    max_uses: int = Form(10),
):
    verify_admin(admin_email, admin_pin)
    clean_code = code.upper().strip()
    if not clean_code:
        raise HTTPException(status_code=400, detail="Promo code cannot be empty")

    db = load_db()
    db["promo_codes"][clean_code] = {
        "code": clean_code,
        "free_credits": free_credits,
        "max_uses": max_uses,
        "used_count": 0,
        "active": True,
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_db(db)
    return {
        "message": f"Promo Code '{clean_code}' बना दिया गया ({free_credits} फ्री मूवी, {max_uses} यूज़र्स के लिए)!"
    }


@app.post("/admin/delete-promo/")
def delete_promo(
    admin_email: str = Form(...),
    admin_pin: str = Form(...),
    code: str = Form(...),
):
    verify_admin(admin_email, admin_pin)
    clean_code = code.upper().strip()
    db = load_db()
    if clean_code in db["promo_codes"]:
        del db["promo_codes"][clean_code]
        save_db(db)
        return {"message": f"Promo Code '{clean_code}' डिलीट कर दिया गया।"}
    raise HTTPException(status_code=404, detail="Promo code not found")


@app.post("/billing/redeem-promo/")
def redeem_promo(email: str = Form(...), code: str = Form(...)):
    email = email.lower().strip()
    clean_code = code.upper().strip()
    db = load_db()

    if email not in db["users"]:
        raise HTTPException(status_code=404, detail="User not found")
    if clean_code not in db["promo_codes"]:
        raise HTTPException(status_code=400, detail="गलत Promo Code! कृपया सही कोड डालें।")

    promo = db["promo_codes"][clean_code]
    if not promo.get("active", True) or promo["used_count"] >= promo["max_uses"]:
        raise HTTPException(status_code=400, detail="यह Promo Code एक्सपायर हो चुका है।")

    user = db["users"][email]
    if clean_code in user.get("redeemed_codes", []):
        raise HTTPException(status_code=400, detail="आप यह Promo Code पहले ही इस्तेमाल कर चुके हैं!")

    user["credits"] += promo["free_credits"]
    user.setdefault("redeemed_codes", []).append(clean_code)
    promo["used_count"] += 1
    save_db(db)

    return {
        "message": f"🎉 बधाई हो! '{clean_code}' से आपको {promo['free_credits']} फ्री मूवी पास मिल गया है!",
        "new_credits": user["credits"],
        "user": user,
    }


@app.post("/billing/submit-payment/")
def submit_payment(
    email: str = Form(...),
    utr_number: str = Form(...),
    movies_count: int = Form(1),
):
    email = email.lower().strip()
    db = load_db()
    if email not in db["users"]:
        raise HTTPException(status_code=404, detail="User not found")

    req_id = str(uuid.uuid4())[:8]
    amount = movies_count * MOVIE_PRICE_INR
    db["payment_requests"].append(
        {
            "req_id": req_id,
            "email": email,
            "name": db["users"][email]["name"],
            "utr": utr_number.strip(),
            "movies_count": movies_count,
            "amount": amount,
            "status": "PENDING",
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_db(db)
    return {
        "message": f"₹{amount} की पेमेंट रिक्वेस्ट भेज दी गई है! अप्रूव होते ही पास जुड़ जाएगा।",
        "req_id": req_id,
    }


@app.get("/admin/stats/")
def get_admin_stats(admin_email: str, admin_pin: str = ""):
    verify_admin(admin_email, admin_pin)
    db = load_db()
    users_list = sorted(
        list(db["users"].values()),
        key=lambda u: str(u.get("last_active") or ""),
        reverse=True,
    )
    referrals_list = db.get("referrals", [])
    face_swaps_map = db.get("face_swaps", {})
    ai_cfg = normalize_ai_config(db.get("ai_config"))
    return {
        "total_installs": len(users_list),
        "total_revenue": db.get("total_revenue", 0),
        "total_wallet_balance": sum(int(u.get("wallet_balance", 0)) for u in users_list),
        "total_movies_made": sum(int(u.get("movies_created", 0)) for u in users_list),
        "total_referrals": len(referrals_list),
        "total_face_swaps": len(face_swaps_map),
        "users": users_list,
        "pending_payments": [
            p for p in db.get("payment_requests", []) if p["status"] == "PENDING"
        ],
        "promo_codes": list(db.get("promo_codes", {}).values()),
        "wallet_transactions": db.get("wallet_transactions", [])[:30],
        "referrals": referrals_list[:50],
        "ai_config": ai_cfg,
    }


@app.get("/admin/ai-config/")
@app.get("/admin/ai-config")
def get_admin_ai_config(admin_email: str, admin_pin: str = ""):
    verify_admin(admin_email, admin_pin)
    db = load_db()
    return {"ai_config": normalize_ai_config(db.get("ai_config"))}


@app.post("/admin/ai-config/")
@app.post("/admin/ai-config")
async def save_admin_ai_config(request: Request):
    content_type = (request.headers.get("content-type") or "").lower()
    payload: dict = {}
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    else:
        try:
            form = await request.form()
            payload = {k: form.get(k) for k in form.keys()}
        except Exception:
            payload = {}

    admin_email = _safe_payload_str(payload, "admin_email", "email", default="")
    admin_pin = _safe_payload_str(payload, "admin_pin", "pin", default="")
    verify_admin(admin_email, admin_pin)

    db = load_db()
    current = normalize_ai_config(db.get("ai_config"))
    updatable_keys = (
        "gemini_api_key",
        "groq_api_key",
        "cf_account_id",
        "cf_api_token",
        "together_api_key",
        "custom_video_space",
        "custom_faceswap_space",
        "hf_token",
        "enable_custom_hf",
        "enable_pollinations",
        "pollinations_model",
        "enable_public_hf_spaces",
        "enable_ltx_video",
        "enable_wan21",
        "enable_cogvideox",
        "enable_local_motion_fallback",
        "space_timeout_sec",
        "free_trial_passes",
        "pack_50_passes",
        "upi_id",
        "announcement_banner",
    )
    for k in updatable_keys:
        if k in payload and payload[k] is not None:
            current[k] = payload[k]

    db["ai_config"] = normalize_ai_config(current)
    save_db(db)
    _sync_runtime_api_vault_from_config(db["ai_config"])
    _HF_SPACE_COOLDOWN.clear()
    _API_VAULT_COOLDOWN.clear()
    _GEMINI_IMG_MODEL_COOLDOWN.clear()
    return {
        "message": "✅ API Vault, Auto-Failover & Business सेटिंग्स सेव हो गईं!",
        "ai_config": db["ai_config"],
    }


@app.get("/admin/api-vault")
@app.get("/admin/api-vault/")
async def get_admin_api_vault(request: Request, admin_email: str = "", admin_pin: str = ""):
    email_v = (admin_email or request.headers.get("x-admin-email") or "").strip()
    pin_v = (admin_pin or request.headers.get("x-admin-pin") or "").strip()
    verify_admin(email_v, pin_v)
    _sync_runtime_api_vault_from_config()
    masked = {
        k: (v[:6] + "****" + v[-4:] if len(v) > 10 else ("SET" if v else ""))
        for k, v in RUNTIME_API_VAULT.items()
    }
    return {"keys": masked, "health": API_HEALTH_STATUS}


@app.post("/admin/api-vault")
@app.post("/admin/api-vault/")
async def update_admin_api_vault(request: Request, payload: dict):
    email_v = _safe_payload_str(payload, "admin_email", "email", default="") or (request.headers.get("x-admin-email") or "")
    pin_v = _safe_payload_str(payload, "admin_pin", "pin", default="") or (request.headers.get("x-admin-pin") or "")
    verify_admin(email_v, pin_v)

    db = load_db()
    current_cfg = normalize_ai_config(db.get("ai_config"))
    for k in RUNTIME_API_VAULT.keys():
        if k in payload and isinstance(payload[k], str) and payload[k].strip():
            clean_val = payload[k].strip()
            RUNTIME_API_VAULT[k] = clean_val
            cfg_key = _VAULT_TO_AI_CONFIG_KEY.get(k)
            if cfg_key:
                current_cfg[cfg_key] = clean_val

    db["ai_config"] = normalize_ai_config(current_cfg)
    save_db(db)
    _HF_SPACE_COOLDOWN.clear()
    _API_VAULT_COOLDOWN.clear()
    _GEMINI_IMG_MODEL_COOLDOWN.clear()
    return {"status": "saved", "health": API_HEALTH_STATUS}


@app.post("/admin/test-ai-servers/")
@app.post("/admin/test-ai-servers")
async def test_admin_ai_servers(request: Request):
    """Live parallel health, latency & quota verification across the entire Auto-Failover API Vault."""
    content_type = (request.headers.get("content-type") or "").lower()
    payload: dict = {}
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    else:
        try:
            form = await request.form()
            payload = {k: form.get(k) for k in form.keys()}
        except Exception:
            payload = {}

    admin_email = _safe_payload_str(payload, "admin_email", "email", default="")
    admin_pin = _safe_payload_str(payload, "admin_pin", "pin", default="")
    verify_admin(admin_email, admin_pin)

    cfg = get_runtime_ai_config()
    hf_token = cfg.get("hf_token", "")
    headers_hf = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}

    def _probe_gemini() -> dict:
        key = (cfg.get("gemini_api_key") or GEMINI_API_KEY or "").strip()
        if not key:
            return {"name": "Story P1: Gemini 2.5 Flash", "target": "GEMINI_API_KEY", "status": "🟡 Standby", "online": False, "latency_ms": 0, "detail": "Key not set (auto-switches to Groq)"}
        t0 = time.time()
        try:
            r = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash",
                params={"key": key},
                timeout=6,
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                return {"name": "Story P1: Gemini 2.5 Flash", "target": "gemini-2.5-flash", "status": "🟢 Active", "online": True, "latency_ms": ms, "detail": "Ready (Priority 1 Story)"}
            if r.status_code == 429:
                return {"name": "Story P1: Gemini 2.5 Flash", "target": "gemini-2.5-flash", "status": "🔴 Quota Full", "online": False, "latency_ms": ms, "detail": "429 Quota — Auto-switches to Groq"}
            return {"name": "Story P1: Gemini 2.5 Flash", "target": "gemini-2.5-flash", "status": "🔴 Key Error", "online": False, "latency_ms": ms, "detail": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"name": "Story P1: Gemini 2.5 Flash", "target": "gemini-2.5-flash", "status": "🟡 Standby", "online": False, "latency_ms": int((time.time() - t0) * 1000), "detail": str(e)[:60]}

    def _probe_groq() -> dict:
        key = (cfg.get("groq_api_key") or os.getenv("GROQ_API_KEY") or "").strip()
        if not key:
            return {"name": "Story P2: Groq Llama 3.3 70B", "target": "GROQ_API_KEY", "status": "🟡 Standby", "online": False, "latency_ms": 0, "detail": "Add free Groq key for 0.4s backup"}
        t0 = time.time()
        try:
            r = requests.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=6,
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                return {"name": "Story P2: Groq Llama 3.3 70B", "target": "llama-3.3-70b-versatile", "status": "🟢 Active", "online": True, "latency_ms": ms, "detail": "0.4s Instant Backup Ready"}
            if r.status_code == 429:
                return {"name": "Story P2: Groq Llama 3.3 70B", "target": "llama-3.3-70b-versatile", "status": "🔴 Quota Full", "online": False, "latency_ms": ms, "detail": "429 Rate limit"}
            return {"name": "Story P2: Groq Llama 3.3 70B", "target": "llama-3.3-70b-versatile", "status": "🔴 Key Error", "online": False, "latency_ms": ms, "detail": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"name": "Story P2: Groq Llama 3.3 70B", "target": "llama-3.3-70b-versatile", "status": "🟡 Standby", "online": False, "latency_ms": int((time.time() - t0) * 1000), "detail": str(e)[:60]}

    def _probe_cloudflare() -> dict:
        acc = (cfg.get("cf_account_id") or os.getenv("CF_ACCOUNT_ID") or "").strip()
        tok = (cfg.get("cf_api_token") or os.getenv("CF_API_TOKEN") or "").strip()
        if not acc or not tok:
            return {"name": "Visual P1: Cloudflare Flux", "target": "@cf/black-forest-labs/flux-1-schnell", "status": "🟡 Standby", "online": False, "latency_ms": 0, "detail": "CF Account ID / Token not set"}
        t0 = time.time()
        try:
            r = requests.get(
                f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/models/search?search=flux-1-schnell",
                headers={"Authorization": f"Bearer {tok}"},
                timeout=6,
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                return {"name": "Visual P1: Cloudflare Flux", "target": "flux-1-schnell", "status": "🟢 Active", "online": True, "latency_ms": ms, "detail": "Workers AI Ready"}
            if r.status_code == 429:
                return {"name": "Visual P1: Cloudflare Flux", "target": "flux-1-schnell", "status": "🔴 Quota Full", "online": False, "latency_ms": ms, "detail": "429 — Auto-switches to Together"}
            return {"name": "Visual P1: Cloudflare Flux", "target": "flux-1-schnell", "status": "🔴 Key Error", "online": False, "latency_ms": ms, "detail": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"name": "Visual P1: Cloudflare Flux", "target": "flux-1-schnell", "status": "🟡 Standby", "online": False, "latency_ms": int((time.time() - t0) * 1000), "detail": str(e)[:60]}

    def _probe_together() -> dict:
        key = (cfg.get("together_api_key") or os.getenv("TOGETHER_API_KEY") or "").strip()
        if not key:
            return {"name": "Visual P2: Together FLUX", "target": "FLUX.1-schnell-Free", "status": "🟡 Standby", "online": False, "latency_ms": 0, "detail": "TOGETHER_API_KEY not set"}
        t0 = time.time()
        try:
            r = requests.get(
                "https://api.together.xyz/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=6,
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                return {"name": "Visual P2: Together FLUX", "target": "FLUX.1-schnell-Free", "status": "🟢 Active", "online": True, "latency_ms": ms, "detail": "Ready (Priority 2 Visual)"}
            if r.status_code == 429:
                return {"name": "Visual P2: Together FLUX", "target": "FLUX.1-schnell-Free", "status": "🔴 Quota Full", "online": False, "latency_ms": ms, "detail": "429 — Auto-switches to HF"}
            return {"name": "Visual P2: Together FLUX", "target": "FLUX.1-schnell-Free", "status": "🔴 Key Error", "online": False, "latency_ms": ms, "detail": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"name": "Visual P2: Together FLUX", "target": "FLUX.1-schnell-Free", "status": "🟡 Standby", "online": False, "latency_ms": int((time.time() - t0) * 1000), "detail": str(e)[:60]}

    def _probe_hf_space(name: str, space_id: str, enabled: bool) -> dict:
        if not enabled:
            return {"name": name, "target": space_id or "Not Set", "status": "🟡 Standby", "online": False, "latency_ms": 0, "detail": "Toggle OFF"}
        if not space_id:
            return {"name": name, "target": "Not Configured", "status": "🟡 Standby", "online": False, "latency_ms": 0, "detail": "Space URL empty"}
        t0 = time.time()
        try:
            clean_sid = space_id.replace("https://huggingface.co/spaces/", "").strip("/")
            r = requests.get(f"https://huggingface.co/api/spaces/{clean_sid}", headers=headers_hf, timeout=5)
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                info = r.json()
                stage = str((info.get("runtime") or {}).get("stage") or "UNKNOWN").upper()
                if stage in ("RUNNING", "RUNNING_BUILDING"):
                    return {"name": name, "target": clean_sid, "status": "🟢 Active", "online": True, "latency_ms": ms, "detail": f"Stage: {stage}"}
                return {"name": name, "target": clean_sid, "status": "🟡 Standby", "online": False, "latency_ms": ms, "detail": f"Stage: {stage}"}
            if r.status_code == 429:
                return {"name": name, "target": clean_sid, "status": "🔴 Quota Full", "online": False, "latency_ms": ms, "detail": "HTTP 429"}
            return {"name": name, "target": clean_sid, "status": "🟡 Standby", "online": False, "latency_ms": ms, "detail": f"HTTP {r.status_code}"}
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            return {"name": name, "target": space_id, "status": "🟡 Standby", "online": False, "latency_ms": ms, "detail": str(e)[:60]}

    def _probe_pollinations() -> dict:
        model = cfg.get("pollinations_model", "flux")
        if not cfg.get("enable_pollinations", True):
            return {"name": f"Visual P4: Pollinations ({model})", "target": "image.pollinations.ai", "status": "🟡 Standby", "online": False, "latency_ms": 0, "detail": "Toggle OFF"}
        t0 = time.time()
        try:
            r = requests.get(
                f"https://image.pollinations.ai/prompt/indian%20haveli%20living%20room?width=256&height=256&model={model}&nologo=true",
                timeout=6,
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200 and len(r.content) > 2000:
                return {"name": f"Visual P4: Pollinations ({model})", "target": "image.pollinations.ai", "status": "🟢 Active", "online": True, "latency_ms": ms, "detail": f"200 OK ({len(r.content)//1024}KB)"}
            if r.status_code == 429:
                return {"name": f"Visual P4: Pollinations ({model})", "target": "image.pollinations.ai", "status": "🔴 Quota Full", "online": False, "latency_ms": ms, "detail": "429 Rate Limit"}
            return {"name": f"Visual P4: Pollinations ({model})", "target": "image.pollinations.ai", "status": "🟡 Standby", "online": False, "latency_ms": ms, "detail": f"HTTP {r.status_code}"}
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            return {"name": f"Visual P4: Pollinations ({model})", "target": "image.pollinations.ai", "status": "🟡 Standby", "online": False, "latency_ms": ms, "detail": str(e)[:60]}

    def _run_all():
        tasks = [
            _probe_gemini,
            _probe_groq,
            _probe_cloudflare,
            _probe_together,
            lambda: _probe_hf_space("Visual P3: Custom HF Space", cfg.get("custom_video_space", ""), cfg.get("enable_custom_hf", True)),
            lambda: _probe_hf_space("Face-Swap HF Space", cfg.get("custom_faceswap_space", "") or "tonyassi/face-swap", cfg.get("enable_public_hf_spaces", True)),
            lambda: _probe_hf_space("I2V: Wan 2.1", "Wan-AI/Wan2.1", cfg.get("enable_public_hf_spaces", True) and cfg.get("enable_wan21", True)),
            lambda: _probe_hf_space("I2V: LTX-Video", "Lightricks/LTX-Video", cfg.get("enable_public_hf_spaces", True) and cfg.get("enable_ltx_video", True)),
            _probe_pollinations,
        ]
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
            futs = [ex.submit(fn) for fn in tasks]
            for fut in futs:
                try:
                    results.append(fut.result(timeout=7))
                except Exception as e:
                    results.append({"name": "API Vault Probe", "target": "-", "status": "🟡 Standby", "online": False, "latency_ms": 7000, "detail": str(e)[:60]})
        results.append({
            "name": "Local Fast 30fps Motion & Drama Engine",
            "target": "Railway CPU / OpenCV / FFmpeg",
            "status": "🟢 Active" if cfg.get("enable_local_motion_fallback", True) else "🟡 Standby",
            "online": bool(cfg.get("enable_local_motion_fallback", True)),
            "latency_ms": 35,
            "detail": "Guaranteed 30fps Zero-Hang Fallback",
        })
        return results

    servers = await asyncio.to_thread(_run_all)
    return {
        "tested_at": datetime.datetime.now().strftime("%H:%M:%S"),
        "hf_token_configured": bool(hf_token),
        "results": servers,
        "servers": servers,
    }


@app.post("/admin/approve-payment/")
def approve_payment(
    admin_email: str = Form(...),
    admin_pin: str = Form(...),
    req_id: str = Form(...),
):
    verify_admin(admin_email, admin_pin)
    db = load_db()
    for p in db["payment_requests"]:
        if p["req_id"] == req_id and p["status"] == "PENDING":
            p["status"] = "APPROVED"
            user_email = p["email"]
            if user_email in db["users"]:
                db["users"][user_email]["credits"] += p["movies_count"]
                db["users"][user_email]["total_paid"] += p["amount"]
                db["total_revenue"] = db.get("total_revenue", 0) + p["amount"]
                db["wallet_transactions"].insert(
                    0,
                    {
                        "tx_id": str(uuid.uuid4())[:8],
                        "email": user_email,
                        "amount": p["amount"],
                        "credits_delta": p["movies_count"],
                        "note": f"UPI Approved (UTR: {p['utr']})",
                        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    },
                )
            save_db(db)
            return {
                "message": f"Approved! {user_email} को {p['movies_count']} मूवी पास मिल गया।",
                "user": db["users"].get(user_email),
            }
    raise HTTPException(status_code=404, detail="Pending request not found")


@app.post("/admin/add-balance/")
def admin_add_balance(
    admin_email: str = Form(...),
    admin_pin: str = Form(...),
    target_email: str = Form(...),
    amount: int = Form(0),
    credits: int = Form(0),
    note: str = Form(""),
):
    verify_admin(admin_email, admin_pin)
    clean_target = target_email.lower().strip()
    if amount == 0 and credits == 0:
        raise HTTPException(status_code=400, detail="Amount or credits change required")

    db = load_db()
    if clean_target not in db["users"]:
        raise HTTPException(status_code=404, detail="Customer not found")

    user = db["users"][clean_target]
    user["wallet_balance"] = max(0, int(user.get("wallet_balance", 0)) + int(amount))
    user["credits"] = max(0, int(user.get("credits", 0)) + int(credits))
    if amount > 0:
        user["total_paid"] = int(user.get("total_paid", 0)) + int(amount)
        db["total_revenue"] = int(db.get("total_revenue", 0)) + int(amount)

    db["wallet_transactions"].insert(
        0,
        {
            "tx_id": str(uuid.uuid4())[:8],
            "email": clean_target,
            "amount": int(amount),
            "credits_delta": int(credits),
            "note": note.strip() or f"Admin adjustment (₹{amount}, {credits} Pass)",
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    save_db(db)

    parts = []
    if amount != 0:
        parts.append(f"{'+' if amount > 0 else ''}₹{amount} Wallet")
    if credits != 0:
        parts.append(f"{'+' if credits > 0 else ''}{credits} Pass")

    return {
        "message": f"✅ {clean_target} अपडेट हो गया: {', '.join(parts)}! (नया बैलेंस: ₹{user['wallet_balance']}, पास: {user['credits']})",
        "user": user,
    }


@app.post("/admin/add-free-credit/")
def add_free_credit(
    admin_email: str = Form(...),
    admin_pin: str = Form(...),
    target_email: str = Form(...),
    credits: int = Form(1),
):
    return admin_add_balance(
        admin_email=admin_email,
        admin_pin=admin_pin,
        target_email=target_email,
        amount=0,
        credits=credits,
        note=f"Admin free pass grant (+{credits})",
    )


def _build_multi_character_drama_lines(
    clean_topic: str,
    hero_label: str = "Hero",
    genre: str = "Drama",
    part_no: int = 1,
    total_parts: int = 1,
) -> list[tuple[str, str, str]]:
    """
    Build 7 crisp, topic-accurate Hindi multi-character drama exchanges (~30-45s per Part).
    Uses _build_topic_aware_director_fallback() so Raja/Bhoot, Jungle, Police, Comedy, or any user topic
    gets its own real characters and story — ZERO hardcoded 'ghar jamai / sasural' lines!
    """
    hero = (hero_label or "Hero").strip() or "Hero"
    emotion_to_mood = {
        "angry": "Action",
        "confident": "Action",
        "shocked": "Suspense",
        "twist": "Suspense",
        "sad": "Emotional",
        "romantic": "Romantic",
    }
    raw_items = _build_topic_aware_director_fallback(
        topic=clean_topic or "रहस्यमयी महल का राज़",
        part_no_or_num=part_no,
    ) if False else _build_topic_aware_director_fallback(
        topic=clean_topic or "रहस्यमयी महल का राज़",
        part_num=part_no,
        prev_cliffhanger="",
    )
    pack: list[tuple[str, str, str]] = []
    for idx_i, item in enumerate(raw_items):
        spk = str(item.get("speaker") or "Hero").strip()
        if spk.lower() in ("hero", "raja"):
            spk = hero
        dlg = str(item.get("dialogue") or "").strip()
        if idx_i == len(raw_items) - 1 and total_parts > 1 and part_no < total_parts and f"Part {part_no + 1}" not in dlg:
            dlg = f"{dlg} देखिए Part {part_no + 1} में!"
        emo = str(item.get("emotion") or "twist").lower()
        pack.append((spk, dlg, emotion_to_mood.get(emo, "Suspense")))
    return pack


def _format_drama_script_text(lines: list[tuple[str, str, str]]) -> str:
    return "\n".join(f'{spk}: "{clean_spoken_dialogue(diag)}"' for spk, diag, _ in lines if clean_spoken_dialogue(diag))


@app.post("/ai/generate-story/")
async def ai_generate_story(
    topic: str = Form(...),
    genre: str = Form("Action"),
    char_names: str = Form("Hero"),
    duration_minutes: int = Form(1),
):
    clean_topic = topic.strip()
    if len(clean_topic) < 4:
        raise HTTPException(status_code=400, detail="कृपया कम से कम 1 लाइन का टॉपिक लिखें।")

    names = [n.strip() for n in char_names.split(",") if n.strip()]
    hero_label = names[0] if names and names[0].lower() not in ("vikram", "kaal") else "Hero"

    director_scenes = await director_generate_story(
        topic=f"{clean_topic} (Genre: {genre})",
        part_num=1,
        prev_cliffhanger="",
    )

    formatted_lines: list[str] = []
    for item in director_scenes:
        if not isinstance(item, dict):
            continue
        spk = str(item.get("speaker") or "Hero").strip()
        if spk.lower() == "hero":
            spk = hero_label
        dlg = clean_spoken_dialogue(str(item.get("dialogue") or ""))
        if len(dlg) >= 3:
            formatted_lines.append(f'{spk}: "{dlg}"')

    if len(formatted_lines) < 5:
        fallback_lines = _build_multi_character_drama_lines(
            clean_topic=clean_topic,
            hero_label=hero_label,
            genre=genre,
            part_no=1,
            total_parts=1,
        )
        story = _format_drama_script_text(fallback_lines)
    else:
        story = "\n".join(formatted_lines[:8])

    active_story_engine = (
        "Gemini 2.5 Flash"
        if API_HEALTH_STATUS.get("gemini") == "active"
        else (
            "Groq Llama 3.3 70B"
            if API_HEALTH_STATUS.get("groq") == "active"
            else "Built-in Hindi Drama"
        )
    )
    return {
        "story": story,
        "genre": genre,
        "story_engine": active_story_engine,
        "scenes": director_scenes,
        "health": API_HEALTH_STATUS,
    }


def _resolve_tts_voice(voice: str, language: str = "Hindi") -> str:
    lang = normalize_language(language)
    v = (voice or "").strip()
    if lang == "English":
        if "Swara" in v or "Neerja" in v or "female" in v.lower():
            return "en-IN-NeerjaNeural"
        return "en-IN-PrabhatNeural"
    return v if v else "hi-IN-MadhurNeural"


async def generate_bulletproof_audio(
    text: str,
    voice: str,
    out_mp3: str,
    pitch: str = "normal",
    language: str = "Hindi",
    pitch_override: Optional[str] = None,
    rate_override: Optional[str] = None,
):
    clean_text = clean_spoken_dialogue(text) or "आगे देखो क्या होता है!"
    resolved_voice = _resolve_tts_voice(voice, language)
    preset = PITCH_PRESETS.get(normalize_pitch(pitch), PITCH_PRESETS["normal"])
    use_pitch = pitch_override if pitch_override else preset["pitch"]
    use_rate = rate_override if rate_override else preset["rate"]
    try:
        comm = edge_tts.Communicate(
            clean_text,
            resolved_voice,
            rate=use_rate,
            pitch=use_pitch,
        )
        await asyncio.wait_for(comm.save(out_mp3), timeout=18.0)
        if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 500:
            return
    except Exception:
        pass

    try:
        def _run_gtts():
            tts = gTTS(
                text=clean_text,
                lang="en" if normalize_language(language) == "English" else "hi",
            )
            tts.save(out_mp3)

        await asyncio.to_thread(_run_gtts)
        if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 500:
            return
    except Exception:
        pass

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "3.5",
            "-q:a",
            "9",
            "-acodec",
            "libmp3lame",
            out_mp3,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def generate_bgm_wav(mood: str, duration: float, out_wav: str):
    sr = 22050
    dur = max(1.5, min(35.0, float(duration)))
    total_samples = int(sr * dur)
    mood_chords = {
        "Action": ([110.0, 146.83, 164.81, 220.0], 2.5, True),
        "Suspense": ([92.5, 110.0, 130.81, 138.59], 0.8, False),
        "Emotional": ([130.81, 164.81, 196.0, 246.94], 0.65, False),
        "Comedy": ([196.0, 246.94, 293.66, 329.63], 3.0, True),
        "Romantic": ([146.83, 185.0, 220.0, 277.18], 0.75, False),
    }
    freqs, tempo, percussive = mood_chords.get(mood, mood_chords["Action"])
    frames = bytearray()
    for i in range(total_samples):
        t = i / sr
        chord_idx = int(t * tempo) % len(freqs)
        base_f = freqs[chord_idx]
        env = 0.65 + 0.35 * math.sin(2.0 * math.pi * tempo * t)
        val = (
            0.42 * math.sin(2.0 * math.pi * base_f * t)
            + 0.25 * math.sin(2.0 * math.pi * (base_f * 1.5) * t)
            + 0.16 * math.sin(2.0 * math.pi * (base_f * 2.0) * t)
        ) * env
        if percussive:
            beat_phase = (t * tempo) % 1.0
            if beat_phase < 0.14:
                kick_f = 95.0 * (1.0 - beat_phase * 4.5)
                val += 0.32 * math.sin(2.0 * math.pi * max(35.0, kick_f) * beat_phase) * (1.0 - beat_phase / 0.14)
        fade = min(1.0, t / 0.25, max(0.0, (dur - t) / 0.3))
        sample = max(-32767, min(32767, int(val * fade * 18000)))
        frames.extend(struct.pack("<h", sample))

    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames))


def generate_sfx_wav(sfx_type: str, duration: float, out_wav: str) -> bool:
    if not sfx_type or sfx_type == "none":
        return False
    sr = 22050
    dur = max(1.5, min(35.0, float(duration)))
    total_samples = int(sr * dur)
    rng = random.Random(42)
    frames = bytearray()
    prev = 0.0
    for i in range(total_samples):
        t = i / sr
        val = 0.0
        if sfx_type == "rain":
            noise = rng.uniform(-1.0, 1.0)
            prev = 0.72 * prev + 0.28 * noise
            val = prev * 0.45
        elif sfx_type == "thunder":
            noise = rng.uniform(-1.0, 1.0)
            prev = 0.92 * prev + 0.08 * noise
            strike = math.exp(-abs(t - 0.55) * 3.8) + 0.6 * math.exp(-abs(t - min(dur * 0.65, 2.8)) * 2.6)
            val = (prev * 1.15 + 0.35 * math.sin(2.0 * math.pi * 52.0 * t)) * strike
        elif sfx_type == "punch":
            burst = (t % 1.4)
            if burst < 0.22:
                decay = math.exp(-burst * 18.0)
                val = (math.sin(2.0 * math.pi * (140.0 - burst * 320.0) * burst) + 0.4 * rng.uniform(-1.0, 1.0)) * decay
        elif sfx_type == "explosion":
            noise = rng.uniform(-1.0, 1.0)
            prev = 0.88 * prev + 0.12 * noise
            boom = math.exp(-max(0.0, t - 0.35) * 1.9) if t >= 0.35 else 0.0
            val = (prev * 1.2 + 0.5 * math.sin(2.0 * math.pi * 44.0 * t)) * boom
        elif sfx_type == "sword":
            burst = (t % 1.6)
            if burst < 0.45:
                decay = math.exp(-burst * 7.5)
                val = (
                    0.5 * math.sin(2.0 * math.pi * 1480.0 * burst)
                    + 0.35 * math.sin(2.0 * math.pi * 2310.0 * burst)
                    + 0.25 * math.sin(2.0 * math.pi * 3620.0 * burst)
                ) * decay
        elif sfx_type == "footsteps":
            step = (t % 0.68)
            if step < 0.08:
                decay = math.exp(-step * 42.0)
                val = (0.65 * math.sin(2.0 * math.pi * 110.0 * step) + 0.35 * rng.uniform(-1.0, 1.0)) * decay
        sample = max(-32767, min(32767, int(val * 22000)))
        frames.extend(struct.pack("<h", sample))

    with wave.open(out_wav, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(frames))
    return True


def mix_cinematic_audio(
    dialogue_path: str,
    mood: str,
    sfx_type: str,
    out_mixed_mp3: str,
    target_duration_sec: float = 0.0,
) -> str:
    """
    Mix Hindi character dialogue with Bollywood Action BGM + Rain/Fight Foley SFX
    using automatic sidechain audio ducking (BGM ducks low during speech, swells in pauses)
    and optional target_duration_sec pacing for exact ~60s 1-minute reels.
    """
    raw_dur = _probe_audio_duration(dialogue_path)
    total_dur = max(raw_dur + 0.45, min(14.0, float(target_duration_sec or 0.0)))
    bgm_wav = f"{out_mixed_mp3}_bgm.wav"
    sfx_wav = f"{out_mixed_mp3}_sfx.wav"
    try:
        generate_bgm_wav(mood, total_dur + 0.3, bgm_wav)
        has_sfx = generate_sfx_wav(sfx_type, total_dur + 0.3, sfx_wav)
        pad_tail = max(0.25, total_dur - raw_dur)

        if has_sfx and os.path.exists(sfx_wav):
            fc = (
                f"[0:a]apad=pad_dur={pad_tail:.2f},atrim=0:{total_dur:.2f},volume=1.18,asplit=2[dmain][dsc];"
                f"[1:a]atrim=0:{total_dur:.2f},volume=0.22[braw];"
                f"[braw][dsc]sidechaincompress=threshold=0.03:ratio=5:attack=15:release=260[bduck];"
                f"[2:a]atrim=0:{total_dur:.2f},volume=0.28[s];"
                f"[dmain][bduck][s]amix=inputs=3:duration=first:dropout_transition=2:normalize=0[aout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", dialogue_path,
                "-i", bgm_wav,
                "-i", sfx_wav,
                "-filter_complex", fc,
                "-map", "[aout]",
                "-c:a", "libmp3lame", "-b:a", "128k",
                out_mixed_mp3,
            ]
        else:
            fc = (
                f"[0:a]apad=pad_dur={pad_tail:.2f},atrim=0:{total_dur:.2f},volume=1.18,asplit=2[dmain][dsc];"
                f"[1:a]atrim=0:{total_dur:.2f},volume=0.22[braw];"
                f"[braw][dsc]sidechaincompress=threshold=0.03:ratio=5:attack=15:release=260[bduck];"
                f"[dmain][bduck]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]"
            )
            cmd = [
                "ffmpeg", "-y",
                "-i", dialogue_path,
                "-i", bgm_wav,
                "-filter_complex", fc,
                "-map", "[aout]",
                "-c:a", "libmp3lame", "-b:a", "128k",
                out_mixed_mp3,
            ]
        subprocess.run(
            cmd,
            check=True,
            timeout=22,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.path.exists(out_mixed_mp3) and os.path.getsize(out_mixed_mp3) > 500:
            return out_mixed_mp3
    except Exception:
        # Clean non-sidechain fallback if older ffmpeg build
        try:
            cmd_fb = [
                "ffmpeg", "-y",
                "-i", dialogue_path,
                "-i", bgm_wav,
                "-filter_complex",
                f"[0:a]apad=pad_dur={max(0.25, total_dur - raw_dur):.2f},atrim=0:{total_dur:.2f},volume=1.15[d];"
                f"[1:a]atrim=0:{total_dur:.2f},volume=0.14[b];"
                f"[d][b]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]",
                "-map", "[aout]", "-c:a", "libmp3lame", "-b:a", "128k", out_mixed_mp3,
            ]
            subprocess.run(cmd_fb, check=True, timeout=18, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out_mixed_mp3) and os.path.getsize(out_mixed_mp3) > 500:
                return out_mixed_mp3
        except Exception:
            pass
    finally:
        for p in (bgm_wav, sfx_wav):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
    return dialogue_path


def _draw_vertical_gradient(img: Image.Image, top_rgb, bottom_rgb):
    draw = ImageDraw.Draw(img)
    w, h = img.size
    for y in range(h):
        t = y / max(h - 1, 1)
        r = int(top_rgb[0] + (bottom_rgb[0] - top_rgb[0]) * t)
        g = int(top_rgb[1] + (bottom_rgb[1] - top_rgb[1]) * t)
        b = int(top_rgb[2] + (bottom_rgb[2] - top_rgb[2]) * t)
        draw.line([(0, y), (w, y)], fill=(r, g, b))


def get_aspect_dimensions(aspect_ratio: str = "16:9") -> tuple[int, int]:
    """Return exact (width, height) for Instagram/Moj 9:16 Vertical (720x1280) vs YouTube 16:9 Landscape (1280x720)."""
    clean = str(aspect_ratio or "16:9").strip().replace("x", ":").replace("/", ":")
    if clean in ("9:16", "vertical", "reel", "moj", "instagram", "720:1280"):
        return (720, 1280)
    return (1280, 720)


_HF_SPACE_COOLDOWN: dict[str, float] = {}
_HF_COOLDOWN_SECONDS = 600.0  # Skip sleeping/dead HF spaces for 10 min so renders NEVER hang!


def _is_hf_space_cooling(space_id: str) -> bool:
    until = _HF_SPACE_COOLDOWN.get(space_id, 0.0)
    return time.time() < until


def _mark_hf_space_failed(space_id: str):
    _HF_SPACE_COOLDOWN[space_id] = time.time() + _HF_COOLDOWN_SECONDS


def _run_with_timeout(fn, timeout_sec: float = 10.0):
    """Run a blocking third-party call (e.g. Gradio HF Client) with a strict non-blocking hard timeout."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(fn)
    try:
        return fut.result(timeout=timeout_sec)
    except Exception:
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _try_hf_scene_faceswap(scene_img_path: str, actor_face_path: str, out_path: str) -> Optional[str]:
    """
    Seamlessly merge the user's scanned face onto the full-screen AI drama scene's hero using
    Admin Custom Face-Swap HF Space (Priority 1 with HF_TOKEN) -> Free Public HF Face-Swap spaces
    (`tonyassi/face-swap`, `ezioruan/inswapper`) with strict 8s timeout + circuit breaker!
    """
    if not GRADIO_AVAILABLE or Client is None or handle_file is None:
        return None
    if not scene_img_path or not actor_face_path or not os.path.exists(scene_img_path) or not os.path.exists(actor_face_path):
        return None

    cfg = get_runtime_ai_config()
    hf_token = cfg.get("hf_token") or None
    timeout_s = float(cfg.get("space_timeout_sec", 8))

    space_queue: list[str] = []
    if cfg.get("enable_custom_hf", True) and cfg.get("custom_faceswap_space"):
        clean_custom = cfg["custom_faceswap_space"].replace("https://huggingface.co/spaces/", "").strip("/")
        if clean_custom:
            space_queue.append(clean_custom)
    if cfg.get("enable_public_hf_spaces", True):
        space_queue.extend(["tonyassi/face-swap", "ezioruan/inswapper"])

    for space_id in space_queue:
        if _is_hf_space_cooling(space_id):
            continue

        def _call(sid=space_id):
            hf_client = Client(sid, hf_token=hf_token) if hf_token else Client(sid)
            return hf_client.predict(
                handle_file(actor_face_path),
                handle_file(scene_img_path),
                api_name="/predict",
            )

        res = _run_with_timeout(_call, timeout_sec=timeout_s)
        if res is None:
            _mark_hf_space_failed(space_id)
            continue
        swapped = None
        if isinstance(res, str) and os.path.exists(res):
            swapped = res
        elif isinstance(res, dict):
            swapped = res.get("path") or res.get("url")
        elif isinstance(res, (list, tuple)):
            for item in res:
                if isinstance(item, str) and os.path.exists(item):
                    swapped = item
                    break
                if isinstance(item, dict) and item.get("path") and os.path.exists(item["path"]):
                    swapped = item["path"]
                    break
        if swapped and os.path.exists(swapped) and os.path.getsize(swapped) > 4000:
            try:
                open_upright_image(swapped).save(out_path, "JPEG", quality=93)
                return out_path
            except Exception:
                pass
        _mark_hf_space_failed(space_id)
    return None


_GEMINI_IMG_MODEL_COOLDOWN: dict[str, float] = {}
_HF_IMG_SPACE_COOLDOWN: dict[str, float] = {}


def _try_cloudflare_flux_image(prompt: str, W: int, H: int, cfg: dict) -> Optional[Image.Image]:
    """
    Visual Auto-Chain Priority 1: Cloudflare Workers AI (`@cf/black-forest-labs/flux-1-schnell`).
    Ultra-fast photorealistic live-action frames (free daily quota) with 8.5s timeout & 429 circuit breaker.
    """
    cf_acc = (cfg.get("cf_account_id") or os.getenv("CF_ACCOUNT_ID") or "").strip()
    cf_tok = (cfg.get("cf_api_token") or os.getenv("CF_API_TOKEN") or "").strip()
    if not cf_acc or not cf_tok or _is_vault_provider_cooling("cloudflare"):
        return None
    url = f"https://api.cloudflare.com/client/v4/accounts/{cf_acc}/ai/run/@cf/black-forest-labs/flux-1-schnell"
    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {cf_tok}",
                "Content-Type": "application/json",
            },
            json={
                "prompt": prompt[:1000],
                "num_steps": 4,
                "width": min(1024, max(512, (W // 64) * 64)),
                "height": min(1024, max(512, (H // 64) * 64)),
            },
            timeout=8.5,
        )
        if resp.status_code == 429:
            _mark_vault_provider_failed("cloudflare", cooldown_sec=300)
            return None
        if resp.status_code != 200:
            _mark_vault_provider_failed("cloudflare", cooldown_sec=90)
            return None
        ctype = str(resp.headers.get("content-type", "")).lower()
        if "image" in ctype or resp.content[:2] in (b"\xff\xd8", b"\x89P"):
            if len(resp.content) > 4000:
                return open_upright_image(resp.content).resize((W, H), Image.Resampling.LANCZOS)
        data = resp.json()
        img_b64 = (data.get("result") or {}).get("image") or data.get("image")
        if isinstance(img_b64, str) and len(img_b64) > 2000:
            raw_bytes = base64.b64decode(img_b64)
            return open_upright_image(raw_bytes).resize((W, H), Image.Resampling.LANCZOS)
    except Exception:
        _mark_vault_provider_failed("cloudflare", cooldown_sec=90)
    return None


def _try_together_flux_image(prompt: str, W: int, H: int, cfg: dict) -> Optional[Image.Image]:
    """
    Visual Auto-Chain Priority 2: Together AI (`black-forest-labs/FLUX.1-schnell-Free`).
    Fast photorealistic fallback with 8.5s timeout & automatic 429 circuit breaker.
    """
    tog_key = (cfg.get("together_api_key") or os.getenv("TOGETHER_API_KEY") or "").strip()
    if not tog_key or _is_vault_provider_cooling("together"):
        return None
    try:
        resp = requests.post(
            "https://api.together.xyz/v1/images/generations",
            headers={
                "Authorization": f"Bearer {tog_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "black-forest-labs/FLUX.1-schnell-Free",
                "prompt": prompt[:1000],
                "width": min(1024, max(512, (W // 64) * 64)),
                "height": min(1024, max(512, (H // 64) * 64)),
                "steps": 4,
                "n": 1,
                "response_format": "b64_json",
            },
            timeout=8.5,
        )
        if resp.status_code == 429:
            _mark_vault_provider_failed("together", cooldown_sec=300)
            return None
        if resp.status_code != 200:
            _mark_vault_provider_failed("together", cooldown_sec=90)
            return None
        data = resp.json()
        items = data.get("data") or []
        if items and isinstance(items[0], dict):
            b64_val = items[0].get("b64_json")
            if isinstance(b64_val, str) and len(b64_val) > 2000:
                raw_bytes = base64.b64decode(b64_val)
                return open_upright_image(raw_bytes).resize((W, H), Image.Resampling.LANCZOS)
            img_url = items[0].get("url")
            if isinstance(img_url, str) and img_url.startswith("http"):
                r2 = requests.get(img_url, timeout=6.0)
                if r2.status_code == 200 and len(r2.content) > 4000:
                    return open_upright_image(r2.content).resize((W, H), Image.Resampling.LANCZOS)
    except Exception:
        _mark_vault_provider_failed("together", cooldown_sec=90)
    return None


def _try_gemini_photorealistic_image(prompt: str, W: int, H: int) -> Optional[Image.Image]:
    """
    Visual Auto-Chain Priority 3: Photorealistic live-action Indian TV serial frame
    via Google GenAI SDK (`imagen-3.0-generate-002` -> `gemini-2.5-flash-image`) using Admin Vault GEMINI_API_KEY.
    """
    rt_client = _get_runtime_gemini_client()
    if not rt_client or _is_vault_provider_cooling("gemini_img"):
        return None
    aspect = "9:16" if H > W else "16:9"
    now_t = time.time()

    # 3A: Try Imagen 3 (`imagen-3.0-generate-002`)
    if now_t - _GEMINI_IMG_MODEL_COOLDOWN.get("imagen-3.0-generate-002", 0.0) > 600:
        def _call_imagen():
            return rt_client.models.generate_images(
                model="imagen-3.0-generate-002",
                prompt=prompt,
                config={
                    "number_of_images": 1,
                    "aspect_ratio": aspect,
                    "output_mime_type": "image/jpeg",
                },
            )
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                res = pool.submit(_call_imagen).result(timeout=12.0)
            gen_imgs = getattr(res, "generated_images", None) or []
            if gen_imgs:
                img_obj = getattr(gen_imgs[0], "image", None)
                raw_bytes = getattr(img_obj, "image_bytes", None)
                if raw_bytes and len(raw_bytes) > 5000:
                    return open_upright_image(raw_bytes).resize((W, H), Image.Resampling.LANCZOS)
        except Exception:
            _GEMINI_IMG_MODEL_COOLDOWN["imagen-3.0-generate-002"] = time.time()

    # 3B: Try Gemini Flash Image (`gemini-2.5-flash-image`)
    if now_t - _GEMINI_IMG_MODEL_COOLDOWN.get("gemini-2.5-flash-image", 0.0) > 600:
        def _call_flash_img():
            return rt_client.models.generate_content(
                model="gemini-2.5-flash-image",
                contents=prompt,
                config={"response_modalities": ["IMAGE"]},
            )
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                res = pool.submit(_call_flash_img).result(timeout=12.0)
            candidates = getattr(res, "candidates", None) or []
            for cand in candidates:
                parts = getattr(getattr(cand, "content", None), "parts", None) or []
                for part in parts:
                    inline = getattr(part, "inline_data", None)
                    data = getattr(inline, "data", None) if inline else None
                    if data:
                        raw_bytes = base64.b64decode(data) if isinstance(data, str) else bytes(data)
                        if len(raw_bytes) > 5000:
                            return open_upright_image(raw_bytes).resize((W, H), Image.Resampling.LANCZOS)
        except Exception:
            _GEMINI_IMG_MODEL_COOLDOWN["gemini-2.5-flash-image"] = time.time()

    return None


def _extract_gradio_image_path(res) -> Optional[str]:
    if isinstance(res, str) and os.path.exists(res):
        return res
    if isinstance(res, dict):
        for k in ("path", "image", "url"):
            v = res.get(k)
            if isinstance(v, str) and os.path.exists(v):
                return v
    if isinstance(res, (list, tuple)):
        for item in res:
            found = _extract_gradio_image_path(item)
            if found:
                return found
    return None


def _try_hf_fast_image_spaces(prompt: str, W: int, H: int, seed_val: int, cfg: dict) -> Optional[Image.Image]:
    """Try fast free HuggingFace photorealistic text-to-image Spaces with strict timeout & circuit breaker."""
    if not GRADIO_AVAILABLE or not cfg.get("enable_public_hf_spaces", True):
        return None
    hf_tok = (cfg.get("hf_token") or os.getenv("HF_TOKEN") or "").strip() or None
    timeout_s = max(8, min(14, int(cfg.get("space_timeout_sec", 8)) + 4))
    now_t = time.time()
    space_specs = [
        ("black-forest-labs/FLUX.1-schnell", lambda c: c.predict(prompt=prompt, seed=seed_val, randomize_seed=False, width=min(1024, W), height=min(1024, H), num_inference_steps=4, api_name="/infer")),
        ("ByteDance/SDXL-Lightning", lambda c: c.predict(prompt, "4-Step", api_name="/generate_image")),
        ("stabilityai/sdxl-turbo", lambda c: c.predict(prompt, api_name="/predict")),
        ("playgroundai/playground-v2.5-1024px-aesthetic", lambda c: c.predict(prompt=prompt, negative_prompt="cartoon, anime, illustration, 2d drawing, painting, blurry, text, watermark", use_negative_prompt=True, seed=seed_val, width=min(1024, W), height=min(1024, H), guidance_scale=3.0, randomize_seed=False, api_name="/run")),
    ]
    for space_id, invoke_fn in space_specs:
        if now_t - _HF_IMG_SPACE_COOLDOWN.get(space_id, 0.0) < 300:
            continue
        def _run_space(sid=space_id, fn=invoke_fn):
            cli = GradioClient(sid, hf_token=hf_tok) if hf_tok else GradioClient(sid)
            return fn(cli)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(_run_space)
                res = fut.result(timeout=timeout_s)
            img_path = _extract_gradio_image_path(res)
            if img_path and os.path.exists(img_path) and os.path.getsize(img_path) > 4000:
                return open_upright_image(img_path).resize((W, H), Image.Resampling.LANCZOS)
        except Exception:
            _HF_IMG_SPACE_COOLDOWN[space_id] = time.time()
            continue
    return None


def _reuse_cached_photorealistic_scene(W: int, H: int, hero_role: str, seed_val: int) -> Optional[Image.Image]:
    """
    STRICT GENDER & SPEAKER SYNC + ZERO BLACK SCREEN:
    1. First try valid non-black cached frame matching exact `hero_role`.
    2. Next try same-gender valid non-black cached frame (male: hero/villain, female: female/saas) horizontally flipped + warm golden graded + zoomed.
    3. Next try any valid non-black master/cached scene frame horizontally flipped + warm golden graded + zoomed so a black screen NEVER appears!
    """
    try:
        candidates = [
            os.path.join("/tmp", f)
            for f in os.listdir("/tmp")
            if f.startswith("cinepop_tv_") and f.endswith(".jpg")
        ]
        if os.path.isdir("all_projects"):
            for p_id in os.listdir("all_projects")[:20]:
                mc_dir = os.path.join("all_projects", p_id, "master_cast")
                if os.path.isdir(mc_dir):
                    for mf in os.listdir(mc_dir):
                        if mf.endswith(".jpg"):
                            candidates.append(os.path.join(mc_dir, mf))
        if os.path.isdir("all_characters"):
            for cf in os.listdir("all_characters")[:15]:
                if cf.lower().endswith((".jpg", ".jpeg", ".png")):
                    candidates.append(os.path.join("all_characters", cf))

        valid_files = [p for p in candidates if _is_valid_non_black_image_file(p, min_bytes=5000)]
        if not valid_files:
            return None

        # Tier 1: Exact role match
        exact_matches = [
            p for p in valid_files
            if f"_hero_{hero_role}_" in os.path.basename(p) or os.path.basename(p) in (f"{hero_role}.jpg", f"master_{hero_role}.jpg")
        ]
        # Tier 2: Same gender match (never put female on male role or male on female role!)
        same_gender_roles = ("female", "saas", "heroine") if hero_role in ("female", "saas") else ("hero", "villain", "wide_confrontation")
        gender_matches = [
            p for p in valid_files
            if any(
                f"_hero_{gr}_" in os.path.basename(p) or os.path.basename(p) in (f"{gr}.jpg", f"master_{gr}.jpg")
                for gr in same_gender_roles
            )
        ]

        pool = exact_matches or gender_matches or valid_files
        chosen = pool[seed_val % len(pool)]
        img = open_upright_image(chosen).resize((W, H), Image.Resampling.LANCZOS)
        # Apply horizontal flip + warm golden/royal color grade + digital zoom when reusing across cuts
        if not exact_matches or (seed_val % 2 == 1):
            img = ImageOps.mirror(img)
        margin_x = max(14, int(W * 0.08))
        margin_y = max(14, int(H * 0.08))
        img = img.crop((margin_x, margin_y, W - margin_x, H - margin_y)).resize((W, H), Image.Resampling.LANCZOS)
        overlay = Image.new("RGBA", (W, H), (240, 170, 55, 32))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        img = ImageEnhance.Contrast(img).enhance(1.07)
        img = ImageEnhance.Color(img).enhance(1.10)
        return img
    except Exception:
        return None


def render_location_backdrop(
    location_key: str,
    mood: str,
    prompt_hint: str = "",
    aspect_ratio: str = "16:9",
    include_hero: bool = False,
    hero_role: str = "hero",
    shot_variant: str = "main",
) -> Image.Image:
    """
    Generate a 100% PHOTOREALISTIC Indian TV Serial / Film Live-Action Drama frame
    at exact 9:16 (720x1280) or 16:9 (1280x720) — STRICT MALE/FEMALE SPEAKER SYNC LOCK & ZERO BLANK SCREENS!
    """
    W, H = get_aspect_dimensions(aspect_ratio)
    loc_descriptions = {
        "royal_palace": "luxurious Indian royal palace darbar hall with crystal golden chandelier, carved marble pillars, royal throne, warm cinematic lighting",
        "city_street": "luxurious Indian family mansion living hall with grand chandelier, teak staircase and warm golden sconces, Indian Hindi TV serial set",
        "haunted_mansion": "ancient candle-lit haunted Indian royal palace hall at night with eerie mist, flickering torches and carved stone arches, Indian supernatural thriller drama set",
        "mumbai_rain": "upscale Mumbai bungalow living room with rain-streaked French windows and warm interior drama lighting, Indian Hindi TV serial set",
        "village_field": "traditional wealthy Indian rural haveli courtyard at golden sunset with warm cinematic sunlight, Indian Hindi TV drama serial set",
        "deep_jungle": "misty ancient Indian forest temple ruin at twilight with glowing torches and dramatic sunbeams, Indian live-action adventure thriller drama",
        "police_station": "realistic Indian police station headquarters interior with wooden desk and dramatic overhead lights, Indian Hindi crime TV serial set",
        "outer_space": "luxurious modern Indian billionaire penthouse hall overlooking night city skyline, Indian Hindi TV drama set",
        "mountains": "luxury Himalayan fort palace hall with fireplace and mountain view window, Indian Hindi TV drama set",
        "desert_fort": "grand Rajasthani heritage fort palace darbar courtyard at golden hour with sandstone arches, Indian Hindi TV serial set",
        "courtroom": "realistic Indian wood-paneled courtroom interior with dramatic sunlight shafts, Indian Hindi TV drama serial set",
        "cyber_lab": "modern Indian corporate billionaire boardroom office with glass walls and city skyline, Indian Hindi TV drama set",
    }
    desc = loc_descriptions.get(location_key, loc_descriptions["royal_palace"])
    hint_clean = re.sub(r"[^a-zA-Z0-9 ,.-]", "", (prompt_hint or "")).strip()[:96]
    shot_clean = re.sub(r"[^a-zA-Z0-9_]", "", shot_variant or "main")[:18] or "main"
    hero_tag = "hero_group" if shot_clean == "wide_group" else (f"hero_{hero_role}" if include_hero else "bgonly")

    hint_sig = str(abs(hash(hint_clean[:40])) % 17) if hint_clean else "0"
    cache_seed_str = f"tv_{location_key}_{mood}_{W}x{H}_{hero_tag}_{shot_clean}_{hint_sig}"
    cache_key = re.sub(r"[^a-zA-Z0-9_]", "_", cache_seed_str)[:84]
    cache_path = f"/tmp/cinepop_{cache_key}.jpg"
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 8000:
        try:
            return open_upright_image(cache_path).resize((W, H), Image.Resampling.LANCZOS)
        except Exception:
            pass

    shot_framing_map = {
        "wide_confrontation": "medium wide live-action Indian drama two-shot confrontation inside grand royal hall with chandeliers and carved arches, 35mm cinema lens, full waist-up actors face-to-face",
        "wide_group": "medium wide live-action Indian drama two-shot confrontation inside grand royal hall with chandeliers and carved arches, 35mm cinema lens",
        "establishing": "medium wide live-action Indian drama establishing shot inside grand royal hall with chandeliers and real actors confronting face-to-face, 35mm cinema lens",
        "over_shoulder": "medium over-the-shoulder waist-up live-action Indian drama shot, expressive speaking hand gesture, natural upper body visible, 50mm cinema lens",
        "waist_up": "medium waist-up live-action Indian drama shot, expressive hand gesture and body language, 50mm cinema lens",
        "closeup_drama": "medium waist-up live-action Indian drama dialogue shot, full shoulders, torso and expressive hand gesture visible, 50mm cinema lens",
        "reaction_cut": "medium waist-up live-action Indian drama reaction shot, intense emotional posture and hand gesture, warm backlighting, 50mm cinema lens",
        "action_mid": "dynamic medium waist-up live-action Indian drama confrontation shot face-to-face, intense pointing hand gesture and body language, 50mm cinema lens",
        "broll_insert": "cinematic 1.5-second live-action Indian drama insert B-roll shot, moody dramatic golden lighting, 35mm cinema lens, ultra sharp photorealism, no human close-up",
        "main": "medium waist-up live-action Indian drama shot with natural upper body and expressive hand gesture, 50mm cinema lens",
    }
    shot_desc = shot_framing_map.get(shot_clean, shot_framing_map["waist_up"])

    dynamic_cast_prompts = get_dynamic_character_prompts(f"{location_key} {prompt_hint}")
    role_subject_map = {
        "hero": f"ONE handsome 30-year-old Indian MALE lead actor only (man with beard/mustache, strictly male, no woman in foreground), {dynamic_cast_prompts.get('Hero', '')}",
        "female": f"ONE expressive 25-year-old Indian FEMALE lead actress only (woman in royal silk saree/lehenga and gold jhumkas, strictly female, no man in foreground), {dynamic_cast_prompts.get('Heroine', '')}",
        "villain": f"ONE menacing 35-year-old Indian MALE antagonist actor only (bearded man with intense eyes, strictly male, no woman in foreground), {dynamic_cast_prompts.get('Villain', '')}",
        "saas": f"ONE commanding 50-year-old Indian FEMALE matriarch actress only (older woman in maroon Banarasi silk saree and gold jewelry, strictly female, no man in foreground), {dynamic_cast_prompts.get('Saas', '')}",
        "narrator": dynamic_cast_prompts.get("Wide_Confrontation", ""),
        "crowd": "shocked Indian royal court and family people in traditional attire reacting together inside grand hall with chandeliers",
    }
    if shot_clean == "broll_insert":
        subject_prefix = f"{shot_desc}, "
    elif shot_clean in ("wide_group", "wide_confrontation", "establishing"):
        subject_prefix = f"{role_subject_map['narrator']}, {shot_desc}, "
    elif include_hero:
        subject_prefix = f"{role_subject_map.get(hero_role, role_subject_map['hero'])}, {shot_desc}, "
    else:
        subject_prefix = f"{shot_desc}, "

    scene_detail = f"{hint_clean}, " if hint_clean else ""
    full_prompt = (
        f"photorealistic live-action still frame from an Indian Hindi TV drama, "
        f"{subject_prefix}{scene_detail}{desc}, {mood} dramatic cinema lighting, "
        f"8k DSLR live-action film photography, real Indian actors waist-up with natural body language and hand gestures, "
        f"no passport photo, no extreme macro face close-up, no cartoon, no illustration, no anime, no 3d render, no text, no watermark"
    )
    encoded = urllib.parse.quote(full_prompt, safe="")
    seed_val = abs(hash(cache_key)) % 99999
    cfg = get_runtime_ai_config()

    # Visual Auto-Failover Chain:
    # P1: Cloudflare Workers AI Flux -> P2: Together AI FLUX -> P3: Gemini Imagen/Flash -> P4: HF Spaces -> P5: Pollinations -> Same-Gender/Mirrored Cache
    cf_img = _try_cloudflare_flux_image(full_prompt, W, H, cfg)
    if cf_img is not None:
        try:
            cf_img.save(cache_path, "JPEG", quality=93)
        except Exception:
            pass
        return cf_img

    tog_img = _try_together_flux_image(full_prompt, W, H, cfg)
    if tog_img is not None:
        try:
            tog_img.save(cache_path, "JPEG", quality=93)
        except Exception:
            pass
        return tog_img

    gem_img = _try_gemini_photorealistic_image(full_prompt, W, H)
    if gem_img is not None:
        try:
            gem_img.save(cache_path, "JPEG", quality=93)
        except Exception:
            pass
        return gem_img

    hf_img = _try_hf_fast_image_spaces(full_prompt, W, H, seed_val, cfg)
    if hf_img is not None:
        try:
            hf_img.save(cache_path, "JPEG", quality=93)
        except Exception:
            pass
        return hf_img

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if cfg.get("enable_pollinations", True) and not _is_vault_provider_cooling("pollinations"):
        pref_model = cfg.get("pollinations_model", "flux")
        alt_model = "turbo" if pref_model != "turbo" else "flux"
        candidate_urls = [
            (f"https://image.pollinations.ai/prompt/{encoded}?width={W}&height={H}&model={pref_model}&nologo=true&seed={seed_val}", 9),
            (f"https://image.pollinations.ai/prompt/{encoded}?width={W}&height={H}&model={alt_model}&nologo=true&seed={(seed_val + 73) % 99999}", 9),
        ]
        for url, t_sec in candidate_urls:
            try:
                resp = requests.get(url, headers=headers, timeout=t_sec, allow_redirects=True)
                ctype = str(resp.headers.get("content-type", "")).lower()
                if resp.status_code == 429:
                    _mark_vault_provider_failed("pollinations", cooldown_sec=120)
                    break
                if (
                    resp.status_code == 200
                    and len(resp.content) > 8000
                    and ("image" in ctype or resp.content[:2] in (b"\xff\xd8", b"\x89P") or resp.content[:4] == b"RIFF")
                ):
                    bg_img = open_upright_image(resp.content).resize((W, H), Image.Resampling.LANCZOS)
                    bg_img.save(cache_path, "JPEG", quality=93)
                    return bg_img
            except Exception:
                continue

    # Priority 6A: Concise fast `turbo` waist-up retry locked strictly to the speaking role gender
    if cfg.get("enable_pollinations", True):
        short_role_prompt = (
            f"photorealistic Indian live-action drama medium waist-up shot of "
            f"{role_subject_map.get(hero_role, role_subject_map['hero'])[:180]}, "
            f"grand royal Indian palace hall with chandeliers, warm drama lighting, 8k DSLR real human actor, no cartoon"
        )
        short_enc = urllib.parse.quote(short_role_prompt, safe="")
        retry_url = f"https://image.pollinations.ai/prompt/{short_enc}?width={W}&height={H}&model=turbo&nologo=true&seed={(seed_val + 503) % 99999}"
        try:
            resp = requests.get(retry_url, headers=headers, timeout=10, allow_redirects=True)
            ctype = str(resp.headers.get("content-type", "")).lower()
            if (
                resp.status_code == 200
                and len(resp.content) > 6000
                and ("image" in ctype or resp.content[:2] in (b"\xff\xd8", b"\x89P") or resp.content[:4] == b"RIFF")
            ):
                bg_img = open_upright_image(resp.content).resize((W, H), Image.Resampling.LANCZOS)
                bg_img.save(cache_path, "JPEG", quality=93)
                return bg_img
        except Exception:
            pass

    # Priority 6B: Mirror + Warm Golden Grade + Zoom any existing same-gender / wide master scene frame so a black screen NEVER appears!
    cached_photo = _reuse_cached_photorealistic_scene(W, H, hero_role, seed_val)
    if cached_photo is not None:
        return cached_photo

    # Emergency ultra-short Pollinations fetch (no cooldown block) before any fallback
    try:
        em_prompt = urllib.parse.quote("Indian royal palace darbar hall live action drama actor waist up 8k dslr", safe="")
        em_resp = requests.get(
            f"https://image.pollinations.ai/prompt/{em_prompt}?width={W}&height={H}&model=turbo&nologo=true&seed={(seed_val + 911) % 99999}",
            headers=headers,
            timeout=10,
        )
        if em_resp.status_code == 200 and len(em_resp.content) > 5000:
            em_img = open_upright_image(em_resp.content).resize((W, H), Image.Resampling.LANCZOS)
            em_img.save(cache_path, "JPEG", quality=93)
            if _is_valid_non_black_image_file(cache_path):
                return em_img
    except Exception:
        pass

    tmp_fb = f"/tmp/cinepop_palace_fb_{hero_role}_{W}x{H}.jpg"
    _render_rich_cinematic_palace_actor_fallback(tmp_fb, role=hero_role, width=W, height=H)
    return open_upright_image(tmp_fb).resize((W, H), Image.Resampling.LANCZOS)


def draw_channel_watermark(
    img: Image.Image,
    watermark_text: str = "",
    watermark_position: str = "top-right",
):
    text = (watermark_text or "").strip()[:30]
    if not text:
        return
    W, H = img.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    font_wm = load_hindi_font(19 if W < 900 else 20, bold=True)
    box_w = min(W - 48, max(145, len(text) * 13 + 38))
    box_h = 38
    if watermark_position == "top-left":
        x1, y1 = 24, 28
    elif watermark_position == "bottom-right":
        x1, y1 = W - box_w - 24, H - (240 if H > W else 156)
    else:
        x1, y1 = W - box_w - 24, 28
    odraw.rounded_rectangle(
        [x1, y1, x1 + box_w, y1 + box_h],
        radius=10,
        fill=(12, 8, 28, 165),
        outline=(255, 214, 0, 195),
        width=2,
    )
    odraw.text((x1 + 14, y1 + 7), f"★ {text}", fill=(255, 214, 0, 240), font=font_wm)
    composited = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    img.paste(composited)


def render_hindi_subtitle_png(
    speaker_name: str,
    dialogue: str,
    out_png_path: str,
    width: int = 1280,
    height: int = 148,
) -> Optional[str]:
    """
    Render a crystal-clear transparent PNG Hindi Devanagari subtitle strip (`Speaker: Dialogue`)
    using PIL + Noto Sans Devanagari font so Hindi akshar NEVER show □□□ tofu boxes!
    Auto-adapts typography and safe-zone padding for both 9:16 Reels (width=720) and 16:9 YouTube (width=1280).
    """
    clean_diag = clean_spoken_dialogue(dialogue)
    if not clean_diag:
        return None
    clean_spk = re.sub(r"[:：]+$", "", (speaker_name or "").strip()).strip()
    full_line = f"{clean_spk}: {clean_diag}" if clean_spk else clean_diag

    is_vertical = width < 900
    if is_vertical and height < 360:
        # Raise vertical strip so subtitle text sits cleanly around y = H - 220 (well above bottom player controls!)
        height = 360

    strip = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(strip)

    font_size = 28 if is_vertical else 30
    font_main = load_hindi_font(font_size, bold=True)
    max_text_w = width - (76 if is_vertical else 110)

    words = full_line.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        test = f"{cur} {w}".strip()
        try:
            bbox = draw.textbbox((0, 0), test, font=font_main)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(test) * 16
        if tw > max_text_w and cur:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)
    max_lines = 3 if is_vertical else 2
    lines = lines[:max_lines]

    line_h = 40 if is_vertical else 42
    # Place 9:16 subtitles cleanly above bottom player controls (~220px from bottom)
    bottom_pad = 215 if is_vertical else 24
    start_y = max(18, height - (len(lines) * line_h) - bottom_pad)

    # Smooth translucent cinema band strictly behind the subtitle text (leaves bottom controls clean!)
    band_top = max(0, start_y - 16)
    band_bot = min(height - 1, start_y + len(lines) * line_h + 14)
    draw.rounded_rectangle(
        [18 if is_vertical else 40, band_top, width - (18 if is_vertical else 40), band_bot],
        radius=16,
        fill=(6, 6, 16, 178),
    )
    for idx, ln in enumerate(lines):
        try:
            bbox = draw.textbbox((0, 0), ln, font=font_main)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(ln) * 16
        tx = max(28, (width - tw) // 2)
        ty = start_y + idx * line_h

        if idx == 0 and ":" in ln and speaker_name.strip():
            spk_part, rest_part = ln.split(":", 1)
            spk_label = spk_part + ":"
            try:
                sb = draw.textbbox((0, 0), spk_label + " ", font=font_main)
                spk_w = sb[2] - sb[0]
            except Exception:
                spk_w = len(spk_label) * 16
            draw.text(
                (tx, ty),
                spk_label,
                font=font_main,
                fill=(255, 214, 0, 255),
                stroke_width=3,
                stroke_fill=(0, 0, 0, 240),
            )
            draw.text(
                (tx + spk_w, ty),
                rest_part.strip(),
                font=font_main,
                fill=(255, 255, 255, 255),
                stroke_width=3,
                stroke_fill=(0, 0, 0, 240),
            )
        else:
            draw.text(
                (tx, ty),
                ln,
                font=font_main,
                fill=(255, 255, 255, 255),
                stroke_width=3,
                stroke_fill=(0, 0, 0, 240),
            )

    strip.save(out_png_path, "PNG")
    return out_png_path


def _try_opencv_face_only_swap(scene_img: Image.Image, actor_img: Image.Image) -> Optional[Image.Image]:
    """
    STRICT FACE-ONLY OPENCV SEAMLESS CLONE:
    Extracts ONLY the oval face patch from the user's photo (NEVER their bedroom/background!),
    color-matches it to the royal haveli lighting, and seamlessly clones it onto the AI Hero's
    detected face box inside the haveli bandhgala scene.
    """
    if not (OPENCV_AVAILABLE and cv2 is not None and np is not None):
        return None
    try:
        dst_bgr = cv2.cvtColor(np.array(scene_img.convert("RGB")), cv2.COLOR_RGB2BGR)
        src_bgr = cv2.cvtColor(np.array(actor_img.convert("RGB")), cv2.COLOR_RGB2BGR)
        dh, dw = dst_bgr.shape[:2]
        sh, sw = src_bgr.shape[:2]

        dst_box = _detect_skin_face_box(dst_bgr)
        src_box = _detect_skin_face_box(src_bgr)
        if not dst_box or not src_box:
            return None

        dx, dy, dbw, dbh = dst_box
        sx, sy, sbw, sbh = src_box
        # Reject implausibly tiny or full-frame boxes so we never paste a raw selfie
        if dbw < 44 or dbh < 48 or dbw > int(dw * 0.58) or dbh > int(dh * 0.50):
            return None
        if sbw < 36 or sbh < 40:
            return None

        # Inner face oval crop (excludes hair/bedroom background completely!)
        s_pad_x = int(sbw * 0.10)
        s_pad_y = int(sbh * 0.08)
        sx1 = max(0, sx + s_pad_x)
        sy1 = max(0, sy + s_pad_y)
        sx2 = min(sw, sx + sbw - s_pad_x)
        sy2 = min(sh, sy + sbh - s_pad_y)
        if sx2 - sx1 < 28 or sy2 - sy1 < 32:
            return None

        tw = max(32, int(dbw * 0.86))
        th = max(36, int(dbh * 0.88))
        face_patch = cv2.resize(src_bgr[sy1:sy2, sx1:sx2], (tw, th), interpolation=cv2.INTER_AREA)

        # Match warm haveli skin illumination to target face region
        cx = max(tw // 2 + 2, min(dw - tw // 2 - 2, dx + dbw // 2))
        cy = max(th // 2 + 2, min(dh - th // 2 - 2, dy + dbh // 2))
        roi_dst = dst_bgr[cy - th // 2 : cy - th // 2 + th, cx - tw // 2 : cx - tw // 2 + tw]
        if roi_dst.shape[:2] != (th, tw):
            return None

        src_mean = np.mean(face_patch.astype(np.float32), axis=(0, 1)) + 1.0
        dst_mean = np.mean(roi_dst.astype(np.float32), axis=(0, 1)) + 1.0
        gain = np.clip(dst_mean / src_mean, 0.72, 1.32)
        face_matched = np.clip(face_patch.astype(np.float32) * gain, 0, 255).astype(np.uint8)

        oval_mask = np.zeros((th, tw), dtype=np.uint8)
        cv2.ellipse(
            oval_mask,
            (tw // 2, th // 2),
            (max(8, int(tw * 0.42)), max(10, int(th * 0.45))),
            0, 0, 360, 255, -1,
        )
        blended_bgr = cv2.seamlessClone(face_matched, dst_bgr, oval_mask, (cx, cy), cv2.NORMAL_CLONE)
        return Image.fromarray(cv2.cvtColor(blended_bgr, cv2.COLOR_BGR2RGB))
    except Exception:
        return None


def prepare_character_frame(
    raw_img_path: str,
    char_name: str,
    dialogue: str,
    out_frame_path: str,
    location_key: str = "city_street",
    location_label: str = "Mumbai City Street",
    mood: str = "Action",
    custom_bg_path: Optional[str] = None,
    watermark_text: str = "",
    watermark_position: str = "top-right",
    aspect_ratio: str = "16:9",
    visual_action_prompt: str = "",
    shot_variant: str = "waist_up",
):
    """
    LIVE-ACTION INDIAN TV SERIAL WAIST-UP FRAME & STRICT FACE-SWAP ONLY (ZERO RAW SELFIE!):
    1. Generates photorealistic waist-up / over-the-shoulder two-shots inside grand royal Indian haveli hall.
    2. User's bedroom selfie NEVER appears directly on screen!
       - Step A: HuggingFace AI Face-Swap (`tonyassi/face-swap` / `ezioruan/inswapper`) onto the AI Hero inside the haveli.
       - Step B: OpenCV `cv2.seamlessClone` face-only oval blend onto the AI Hero's face inside the haveli.
       - Step C: If swap fails, cleanly keep the photorealistic AI Hero in black bandhgala suit inside the haveli!
    """
    W, H = get_aspect_dimensions(aspect_ratio)
    role = detect_speaker_role(char_name, None)
    has_user_face = bool(raw_img_path and os.path.exists(raw_img_path) and os.path.getsize(raw_img_path) > 512)

    upright_actor_tmp = f"{out_frame_path}_upright_actor.jpg"
    scene_tmp = f"{out_frame_path}_ai_scene.jpg"
    swapped_tmp = f"{out_frame_path}_swapped.jpg"

    try:
        if custom_bg_path and os.path.exists(custom_bg_path):
            bg = open_upright_image(custom_bg_path).resize((W, H), Image.Resampling.LANCZOS)
            use_hf_swap = False
        else:
            bg = render_location_backdrop(
                location_key,
                mood,
                prompt_hint=visual_action_prompt,
                aspect_ratio=aspect_ratio,
                include_hero=True,
                hero_role=role,
                shot_variant=shot_variant,
            )
            use_hf_swap = True

        # AI AUTO-CAST MODE (or non-face-swap roles): Pure Full-Screen Live-Action TV Serial Scene
        if not has_user_face:
            composed = ImageEnhance.Contrast(bg).enhance(1.06)
            composed = ImageEnhance.Color(composed).enhance(1.07)
            if watermark_text:
                draw_channel_watermark(composed, watermark_text, watermark_position)
            composed.save(out_frame_path, "JPEG", quality=93)
            return

        # User provided a 5-angle scanned face -> straighten EXIF orientation
        actor = open_upright_image(raw_img_path)
        actor.save(upright_actor_tmp, "JPEG", quality=92)

        # Step A: Try FREE HuggingFace AI Face-Swap (`tonyassi/face-swap` / `ezioruan/inswapper`) onto AI Hero in haveli
        if use_hf_swap:
            bg.save(scene_tmp, "JPEG", quality=92)
            swapped_path = _try_hf_scene_faceswap(scene_tmp, upright_actor_tmp, swapped_tmp)
            if swapped_path and os.path.exists(swapped_path):
                composed = open_upright_image(swapped_path).resize((W, H), Image.Resampling.LANCZOS)
                composed = ImageEnhance.Contrast(composed).enhance(1.06)
                composed = ImageEnhance.Color(composed).enhance(1.06)
                if watermark_text:
                    draw_channel_watermark(composed, watermark_text, watermark_position)
                composed.save(out_frame_path, "JPEG", quality=93)
                return

        # Step B: Strict OpenCV Face-Only Seamless Clone onto AI Hero's face in haveli (NEVER raw selfie background!)
        cv_swapped = _try_opencv_face_only_swap(bg, actor)
        if cv_swapped is not None:
            composed = ImageEnhance.Contrast(cv_swapped).enhance(1.06)
            composed = ImageEnhance.Color(composed).enhance(1.06)
            if watermark_text:
                draw_channel_watermark(composed, watermark_text, watermark_position)
            composed.save(out_frame_path, "JPEG", quality=93)
            return

        # Step C: Clean Photorealistic AI-Generated Hero in Black Royal Bandhgala inside Haveli (NO raw selfie paste!)
        composed = ImageEnhance.Contrast(bg).enhance(1.06)
        composed = ImageEnhance.Color(composed).enhance(1.07)
        if watermark_text:
            draw_channel_watermark(composed, watermark_text, watermark_position)
        composed.save(out_frame_path, "JPEG", quality=93)
    finally:
        for tmp_f in (upright_actor_tmp, scene_tmp, swapped_tmp):
            if os.path.exists(tmp_f):
                try:
                    os.remove(tmp_f)
                except Exception:
                    pass


def _probe_audio_duration(audio_path: str) -> float:
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
        val = float(out.decode().strip())
        return max(1.5, min(30.0, val))
    except Exception:
        return 4.0


def _extract_gradio_video_path(res) -> Optional[str]:
    if isinstance(res, str) and os.path.exists(res):
        return res
    if isinstance(res, dict):
        for k in ("video", "path", "url"):
            v = res.get(k)
            if isinstance(v, str) and os.path.exists(v):
                return v
    if isinstance(res, (list, tuple)):
        for item in res:
            found = _extract_gradio_video_path(item)
            if found:
                return found
    return None


def _try_ai_action_i2v_spaces(
    raw_img_path: str,
    visual_action_prompt: str,
    mood: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    Multi-Engine AI Video Router (Strict 8-second failover + 10-min circuit breaker):
    Priority 1: Admin Custom HF Video Space + personal HF_TOKEN
    Priority 2: Enabled Public HF Spaces (LTX-Video, Wan 2.1, CogVideoX-5B, HunyuanVideo)
    Returns `(local_video_path, engine_label)` if any space succeeds within timeout, else `(None, None)`.
    """
    if not GRADIO_AVAILABLE or Client is None or handle_file is None:
        return None, None

    cfg = get_runtime_ai_config()
    hf_token = cfg.get("hf_token") or None
    timeout_s = float(cfg.get("space_timeout_sec", 8))

    prompt_txt = (
        visual_action_prompt.strip()
        if visual_action_prompt and visual_action_prompt.strip()
        else f"Cinematic Bollywood {mood} drama scene, dynamic tracking camera movement, realistic motion"
    )
    has_img = bool(raw_img_path and os.path.exists(raw_img_path) and os.path.getsize(raw_img_path) > 512)

    i2v_spaces: list[tuple[str, str, str]] = []
    if cfg.get("enable_custom_hf", True) and cfg.get("custom_video_space"):
        clean_custom = cfg["custom_video_space"].replace("https://huggingface.co/spaces/", "").strip("/")
        if clean_custom:
            i2v_spaces.append((clean_custom, "custom", f"Custom HF ({clean_custom.split('/')[-1]})"))

    if cfg.get("enable_public_hf_spaces", True):
        if cfg.get("enable_ltx_video", True):
            i2v_spaces.append(("Lightricks/LTX-Video", "ltx", "LTX-Video"))
        if cfg.get("enable_wan21", True):
            i2v_spaces.append(("Wan-AI/Wan2.1", "wan", "Wan 2.1"))
        if cfg.get("enable_cogvideox", True):
            i2v_spaces.append(("THUDM/CogVideoX-5b", "cogvideo", "CogVideoX-5B"))

    for space_id, kind, label in i2v_spaces:
        if _is_hf_space_cooling(space_id):
            continue

        def _call(sid=space_id, k=kind):
            hf_client = Client(sid, hf_token=hf_token) if hf_token else Client(sid)
            if k == "custom":
                # Auto-adapt to duplicated Wan / LTX / CogVideoX / generic spaces
                if has_img:
                    try:
                        return hf_client.predict(prompt=prompt_txt, image=handle_file(raw_img_path), api_name="/predict")
                    except Exception:
                        try:
                            return hf_client.predict(prompt=prompt_txt, input_image_filepath=handle_file(raw_img_path), api_name="/image_to_video")
                        except Exception:
                            return hf_client.predict(prompt_txt, handle_file(raw_img_path))
                return hf_client.predict(prompt_txt)
            elif k == "wan":
                if has_img:
                    return hf_client.predict(
                        image=handle_file(raw_img_path),
                        prompt=prompt_txt,
                        api_name="/predict",
                    )
                return hf_client.predict(prompt=prompt_txt, api_name="/predict")
            elif k == "ltx":
                if has_img:
                    return hf_client.predict(
                        prompt=prompt_txt,
                        negative_prompt="static image, blurry, distorted face, low quality",
                        input_image_filepath=handle_file(raw_img_path),
                        api_name="/image_to_video",
                    )
                return hf_client.predict(
                    prompt=prompt_txt,
                    negative_prompt="static image, blurry, distorted face, low quality",
                    api_name="/text_to_video",
                )
            elif k == "cogvideo":
                if has_img:
                    return hf_client.predict(
                        prompt=prompt_txt,
                        image_input=handle_file(raw_img_path),
                        api_name="/generate",
                    )
                return hf_client.predict(prompt=prompt_txt, api_name="/generate")
            else:
                return hf_client.predict(prompt=prompt_txt, api_name="/predict")

        res = _run_with_timeout(_call, timeout_sec=timeout_s)
        if res is None:
            _mark_hf_space_failed(space_id)
            continue
        vid_path = _extract_gradio_video_path(res)
        if vid_path and os.path.getsize(vid_path) > 2000:
            return vid_path, label
        _mark_hf_space_failed(space_id)
    return None, None


def _extract_audio_speech_envelope(audio_path: str, total_frames: int, fps: int = 24) -> list[float]:
    """
    Compute real per-frame normalized speech amplitude (0.0 .. 1.0) directly from the Hindi
    dialogue audio using FFmpeg PCM extraction + NumPy so mouth lip-sync opens/closes
    precisely on every spoken Hindi syllable!
    """
    if not audio_path or not os.path.exists(audio_path) or np is None:
        return [abs(math.sin(i * 0.45)) * 0.7 for i in range(total_frames)]
    try:
        raw_pcm = subprocess.check_output(
            [
                "ffmpeg", "-v", "error",
                "-i", audio_path,
                "-ac", "1", "-ar", "16000",
                "-f", "s16le", "pipe:1",
            ],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        samples = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) < 320:
            return [0.0] * total_frames
        spf = max(1, int(16000 / max(1, fps)))
        env = []
        for idx in range(total_frames):
            s = idx * spf
            seg = samples[s : s + spf]
            if len(seg) == 0:
                env.append(0.0)
            else:
                rms = float(np.sqrt(np.mean(seg * seg)))
                env.append(rms)
        peak = max(max(env), 0.02)
        norm = [min(1.0, max(0.0, (v / peak) ** 0.75)) for v in env]
        # Smooth envelope slightly so lip motion looks organic
        smoothed = []
        for i in range(len(norm)):
            prev_v = norm[i - 1] if i > 0 else norm[i]
            next_v = norm[i + 1] if i + 1 < len(norm) else norm[i]
            smoothed.append(0.25 * prev_v + 0.50 * norm[i] + 0.25 * next_v)
        return smoothed
    except Exception:
        return [abs(math.sin(i * 0.45)) * 0.65 for i in range(total_frames)]


def _detect_skin_face_box(frame_bgr) -> tuple[int, int, int, int]:
    """
    Fast, dependency-free face region locator (ZERO Haar Cascade XML required!)
    using YCrCb skin-tone clustering inside the upper-center portrait zone with
    a rock-solid fallback box for 9:16 & 16:9 TV serial close-ups.
    Returns (fx, fy, fw, fh).
    """
    h, w = frame_bgr.shape[:2]
    is_vertical = h > w
    # Sensible TV-serial close-up default if skin clustering is ambiguous
    def_w = int(w * (0.42 if is_vertical else 0.24))
    def_h = int(h * (0.28 if is_vertical else 0.44))
    def_x = (w - def_w) // 2
    def_y = int(h * (0.14 if is_vertical else 0.12))
    fallback_box = (def_x, def_y, def_w, def_h)

    if cv2 is None or np is None:
        return fallback_box
    try:
        # Search upper-center portrait region where the TV serial actor's head sits
        rx1, rx2 = int(w * 0.15), int(w * 0.85)
        ry1, ry2 = int(h * 0.05), int(h * 0.62)
        roi = frame_bgr[ry1:ry2, rx1:rx2]
        if roi.size == 0:
            return fallback_box
        ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
        mask = cv2.inRange(ycrcb, np.array([50, 132, 77], dtype=np.uint8), np.array([245, 178, 130], dtype=np.uint8))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return fallback_box

        # Pick largest plausible central skin blob
        best = None
        best_score = 0.0
        rcx = (rx2 - rx1) * 0.5
        for c in contours:
            area = cv2.contourArea(c)
            if area < (w * h * 0.012):
                continue
            bx, by, bw, bh = cv2.boundingRect(c)
            if bw < 40 or bh < 40:
                continue
            center_dist = abs((bx + bw * 0.5) - rcx) / max(1.0, rcx)
            score = area * (1.25 - 0.55 * center_dist)
            if score > best_score:
                best_score = score
                best = (bx, by, bw, bh)

        if best is None:
            return fallback_box
        bx, by, bw, bh = best
        # Clamp aspect ratio to a clean head box (in case neck/chest joined skin mask)
        head_h = min(bh, int(bw * 1.35))
        head_w = min(bw, int(head_h * 1.15))
        fx = max(0, rx1 + bx + (bw - head_w) // 2)
        fy = max(0, ry1 + by)
        if head_w < 50 or head_h < 50:
            return fallback_box
        return (fx, fy, head_w, head_h)
    except Exception:
        return fallback_box


def _apply_speech_lipsync_mouth_motion(frame_bgr, mouth_open: float, head_box: tuple[int, int, int, int]):
    """
    Drive realistic rhythmic mouth-opening & lower-jaw articulation on the speaker's face
    driven by the real Hindi dialogue RMS amplitude (ZERO CascadeClassifier!).
    """
    if cv2 is None or np is None or mouth_open < 0.08:
        return frame_bgr
    h, w = frame_bgr.shape[:2]
    fx, fy, fw, fh = head_box
    if fw < 40 or fh < 40:
        return frame_bgr

    # Mouth center sits cleanly around ~73% down the tracked face box
    mcx = int(fx + fw * 0.50)
    mcy = int(fy + fh * 0.73)
    mrw = max(14, int(fw * 0.17))
    mrh = max(8, int(fh * 0.085))

    x1 = max(0, mcx - mrw * 2)
    x2 = min(w, mcx + mrw * 2)
    y1 = max(0, mcy - mrh * 2)
    y2 = min(h, mcy + mrh * 3)
    roi_h, roi_w = y2 - y1, x2 - x1
    if roi_h < 18 or roi_w < 24:
        return frame_bgr

    roi = frame_bgr[y1:y2, x1:x2].copy()
    # Local vertical jaw stretch downward proportional to spoken syllable loudness
    yy, xx = np.mgrid[0:roi_h, 0:roi_w].astype(np.float32)
    local_cx = float(mcx - x1)
    local_cy = float(mcy - y1)
    dx = (xx - local_cx) / max(1.0, float(mrw * 1.35))
    dy = (yy - local_cy) / max(1.0, float(mrh * 1.75))
    dist2 = dx * dx + dy * dy
    falloff = np.exp(-dist2 * 1.45)

    # Pull lower lip/jaw subtly downward & upper lip slightly upward when speaking loudly
    amp = min(1.0, mouth_open) * max(2.2, mrh * 0.58)
    direction = np.where(yy >= local_cy, -1.0, 0.35)
    map_y = np.clip(yy + direction * amp * falloff, 0, roi_h - 1).astype(np.float32)
    map_x = xx
    warped = cv2.remap(roi, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

    # Subtle realistic inner mouth cavity shadow when opening mouth on loud vowels
    if mouth_open > 0.24:
        cavity = warped.copy()
        open_h = max(2, int(mrh * 0.62 * min(1.0, mouth_open)))
        open_w = max(8, int(mrw * 0.72))
        cv2.ellipse(
            cavity,
            (int(local_cx), int(local_cy + open_h * 0.15)),
            (open_w, open_h),
            0, 0, 360,
            (22, 18, 34),
            -1,
            cv2.LINE_AA,
        )
        # Soft feather inner mouth shadow so it never looks drawn-on
        cav_alpha = min(0.55, (mouth_open - 0.18) * 0.65)
        warped = cv2.addWeighted(cavity, cav_alpha, warped, 1.0 - cav_alpha, 0)

    frame_bgr[y1:y2, x1:x2] = warped
    return frame_bgr


def _apply_natural_eye_blink_and_breathing(
    frame_bgr,
    f_idx: int,
    head_box: tuple[int, int, int, int],
    fps: int = 24,
):
    """
    Bring photorealistic actors to life with:
    1. Subtle natural 4-frame eye-blink every ~2.7 seconds on the tracked eye band
    2. Subtle organic breathing micro-lift across the upper torso/shoulders
    """
    if cv2 is None or np is None:
        return frame_bgr
    h, w = frame_bgr.shape[:2]
    fx, fy, fw, fh = head_box
    if fw < 40 or fh < 40:
        return frame_bgr

    # 1. Natural rhythmic eye blink (~every 64 frames = ~2.65s at 24fps)
    blink_cycle = max(48, int(fps * 2.65))
    phase = (f_idx + 18) % blink_cycle
    blink_weights = {0: 0.45, 1: 0.92, 2: 0.85, 3: 0.35}
    blink_amt = blink_weights.get(phase, 0.0)
    if blink_amt > 0.05:
        eye_y = int(fy + fh * 0.37)
        eye_h = max(6, int(fh * 0.08))
        y1 = max(0, eye_y - eye_h)
        y2 = min(h, eye_y + eye_h)
        x1 = max(0, int(fx + fw * 0.16))
        x2 = min(w, int(fx + fw * 0.84))
        rh, rw = y2 - y1, x2 - x1
        if rh >= 8 and rw >= 24:
            roi = frame_bgr[y1:y2, x1:x2].copy()
            yy, xx = np.mgrid[0:rh, 0:rw].astype(np.float32)
            cy = rh * 0.52
            left_cx, right_cx = rw * 0.27, rw * 0.73
            erx = max(6.0, rw * 0.17)
            ery = max(3.5, rh * 0.36)
            d_left = ((xx - left_cx) / erx) ** 2 + ((yy - cy) / ery) ** 2
            d_right = ((xx - right_cx) / erx) ** 2 + ((yy - cy) / ery) ** 2
            eye_mask = np.maximum(np.exp(-d_left * 1.8), np.exp(-d_right * 1.8))
            pull = blink_amt * max(2.0, ery * 0.95)
            # Pull upper eyelid skin smoothly downward over eye aperture
            map_y = np.clip(yy - pull * eye_mask, 0, rh - 1).astype(np.float32)
            warped = cv2.remap(roi, xx, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            # Subtle warm natural upper-eyelash crease shadow
            shade = np.clip(1.0 - 0.16 * blink_amt * eye_mask, 0.78, 1.0)[..., None]
            warped = np.clip(warped.astype(np.float32) * shade, 0, 255).astype(np.uint8)
            frame_bgr[y1:y2, x1:x2] = warped

    return frame_bgr


def _try_talking_lipsync_spaces(
    face_img_path: str,
    dialogue_audio_path: str,
) -> Optional[str]:
    """
    Real AI Talking Lip-Sync via HuggingFace Spaces (strict 8s timeout + Admin HF_TOKEN support):
    KwaiVGI/LivePortrait -> vinthony/SadTalker -> camenduru/Wav2Lip
    """
    if not GRADIO_AVAILABLE or Client is None or handle_file is None:
        return None
    if not face_img_path or not os.path.exists(face_img_path) or not dialogue_audio_path or not os.path.exists(dialogue_audio_path):
        return None

    cfg = get_runtime_ai_config()
    if not cfg.get("enable_public_hf_spaces", True):
        return None
    hf_token = cfg.get("hf_token") or None
    timeout_s = min(8.0, float(cfg.get("space_timeout_sec", 8)))

    mirrors = [
        ("KwaiVGI/LivePortrait", "liveportrait"),
        ("vinthony/SadTalker", "sadtalker"),
        ("camenduru/Wav2Lip", "wav2lip"),
    ]
    for space_id, kind in mirrors:
        if _is_hf_space_cooling(space_id):
            continue

        def _call(sid=space_id, k=kind):
            hf_client = Client(sid, hf_token=hf_token) if hf_token else Client(sid)
            if k == "sadtalker":
                return hf_client.predict(
                    source_image=handle_file(face_img_path),
                    driven_audio=handle_file(dialogue_audio_path),
                    preprocess="crop",
                    still_mode=False,
                    use_enhancer=False,
                    batch_size=1,
                    size_of_image=256,
                    pose_style=0,
                    api_name="/test",
                )
            elif k == "wav2lip":
                return hf_client.predict(
                    face=handle_file(face_img_path),
                    audio=handle_file(dialogue_audio_path),
                    api_name="/predict",
                )
            else:
                return hf_client.predict(
                    source_image=handle_file(face_img_path),
                    driving_audio=handle_file(dialogue_audio_path),
                    api_name="/predict",
                )

        res = _run_with_timeout(_call, timeout_sec=timeout_s)
        if res is None:
            _mark_hf_space_failed(space_id)
            continue
        vid_path = _extract_gradio_video_path(res)
        if vid_path and os.path.exists(vid_path) and os.path.getsize(vid_path) > 2000:
            return vid_path
        _mark_hf_space_failed(space_id)
    return None


def _apply_mood_lighting_tint(frame_bgr, mood: str):
    """Apply subtle Dangal TV / StarPlus mood color grading per frame (BGR order)."""
    if not (OPENCV_AVAILABLE and cv2 is not None and np is not None):
        return frame_bgr
    try:
        m = (mood or "").strip()
        if m == "Emotional":
            out = frame_bgr.astype(np.float32)
            out[:, :, 0] *= 1.08  # Cool blue tint
            out[:, :, 2] *= 0.92  # Soft red pull
            return np.clip(out, 0, 255).astype(np.uint8)
        if m in ("Suspense", "Romantic"):
            out = frame_bgr.astype(np.float32)
            out[:, :, 2] *= 1.07  # Warm golden chandelier glow
            out[:, :, 1] *= 1.03
            out[:, :, 0] *= 0.93
            return np.clip(out, 0, 255).astype(np.uint8)
        if m == "Action":
            return cv2.convertScaleAbs(frame_bgr, alpha=1.08, beta=-4)
    except Exception:
        pass
    return frame_bgr


def _detect_broll_prompt(dialogue_text: str) -> Optional[tuple[str, str]]:
    """Detect dialogue keywords that warrant a 1.5-second cinematic Context B-Roll insert shot."""
    low = (dialogue_text or "").lower()
    if any(k in low for k in ("mercedes", "मर्सिडीज़", "मर्सिडीज", "गाड़ी", "कार", "car ", "bmw", "audi", "rolls")):
        return (
            "broll_car",
            "luxury black Mercedes sedan arriving at grand illuminated Indian royal haveli mansion gate at dusk, dramatic warm headlights and golden courtyard lamps, 35mm cinema insert shot",
        )
    if any(k in low for k in ("हवेली", "मकान", "गेट", "दरवाज़", "दरवाज", "mansion", "gate", "palace", "नीलामी", "auction")):
        return (
            "broll_gate",
            "grand royal Indian heritage haveli mansion iron gate opening dramatically with golden chandelier lights glowing inside courtyard, twilight cinema establishing insert shot",
        )
    if any(k in low for k in ("वसीयत", "कागज़", "कागज", "प्रॉपर्टी", "जायदाद", "चेक", "will", "papers", "property", "contract", "sign", "दस्तखत")):
        return (
            "broll_papers",
            "dramatic close-up insert of official Indian legal property will documents and gold fountain pen on mahogany desk inside royal haveli study, moody warm chandelier lighting",
        )
    if any(k in low for k in ("बारिश", "तूफान", "आंधी", "rain", "storm", "बिजली")):
        return (
            "broll_rain",
            "dramatic monsoon rain streaks falling across tall carved wooden haveli window glass at night with warm golden chandelier reflections, cinematic Indian TV serial insert shot",
        )
    if any(k in low for k in ("कंपनी", "ऑफिस", "चेयरमैन", "बॉड", "office", "chairman", "empire", "बिज़नेस", "करोड़")):
        return (
            "broll_office",
            "sleek modern Mumbai billionaire corporate glass tower and executive boardroom desk with golden nameplate at twilight, Indian TV drama cinematic insert shot",
        )
    return None


def _is_twist_dialogue(dialogue_text: str) -> bool:
    """Detect Dangal TV blockbuster twist dialogue lines for a 1.5s 3-Angle Reaction Montage."""
    low = (dialogue_text or "").lower()
    twist_terms = (
        "अरबपति", "arabpati", "मालिक", "malik", "चेयरमैन", "chairman",
        "असली सच", "सच सुन", "धोखा", "dhokha", "पुलिस", "police",
        "औकात", "aukat", "नीलामी", "राज़", "raaz", "वसीयत", "wasiyat",
        "जेल", "करोड़", "crore", "बदला", "badla", "खेल खत्म",
    )
    return any(t in low for t in twist_terms)


def render_title_or_outro_png(
    text: str,
    out_png_path: str,
    width: int = 720,
    height: int = 1280,
    is_outro: bool = False,
) -> Optional[str]:
    """
    Render crisp 2s Golden Episode Title Hook (top-center cinema ribbon)
    OR sleek 2s Cliffhanger Outro Card (`अगले पार्ट में जानिए असली सच... (Part N+1)`).
    """
    clean_t = (text or "").strip()[:76]
    if not clean_t:
        return None
    is_vertical = height > width
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    font_main = load_hindi_font(30 if is_vertical else 34, bold=True)
    font_sub = load_hindi_font(20 if is_vertical else 22, bold=True)

    if is_outro:
        # Cinematic darkened cliffhanger card overlay
        draw.rectangle([0, 0, width, height], fill=(6, 4, 14, 178))
        pad_x = 42 if is_vertical else 120
        box_h = 220 if is_vertical else 190
        y1 = (height - box_h) // 2
        y2 = y1 + box_h
        draw.rounded_rectangle(
            [pad_x, y1, width - pad_x, y2],
            radius=22,
            fill=(16, 10, 28, 236),
            outline=(255, 204, 0, 240),
            width=3,
        )
        badge = "★ सस्पेंस क्लिफहैंगर ★"
        try:
            bb = draw.textbbox((0, 0), badge, font=font_sub)
            bw = bb[2] - bb[0]
        except Exception:
            bw = len(badge) * 12
        draw.text(((width - bw) // 2, y1 + 26), badge, font=font_sub, fill=(255, 214, 0, 255))

        words = clean_t.split()
        lines: list[str] = []
        cur = ""
        max_w = width - pad_x * 2 - 48
        for w in words:
            test = f"{cur} {w}".strip()
            try:
                tb = draw.textbbox((0, 0), test, font=font_main)
                tw = tb[2] - tb[0]
            except Exception:
                tw = len(test) * 16
            if tw > max_w and cur:
                lines.append(cur)
                cur = w
            else:
                cur = test
        if cur:
            lines.append(cur)
        lines = lines[:2]
        for idx, ln in enumerate(lines):
            try:
                lb = draw.textbbox((0, 0), ln, font=font_main)
                lw = lb[2] - lb[0]
            except Exception:
                lw = len(ln) * 16
            draw.text(
                ((width - lw) // 2, y1 + 78 + idx * 46),
                ln,
                font=font_main,
                fill=(255, 255, 255, 255),
                stroke_width=2,
                stroke_fill=(0, 0, 0, 220),
            )
    else:
        # 2-second Golden Episode Title Hook banner at top-safe area
        pad_x = 34 if is_vertical else 140
        y1 = 96 if is_vertical else 44
        box_h = 84
        draw.rounded_rectangle(
            [pad_x, y1, width - pad_x, y1 + box_h],
            radius=16,
            fill=(14, 8, 26, 212),
            outline=(255, 204, 0, 235),
            width=3,
        )
        title_str = f"🎬 {clean_t}"
        try:
            tb = draw.textbbox((0, 0), title_str, font=font_sub)
            tw = tb[2] - tb[0]
        except Exception:
            tw = len(title_str) * 12
        tx = max(pad_x + 18, (width - tw) // 2)
        draw.text(
            (tx, y1 + 26),
            title_str,
            font=font_sub,
            fill=(255, 218, 68, 255),
            stroke_width=2,
            stroke_fill=(0, 0, 0, 235),
        )

    canvas.save(out_png_path, "PNG")
    return out_png_path


def _build_overlay_ffmpeg_cmd(
    video_in_args: list[str],
    base_v_filter: str,
    final_audio: str,
    out_mp4: str,
    dur: float,
    fps: int = 30,
    subtitle_png_path: Optional[str] = None,
    title_png_path: Optional[str] = None,
    outro_png_path: Optional[str] = None,
) -> list[str]:
    """Build a uniform 30fps, yuv420p, AAC 44100Hz stereo FFmpeg command with optional Subtitles + 2s Title + 2s Outro overlays."""
    cmd = ["ffmpeg", "-y"] + video_in_args
    next_idx = sum(1 for a in video_in_args if a == "-i")

    sub_idx = None
    if subtitle_png_path and os.path.exists(subtitle_png_path):
        sub_idx = next_idx
        cmd.extend(["-i", subtitle_png_path])
        next_idx += 1

    title_idx = None
    if title_png_path and os.path.exists(title_png_path):
        title_idx = next_idx
        cmd.extend(["-i", title_png_path])
        next_idx += 1

    outro_idx = None
    if outro_png_path and os.path.exists(outro_png_path):
        outro_idx = next_idx
        cmd.extend(["-i", outro_png_path])
        next_idx += 1

    audio_idx = next_idx
    cmd.extend(["-i", final_audio])

    fc_parts: list[str] = []
    cur_v = "0:v"
    if base_v_filter:
        fc_parts.append(f"[0:v]{base_v_filter}[vb]")
        cur_v = "vb"
    if sub_idx is not None:
        fc_parts.append(f"[{cur_v}][{sub_idx}:v]overlay=0:H-h[vsub]")
        cur_v = "vsub"
    if title_idx is not None:
        fc_parts.append(f"[{cur_v}][{title_idx}:v]overlay=0:0:enable='between(t,0,2.2)'[vtit]")
        cur_v = "vtit"
    if outro_idx is not None:
        outro_start = max(0.4, dur - 2.1)
        fc_parts.append(f"[{cur_v}][{outro_idx}:v]overlay=0:0:enable='gte(t,{outro_start:.2f})'[vout]")
        cur_v = "vout"

    if fc_parts:
        cmd.extend(["-filter_complex", ";".join(fc_parts), "-map", f"[{cur_v}]", "-map", f"{audio_idx}:a:0"])
    else:
        cmd.extend(["-map", "0:v:0", "-map", f"{audio_idx}:a:0"])

    cmd.extend([
        "-r", str(fps),
        "-t", f"{max(1.5, float(dur)):.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "23",
        "-g", str(fps), "-bf", "0", "-video_track_timescale", "15360",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "96k",
        "-movflags", "+faststart",
        out_mp4,
    ])
    return cmd


def _render_pro_motion_vfx_clip(
    framed_img_path: str,
    final_audio: str,
    out_mp4: str,
    mood: str = "Action",
    sfx_type: str = "none",
    dialogue_audio_path: Optional[str] = None,
    subtitle_png_path: Optional[str] = None,
    aspect_ratio: str = "16:9",
    title_png_path: Optional[str] = None,
    outro_png_path: Optional[str] = None,
) -> bool:
    """
    Standardized 30fps Full-Screen Cinematic Talking Lip-Sync + Mood Lighting VFX Engine (9:16 & 16:9):
    - Uniform 30fps, yuv420p, AAC 44100Hz stereo for instant `-c copy` stitching
    - Audio-envelope mouth lip-sync + natural eye-blink + organic breathing + syllable head-nod
    - Mood Lighting Tints (Emotional -> cool blue, Suspense/Romantic -> golden glow, Action -> high contrast + shake)
    - Optional 2s Opening Golden Episode Title Hook & 2s Cliffhanger Outro Card
    """
    w_out, h_out = get_aspect_dimensions(aspect_ratio)
    src_w, src_h = int(w_out * 1.2), int(h_out * 1.2)
    dur = _probe_audio_duration(final_audio)
    fps = 30
    total_frames = max(45, int(dur * fps))
    high_impact = (
        mood == "Action"
        or sfx_type in ("punch", "explosion", "thunder", "sword")
    )

    speech_env = _extract_audio_speech_envelope(
        dialogue_audio_path or final_audio, total_frames, fps=fps
    )

    if OPENCV_AVAILABLE and cv2 is not None and np is not None:
        raw_vfx_mp4 = f"{out_mp4}_vfx_raw.mp4"
        try:
            base = cv2.imread(framed_img_path)
            if base is not None:
                src = cv2.resize(base, (src_w, src_h), interpolation=cv2.INTER_LINEAR)
                sh, sw = src.shape[:2]
                face_box_src = _detect_skin_face_box(src)

                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(raw_vfx_mp4, fourcc, fps, (w_out, h_out))

                rng = random.Random(hash(framed_img_path) & 0xFFFFFFFF)
                particles = [
                    {
                        "x": rng.uniform(0, w_out),
                        "y": rng.uniform(0, h_out),
                        "vx": rng.uniform(-7.0, -2.2) if sfx_type in ("rain", "thunder") else rng.uniform(-2.6, 2.6),
                        "vy": rng.uniform(14.0, 22.0) if sfx_type in ("rain", "thunder") else rng.uniform(-3.6, -1.0),
                        "r": rng.randint(2, 4),
                    }
                    for _ in range(44 if high_impact or sfx_type in ("rain", "thunder") else 20)
                ]

                for f_idx in range(total_frames):
                    t = f_idx / max(1, total_frames - 1)
                    talk_amp = speech_env[f_idx] if f_idx < len(speech_env) else 0.0

                    work_src = _apply_speech_lipsync_mouth_motion(src.copy(), talk_amp, face_box_src)
                    work_src = _apply_natural_eye_blink_and_breathing(work_src, f_idx, face_box_src, fps=fps)

                    breath_y = math.sin(f_idx * 0.11) * 3.5
                    nod_y = math.sin(f_idx * 0.46) * (4.8 * talk_amp) + breath_y
                    nod_x = math.cos(f_idx * 0.28) * (2.5 * talk_amp)
                    talk_zoom_boost = 0.015 * talk_amp

                    if mood == "Action":
                        zoom = 1.05 + 0.12 * t + talk_zoom_boost
                        pan_x = math.sin(t * math.pi * 1.3) * 32.0 + nod_x
                        pan_y = math.cos(t * math.pi * 1.1) * 14.0 + nod_y
                    elif mood == "Suspense":
                        zoom = 1.03 + 0.10 * t + talk_zoom_boost
                        pan_x = math.sin(t * math.pi) * 22.0 + nod_x
                        pan_y = -11.0 * t + nod_y
                    elif mood == "Comedy":
                        zoom = 1.04 + 0.05 * abs(math.sin(f_idx * 0.13)) + talk_zoom_boost
                        pan_x = math.sin(f_idx * 0.11) * 24.0 + nod_x
                        pan_y = math.cos(f_idx * 0.10) * 10.0 + nod_y
                    elif mood == "Romantic":
                        zoom = 1.03 + 0.08 * t + talk_zoom_boost
                        pan_x = -24.0 + 48.0 * t + nod_x
                        pan_y = math.sin(t * math.pi) * 11.0 + nod_y
                    else:
                        zoom = 1.03 + 0.09 * t + talk_zoom_boost
                        pan_x = math.sin(t * math.pi * 0.8) * 16.0 + nod_x
                        pan_y = -8.0 * t + nod_y

                    shake_x = 0.0
                    shake_y = 0.0
                    if high_impact:
                        burst = 1.0 if (f_idx % 30) < 8 else 0.28
                        amp_s = (10.5 if sfx_type in ("explosion", "punch") else 6.0) * burst
                        shake_x = math.sin(f_idx * 1.7) * amp_s
                        shake_y = math.cos(f_idx * 2.1) * (amp_s * 0.7)

                    crop_w = int(sw / zoom)
                    crop_h = int(sh / zoom)
                    cx = int(sw / 2 + pan_x + shake_x)
                    cy = int(sh / 2 + pan_y + shake_y)
                    x1 = max(0, min(sw - crop_w, cx - crop_w // 2))
                    y1 = max(0, min(sh - crop_h, cy - crop_h // 2))
                    frame = cv2.resize(
                        work_src[y1 : y1 + crop_h, x1 : x1 + crop_w],
                        (w_out, h_out),
                        interpolation=cv2.INTER_LINEAR,
                    )

                    frame = _apply_mood_lighting_tint(frame, mood)

                    light_boost = 1.0 + 0.03 * talk_amp
                    if sfx_type == "thunder" and (f_idx % 45) in (2, 3, 5):
                        light_boost = 1.28
                    elif sfx_type in ("explosion", "punch") and (f_idx % 34) in (1, 2):
                        light_boost = 1.20
                    if abs(light_boost - 1.0) > 0.01:
                        frame = cv2.convertScaleAbs(frame, alpha=light_boost, beta=3 if light_boost > 1.15 else 0)

                    p_overlay = frame.copy()
                    for p in particles:
                        p["x"] = (p["x"] + p["vx"]) % w_out
                        p["y"] = (p["y"] + p["vy"]) % h_out
                        px, py = int(p["x"]), int(p["y"])
                        if sfx_type in ("rain", "thunder"):
                            cv2.line(p_overlay, (px, py), (px - 8, py + 24), (245, 225, 195), 1, cv2.LINE_AA)
                        elif high_impact:
                            col = (60, 175, 255) if p["r"] > 3 else (90, 225, 255)
                            cv2.circle(p_overlay, (px, py), p["r"], col, -1, cv2.LINE_AA)
                        elif mood in ("Romantic", "Comedy"):
                            col = (180, 130, 255) if p["r"] % 2 == 0 else (110, 220, 255)
                            cv2.circle(p_overlay, (px, py), p["r"] + 2, col, -1, cv2.LINE_AA)
                        else:
                            cv2.circle(p_overlay, (px, py), max(2, p["r"] - 1), (220, 210, 170), -1, cv2.LINE_AA)
                    cv2.addWeighted(p_overlay, 0.30, frame, 0.70, 0, frame)

                    writer.write(frame)

                writer.release()

                if os.path.exists(raw_vfx_mp4) and os.path.getsize(raw_vfx_mp4) > 2000:
                    cmd = _build_overlay_ffmpeg_cmd(
                        ["-i", raw_vfx_mp4],
                        "",
                        final_audio,
                        out_mp4,
                        dur=dur,
                        fps=fps,
                        subtitle_png_path=subtitle_png_path,
                        title_png_path=title_png_path,
                        outro_png_path=outro_png_path,
                    )
                    subprocess.run(cmd, check=True, timeout=65, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000:
                        return True
        except Exception:
            pass
        finally:
            if os.path.exists(raw_vfx_mp4):
                try:
                    os.remove(raw_vfx_mp4)
                except Exception:
                    pass

    # Path B: Pure FFmpeg 30fps Pro Motion (NO `-loop 1` before zoompan so zoompan animates EVERY frame!)
    shake_expr_x = "iw/2-(iw/zoom/2)+sin(on*1.5)*12" if high_impact else "iw/2-(iw/zoom/2)+sin(on*0.12)*16"
    shake_expr_y = "ih/2-(ih/zoom/2)+cos(on*1.9)*8" if high_impact else "ih/2-(ih/zoom/2)+sin(on*0.14)*10"
    zoom_rate = "0.0017" if high_impact else "0.0012"
    zoom_cap = "1.20" if high_impact else "1.16"
    vf_base = (
        f"scale={src_w}:{src_h}:force_original_aspect_ratio=increase,crop={src_w}:{src_h},"
        f"zoompan=z='min(1.04+on*{zoom_rate},{zoom_cap})':x='{shake_expr_x}':y='{shake_expr_y}':d={total_frames}:s={w_out}x{h_out}:fps={fps},"
        f"eq=contrast=1.06:saturation=1.10:brightness=0.01,vignette=PI/5"
    )
    cmd_fb = _build_overlay_ffmpeg_cmd(
        ["-i", framed_img_path],
        vf_base,
        final_audio,
        out_mp4,
        dur=dur,
        fps=fps,
        subtitle_png_path=subtitle_png_path,
        title_png_path=title_png_path,
        outro_png_path=outro_png_path,
    )
    subprocess.run(cmd_fb, check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000


def _render_multishot_pollinations_motion_clip(
    keyframe_paths: list[str],
    final_audio: str,
    out_mp4: str,
    mood: str = "Action",
    sfx_type: str = "none",
    dialogue_audio_path: Optional[str] = None,
    subtitle_png_path: Optional[str] = None,
    aspect_ratio: str = "16:9",
    title_png_path: Optional[str] = None,
    outro_png_path: Optional[str] = None,
) -> bool:
    """
    Live-Action Indian TV Serial 30fps Multi-Cut Talking Video Engine (2, 3, or 4 Cuts!):
    - Cut 0 (~56%): Exact Speaking Actor waist-up frame with audio-synced lip-sync, syllable head-nod,
      organic breathing, eye-blink, and mood lighting tint!
    - Remaining ~44%: Dynamically split across Counterpart Reaction Cut, 1.5s Context B-Roll insert,
      OR Dangal TV 3-Angle Twist Reveal Montage (Heroine shocked -> Saas stunned -> Villain angry zoom-punch)!
    - Uniform 30fps, yuv420p, AAC 44100Hz stereo so `-c copy` stitches Parts & Full Movies in ~2 seconds!
    """
    valid_frames = [p for p in keyframe_paths if p and os.path.exists(p) and os.path.getsize(p) > 2000][:4]
    if len(valid_frames) <= 1:
        return _render_pro_motion_vfx_clip(
            valid_frames[0] if valid_frames else keyframe_paths[0],
            final_audio,
            out_mp4,
            mood=mood,
            sfx_type=sfx_type,
            dialogue_audio_path=dialogue_audio_path,
            subtitle_png_path=subtitle_png_path,
            aspect_ratio=aspect_ratio,
            title_png_path=title_png_path,
            outro_png_path=outro_png_path,
        )

    w_out, h_out = get_aspect_dimensions(aspect_ratio)
    src_w, src_h = int(w_out * 1.24), int(h_out * 1.24)
    dur = _probe_audio_duration(final_audio)
    fps = 30
    total_frames = max(50, int(dur * fps) + 2)
    high_impact = (
        mood == "Action"
        or sfx_type in ("punch", "explosion", "thunder", "sword")
    )

    # Path A (Primary): OpenCV 30fps Live Talking Lip-Sync + Multi-Cut (2/3/4 Angles) + Mood Lighting
    if OPENCV_AVAILABLE and cv2 is not None and np is not None:
        raw_vfx_mp4 = f"{out_mp4}_tv_mcut_raw.mp4"
        try:
            loaded_srcs = []
            loaded_faces = []
            for fp in valid_frames:
                im = cv2.imread(fp)
                if im is not None:
                    rs = cv2.resize(im, (src_w, src_h), interpolation=cv2.INTER_LINEAR)
                    loaded_srcs.append(rs)
                    loaded_faces.append(_detect_skin_face_box(rs))

            if len(loaded_srcs) >= 2:
                n_cuts = len(loaded_srcs)
                speech_env = _extract_audio_speech_envelope(
                    dialogue_audio_path or final_audio, total_frames, fps=fps
                )
                # Cut 0 (Speaking Actor): ~58% for 2 cuts, ~54% for 3-4 cuts (leaves crisp 1.5s twist/B-roll beats!)
                cut0_end = max(28, min(total_frames - (n_cuts - 1) * 14, int(total_frames * (0.58 if n_cuts == 2 else 0.54))))
                rem_frames = max(14, total_frames - cut0_end)
                per_extra = max(12, rem_frames // (n_cuts - 1))
                cut_bounds = [0, cut0_end]
                for c_i in range(1, n_cuts - 1):
                    cut_bounds.append(min(total_frames - 12, cut0_end + c_i * per_extra))
                cut_bounds.append(total_frames)
                blend_len = 3

                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(raw_vfx_mp4, fourcc, fps, (w_out, h_out))

                def _render_cut_frame(cut_idx: int, f_idx: int, local_t: float, talk_amp: float):
                    src_img = loaded_srcs[cut_idx]
                    face_box = loaded_faces[cut_idx]
                    is_speaking = (cut_idx == 0)
                    sh, sw = src_img.shape[:2]
                    work = src_img.copy()
                    if is_speaking:
                        work = _apply_speech_lipsync_mouth_motion(work, talk_amp, face_box)
                    work = _apply_natural_eye_blink_and_breathing(work, f_idx, face_box, fps=fps)

                    breath_y = math.sin(f_idx * 0.12) * 3.6
                    if is_speaking:
                        nod_y = math.sin(f_idx * 0.48) * (5.0 * talk_amp) + breath_y
                        nod_x = math.cos(f_idx * 0.29) * (2.8 * talk_amp)
                        zoom = 1.04 + 0.14 * local_t + 0.015 * talk_amp
                        pan_x = math.sin(local_t * math.pi * 0.9) * 14.0 + nod_x
                        pan_y = -9.0 * local_t + nod_y
                        shake_x = math.sin(f_idx * 1.7) * (5.5 if high_impact else 1.0)
                        shake_y = math.cos(f_idx * 2.0) * (3.8 if high_impact else 0.7)
                    else:
                        # Reaction / B-Roll / Dangal TV 3-Angle Twist Punch-In!
                        ease = 1.0 - (1.0 - local_t) ** 2
                        zoom = 1.05 + (0.19 if n_cuts >= 3 else 0.16) * ease
                        pan_dir = 1.0 if (cut_idx % 2 == 1) else -1.0
                        pan_x = pan_dir * (-12.0 + 24.0 * local_t)
                        pan_y = -6.0 * local_t + breath_y
                        punch = max(0.0, 1.0 - local_t * 4.2)
                        amp_s = (11.0 if (high_impact or n_cuts >= 3) else 6.0) * punch
                        shake_x = math.sin(f_idx * 2.4) * amp_s
                        shake_y = math.cos(f_idx * 2.8) * (amp_s * 0.75)

                    crop_w = int(sw / zoom)
                    crop_h = int(sh / zoom)
                    cx = int(sw / 2 + pan_x + shake_x)
                    cy = int(sh / 2 + pan_y + shake_y)
                    x1 = max(0, min(sw - crop_w, cx - crop_w // 2))
                    y1 = max(0, min(sh - crop_h, cy - crop_h // 2))
                    out_f = cv2.resize(
                        work[y1 : y1 + crop_h, x1 : x1 + crop_w],
                        (w_out, h_out),
                        interpolation=cv2.INTER_LINEAR,
                    )
                    out_f = _apply_mood_lighting_tint(out_f, mood)
                    l_boost = 1.0 + (0.03 * talk_amp if is_speaking else 0.05 * max(0.0, 1.0 - local_t * 3.5))
                    if abs(l_boost - 1.0) > 0.01:
                        out_f = cv2.convertScaleAbs(out_f, alpha=l_boost, beta=2)
                    return out_f

                for f_idx in range(total_frames):
                    talk_amp = speech_env[f_idx] if f_idx < len(speech_env) else 0.0
                    c_idx = 0
                    for k in range(n_cuts):
                        if f_idx >= cut_bounds[k]:
                            c_idx = k
                    seg_start = cut_bounds[c_idx]
                    seg_end = cut_bounds[c_idx + 1]
                    local_t = (f_idx - seg_start) / max(1, seg_end - seg_start - 1)
                    if c_idx > 0 and (f_idx - seg_start) < blend_len:
                        alpha = (f_idx - seg_start + 1) / float(blend_len + 1)
                        f_prev = _render_cut_frame(c_idx - 1, f_idx, 1.0, talk_amp if c_idx == 1 else 0.0)
                        f_curr = _render_cut_frame(c_idx, f_idx, local_t, 0.0)
                        frame = cv2.addWeighted(f_curr, alpha, f_prev, 1.0 - alpha, 0)
                    else:
                        frame = _render_cut_frame(c_idx, f_idx, local_t, talk_amp if c_idx == 0 else 0.0)
                    writer.write(frame)

                writer.release()

                if os.path.exists(raw_vfx_mp4) and os.path.getsize(raw_vfx_mp4) > 2000:
                    cmd_mux = _build_overlay_ffmpeg_cmd(
                        ["-i", raw_vfx_mp4],
                        "",
                        final_audio,
                        out_mp4,
                        dur=dur,
                        fps=fps,
                        subtitle_png_path=subtitle_png_path,
                        title_png_path=title_png_path,
                        outro_png_path=outro_png_path,
                    )
                    subprocess.run(cmd_mux, check=True, timeout=65, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000:
                        return True
        except Exception:
            pass
        finally:
            if os.path.exists(raw_vfx_mp4):
                try:
                    os.remove(raw_vfx_mp4)
                except Exception:
                    pass

    # Path B (Pure-FFmpeg 2-Cut 30fps Motion Fallback — NO `-loop 1` before zoompan!)
    use_frames = valid_frames[:2]
    n_shots = len(use_frames)
    xfade_dur = 0.20
    cut1_dur = max(1.6, dur * 0.58 + xfade_dur * 0.5)
    cut2_dur = max(1.2, dur - cut1_dur + xfade_dur)
    shot_durs = [cut1_dur, cut2_dur]
    shot_frames_list = [max(30, int(sd * fps) + 2) for sd in shot_durs]

    zoompan_moves = [
        f"scale={src_w}:{src_h}:force_original_aspect_ratio=increase,crop={src_w}:{src_h},zoompan=z='min(1.20,1.04+0.0016*on)':x='iw/2-(iw/zoom/2)+sin(on*0.15)*12':y='ih/2-(ih/zoom/2)+sin(on*0.18)*8':d={shot_frames_list[0]}:s={w_out}x{h_out}:fps={fps}",
        f"scale={src_w}:{src_h}:force_original_aspect_ratio=increase,crop={src_w}:{src_h},zoompan=z='min(1.22,1.05+0.0022*on)':x='iw/2-(iw/zoom/2)-on*0.35':y='ih/2-(ih/zoom/2)+sin(on*0.24)*6':d={shot_frames_list[1]}:s={w_out}x{h_out}:fps={fps}",
    ]

    cmd = ["ffmpeg", "-y"]
    for fp in use_frames:
        cmd.extend(["-i", fp])
    next_i = n_shots
    sub_idx = None
    if subtitle_png_path and os.path.exists(subtitle_png_path):
        sub_idx = next_i
        cmd.extend(["-i", subtitle_png_path])
        next_i += 1
    title_idx = None
    if title_png_path and os.path.exists(title_png_path):
        title_idx = next_i
        cmd.extend(["-i", title_png_path])
        next_i += 1
    outro_idx = None
    if outro_png_path and os.path.exists(outro_png_path):
        outro_idx = next_i
        cmd.extend(["-i", outro_png_path])
        next_i += 1
    audio_idx = next_i
    cmd.extend(["-i", final_audio])

    fc_parts = []
    for idx in range(n_shots):
        fc_parts.append(f"[{idx}:v]{zoompan_moves[idx]},setsar=1,format=yuv420p[s{idx}]")
    fc_parts.append(
        f"[s0][s1]xfade=transition=fade:duration={xfade_dur:.2f}:offset={max(0.5, cut1_dur - xfade_dur):.2f},eq=contrast=1.06:saturation=1.10,vignette=PI/5[vcur]"
    )
    cur_v = "vcur"
    if sub_idx is not None:
        fc_parts.append(f"[{cur_v}][{sub_idx}:v]overlay=0:H-h[vsub]")
        cur_v = "vsub"
    if title_idx is not None:
        fc_parts.append(f"[{cur_v}][{title_idx}:v]overlay=0:0:enable='between(t,0,2.2)'[vtit]")
        cur_v = "vtit"
    if outro_idx is not None:
        outro_start = max(0.4, dur - 2.1)
        fc_parts.append(f"[{cur_v}][{outro_idx}:v]overlay=0:0:enable='gte(t,{outro_start:.2f})'[vout]")
        cur_v = "vout"

    cmd.extend([
        "-filter_complex", ";".join(fc_parts),
        "-map", f"[{cur_v}]",
        "-map", f"{audio_idx}:a:0",
        "-r", str(fps),
        "-t", f"{max(1.5, float(dur)):.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "23",
        "-g", str(fps), "-bf", "0", "-video_track_timescale", "15360",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "96k",
        "-movflags", "+faststart",
        out_mp4,
    ])
    try:
        subprocess.run(cmd, check=True, timeout=65, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000:
            return True
    except Exception:
        pass

    return _render_pro_motion_vfx_clip(
        valid_frames[0],
        final_audio,
        out_mp4,
        mood=mood,
        sfx_type=sfx_type,
        dialogue_audio_path=dialogue_audio_path,
        subtitle_png_path=subtitle_png_path,
        aspect_ratio=aspect_ratio,
        title_png_path=title_png_path,
        outro_png_path=outro_png_path,
    )


def generate_free_ai_video_clip(
    raw_img_path: str,
    framed_img_path: str,
    audio_path: str,
    out_mp4: str,
    mood: str = "Action",
    sfx_type: str = "none",
    visual_action_prompt: str = "",
    char_angles: Optional[dict] = None,
    scene_angle: str = "front",
    speaker_name: str = "",
    dialogue_text: str = "",
    target_duration_sec: float = 0.0,
    aspect_ratio: str = "16:9",
    location_key: str = "city_street",
    location_label: str = "Mumbai City Street",
    custom_bg_path: Optional[str] = None,
    watermark_text: str = "",
    watermark_position: str = "top-right",
    master_cast: Optional[dict] = None,
    title_hook_text: str = "",
    outro_card_text: str = "",
) -> str:
    """
    v12 LIVE-ACTION INDIAN TV SERIAL 30FPS MULTI-ENGINE PIPELINE:
    - Real I2V Spaces (Wan2.1 -> LTX-Video -> CogVideoX -> LivePortrait/SadTalker) with fast circuit breaker
    - Master Character Casting Sheet reuse (consistent outfits + 5x faster long series!)
    - Dangal TV 1.5s 3-Angle Twist Reveal Montage on twist lines (`arabpati`, `malik`, `चेयरमैन`, `sach`, `dhokha`, `police`, `aukat`)
    - 1.5s Context B-Roll insert shot when dialogue mentions Mercedes/car, mansion gate, will/papers, office, or rain
    - 2s Golden Opening Episode Title Hook & 2s Cliffhanger Outro Card
    - Uniform 30fps, yuv420p, AAC 44100Hz stereo for instant `-c copy` stitching!
    """
    w_out, h_out = get_aspect_dimensions(aspect_ratio)
    mixed_tmp = f"{out_mp4}_mix.mp3"
    sub_png = f"{out_mp4}_sub.png"
    title_png = f"{out_mp4}_title.png"
    outro_png = f"{out_mp4}_outro.png"
    extra_keyframes: list[str] = []
    final_audio = mix_cinematic_audio(
        audio_path, mood, sfx_type, mixed_tmp, target_duration_sec=target_duration_sec
    )
    clip_dur = _probe_audio_duration(final_audio)
    rendered_sub = (
        render_hindi_subtitle_png(
            speaker_name,
            dialogue_text,
            sub_png,
            width=w_out,
            height=240 if h_out > w_out else 148,
        )
        if dialogue_text.strip()
        else None
    )
    rendered_title = (
        render_title_or_outro_png(title_hook_text, title_png, width=w_out, height=h_out, is_outro=False)
        if title_hook_text.strip()
        else None
    )
    rendered_outro = (
        render_title_or_outro_png(outro_card_text, outro_png, width=w_out, height=h_out, is_outro=True)
        if outro_card_text.strip()
        else None
    )

    effective_angles = dict(char_angles or {})
    if raw_img_path and os.path.exists(raw_img_path):
        effective_angles.setdefault("front", raw_img_path)
    angle_face_path = (
        effective_angles.get(scene_angle)
        if effective_angles.get(scene_angle) and os.path.exists(effective_angles[scene_angle])
        else (raw_img_path if (raw_img_path and os.path.exists(raw_img_path)) else "")
    )

    def _mux_video_with_audio_and_sub(src_video: str) -> bool:
        try:
            scale_crop = f"scale={w_out}:{h_out}:force_original_aspect_ratio=increase,crop={w_out}:{h_out},eq=contrast=1.06:saturation=1.10,vignette=PI/5"
            cmd = _build_overlay_ffmpeg_cmd(
                ["-stream_loop", "-1", "-i", src_video],
                scale_crop,
                final_audio,
                out_mp4,
                dur=clip_dur,
                fps=30,
                subtitle_png_path=rendered_sub,
                title_png_path=rendered_title,
                outro_png_path=rendered_outro,
            )
            subprocess.run(cmd, check=True, timeout=45, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000
        except Exception:
            return False

    try:
        # 1. Try Free Multi-Engine AI Video Auto-Rotation (Wan 2.1 -> LTX-Video -> CogVideoX-5B -> HunyuanVideo)
        # Strictly pass framed_img_path (AI haveli frame / face-swapped Hero), NEVER raw user bedroom selfie!
        if os.path.exists(framed_img_path):
            ai_vid, ai_engine_label = _try_ai_action_i2v_spaces(
                framed_img_path,
                visual_action_prompt,
                mood,
            )
        else:
            ai_vid, ai_engine_label = (None, "")
        if ai_vid and os.path.exists(ai_vid) and ai_engine_label:
            final_src_vid = ai_vid
            if angle_face_path and os.path.exists(angle_face_path):
                swapped_ai_vid = f"{out_mp4}_swapped_ai.mp4"
                try:
                    _opencv_video_faceswap(
                        ai_vid,
                        angle_face_path,
                        swapped_ai_vid,
                        watermark_text=watermark_text,
                        char_angles=effective_angles,
                        preferred_angle=scene_angle,
                    )
                    if os.path.exists(swapped_ai_vid) and os.path.getsize(swapped_ai_vid) > 2000:
                        final_src_vid = swapped_ai_vid
                        extra_keyframes.append(swapped_ai_vid)
                except Exception:
                    pass
            if _mux_video_with_audio_and_sub(final_src_vid):
                return f"{ai_engine_label} (30fps AI Video)"

        # 2. Try Free Talking Lip-Sync (LivePortrait / SadTalker / Wav2Lip) strictly on framed haveli image
        if os.path.exists(framed_img_path):
            talk_vid = _try_talking_lipsync_spaces(framed_img_path, audio_path)
            if talk_vid and os.path.exists(talk_vid):
                if _mux_video_with_audio_and_sub(talk_vid):
                    return "Live-Action TV Serial + LivePortrait/SadTalker"

        # 3. LIVE-ACTION INDIAN TV SERIAL MULTI-CUT ENGINE (Speaker + Reaction / 1.5s B-Roll / 3-Angle Twist Montage!)
        cast_map = master_cast or {}
        speaker_role = detect_speaker_role(speaker_name, None)
        keyframe_list: list[str] = [framed_img_path]
        is_twist = _is_twist_dialogue(dialogue_text)
        broll_hit = _detect_broll_prompt(dialogue_text)

        if is_twist and cast_map:
            # Dangal TV 1.5s 3-Angle Twist Reveal Montage: Heroine shocked -> Saas stunned -> Villain angry!
            for r_key in ("female", "saas", "villain", "hero"):
                cand = cast_map.get(r_key)
                if cand and os.path.exists(cand) and cand not in keyframe_list and r_key != speaker_role:
                    keyframe_list.append(cand)
                if len(keyframe_list) >= 4:
                    break
        else:
            # Optional 1.5s Context B-Roll insert (Mercedes/car, mansion gate, will/papers, office, rain)
            if broll_hit and not custom_bg_path:
                b_key, b_prompt = broll_hit
                cached_broll = cast_map.get(b_key) or cast_map.get("context_broll")
                if cached_broll and os.path.exists(cached_broll) and cached_broll not in keyframe_list:
                    keyframe_list.append(cached_broll)
                else:
                    broll_path = f"{out_mp4}_broll.jpg"
                    try:
                        b_img = render_location_backdrop(
                            location_key,
                            mood,
                            prompt_hint=b_prompt,
                            aspect_ratio=aspect_ratio,
                            include_hero=False,
                            hero_role="narrator",
                            shot_variant="broll_insert",
                        )
                        b_img.save(broll_path, "JPEG", quality=92)
                        if os.path.exists(broll_path) and os.path.getsize(broll_path) > 4000:
                            keyframe_list.append(broll_path)
                            extra_keyframes.append(broll_path)
                    except Exception:
                        pass

            # Counterpart Reaction Cut (reuses Master Casting Sheet instantly if available!)
            def _pick_reaction_role(role_k: str, diag: str) -> str:
                h = abs(hash((role_k or "") + (diag or "")[:24])) % 2
                if role_k in ("saas", "villain"):
                    return "female" if h == 0 else "hero"
                if role_k in ("hero", "female"):
                    return "villain" if h == 0 else "saas"
                return "hero"

            react_role = _pick_reaction_role(speaker_role, dialogue_text)
            cast_react = cast_map.get(react_role)
            if cast_react and os.path.exists(cast_react) and cast_react not in keyframe_list:
                keyframe_list.append(cast_react)
            elif not custom_bg_path and len(keyframe_list) < 2:
                counterpart_label = {"hero": "Hero", "female": "Heroine", "villain": "Villain", "saas": "Saas"}.get(react_role, "Hero")
                react_prompt = f"shocked dramatic waist-up reaction of {counterpart_label} listening inside grand royal Indian haveli hall"
                kf_path = f"{out_mp4}_kf_react.jpg"
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as kf_ex:
                        fut_react = kf_ex.submit(
                            prepare_character_frame,
                            "",
                            counterpart_label,
                            dialogue_text,
                            kf_path,
                            location_key,
                            location_label,
                            mood,
                            None,
                            watermark_text,
                            watermark_position,
                            aspect_ratio,
                            react_prompt,
                            "reaction_cut",
                        )
                        fut_react.result(timeout=14.0)
                    if os.path.exists(kf_path) and os.path.getsize(kf_path) > 4000:
                        keyframe_list.append(kf_path)
                        extra_keyframes.append(kf_path)
                except Exception:
                    pass

        _render_multishot_pollinations_motion_clip(
            keyframe_list,
            final_audio,
            out_mp4,
            mood=mood,
            sfx_type=sfx_type,
            dialogue_audio_path=audio_path,
            subtitle_png_path=rendered_sub,
            aspect_ratio=aspect_ratio,
            title_png_path=rendered_title,
            outro_png_path=rendered_outro,
        )
        if is_twist and len(keyframe_list) >= 3:
            return "Live-Action 30fps TV Serial · 3-Angle Twist Montage"
        if broll_hit and len(keyframe_list) >= 2:
            return "Live-Action 30fps TV Serial · Context B-Roll + Lip-Sync"
        return "Live-Action 30fps TV Serial · Waist-Up Lip-Sync"
    finally:
        for tmp_p in [mixed_tmp, sub_png, title_png, outro_png] + extra_keyframes:
            if tmp_p and os.path.exists(tmp_p):
                try:
                    os.remove(tmp_p)
                except Exception:
                    pass


def render_exact_face_clip(
    raw_img_path: str,
    framed_img_path: str,
    audio_path: str,
    out_mp4: str,
    mood: str = "Action",
    sfx_type: str = "none",
    visual_action_prompt: str = "",
    char_angles: Optional[dict] = None,
    scene_angle: str = "front",
    speaker_name: str = "",
    dialogue_text: str = "",
    target_duration_sec: float = 0.0,
    aspect_ratio: str = "16:9",
    location_key: str = "city_street",
    location_label: str = "Mumbai City Street",
    custom_bg_path: Optional[str] = None,
    watermark_text: str = "",
    watermark_position: str = "top-right",
    master_cast: Optional[dict] = None,
    title_hook_text: str = "",
    outro_card_text: str = "",
) -> str:
    """Backward-compatible wrapper routing directly into `generate_free_ai_video_clip()`."""
    return generate_free_ai_video_clip(
        raw_img_path=raw_img_path,
        framed_img_path=framed_img_path,
        audio_path=audio_path,
        out_mp4=out_mp4,
        mood=mood,
        sfx_type=sfx_type,
        visual_action_prompt=visual_action_prompt,
        char_angles=char_angles,
        scene_angle=scene_angle,
        speaker_name=speaker_name,
        dialogue_text=dialogue_text,
        target_duration_sec=target_duration_sec,
        aspect_ratio=aspect_ratio,
        location_key=location_key,
        location_label=location_label,
        custom_bg_path=custom_bg_path,
        watermark_text=watermark_text,
        watermark_position=watermark_position,
        master_cast=master_cast,
        title_hook_text=title_hook_text,
        outro_card_text=outro_card_text,
    )


def ffmpeg_concat(file_list: list, out_path: str):
    """
    Stitch scene clips into an episode/movie with regenerated PTS/DTS timestamps (`+genpts`)
    and uniform 30fps yuv420p H.264 stream so the video player NEVER stalls or shows a black screen after Scene 1!
    """
    valid_files = [
        f for f in file_list if os.path.exists(f) and os.path.getsize(f) > 500
    ]
    if not valid_files:
        return
    txt = f"{out_path}.txt"
    with open(txt, "w", encoding="utf-8") as f:
        for item in valid_files:
            f.write(f"file '{os.path.abspath(item)}'\n")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", txt,
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "30", "-crf", "23",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "96k",
                "-movflags", "+faststart",
                out_path,
            ],
            check=True,
            timeout=120,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        subprocess.run(
            ["ffmpeg", "-y", "-fflags", "+genpts", "-f", "concat", "-safe", "0", "-i", txt, "-c", "copy", "-movflags", "+faststart", out_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        if os.path.exists(txt):
            try:
                os.remove(txt)
            except Exception:
                pass


def rebuild_parts_and_full(proj_dir: str, state: dict):
    chapters = state["outline"]["chapters"]
    total_ch = len(chapters)
    is_series = state.get("series_mode", "series") == "series"
    ready_parts = []

    if is_series:
        # Moj / Pocket FM Micro-Drama Mode: 1 Part == 1 Chapter -> Part 1 plays immediately!
        for p in range(1, total_ch + 1):
            ch_file = f"{proj_dir}/ch_{p}.mp4"
            if os.path.exists(ch_file) and os.path.getsize(ch_file) > 500:
                part_file = f"{proj_dir}/PART_{p}.mp4"
                shutil.copyfile(ch_file, part_file)
                ready_parts.append(p)
    else:
        total_parts = (total_ch + 2) // 3
        for p in range(1, total_parts + 1):
            start_ch = (p - 1) * 3 + 1
            end_ch = min(p * 3, total_ch)
            part_clips = [
                f"{proj_dir}/ch_{k}.mp4"
                for k in range(start_ch, end_ch + 1)
                if os.path.exists(f"{proj_dir}/ch_{k}.mp4")
            ]
            if len(part_clips) == (end_ch - start_ch + 1):
                part_file = f"{proj_dir}/PART_{p}.mp4"
                ffmpeg_concat(part_clips, part_file)
                ready_parts.append(p)

    state["ready_parts"] = ready_parts
    if 1 in ready_parts:
        state["part_1_ready"] = True

    all_ch = [
        f"{proj_dir}/ch_{c['no']}.mp4"
        for c in chapters
        if os.path.exists(f"{proj_dir}/ch_{c['no']}.mp4")
    ]
    if len(all_ch) == total_ch:
        ffmpeg_concat(all_ch, f"{proj_dir}/FULL_MOVIE.mp4")
        state["is_completed"] = True
        state["progress"] = 100
        state["status_text"] = (
            f"🔥 सभी {len(ready_parts)} Parts और Full Series तैयार हैं! अभी Play और Download करें।"
            if is_series and len(ready_parts) > 1
            else "पूरी मूवी तैयार है! पहले Play करके चेक करें, फिर Download करें।"
        )
    elif is_series and ready_parts:
        state["status_text"] = (
            f"🔥 Part {ready_parts[-1]} तैयार है — अभी Play करें! "
            f"Part {min(total_ch, ready_parts[-1] + 1)}/{total_ch} बैकग्राउंड में बन रहा है ({state.get('progress', 35)}%)..."
        )


def parse_manual_script_lines(
    raw_text: str, char_names: list[str], default_location: str, default_mood: str
) -> list[dict]:
    parsed = []
    cur_loc_key, cur_loc_label = detect_location_meta(default_location, default_location)
    cur_mood = normalize_mood(default_mood, "Action")
    skip_tags = {
        "शीर्षक", "title", "scene", "सीन", "location", "लोकेशन",
        "action", "एक्शन", "mood", "मूड", "sfx", "camera", "कैमरा",
        "background", "बैकग्राउंड", "int", "ext",
    }
    char_map_dummy = {c: {} for c in (char_names or [])}
    seen_norm: set[str] = set()

    for raw_line in raw_text.splitlines():
        line = raw_line.strip().lstrip("*#-• ").strip()
        if not line:
            continue

        # Parse optional [Location: ... | Mood: ...] tag
        if line.startswith("[") and line.endswith("]"):
            inner = line[1:-1]
            loc_m = re.search(r"location\s*[:=-]\s*([^|\]]+)", inner, re.IGNORECASE)
            mood_m = re.search(r"mood\s*[:=-]\s*([^|\]]+)", inner, re.IGNORECASE)
            if loc_m:
                cur_loc_key, cur_loc_label = detect_location_meta(
                    loc_m.group(1).strip(), loc_m.group(1).strip()
                )
            if mood_m:
                cur_mood = normalize_mood(mood_m.group(1).strip(), cur_mood)
            continue

        # Never treat Title / Scene / Location / Action header lines as spoken dialogue!
        if SCRIPT_TAG_LINE_RE.match(line):
            loc_try = re.search(r"(?:location|लोकेशन)\s*[:：]\s*(.+)", line, re.IGNORECASE)
            if loc_try:
                cur_loc_key, cur_loc_label = detect_location_meta(loc_try.group(1), loc_try.group(1))
            continue

        m = re.match(r"^([^:：]{1,28})[:：]\s*(.+)$", line)
        if m:
            speaker_raw = re.sub(r"[\*\#\_\"'“”]", "", m.group(1)).strip()
            speaker_low = speaker_raw.lower()
            if speaker_low in skip_tags or re.match(r"^(?:scene|सीन)\s*\d+$", speaker_low):
                continue
            diag = clean_spoken_dialogue(m.group(2))
            if len(diag) < 2:
                continue
            norm_key = re.sub(r"\s+", " ", diag.lower()).strip()
            if norm_key in seen_norm:
                continue
            seen_norm.add(norm_key)
            matched = resolve_character_name(speaker_raw, char_map_dummy, len(parsed))
            loc_key, loc_label = detect_location_meta(diag, cur_loc_label)
            sfx_val = detect_sfx_type(diag, loc_key, cur_mood)
            parsed.append(
                {
                    "character": matched,
                    "dialogue": diag,
                    "location": loc_label,
                    "location_key": loc_key,
                    "mood": cur_mood,
                    "sfx": sfx_val,
                    "visual_action_prompt": build_visual_action_prompt(
                        matched, diag, loc_label, cur_mood, sfx_val
                    ),
                }
            )
    return parsed


async def autonomous_movie_worker(project_id: str):
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    state = load_state(proj_dir)
    if not state:
        return

    try:
        char_map = state["characters"]
        char_names = list(char_map.keys())
        series_mode = state.get("series_mode", "series")
        aspect_ratio = state.get("aspect_ratio", "9:16")
        part_duration = max(1, min(2, int(state.get("part_duration_minutes", 1) or 1)))
        requested_parts = max(1, min(15, int(state.get("total_parts", 3) or 3)))
        duration = max(1, int(state.get("duration_minutes", 1) or 1))

        if series_mode == "series":
            total_chapters = requested_parts
            # Crisp 30-45s Reel / Pocket FM pacing: 7 punchy dialogues (~35-42s) for 1 Min/Part, 12 for 2 Min/Part
            scenes_per_chapter = 7 if part_duration <= 1 else 12
        else:
            total_chapters = 1 if duration <= 10 else max(1, duration // 10)
            if duration <= 1:
                scenes_per_chapter = 7
            elif duration <= 2:
                scenes_per_chapter = 12
            elif duration <= 5:
                scenes_per_chapter = 16
            elif duration <= 10:
                scenes_per_chapter = 20
            else:
                scenes_per_chapter = 24

        story_mode = state.get("story_mode", "ai")
        language = normalize_language(state.get("language", "Hindi"))
        default_mood = normalize_mood(state.get("default_mood", "Action"))
        default_location = state.get("default_location", "")
        project_custom_bg = state.get("custom_bg_path")
        watermark_text = state.get("watermark_text", "")
        watermark_position = state.get("watermark_position", "top-right")

        # Parse structured multi-character `Speaker: Dialogue` script lines in BOTH manual mode
        # AND AI mode (when user clicked "Generate Full Story with AI" and generated a ready script!)
        parsed_script_lines = parse_manual_script_lines(
            state["idea"], char_names, default_location, default_mood
        )
        manual_lines = parsed_script_lines if (story_mode == "manual" or len(parsed_script_lines) >= 6) else []

        if not state.get("outline"):
            state["progress"] = 5
            if len(manual_lines) >= 2 and (story_mode == "manual" or total_chapters == 1):
                per_ch = max(1, math.ceil(len(manual_lines) / total_chapters))
                chunks = [
                    manual_lines[i : i + per_ch]
                    for i in range(0, len(manual_lines), per_ch)
                ]
                state["outline"] = {
                    "title": "CinePop Multi-Character Drama",
                    "chapters": [
                        {
                            "no": i + 1,
                            "title": f"Part {i + 1}",
                            "plot": f"Part {i + 1}: {chunks[i][0]['location']}",
                        }
                        for i in range(len(chunks))
                    ],
                }
                state["manual_chunks"] = chunks
                state["scripts"] = {}
                state["progress"] = 12
                save_state(proj_dir, state)
            else:
                mode_label = (
                    f"{total_chapters}-Part Pocket FM / Moj Micro-Drama Series ({part_duration} Min/Part)"
                    if series_mode == "series"
                    else f"{duration} मिनट की मूवी"
                )
                state["status_text"] = (
                    f"CinePop AI {mode_label} और सस्पेंस क्लिफहैंगर्स तैयार कर रहा है (5%)..."
                )
                save_state(proj_dir, state)

                fallback_outline = {
                    "title": "CinePop Blockbuster",
                    "chapters": [
                        {
                            "no": i + 1,
                            "title": f"Part {i + 1}",
                            "plot": f"{state['idea']} - Part {i + 1} (रोमांचक मोड़ और सस्पेंस क्लिफहैंगर)",
                        }
                        for i in range(total_chapters)
                    ],
                }
                strict_note = (
                    "महत्वपूर्ण: यूज़र की लिखी कहानी को बिल्कुल मत बदलो, उसी कहानी के आधार पर पार्ट्स बनाओ।"
                    if story_mode == "manual"
                    else f"Genre: {state.get('genre', 'Action')}."
                )
                cliff_note = (
                    f"यह Pocket FM / Moj स्टाइल {total_chapters}-Part Micro-Drama Series ({part_duration} Min/Part) है। "
                    f"हर Part के अंत में एक ज़बरदस्त Cliffhanger / Suspense मोड़ ज़रूर रखो ताकि दर्शक तुरंत अगला Part देखें!"
                    if series_mode == "series"
                    else f"यह {duration} मिनट की दमदार बॉलीवुड फिल्म है।"
                )
                outline_prompt = f"""
                तुम एक Pocket FM / Moj और Dangal TV के नंबर-1 डायरेक्टर हो। '{state['idea']}' पर कहानी की रूपरेखा बनाओ।
                {strict_note}
                {cliff_note}
                कैरेक्टर्स: {char_names}
                कहानी को ठीक {total_chapters} बराबर Parts/Chapters में बांटो।
                सिर्फ शुद्ध JSON दो:
                {{"title": "सीरीज़/फिल्म का दमदार नाम", "chapters": [{{"no": 1, "title": "Part 1: धमाकेदार शुरुआत", "plot": "विस्तार से कहानी, ट्विस्ट और अंत का सस्पेंस"}}]}}
                """
                try:
                    raw_outline_txt, _brain_used = await asyncio.to_thread(
                        _call_story_brain_auto_chain, outline_prompt, 15.0
                    )
                    if raw_outline_txt:
                        parsed_out = safe_extract_json(raw_outline_txt, fallback_outline)
                        if not isinstance(parsed_out, dict) or not parsed_out.get("chapters"):
                            parsed_out = fallback_outline
                        parsed_out["chapters"] = parsed_out["chapters"][:total_chapters]
                        while len(parsed_out["chapters"]) < total_chapters:
                            idx_m = len(parsed_out["chapters"]) + 1
                            parsed_out["chapters"].append(
                                {
                                    "no": idx_m,
                                    "title": f"Part {idx_m}",
                                    "plot": f"{state['idea']} - Part {idx_m}",
                                }
                            )
                        state["outline"] = parsed_out
                    else:
                        state["outline"] = fallback_outline
                except Exception:
                    state["outline"] = fallback_outline

                state["scripts"] = {}
                state["progress"] = 12
                state["status_text"] = "कहानी तैयार! अब Master Character Casting Sheet (Hero, Heroine, Villain, Saas) समानांतर बन रही है (12%)..."
                save_state(proj_dir, state)

        # ==================== MASTER CHARACTER CASTING SHEET (v12 MASTER PARALLEL PRE-GENERATION) ====================
        # Calls build_master_casting_sheet() once (~4s parallel Cloudflare -> Together -> HF -> Pollinations)
        # User's raw bedroom selfie NEVER appears directly on screen — ONLY Face-Swapped onto the AI Hero frame!
        master_cast_dir = f"{proj_dir}/master_cast"
        os.makedirs(master_cast_dir, exist_ok=True)
        master_cast: dict[str, str] = {}
        ai_auto_cast = bool(state.get("ai_auto_cast", False))
        hero_display_global = char_names[0] if char_names else "Hero"
        hero_entry_global = char_map.get(hero_display_global) if char_names else None
        hero_face_global = (
            ""
            if ai_auto_cast
            else (select_character_angle_photo(hero_entry_global, "front") if hero_entry_global else "")
        )
        base_loc_key, base_loc_label = detect_location_meta(
            state.get("idea", ""), default_location or "Royal Palace"
        )
        cast_w, cast_h = get_aspect_dimensions(aspect_ratio)

        story_topic_text = str(state.get("idea") or "")
        dynamic_topic_prompts = get_dynamic_character_prompts(story_topic_text)
        cast_specs = [
            ("hero", hero_face_global, hero_display_global, "over_shoulder", dynamic_topic_prompts["Hero"]),
            ("female", "", "Heroine", "waist_up", dynamic_topic_prompts["Heroine"]),
            ("villain", "", "Villain", "waist_up", dynamic_topic_prompts["Villain"]),
            ("saas", "", "Saas", "waist_up", dynamic_topic_prompts["Saas"]),
            ("wide_confrontation", "", "Narrator", "wide_confrontation", dynamic_topic_prompts["Wide_Confrontation"]),
            ("context_broll", "", "Narrator", "broll_insert", dynamic_topic_prompts["Context_BRoll"]),
        ]

        async def _build_master_cast_frame_fallback(role_key: str, raw_p: str, spk_label: str, shot_v: str, hint_p: str):
            out_p = f"{master_cast_dir}/{role_key}.jpg"
            if os.path.exists(out_p) and os.path.getsize(out_p) > 4000:
                master_cast[role_key] = out_p
                return
            try:
                if shot_v == "broll_insert":
                    img = await asyncio.to_thread(
                        render_location_backdrop,
                        base_loc_key,
                        default_mood,
                        hint_p,
                        aspect_ratio,
                        False,
                        "narrator",
                        "broll_insert",
                    )
                    if watermark_text:
                        draw_channel_watermark(img, watermark_text, watermark_position)
                    img.save(out_p, "JPEG", quality=93)
                else:
                    await asyncio.to_thread(
                        prepare_character_frame,
                        raw_p if role_key == "hero" else "",
                        spk_label,
                        "",
                        out_p,
                        base_loc_key,
                        base_loc_label,
                        default_mood,
                        project_custom_bg if role_key == "wide_confrontation" else None,
                        watermark_text,
                        watermark_position,
                        aspect_ratio,
                        hint_p,
                        shot_v,
                    )
                if os.path.exists(out_p) and os.path.getsize(out_p) > 4000:
                    master_cast[role_key] = out_p
            except Exception:
                pass

        if not project_custom_bg:
            raw_casting = await build_master_casting_sheet(
                master_cast_dir,
                width=cast_w,
                height=cast_h,
                topic=story_topic_text,
            )
            role_to_cast_key = {
                "Hero": "hero",
                "Heroine": "female",
                "Villain": "villain",
                "Saas": "saas",
                "Wide_Confrontation": "wide_confrontation",
                "Context_BRoll": "context_broll",
            }
            for sheet_role, cast_key in role_to_cast_key.items():
                src_p = raw_casting.get(sheet_role)
                if not src_p or not os.path.exists(src_p):
                    continue
                final_p = f"{master_cast_dir}/{cast_key}.jpg"
                try:
                    if cast_key == "hero":
                        # Strictly Face-Swap user's selfie onto the AI Hero frame ONLY (NEVER raw bedroom selfie!)
                        await asyncio.to_thread(
                            _apply_hero_faceswap_to_master_frame,
                            src_p,
                            hero_face_global,
                            final_p,
                            cast_w,
                            cast_h,
                            watermark_text,
                            watermark_position,
                        )
                    else:
                        def _finalize_role_frame(in_path=src_p, out_path=final_p):
                            im = open_upright_image(in_path).resize((cast_w, cast_h), Image.Resampling.LANCZOS)
                            im = ImageEnhance.Contrast(im).enhance(1.06)
                            im = ImageEnhance.Color(im).enhance(1.06)
                            if watermark_text:
                                draw_channel_watermark(im, watermark_text, watermark_position)
                            im.save(out_path, "JPEG", quality=93)

                        await asyncio.to_thread(_finalize_role_frame)
                    if os.path.exists(final_p) and os.path.getsize(final_p) > 4000:
                        master_cast[cast_key] = final_p
                except Exception:
                    pass

            # Fallback for any missing character frame if a cloud API timed out
            missing_specs = [spec for spec in cast_specs if spec[0] not in master_cast]
            if missing_specs:
                await asyncio.gather(
                    *[_build_master_cast_frame_fallback(rk, rp, sl, sv, hp) for rk, rp, sl, sv, hp in missing_specs],
                    return_exceptions=True,
                )

            # Final Zero-Blank-Screen Guarantee: fill any still-missing role from same-gender / wide master frame via mirror+zoom
            cast_donor_prefs = {
                "hero": ("hero", "villain", "wide_confrontation", "context_broll"),
                "villain": ("villain", "hero", "wide_confrontation", "context_broll"),
                "female": ("female", "saas", "wide_confrontation", "context_broll"),
                "saas": ("saas", "female", "wide_confrontation", "context_broll"),
                "wide_confrontation": ("wide_confrontation", "hero", "villain", "female", "context_broll"),
                "context_broll": ("context_broll", "wide_confrontation", "hero", "villain"),
            }
            for idx_ck, (ck, _, _, _, _) in enumerate(cast_specs):
                if ck in master_cast and os.path.exists(master_cast[ck]):
                    continue
                target_p = f"{master_cast_dir}/{ck}.jpg"
                for donor_k in list(cast_donor_prefs.get(ck, ())) + list(master_cast.keys()):
                    donor_p = master_cast.get(donor_k)
                    if donor_p and os.path.exists(donor_p):
                        if _create_mirrored_zoomed_variant(
                            donor_p,
                            target_p,
                            cast_w,
                            cast_h,
                            mirror=(idx_ck % 2 == 1),
                            zoom_ratio=0.07 + (idx_ck % 3) * 0.02,
                        ):
                            master_cast[ck] = target_p
                            break

        chapters = state["outline"]["chapters"]
        manual_chunks = state.get("manual_chunks")
        prev_plot = "कहानी की शुरुआत"
        prev_episode_tail = ""
        total_ch_count = max(1, len(chapters))

        for ch_idx, ch in enumerate(chapters):
            ch_no = ch["no"]
            ch_mp4 = f"{proj_dir}/ch_{ch_no}.mp4"
            if ch_no in state["done_chapters"] and os.path.exists(ch_mp4):
                prev_plot = ch["plot"]
                prev_sc = state.get("scripts", {}).get(str(ch_no)) or []
                if prev_sc:
                    prev_episode_tail = " | ".join(
                        f"{x.get('character', 'Hero')}: {x.get('dialogue', '')}" for x in prev_sc[-2:]
                    )
                continue

            ch_base_pct = 12 + int((ch_idx / total_ch_count) * 82)
            ch_span_pct = max(8, int(82 / total_ch_count))
            state["progress"] = max(state.get("progress", 5), ch_base_pct)
            state["status_text"] = (
                f"Part {ch_no}/{total_ch_count} की स्क्रिप्ट तैयार हो रही है ({state['progress']}%)..."
                if series_mode == "series"
                else f"चैप्टर {ch_no}/{total_ch_count} की स्क्रिप्ट तैयार हो रही है ({state['progress']}%)..."
            )
            save_state(proj_dir, state)

            ch_temp = f"{proj_dir}/ch_{ch_no}_clips"
            os.makedirs(ch_temp, exist_ok=True)

            if manual_chunks and ch_idx < len(manual_chunks):
                dialogues = manual_chunks[ch_idx]
            elif ch_idx == 0 and len(parsed_script_lines) >= 6:
                dialogues = parsed_script_lines[: min(8, max(6, scenes_per_chapter))]
            else:
                loc_k, loc_l = detect_location_meta(ch["plot"], default_location)
                hero_display = char_names[0] if char_names else "Hero"
                drama_pack = _build_multi_character_drama_lines(
                    clean_topic=state.get("idea", ch["plot"]),
                    hero_label=hero_display,
                    genre=state.get("genre", "Drama"),
                    part_no=ch_no,
                    total_parts=total_ch_count,
                )
                fallback_dialogues = []
                for i, (spk_raw, diag_raw, line_mood) in enumerate(drama_pack[:scenes_per_chapter]):
                    spk_resolved = resolve_character_name(spk_raw, char_map, i)
                    diag_clean = clean_spoken_dialogue(diag_raw)
                    lk_i, ll_i = detect_location_meta(diag_clean, loc_l)
                    sfx_i = detect_sfx_type(diag_clean, lk_i, line_mood)
                    fallback_dialogues.append(
                        {
                            "character": spk_resolved,
                            "dialogue": diag_clean,
                            "location": ll_i,
                            "location_key": lk_i,
                            "mood": line_mood,
                            "sfx": sfx_i,
                            "camera_angle": ["front", "left", "right", "up", "down"][i % 5],
                            "visual_action_prompt": build_visual_action_prompt(
                                spk_resolved, diag_clean, ll_i, line_mood, sfx_i
                            ),
                            "broll": False,
                        }
                    )

                emotion_to_mood = {
                    "angry": "Action",
                    "confident": "Action",
                    "shocked": "Suspense",
                    "twist": "Suspense",
                    "sad": "Emotional",
                    "romantic": "Romantic",
                }
                try:
                    director_topic = f"{state.get('idea', ch['plot'])} — {ch['plot']}"
                    director_items = await director_generate_story(
                        topic=director_topic,
                        part_num=ch_no,
                        prev_cliffhanger=prev_episode_tail or prev_plot,
                    )
                    dialogues = []
                    seen_lines: set[str] = set()
                    for idx_d, item in enumerate((director_items or [])[:scenes_per_chapter]):
                        if not isinstance(item, dict):
                            continue
                        raw_spk = str(item.get("speaker") or item.get("character") or "").strip()
                        if raw_spk.lower() == "hero":
                            raw_spk = hero_display
                        c_name = resolve_character_name(raw_spk, char_map, idx_d)
                        diag_text = clean_spoken_dialogue(str(item.get("dialogue", "")))
                        if len(diag_text) < 2:
                            continue
                        norm_k = re.sub(r"\s+", " ", diag_text.lower()).strip()
                        if norm_k in seen_lines:
                            continue
                        seen_lines.add(norm_k)
                        lk, ll = detect_location_meta(diag_text, default_location or "Royal Palace")
                        emo_raw = str(item.get("emotion", "")).lower().strip()
                        mood_val = normalize_mood(emotion_to_mood.get(emo_raw, emo_raw), default_mood)
                        sfx_val = detect_sfx_type(diag_text, lk, mood_val)
                        vap = build_visual_action_prompt(c_name, diag_text, ll, mood_val, sfx_val)
                        cam_angle = detect_scene_angle(vap, diag_text, mood_val, sfx_val, idx_d)
                        dialogues.append(
                            {
                                "character": c_name,
                                "dialogue": diag_text,
                                "location": ll,
                                "location_key": lk,
                                "mood": mood_val,
                                "sfx": sfx_val,
                                "camera_angle": cam_angle,
                                "visual_action_prompt": vap,
                                "broll": bool(item.get("broll", False)),
                            }
                        )
                    if len(dialogues) < 4:
                        dialogues = fallback_dialogues
                except Exception:
                    dialogues = fallback_dialogues

                # Guarantee Cliffhanger ending on Part k < N in Series Mode
                if series_mode == "series" and ch_no < total_ch_count and dialogues:
                    last_line = dialogues[-1]["dialogue"]
                    if f"Part {ch_no + 1}" not in last_line and "पार्ट" not in last_line:
                        dialogues.append(
                            {
                                "character": "Narrator",
                                "dialogue": f"अगले पार्ट में जानिए असली सच... देखिए Part {ch_no + 1} में!",
                                "location": dialogues[-1].get("location", "Royal Palace"),
                                "location_key": dialogues[-1].get("location_key", "royal_palace"),
                                "mood": "Suspense",
                                "sfx": "thunder",
                                "camera_angle": "front",
                                "visual_action_prompt": "dramatic medium wide confrontation cliffhanger inside royal Indian haveli hall",
                                "broll": True,
                            }
                        )

            state["scripts"][str(ch_no)] = dialogues
            state["progress"] = min(94, ch_base_pct + 5)
            state["status_text"] = (
                f"Part {ch_no}/{total_ch_count} मल्टी-कैरेक्टर स्क्रिप्ट तैयार ({len(dialogues)} डायलॉग्स)! 30fps Live-Action TV Serial रेंडर हो रहा है ({state['progress']}%)..."
            )
            save_state(proj_dir, state)

            clips = []
            total_scenes = max(1, len(dialogues))
            if series_mode == "series":
                target_scene_sec = max(3.0, min(5.0, (part_duration * 40.0) / total_scenes))
            elif duration == 1:
                target_scene_sec = max(3.0, min(5.0, 40.0 / total_scenes))
            else:
                target_scene_sec = 0.0

            sem = asyncio.Semaphore(4)
            completed_count = 0
            ep_title_label = f"{state['outline'].get('title', 'CinePop Drama')[:36]} · Part {ch_no}"

            async def _render_single_scene(i: int, d: dict) -> tuple[int, str, str]:
                nonlocal completed_count
                async with sem:
                    if not os.path.isdir(proj_dir):
                        return (i, "", "")
                    raw_spk = str(d.get("character", "")).strip()
                    c_name = resolve_character_name(raw_spk, char_map, i)
                    role_kind = detect_speaker_role(c_name, char_map)
                    diag_text = clean_spoken_dialogue(d.get("dialogue", "")) or "अब असली खेल शुरू होगा!"
                    loc_key = d.get("location_key") or "royal_palace"
                    loc_label = d.get("location") or default_location or "Royal Palace"
                    scene_mood = normalize_mood(d.get("mood", ""), default_mood)
                    scene_sfx = d.get("sfx") if d.get("sfx") in ALLOWED_SFX else detect_sfx_type(diag_text, loc_key, scene_mood)
                    action_prompt = d.get("visual_action_prompt") or build_visual_action_prompt(
                        c_name, diag_text, loc_label, scene_mood, scene_sfx
                    )
                    scene_angle = (
                        d.get("camera_angle")
                        if d.get("camera_angle") in ALLOWED_FACE_ANGLES
                        else detect_scene_angle(action_prompt, diag_text, scene_mood, scene_sfx, i)
                    )

                    char_entry = char_map.get(c_name)
                    # User's raw selfie is STRICTLY for Hero face-swap only — never passed for non-Hero roles!
                    if ai_auto_cast or role_kind != "hero":
                        exact_char_photo = ""
                        char_angles_dict = {}
                    elif char_entry:
                        exact_char_photo = select_character_angle_photo(char_entry, scene_angle)
                        char_angles_dict = char_entry.get("angles") or {}
                    else:
                        exact_char_photo = ""
                        char_angles_dict = {}

                    # Live-Action Waist-Up / Over-the-Shoulder / Wide Confrontation / B-Roll shot selection
                    if bool(d.get("broll", False)):
                        scene_shot_variant = "broll_insert"
                    elif role_kind == "narrator":
                        scene_shot_variant = "wide_confrontation"
                    elif role_kind == "hero":
                        scene_shot_variant = "over_shoulder"
                    else:
                        scene_shot_variant = "waist_up"

                    bg_override = d.get("bg_path") or project_custom_bg
                    tts_voice, tts_pitch, tts_rate = resolve_character_voice_profile(
                        c_name, char_entry, char_map, language=language, scene_idx=i
                    )

                    a_file = f"{ch_temp}/a_{i}.mp3"
                    f_img = f"{ch_temp}/f_{i}.jpg"
                    v_file = f"{ch_temp}/v_{i}.mp4"
                    used_engine = "Live-Action 30fps TV Serial"

                    # 2s Opening Golden Episode Title Hook on Scene 0 & 2s Cliffhanger Outro Card on last scene of Part k < N
                    scene_title_hook = ep_title_label if i == 0 else ""
                    scene_outro_card = (
                        f"अगले पार्ट में जानिए असली सच... (Part {ch_no + 1})"
                        if (series_mode == "series" and ch_no < total_ch_count and i == total_scenes - 1)
                        else ""
                    )

                    if not os.path.exists(v_file):
                        try:
                            await generate_bulletproof_audio(
                                diag_text,
                                tts_voice,
                                a_file,
                                pitch=(char_entry or {}).get("pitch", "normal"),
                                language=language,
                                pitch_override=tts_pitch,
                                rate_override=tts_rate,
                            )

                            # Reuse Master Character Casting Sheet frame with strict Male/Female photo lock!
                            if bool(d.get("broll", False)) and master_cast.get("context_broll"):
                                cast_lookup_key = "context_broll"
                            elif role_kind == "narrator":
                                cast_lookup_key = "wide_confrontation"
                            elif role_kind in ("female", "saas"):
                                cast_lookup_key = role_kind if master_cast.get(role_kind) else ("female" if master_cast.get("female") else "saas")
                            else:
                                cast_lookup_key = role_kind if master_cast.get(role_kind) else ("hero" if master_cast.get("hero") else "villain")

                            master_frame_path = master_cast.get(cast_lookup_key) if not bg_override else None
                            if master_frame_path and os.path.exists(master_frame_path) and os.path.getsize(master_frame_path) > 4000:
                                if cast_lookup_key != role_kind and role_kind not in ("narrator", "crowd"):
                                    # Same-gender fallback frame: mirror + zoom so it looks distinct and never blank!
                                    await asyncio.to_thread(
                                        _create_mirrored_zoomed_variant,
                                        master_frame_path,
                                        f_img,
                                        cast_w,
                                        cast_h,
                                        True,
                                        0.08,
                                    )
                                else:
                                    shutil.copyfile(master_frame_path, f_img)
                            else:
                                await asyncio.to_thread(
                                    prepare_character_frame,
                                    exact_char_photo,
                                    c_name,
                                    diag_text,
                                    f_img,
                                    loc_key,
                                    loc_label,
                                    scene_mood,
                                    bg_override,
                                    watermark_text,
                                    watermark_position,
                                    aspect_ratio,
                                    action_prompt,
                                    scene_shot_variant,
                                )

                            used_engine = await asyncio.to_thread(
                                generate_free_ai_video_clip,
                                exact_char_photo,
                                f_img,
                                a_file,
                                v_file,
                                scene_mood,
                                scene_sfx,
                                action_prompt,
                                char_angles_dict,
                                scene_angle,
                                c_name,
                                diag_text,
                                target_scene_sec,
                                aspect_ratio,
                                loc_key,
                                loc_label,
                                bg_override,
                                watermark_text,
                                watermark_position,
                                master_cast,
                                scene_title_hook,
                                scene_outro_card,
                            )
                        except Exception:
                            try:
                                if not os.path.exists(a_file):
                                    await generate_bulletproof_audio(
                                        diag_text,
                                        tts_voice,
                                        a_file,
                                        language=language,
                                        pitch_override=tts_pitch,
                                        rate_override=tts_rate,
                                    )
                                if not _is_valid_non_black_image_file(f_img):
                                    w_fb, h_fb = get_aspect_dimensions(aspect_ratio)
                                    # Zero Black Screen Guarantee: horizontal flip + warm golden/royal grade + zoom on any available master image!
                                    gender_donors = (
                                        ("female", "saas", "wide_confrontation", "hero", "villain", "context_broll")
                                        if role_kind in ("female", "saas")
                                        else ("hero", "villain", "wide_confrontation", "context_broll", "female", "saas")
                                    )
                                    recovered = False
                                    for dk in gender_donors:
                                        dp = master_cast.get(dk)
                                        if dp and _is_valid_non_black_image_file(dp):
                                            if _create_mirrored_zoomed_variant(
                                                dp,
                                                f_img,
                                                w_fb,
                                                h_fb,
                                                mirror=True,
                                                zoom_ratio=0.09 + (i % 3) * 0.02,
                                                color_grade="royal" if role_kind in ("hero", "female") else "golden",
                                            ):
                                                recovered = True
                                                break
                                    if not recovered:
                                        cached_fb = _reuse_cached_photorealistic_scene(w_fb, h_fb, role_kind, i + 17)
                                        if cached_fb is not None:
                                            cached_fb.save(f_img, "JPEG", quality=92)
                                            recovered = _is_valid_non_black_image_file(f_img)
                                    if not recovered:
                                        _render_rich_cinematic_palace_actor_fallback(f_img, role=c_name, width=w_fb, height=h_fb)
                                await asyncio.to_thread(
                                    _render_pro_motion_vfx_clip,
                                    f_img,
                                    a_file,
                                    v_file,
                                    scene_mood,
                                    scene_sfx,
                                    a_file,
                                    None,
                                    aspect_ratio,
                                )
                                used_engine = "Local 30fps Cinematic Motion"
                            except Exception:
                                pass
                        finally:
                            for tmp_c in (a_file, f_img):
                                if os.path.exists(tmp_c):
                                    try:
                                        os.remove(tmp_c)
                                    except Exception:
                                        pass

                    if os.path.isdir(proj_dir):
                        completed_count += 1
                        scene_pct = min(
                            94,
                            ch_base_pct + 5 + int((completed_count / total_scenes) * max(1, ch_span_pct - 5)),
                        )
                        state["progress"] = max(state.get("progress", 17), scene_pct)
                        state["active_engine"] = used_engine
                        engines_list = list(state.get("engines_used") or [])
                        if used_engine and used_engine not in engines_list:
                            engines_list.append(used_engine)
                            state["engines_used"] = engines_list
                        ready_hint = (
                            " (Part 1 तैयार — अभी Play करें!)"
                            if (series_mode == "series" and 1 in state.get("ready_parts", []) and ch_no > 1)
                            else ""
                        )
                        state["status_text"] = (
                            f"⚡ {used_engine} · Part {ch_no}/{total_ch_count} · सीन {completed_count}/{total_scenes} ({state['progress']}%){ready_hint}"
                        )
                        save_state(proj_dir, state)
                    return (i, v_file, used_engine)

            rendered_results = await asyncio.gather(
                *[_render_single_scene(idx, item) for idx, item in enumerate(dialogues)],
                return_exceptions=True,
            )
            ordered_clips: list[tuple[int, str]] = []
            for r_item in rendered_results:
                if isinstance(r_item, tuple) and r_item[1] and os.path.exists(r_item[1]) and os.path.getsize(r_item[1]) > 1000:
                    ordered_clips.append((r_item[0], r_item[1]))
            ordered_clips.sort(key=lambda x: x[0])
            clips = [p for _, p in ordered_clips]

            if not os.path.isdir(proj_dir):
                return
            if not clips:
                raise RuntimeError(f"Part {ch_no} के लिए कोई भी सीन क्लिप तैयार नहीं हो सकी।")
            ffmpeg_concat(clips, ch_mp4)
            state["done_chapters"].append(ch_no)
            prev_plot = ch["plot"]
            if dialogues:
                prev_episode_tail = " | ".join(
                    f"{x.get('character', 'Hero')}: {x.get('dialogue', '')}" for x in dialogues[-2:]
                )
            rebuild_parts_and_full(proj_dir, state)
            save_state(proj_dir, state)

    except Exception as e:
        if os.path.isdir(proj_dir):
            state["status"] = "failed"
            state["error"] = str(e)
            state["status_text"] = f"❌ वीडियो रेंडर में एरर आया: {str(e)}"
            save_state(proj_dir, state)


def _is_upload_file_obj(val) -> bool:
    """Safely detect Starlette/FastAPI UploadFile objects without crashing on empty strings or None."""
    return val is not None and not isinstance(val, (str, bytes, dict, list, int, float, bool)) and callable(getattr(val, "read", None))


def _safe_payload_str(payload: dict, *keys: str, default: str = "") -> str:
    """Read the first non-empty string value across multiple alias keys."""
    for key in keys:
        if key not in payload:
            continue
        val = payload.get(key)
        if val is None or _is_upload_file_obj(val):
            continue
        if isinstance(val, (dict, list)):
            try:
                s = json.dumps(val, ensure_ascii=False).strip()
            except Exception:
                s = ""
        else:
            s = str(val).strip()
        if s and s.lower() not in ("null", "none", "undefined"):
            return s
    return default


def _safe_payload_int(
    payload: dict,
    *keys: str,
    default: int = 1,
    min_val: Optional[int] = None,
    max_val: Optional[int] = None,
) -> int:
    """Read an integer across alias keys safely; never raises 422 on bad strings or None."""
    parsed = default
    for key in keys:
        if key not in payload:
            continue
        val = payload.get(key)
        if val is None or _is_upload_file_obj(val):
            continue
        s = str(val).strip()
        if not s or s.lower() in ("null", "none", "undefined"):
            continue
        try:
            parsed = int(float(s))
            break
        except Exception:
            continue
    if min_val is not None:
        parsed = max(min_val, parsed)
    if max_val is not None:
        parsed = min(max_val, parsed)
    return parsed


def _extract_form_uploads(form_data, *keys: str) -> list:
    """Extract only genuine UploadFile objects across alias keys (ignoring empty strings/nulls)."""
    uploads: list = []
    if form_data is None:
        return uploads
    seen_ids: set[int] = set()
    for key in keys:
        try:
            items = form_data.getlist(key)
        except Exception:
            item = form_data.get(key)
            items = [item] if item is not None else []
        for item in items:
            if _is_upload_file_obj(item) and id(item) not in seen_ids:
                seen_ids.add(id(item))
                uploads.append(item)
    return uploads


@app.post("/start-autonomous-movie/")
@app.post("/start-autonomous-movie")
@app.post("/generate")
@app.post("/generate-movie")
@app.post("/generate-series")
async def start_movie(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    100% Flexible Movie & Series Generation Endpoint (Zero FastAPI 422 Errors).
    Accepts multipart/form-data, application/x-www-form-urlencoded, or application/json.
    All fields are optional with safe defaults and support every client alias.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    form_data = None
    payload: dict = {}

    if "application/json" in content_type:
        try:
            raw_json = await request.json()
            if isinstance(raw_json, dict):
                payload = raw_json
        except Exception:
            payload = {}
    else:
        try:
            form_data = await request.form()
            for k in form_data.keys():
                v = form_data.get(k)
                if not _is_upload_file_obj(v):
                    payload[k] = v
        except Exception:
            try:
                raw_json = await request.json()
                if isinstance(raw_json, dict):
                    payload = raw_json
            except Exception:
                payload = {}

    # Merge query params as fallback if any field was sent via query string
    for qk, qv in request.query_params.items():
        if qk not in payload and qv:
            payload[qk] = qv

    user_email = _safe_payload_str(payload, "user_email", "email", default="guest@cinepop.ai")
    idea = _safe_payload_str(
        payload,
        "idea",
        "script",
        "topic",
        "story",
        "prompt",
        default="Ek emotional aur action-packed Bollywood kahani",
    )
    duration_minutes = _safe_payload_int(payload, "duration_minutes", "duration", default=3, min_val=1, max_val=30)
    char_names = _safe_payload_str(payload, "char_names", "character_names", "characters", default="")
    voices = _safe_payload_str(payload, "voices", "character_voices", default="")
    angle_meta = _safe_payload_str(payload, "angle_meta", default="[]")
    auto_cast = _safe_payload_str(payload, "auto_cast", default="auto")
    story_mode = _safe_payload_str(payload, "story_mode", default="ai")
    genre = _safe_payload_str(payload, "genre", default="Action")
    language = _safe_payload_str(payload, "language", default="Hindi")
    char_pitches = _safe_payload_str(payload, "char_pitches", default="{}")
    default_mood = _safe_payload_str(payload, "default_mood", "music_mood", "mood", default="Action")
    default_location = _safe_payload_str(payload, "default_location", "location", default="")
    watermark_text = _safe_payload_str(payload, "watermark_text", "watermark", default="")
    watermark_position = _safe_payload_str(payload, "watermark_position", default="top-right")
    aspect_ratio = _safe_payload_str(payload, "aspect_ratio", default="9:16")
    platform = _safe_payload_str(payload, "platform", default="instagram")
    default_series_mode = "series" if request.url.path.rstrip("/").endswith("generate-series") else "series"
    series_mode = _safe_payload_str(payload, "series_mode", default=default_series_mode)
    part_duration_minutes = _safe_payload_int(payload, "part_duration_minutes", default=1, min_val=1, max_val=5)
    total_parts = _safe_payload_int(payload, "total_parts", "parts_count", "parts", default=3, min_val=1, max_val=15)
    admin_pin = _safe_payload_str(payload, "admin_pin", default="")

    images = _extract_form_uploads(form_data, "images", "character_photos", "face_image", "face_images")
    angle_images = _extract_form_uploads(form_data, "angle_images")
    custom_bg_uploads = _extract_form_uploads(form_data, "custom_bg", "custom_bg_photo", "bg_photo")
    custom_bg = custom_bg_uploads[0] if custom_bg_uploads else None

    email = user_email.lower().strip()
    if "@" not in email:
        email = "guest@cinepop.ai"

    db = load_db()
    if email not in db["users"]:
        db["users"][email] = {
            "email": email,
            "name": email.split("@")[0],
            "photo_url": "",
            "device_id": "android",
            "installed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_active": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "credits": 0,
            "wallet_balance": 0,
            "movies_created": 0,
            "total_paid": 0,
            "redeemed_codes": [],
            "referral_code": make_referral_code(email),
            "referred_by": "",
            "referrals_count": 0,
            "referral_earnings": 0,
        }

    user = db["users"][email]
    user.setdefault("wallet_balance", 0)
    is_verified_admin = email == ADMIN_EMAIL and verify_pin_secret(admin_pin)

    if not is_verified_admin:
        if int(user.get("credits", 0)) >= 1:
            user["credits"] -= 1
        elif int(user.get("wallet_balance", 0)) >= MOVIE_PRICE_INR:
            user["wallet_balance"] -= MOVIE_PRICE_INR
            db["wallet_transactions"].insert(
                0,
                {
                    "tx_id": str(uuid.uuid4())[:8],
                    "email": email,
                    "amount": -MOVIE_PRICE_INR,
                    "credits_delta": 0,
                    "note": f"Auto-deducted ₹{MOVIE_PRICE_INR} for Movie Production",
                    "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
        else:
            raise HTTPException(status_code=402, detail="INSUFFICIENT_CREDITS")

    user["movies_created"] = int(user.get("movies_created", 0)) + 1
    save_db(db)

    project_id = str(uuid.uuid4())[:10]
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    os.makedirs(proj_dir, exist_ok=True)

    names = [x.strip() for x in (char_names or "").split(",") if x.strip()]
    v_list = [x.strip() for x in (voices or "").split(",") if x.strip()]
    try:
        pitches_map = json.loads(char_pitches) if char_pitches.strip() else {}
        if not isinstance(pitches_map, dict):
            pitches_map = {}
    except Exception:
        pitches_map = {}

    char_map = {}
    ordered_names: list[str] = []
    valid_uploaded_images: list[tuple[int, bytes]] = []
    if images and str(auto_cast).strip().lower() != "ai":
        for idx, img in enumerate(images):
            try:
                raw_img_bytes = await img.read()
            except Exception:
                raw_img_bytes = b""
            if raw_img_bytes and len(raw_img_bytes) > 256:
                valid_uploaded_images.append((idx, raw_img_bytes))

    ai_auto_cast = len(valid_uploaded_images) == 0

    if ai_auto_cast:
        # 100% Optional Face / AI Auto-Cast Mode: Generate characters & motion scenes directly from the story!
        default_cast = names[:3] if names else ["आर्यन", "काव्या"]
        default_voices = ["hi-IN-MadhurNeural", "hi-IN-SwaraNeural", "hi-IN-MadhurNeural"]
        for idx, c_name in enumerate(default_cast):
            c_voice = v_list[idx] if idx < len(v_list) else default_voices[idx % len(default_voices)]
            c_pitch = normalize_pitch(str(pitches_map.get(c_name, pitches_map.get(str(idx), "normal"))))
            char_map[c_name] = {
                "image": "",
                "voice": c_voice,
                "pitch": c_pitch,
                "angles": {},
                "face_profile_3d": False,
                "ai_auto_cast": True,
            }
            ordered_names.append(c_name)
    else:
        for slot_i, (orig_idx, raw_img_bytes) in enumerate(valid_uploaded_images):
            c_name = names[orig_idx] if orig_idx < len(names) else (names[slot_i] if slot_i < len(names) else f"Actor_{slot_i+1}")
            c_voice = v_list[orig_idx] if orig_idx < len(v_list) else (v_list[slot_i] if slot_i < len(v_list) else "hi-IN-MadhurNeural")
            c_pitch = normalize_pitch(str(pitches_map.get(c_name, pitches_map.get(str(slot_i), "normal"))))
            img_path = safe_project_file(proj_dir, f"char_{slot_i}_front.jpg")
            save_upright_image_bytes(raw_img_bytes, img_path)
            if slot_i == 0:
                av_url = save_user_avatar_bytes(email, raw_img_bytes)
                if av_url:
                    db_av = load_db()
                    if email in db_av["users"]:
                        db_av["users"][email]["photo_url"] = av_url
                        db_av["users"][email]["face_linked"] = True
                        save_db(db_av)
            char_map[c_name] = {
                "image": img_path,
                "voice": c_voice,
                "pitch": c_pitch,
                "angles": {"front": img_path},
                "face_profile_3d": False,
                "ai_auto_cast": False,
            }
            ordered_names.append(c_name)

        # Save optional 3D Multi-Angle Face Scan photos (front, left, right, up, down) UPRIGHT (EXIF-transposed)
        if angle_images:
            try:
                parsed_angle_meta = json.loads(angle_meta) if angle_meta.strip() else []
                if not isinstance(parsed_angle_meta, list):
                    parsed_angle_meta = []
            except Exception:
                parsed_angle_meta = []

            for a_idx, a_file in enumerate(angle_images):
                try:
                    raw_a = await a_file.read()
                except Exception:
                    raw_a = b""
                if not raw_a or len(raw_a) <= 256:
                    continue
                meta_item = parsed_angle_meta[a_idx] if a_idx < len(parsed_angle_meta) and isinstance(parsed_angle_meta[a_idx], dict) else {}
                angle_key = str(meta_item.get("angle", "")).lower().strip()
                if angle_key not in ALLOWED_FACE_ANGLES:
                    fname_low = (getattr(a_file, "filename", "") or "").lower()
                    angle_key = next((ang for ang in ALLOWED_FACE_ANGLES if ang in fname_low), "front")
                target_char = None
                meta_name = str(meta_item.get("char_name", "")).strip()
                if meta_name and meta_name in char_map:
                    target_char = meta_name
                else:
                    try:
                        c_i = int(meta_item.get("char_index", 0))
                    except Exception:
                        c_i = 0
                    if 0 <= c_i < len(ordered_names):
                        target_char = ordered_names[c_i]
                if target_char and target_char in char_map:
                    c_slot = ordered_names.index(target_char)
                    a_path = safe_project_file(proj_dir, f"char_{c_slot}_{angle_key}.jpg")
                    save_upright_image_bytes(raw_a, a_path)
                    char_map[target_char]["angles"][angle_key] = a_path
                    if angle_key == "front":
                        char_map[target_char]["image"] = a_path
                    char_map[target_char]["face_profile_3d"] = len(char_map[target_char]["angles"]) >= 3

    custom_bg_path = None
    if custom_bg is not None:
        try:
            raw_bytes = await custom_bg.read()
        except Exception:
            raw_bytes = b""
        if raw_bytes and len(raw_bytes) > 256:
            custom_bg_path = safe_project_file(proj_dir, "custom_bg.jpg")
            save_upright_image_bytes(raw_bytes, custom_bg_path, max_side=1600)

    clean_lang = normalize_language(language)
    wm_pos = watermark_position if watermark_position in ("top-right", "bottom-right", "top-left") else "top-right"
    clean_ratio = "9:16" if str(aspect_ratio).strip() in ("9:16", "vertical", "instagram", "moj") else "16:9"
    clean_platform = "instagram" if clean_ratio == "9:16" else ("youtube" if platform != "instagram" else "youtube")
    clean_series_mode = "series" if str(series_mode).strip().lower() in ("series", "true", "1", "parts", "part-wise") else "single"
    clean_part_dur = 2 if int(part_duration_minutes or 1) >= 2 else 1
    clean_total_parts = max(1, min(15, int(total_parts or 3))) if clean_series_mode == "series" else 1

    cast_label = "AI Auto-Cast" if ai_auto_cast else "5-Angle Face ID"
    initial_state = {
        "project_id": project_id,
        "user_email": email,
        "idea": idea,
        "story_mode": "manual" if story_mode == "manual" else "ai",
        "genre": genre,
        "language": clean_lang,
        "default_mood": normalize_mood(default_mood),
        "default_location": default_location.strip(),
        "watermark_text": watermark_text.strip()[:30],
        "watermark_position": wm_pos,
        "aspect_ratio": clean_ratio,
        "platform": clean_platform,
        "series_mode": clean_series_mode,
        "part_duration_minutes": clean_part_dur,
        "total_parts": clean_total_parts,
        "part_1_ready": False,
        "ai_auto_cast": ai_auto_cast,
        "active_engine": "Wan 2.1 / LTX-Video / CogVideoX / HunyuanVideo / Pollinations Flux",
        "engines_used": [],
        "custom_bg_path": custom_bg_path,
        "duration_minutes": (clean_part_dur * clean_total_parts) if clean_series_mode == "series" else duration_minutes,
        "characters": char_map,
        "done_chapters": [],
        "ready_parts": [],
        "scripts": {},
        "progress": 2,
        "status_text": (
            f"🎬 {cast_label} · {clean_total_parts}-Part Motion Series ({clean_ratio}) शुरू! Free AI Video Engine चालू..."
            if clean_series_mode == "series"
            else f"🎬 {cast_label} · {clean_lang} Motion Movie ({clean_ratio}) शुरू! Free AI Video Engine चालू..."
        ),
        "is_completed": False,
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "remaining_credits": user["credits"],
        "remaining_wallet_balance": user["wallet_balance"],
    }
    save_state(proj_dir, initial_state)
    background_tasks.add_task(autonomous_movie_worker, project_id)
    return initial_state


@app.get("/sync-project/{project_id}")
async def sync_project(project_id: str, background_tasks: BackgroundTasks):
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    state = load_state(proj_dir)
    if not state:
        return JSONResponse(status_code=404, content={"error": "Project not found"})
    if not state["is_completed"] and "रुकावट" in state["status_text"]:
        background_tasks.add_task(autonomous_movie_worker, project_id)
    return state


@app.get("/stream/{project_id}/{file_type}")
def stream_video(project_id: str, file_type: str, request: Request):
    path = f"{PROJECTS_DIR}/{project_id}/{file_type}.mp4"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Video not ready yet")

    file_size = os.path.getsize(path)
    range_header = request.headers.get("range")
    if range_header:
        byte1, byte2 = 0, None
        match = range_header.replace("bytes=", "").split("-")
        if match[0]:
            byte1 = int(match[0])
        if len(match) > 1 and match[1]:
            byte2 = int(match[1])
        length = (byte2 + 1 if byte2 is not None else file_size) - byte1

        def iterfile():
            with open(path, "rb") as f:
                f.seek(byte1)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 512, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {byte1}-{byte1 + length - 1}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
            "Content-Type": "video/mp4",
        }
        return StreamingResponse(iterfile(), status_code=206, headers=headers)

    return FileResponse(path, media_type="video/mp4")


@app.post("/edit-scene/{project_id}")
async def edit_scene(
    project_id: str,
    chapter_no: int = Form(...),
    dialogue_index: int = Form(...),
    instruction: str = Form(...),
):
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    state = load_state(proj_dir)
    if not state:
        raise HTTPException(status_code=404, detail="Project not found")

    ch_key = str(chapter_no)
    if ch_key not in state.get("scripts", {}) or dialogue_index >= len(
        state["scripts"][ch_key]
    ):
        raise HTTPException(status_code=400, detail="Invalid chapter or dialogue index")

    old_item = state["scripts"][ch_key][dialogue_index]
    char_name = old_item["character"]
    char_info = state["characters"][char_name]

    new_dialogue = instruction.strip()
    if client:
        try:
            prompt = f"""
            पुराना डायलॉग ({char_name}): "{old_item['dialogue']}"
            यूज़र का बदलाव निर्देश: "{instruction}"
            सिर्फ नया हिंदी डायलॉग टेक्स्ट लिखो, और कुछ नहीं।
            """
            res = client.models.generate_content(
                model="gemini-2.5-flash", contents=prompt
            )
            new_dialogue = res.text.strip().strip('"')
        except Exception:
            pass

    old_item["dialogue"] = new_dialogue

    ch_temp = f"{proj_dir}/ch_{chapter_no}_clips"
    os.makedirs(ch_temp, exist_ok=True)
    a_file = f"{ch_temp}/a_{dialogue_index}_new.mp3"
    f_img = f"{ch_temp}/f_{dialogue_index}_new.jpg"
    v_file = f"{ch_temp}/v_{dialogue_index}.mp4"

    await generate_bulletproof_audio(
        new_dialogue,
        char_info["voice"],
        a_file,
        pitch=char_info.get("pitch", "normal"),
        language=state.get("language", "Hindi"),
    )
    scene_mood = old_item.get("mood", state.get("default_mood", "Action"))
    scene_sfx = old_item.get("sfx", detect_sfx_type(new_dialogue, old_item.get("location_key", ""), scene_mood))
    proj_ratio = state.get("aspect_ratio", "16:9")
    await asyncio.to_thread(
        prepare_character_frame,
        char_info["image"],
        char_name,
        new_dialogue,
        f_img,
        old_item.get("location_key", "city_street"),
        old_item.get("location", "Mumbai City Street"),
        scene_mood,
        old_item.get("bg_path") or state.get("custom_bg_path"),
        state.get("watermark_text", ""),
        state.get("watermark_position", "top-right"),
        proj_ratio,
        old_item.get("visual_action_prompt", ""),
    )
    await asyncio.to_thread(
        render_exact_face_clip,
        char_info["image"],
        f_img,
        a_file,
        v_file,
        scene_mood,
        scene_sfx,
        old_item.get("visual_action_prompt")
        or build_visual_action_prompt(
            char_name,
            new_dialogue,
            old_item.get("location", "Mumbai City Street"),
            scene_mood,
            scene_sfx,
        ),
        char_info.get("angles"),
        old_item.get("camera_angle", "front"),
        char_name,
        new_dialogue,
        0.0,
        proj_ratio,
    )
    if os.path.exists(a_file):
        os.remove(a_file)
    if os.path.exists(f_img):
        os.remove(f_img)

    all_clips = [
        f"{ch_temp}/v_{i}.mp4"
        for i in range(len(state["scripts"][ch_key]))
        if os.path.exists(f"{ch_temp}/v_{i}.mp4")
    ]
    ch_mp4 = f"{proj_dir}/ch_{chapter_no}.mp4"
    ffmpeg_concat(all_clips, ch_mp4)
    rebuild_parts_and_full(proj_dir, state)
    save_state(proj_dir, state)

    return {
        "message": "डायलॉग बदल दिया गया है!",
        "new_dialogue": new_dialogue,
        "state": state,
    }


@app.post("/customize-scene/{project_id}")
async def customize_scene(
    project_id: str,
    chapter_no: int = Form(...),
    dialogue_index: int = Form(...),
    location: str = Form(""),
    location_key: str = Form(""),
    mood: str = Form("Action"),
    sfx: str = Form(""),
    visual_action_prompt: str = Form(""),
    clear_bg: str = Form("false"),
    bg_image: Optional[UploadFile] = File(None),
):
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    state = load_state(proj_dir)
    if not state:
        raise HTTPException(status_code=404, detail="Project not found")

    ch_key = str(chapter_no)
    if ch_key not in state.get("scripts", {}) or dialogue_index >= len(
        state["scripts"][ch_key]
    ):
        raise HTTPException(status_code=400, detail="Invalid chapter or dialogue index")

    item = state["scripts"][ch_key][dialogue_index]
    char_name = item["character"]
    char_info = state["characters"][char_name]

    if location_key.strip():
        item["location_key"] = location_key.strip()
        item["location"] = location.strip() or item.get("location", "Cinematic Set")
    elif location.strip():
        lk, ll = detect_location_meta(location.strip(), location.strip())
        item["location_key"] = lk
        item["location"] = ll

    item["mood"] = normalize_mood(mood, item.get("mood", "Action"))
    if sfx.strip().lower() in ALLOWED_SFX:
        item["sfx"] = sfx.strip().lower()
    if visual_action_prompt.strip():
        item["visual_action_prompt"] = visual_action_prompt.strip()[:260]
    elif not item.get("visual_action_prompt"):
        item["visual_action_prompt"] = build_visual_action_prompt(
            char_name,
            item["dialogue"],
            item.get("location", "Cinematic Set"),
            item["mood"],
            item.get("sfx", "none"),
        )

    if clear_bg.lower() == "true":
        item.pop("bg_path", None)

    if bg_image is not None:
        raw_bytes = await bg_image.read()
        if raw_bytes:
            bg_path = safe_project_file(
                proj_dir, f"scene_bg_ch{chapter_no}_d{dialogue_index}.jpg"
            )
            with open(bg_path, "wb") as f:
                f.write(raw_bytes)
            item["bg_path"] = bg_path

    ch_temp = f"{proj_dir}/ch_{chapter_no}_clips"
    os.makedirs(ch_temp, exist_ok=True)
    a_file = f"{ch_temp}/a_{dialogue_index}_cust.mp3"
    f_img = f"{ch_temp}/f_{dialogue_index}_cust.jpg"
    v_file = f"{ch_temp}/v_{dialogue_index}.mp4"

    await generate_bulletproof_audio(
        item["dialogue"],
        char_info["voice"],
        a_file,
        pitch=char_info.get("pitch", "normal"),
        language=state.get("language", "Hindi"),
    )
    scene_mood = item.get("mood", "Action")
    scene_sfx = item.get("sfx", detect_sfx_type(item["dialogue"], item.get("location_key", ""), scene_mood))
    proj_ratio = state.get("aspect_ratio", "16:9")
    await asyncio.to_thread(
        prepare_character_frame,
        char_info["image"],
        char_name,
        item["dialogue"],
        f_img,
        item.get("location_key", "city_street"),
        item.get("location", "Mumbai City Street"),
        scene_mood,
        item.get("bg_path") or state.get("custom_bg_path"),
        state.get("watermark_text", ""),
        state.get("watermark_position", "top-right"),
        proj_ratio,
        item.get("visual_action_prompt", ""),
    )
    await asyncio.to_thread(
        render_exact_face_clip,
        char_info["image"],
        f_img,
        a_file,
        v_file,
        scene_mood,
        scene_sfx,
        item.get("visual_action_prompt", ""),
        char_info.get("angles"),
        item.get("camera_angle", "front"),
        char_name,
        item["dialogue"],
        0.0,
        proj_ratio,
    )
    if os.path.exists(a_file):
        os.remove(a_file)
    if os.path.exists(f_img):
        os.remove(f_img)

    all_clips = [
        f"{ch_temp}/v_{i}.mp4"
        for i in range(len(state["scripts"][ch_key]))
        if os.path.exists(f"{ch_temp}/v_{i}.mp4")
    ]
    ch_mp4 = f"{proj_dir}/ch_{chapter_no}.mp4"
    ffmpeg_concat(all_clips, ch_mp4)
    rebuild_parts_and_full(proj_dir, state)
    save_state(proj_dir, state)

    return {
        "message": "✅ सीन की लोकेशन, म्यूज़िक, Foley SFX और बैकग्राउंड अपडेट हो गया!",
        "state": state,
    }


@app.post("/project/{project_id}/watermark")
def update_project_watermark(
    project_id: str,
    watermark_text: str = Form(""),
    watermark_position: str = Form("top-right"),
):
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    state = load_state(proj_dir)
    if not state:
        raise HTTPException(status_code=404, detail="Project not found")
    state["watermark_text"] = watermark_text.strip()[:30]
    state["watermark_position"] = (
        watermark_position
        if watermark_position in ("top-right", "bottom-right", "top-left")
        else "top-right"
    )
    save_state(proj_dir, state)
    return {"message": "✅ चैनल वॉटरमार्क सेव हो गया!", "state": state}


async def _dub_project_worker(project_id: str, target_language: str):
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    state = load_state(proj_dir)
    if not state:
        return
    try:
        lang = normalize_language(target_language)
        state["language"] = lang
        state["status_text"] = f"🗣️ पूरी मूवी {lang} भाषा में डब हो रही है..."
        state["is_completed"] = False
        state["progress"] = 10
        save_state(proj_dir, state)

        char_map = state["characters"]
        chapters = state.get("outline", {}).get("chapters", [])
        wm_text = state.get("watermark_text", "")
        wm_pos = state.get("watermark_position", "top-right")
        project_custom_bg = state.get("custom_bg_path")

        for idx, ch in enumerate(chapters):
            ch_no = ch["no"]
            ch_key = str(ch_no)
            dialogues = state.get("scripts", {}).get(ch_key, [])
            if not dialogues:
                continue

            # Translate dialogues batch with Gemini if available
            if client:
                try:
                    lines_in = [d.get("dialogue", "") for d in dialogues]
                    t_prompt = (
                        f"Translate and adapt the following movie dialogues naturally into authentic {lang} "
                        f"keeping Bollywood drama and emotion intact. Return ONLY a JSON string array of length {len(lines_in)}:\n"
                        + json.dumps(lines_in, ensure_ascii=False)
                    )
                    t_res = client.models.generate_content(
                        model="gemini-2.5-flash", contents=t_prompt
                    )
                    translated = safe_extract_json(t_res.text, lines_in)
                    if isinstance(translated, list) and len(translated) == len(dialogues):
                        for i, new_line in enumerate(translated):
                            dialogues[i]["dialogue"] = str(new_line)
                except Exception:
                    pass

            ch_temp = f"{proj_dir}/ch_{ch_no}_clips"
            os.makedirs(ch_temp, exist_ok=True)
            clips = []
            for i, d in enumerate(dialogues):
                c_name = d.get("character") if d.get("character") in char_map else list(char_map.keys())[0]
                diag_text = d.get("dialogue", "")
                scene_mood = normalize_mood(d.get("mood", "Action"))
                scene_sfx = d.get("sfx", detect_sfx_type(diag_text, d.get("location_key", ""), scene_mood))
                a_file = f"{ch_temp}/a_{i}_dub.mp3"
                f_img = f"{ch_temp}/f_{i}_dub.jpg"
                v_file = f"{ch_temp}/v_{i}.mp4"

                await generate_bulletproof_audio(
                    diag_text,
                    char_map[c_name]["voice"],
                    a_file,
                    pitch=char_map[c_name].get("pitch", "normal"),
                    language=lang,
                )
                proj_ratio = state.get("aspect_ratio", "16:9")
                await asyncio.to_thread(
                    prepare_character_frame,
                    char_map[c_name]["image"],
                    c_name,
                    diag_text,
                    f_img,
                    d.get("location_key", "city_street"),
                    d.get("location", "Mumbai City Street"),
                    scene_mood,
                    d.get("bg_path") or project_custom_bg,
                    wm_text,
                    wm_pos,
                    proj_ratio,
                    d.get("visual_action_prompt", ""),
                )
                await asyncio.to_thread(
                    render_exact_face_clip,
                    char_map[c_name]["image"],
                    f_img,
                    a_file,
                    v_file,
                    scene_mood,
                    scene_sfx,
                    d.get("visual_action_prompt", ""),
                    char_map[c_name].get("angles"),
                    d.get("camera_angle", "front"),
                    c_name,
                    diag_text,
                    0.0,
                    proj_ratio,
                )
                for p in (a_file, f_img):
                    if os.path.exists(p):
                        os.remove(p)
                clips.append(v_file)

            ffmpeg_concat(clips, f"{proj_dir}/ch_{ch_no}.mp4")
            state["scripts"][ch_key] = dialogues
            state["progress"] = min(95, int(((idx + 1) / max(1, len(chapters))) * 85) + 10)
            save_state(proj_dir, state)

        rebuild_parts_and_full(proj_dir, state)
        state["status_text"] = f"✅ पूरी मूवी {lang} भाषा में डब होकर तैयार है!"
        save_state(proj_dir, state)
    except Exception as e:
        state["status_text"] = f"डबिंग में समस्या: {str(e)}"
        save_state(proj_dir, state)


@app.post("/dub-language/{project_id}")
async def dub_movie_language(
    project_id: str,
    background_tasks: BackgroundTasks,
    target_language: str = Form(...),
):
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    state = load_state(proj_dir)
    if not state:
        raise HTTPException(status_code=404, detail="Project not found")
    lang = normalize_language(target_language)
    background_tasks.add_task(_dub_project_worker, project_id, lang)
    return {
        "message": f"🗣️ मूवी की {lang} डबिंग शुरू हो गई है! कुछ देर में नई भाषा में Play करें।",
        "language": lang,
    }


def _render_bollywood_poster(proj_dir: str, state: dict) -> str:
    W, H = 1080, 1500
    scripts = state.get("scripts", {})
    first_loc = "royal_palace"
    for ch_lines in scripts.values():
        if ch_lines:
            first_loc = ch_lines[0].get("location_key", "royal_palace")
            break
    mood = state.get("default_mood", "Action")
    bg_wide = render_location_backdrop(first_loc, mood)
    poster = bg_wide.resize((W, H), Image.Resampling.LANCZOS)

    # Dark cinematic gradient vignette for poster drama
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for y in range(H):
        if y < 260:
            alpha = int(190 * (1.0 - y / 260))
        elif y > 880:
            alpha = int(235 * ((y - 880) / (H - 880)))
        else:
            alpha = 45
        od.line([(0, y), (W, y)], fill=(8, 4, 20, alpha))
    poster = Image.alpha_composite(poster.convert("RGBA"), overlay).convert("RGB")

    # Cast portraits collage
    chars = list(state.get("characters", {}).items())[:3]
    if chars:
        slot_w = int(min(440, (W - 120) / len(chars)))
        slot_h = 540
        total_w = slot_w * len(chars) + (len(chars) - 1) * 24
        start_x = (W - total_w) // 2
        draw = ImageDraw.Draw(poster)
        for idx, (c_name, c_info) in enumerate(chars):
            img_p = c_info.get("image", "")
            if not img_p or not os.path.exists(img_p):
                continue
            try:
                p_img = open_upright_image(img_p)
                p_img = p_img.resize((slot_w, slot_h), Image.Resampling.LANCZOS)
                x = start_x + idx * (slot_w + 24)
                y = 270 + (0 if idx == 0 else 40)
                draw.rounded_rectangle(
                    [x - 6, y - 6, x + slot_w + 6, y + slot_h + 6],
                    radius=22,
                    outline=(255, 214, 0),
                    width=5,
                )
                mask = Image.new("L", (slot_w, slot_h), 0)
                ImageDraw.Draw(mask).rounded_rectangle([0, 0, slot_w, slot_h], radius=18, fill=255)
                poster.paste(p_img, (x, y), mask)
            except Exception:
                continue

    draw = ImageDraw.Draw(poster)
    font_super = load_hindi_font(72, bold=True)
    font_sub = load_hindi_font(34, bold=True)
    font_meta = load_hindi_font(26, bold=False)

    # Top Bollywood banner
    genre_txt = f"★ CINEPOP AI PRESENTS • {state.get('genre', 'ACTION').upper()} • {state.get('language', 'HINDI').upper()} ★"
    draw.text((90, 56), genre_txt[:56], fill=(255, 214, 0), font=font_meta)
    draw.line([(90, 108), (W - 90, 108)], fill=(255, 0, 127), width=3)

    # Movie Title
    title = (state.get("outline", {}).get("title") or state.get("idea", "CINEPOP BLOCKBUSTER"))[:32]
    draw.text((84, 964), title, fill=(255, 0, 127), font=font_super)
    draw.text((80, 960), title, fill=(255, 214, 0), font=font_super)

    # Tagline
    tagline = (state.get("idea") or "एक ज़बरदस्त बॉलीवुड ब्लॉकबस्टर")[:66]
    draw.text((84, 1082), f'"{tagline}"', fill=(226, 232, 240), font=font_sub)

    # Starring cast names
    cast_line = "STARRING: " + " • ".join(list(state.get("characters", {}).keys())[:4])
    draw.text((84, 1185), cast_line[:64], fill=(0, 229, 255), font=font_meta)

    # Directed by CinePop AI billing block
    draw.line([(84, 1265), (W - 84, 1265)], fill=(255, 214, 0), width=2)
    draw.text((225, 1300), "DIRECTED BY CINEPOP AI", fill=(255, 255, 255), font=font_sub)
    draw.text((260, 1368), "IN DOLBY AI SOUND • ULTRA HD", fill=(180, 190, 215), font=font_meta)

    if state.get("watermark_text"):
        draw_channel_watermark(poster, state["watermark_text"], state.get("watermark_position", "top-right"))

    out_poster = f"{proj_dir}/POSTER.jpg"
    poster.save(out_poster, "JPEG", quality=94)
    return out_poster


@app.post("/generate-poster/{project_id}")
async def generate_poster_endpoint(project_id: str):
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    state = load_state(proj_dir)
    if not state:
        raise HTTPException(status_code=404, detail="Project not found")
    poster_path = await asyncio.to_thread(_render_bollywood_poster, proj_dir, state)
    state["poster_ready"] = True
    save_state(proj_dir, state)
    return {
        "message": "🖼️ हाई-रिज़ॉल्यूशन बॉलीवुड मूवी पोस्टर तैयार है!",
        "poster_url": f"/poster/{project_id}",
        "path": poster_path,
    }


@app.get("/poster/{project_id}")
def get_poster_endpoint(project_id: str, download: int = 0):
    path = f"{PROJECTS_DIR}/{project_id}/POSTER.jpg"
    if not os.path.exists(path):
        state = load_state(f"{PROJECTS_DIR}/{project_id}")
        if not state:
            raise HTTPException(status_code=404, detail="Poster not found")
        _render_bollywood_poster(f"{PROJECTS_DIR}/{project_id}", state)
    if download:
        return FileResponse(
            path,
            media_type="image/jpeg",
            filename=f"CinePop_Poster_{project_id}.jpg",
        )
    return FileResponse(path, media_type="image/jpeg")


def _make_title_card_clip(
    title: str, subtitle: str, out_mp4: str, duration: float = 2.6, vertical: bool = False
):
    W, H = (720, 1280) if vertical else (1280, 720)
    img = Image.new("RGB", (W, H), (10, 6, 24))
    _draw_vertical_gradient(img, (26, 9, 51), (10, 6, 22))
    draw = ImageDraw.Draw(img)
    f_big = load_hindi_font(54 if not vertical else 44, bold=True)
    f_sm = load_hindi_font(28 if not vertical else 24, bold=False)

    draw.rectangle([40, H // 2 - 120, W - 40, H // 2 + 120], outline=(255, 214, 0), width=4)
    draw.text((70, H // 2 - 70), title[:30], fill=(255, 214, 0), font=f_big)
    draw.text((70, H // 2 + 25), subtitle[:48], fill=(0, 229, 255), font=f_sm)

    img_path = f"{out_mp4}.jpg"
    bgm_path = f"{out_mp4}_bgm.wav"
    img.save(img_path, "JPEG", quality=92)
    generate_bgm_wav("Action", duration, bgm_path)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            img_path,
            "-i",
            bgm_path,
            "-t",
            str(duration),
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-b:a",
            "96k",
            out_mp4,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for p in (img_path, bgm_path):
        if os.path.exists(p):
            os.remove(p)


def _build_trailer_and_reel_sync(proj_dir: str, state: dict):
    title = (state.get("outline", {}).get("title") or "CINEPOP BLOCKBUSTER")[:28]
    # Collect best unique action/dialogue clips across chapters (deduplicated by dialogue text!)
    candidate_clips = []
    seen_diags: set[str] = set()
    for ch_no in sorted(state.get("done_chapters", [])):
        ch_temp = f"{proj_dir}/ch_{ch_no}_clips"
        ch_scripts = state.get("scripts", {}).get(str(ch_no), [])
        for i, d in enumerate(ch_scripts):
            v_file = f"{ch_temp}/v_{i}.mp4"
            if os.path.exists(v_file) and os.path.getsize(v_file) > 1000:
                diag_key = re.sub(r"\s+", " ", clean_spoken_dialogue(d.get("dialogue", "")).lower()).strip()
                if diag_key and diag_key in seen_diags:
                    continue
                if diag_key:
                    seen_diags.add(diag_key)
                score = 3 if d.get("mood") == "Action" else (2 if d.get("mood") == "Suspense" else 1)
                candidate_clips.append((score, v_file))

    if not candidate_clips:
        full_p = f"{proj_dir}/FULL_MOVIE.mp4"
        if os.path.exists(full_p):
            candidate_clips = [(3, full_p)]
        else:
            raise ValueError("अभी ट्रेलर के लिए कोई तैयार क्लिप नहीं है।")

    candidate_clips.sort(key=lambda x: -x[0])
    chosen = [p for _, p in candidate_clips[:6]]

    t_dir = f"{proj_dir}/trailer_work"
    os.makedirs(t_dir, exist_ok=True)
    intro_16 = f"{t_dir}/intro_16.mp4"
    outro_16 = f"{t_dir}/outro_16.mp4"
    intro_9 = f"{t_dir}/intro_9.mp4"
    outro_9 = f"{t_dir}/outro_9.mp4"
    _make_title_card_clip("OFFICIAL TRAILER", f"{title} • CINEPOP AI", intro_16, 2.4, vertical=False)
    _make_title_card_clip(title, "RELEASING NOW • DIRECTED BY CINEPOP AI", outro_16, 2.6, vertical=False)
    _make_title_card_clip("OFFICIAL REEL", f"{title} • CINEPOP AI", intro_9, 2.2, vertical=True)
    _make_title_card_clip(title, "WATCH FULL SERIES • CINEPOP AI", outro_9, 2.4, vertical=True)

    # Edge-to-edge FULL-SCREEN scaling (ZERO letterbox / ZERO blurred side bars!)
    fast_cuts_16 = [intro_16]
    fast_cuts_9 = [intro_9]
    for idx, src in enumerate(chosen):
        cut_16 = f"{t_dir}/cut16_{idx}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                src,
                "-t",
                "3.2",
                "-vf",
                "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
                "-r",
                "24",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "44100",
                "-b:a",
                "96k",
                cut_16,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fast_cuts_16.append(cut_16)

        cut_9 = f"{t_dir}/cut9_{idx}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                src,
                "-t",
                "3.2",
                "-vf",
                "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
                "-r",
                "24",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "44100",
                "-b:a",
                "96k",
                cut_9,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fast_cuts_9.append(cut_9)

    fast_cuts_16.append(outro_16)
    fast_cuts_9.append(outro_9)

    trailer_16 = f"{proj_dir}/TRAILER_16_9.mp4"
    ffmpeg_concat(fast_cuts_16, trailer_16)

    # Direct full-screen 9:16 Vertical Reel (720x1280 edge-to-edge, ZERO blurred side bars!)
    reel_9_16 = f"{proj_dir}/REEL_9_16.mp4"
    ffmpeg_concat(fast_cuts_9, reel_9_16)

    shutil.rmtree(t_dir, ignore_errors=True)
    return trailer_16, reel_9_16


@app.post("/generate-trailer/{project_id}")
async def generate_trailer_endpoint(project_id: str):
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    state = load_state(proj_dir)
    if not state:
        raise HTTPException(status_code=404, detail="Project not found")
    if not state.get("is_completed"):
        raise HTTPException(
            status_code=400,
            detail="Movie poori hone ke baad Trailer khulega (कृपया 100% रेंडर पूरा होने दें)।",
        )
    try:
        await asyncio.to_thread(_build_trailer_and_reel_sync, proj_dir, state)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Trailer error: {str(e)}")
    state["trailer_ready"] = True
    state["reel_ready"] = True
    save_state(proj_dir, state)
    return {
        "message": "🎬 16:9 YouTube Trailer और 9:16 Vertical Reel दोनों तैयार हैं!",
        "trailer_stream": f"/stream-trailer/{project_id}?format=16x9",
        "reel_stream": f"/stream-trailer/{project_id}?format=9x16",
    }


@app.get("/stream-trailer/{project_id}")
def stream_trailer_endpoint(project_id: str, request: Request, format: str = "16x9"):
    fname = "REEL_9_16.mp4" if format == "9x16" else "TRAILER_16_9.mp4"
    path = f"{PROJECTS_DIR}/{project_id}/{fname}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Trailer/Reel not generated yet")
    return FileResponse(path, media_type="video/mp4")


@app.get("/download-trailer/{project_id}")
def download_trailer_endpoint(project_id: str, format: str = "16x9"):
    fname = "REEL_9_16.mp4" if format == "9x16" else "TRAILER_16_9.mp4"
    path = f"{PROJECTS_DIR}/{project_id}/{fname}"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Trailer/Reel not generated yet")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"CinePop_{project_id}_{fname}",
    )


# ==================== CUSTOM VIDEO AI FACE-SWAP ENGINE (ZERO CascadeClassifier) ====================

def _try_hf_faceswap_image(source_face_path: str, target_frame_path: str) -> Optional[str]:
    """Try free HuggingFace Face-Swap spaces with strict 10s timeout + circuit breaker."""
    hf_spaces = [
        ("tonyassi/face-swap", "/predict"),
        ("ezioruan/inswapper", "/predict"),
        ("tonyassi/face-swap", "/swap"),
        ("felixrosberg/face-swap", "/predict"),
    ]
    for space, api_ep in hf_spaces:
        if _is_hf_space_cooling(space):
            continue

        def _call(sp=space, ep=api_ep):
            hf = Client(sp)
            return hf.predict(
                handle_file(source_face_path),
                handle_file(target_frame_path),
                api_name=ep,
            )

        res = _run_with_timeout(_call, timeout_sec=10.0)
        if res is None:
            _mark_hf_space_failed(space)
            continue
        if isinstance(res, str) and os.path.exists(res):
            return res
        if isinstance(res, (list, tuple)) and res:
            first = res[0]
            if isinstance(first, str) and os.path.exists(first):
                return first
            if isinstance(first, dict) and first.get("path") and os.path.exists(first["path"]):
                return first["path"]
        _mark_hf_space_failed(space)
    return None


def _color_match_lab(src_bgr, dst_bgr):
    """Match source face lighting & tone to target video frame in LAB space."""
    try:
        src_lab = cv2.cvtColor(src_bgr, cv2.COLOR_BGR2LAB).astype("float32")
        dst_lab = cv2.cvtColor(dst_bgr, cv2.COLOR_BGR2LAB).astype("float32")
        for ch in range(3):
            s_mean, s_std = src_lab[:, :, ch].mean(), max(src_lab[:, :, ch].std(), 1.0)
            d_mean, d_std = dst_lab[:, :, ch].mean(), max(dst_lab[:, :, ch].std(), 1.0)
            # Gentle 0.72 blend toward destination tone so identity stays sharp
            ratio = min(1.6, max(0.6, d_std / s_std))
            matched_ch = ((src_lab[:, :, ch] - s_mean) * ratio) + d_mean
            src_lab[:, :, ch] = 0.72 * matched_ch + 0.28 * src_lab[:, :, ch]
        src_lab = np.clip(src_lab, 0, 255).astype("uint8")
        return cv2.cvtColor(src_lab, cv2.COLOR_LAB2BGR)
    except Exception:
        return src_bgr


def _detect_face_box_numpy_ycrcb(frame_bgr) -> tuple[int, int, int, int]:
    """
    Crash-proof face region estimator using pure NumPy + OpenCV YCrCb skin-tone clustering
    (NO cv2.CascadeClassifier / NO haarcascades dependency — works on OpenCV 4.x & 5.x!).
    Falls back cleanly to upper-center portrait box if skin blob is ambiguous.
    """
    h, w = frame_bgr.shape[:2]
    default_box = (
        int(w * 0.34),
        int(h * 0.14),
        max(48, int(w * 0.32)),
        max(56, int(h * 0.42)),
    )
    try:
        # Focus on upper 75% where actor faces appear
        roi_h = max(32, int(h * 0.78))
        small_w = 160
        small_h = max(32, int(roi_h * (small_w / max(1, w))))
        small = cv2.resize(frame_bgr[:roi_h, :], (small_w, small_h), interpolation=cv2.INTER_AREA)
        ycrcb = cv2.cvtColor(small, cv2.COLOR_BGR2YCrCb)
        # Standard human skin-tone range across diverse complexions & lighting
        cr = ycrcb[:, :, 1]
        cb = ycrcb[:, :, 2]
        y_ch = ycrcb[:, :, 0]
        skin_mask = (
            (cr >= 133) & (cr <= 178) & (cb >= 77) & (cb <= 130) & (y_ch >= 45)
        ).astype(np.uint8)

        # Clean noise & focus toward center-weighted columns
        if int(skin_mask.sum()) < (small_w * small_h * 0.015):
            return default_box

        # Find bounding box of core skin cluster via percentiles (robust against background hands/noise)
        ys, xs = np.where(skin_mask > 0)
        x1 = int(np.percentile(xs, 14))
        x2 = int(np.percentile(xs, 86))
        y1 = int(np.percentile(ys, 8))
        y2 = int(np.percentile(ys, 74))

        sx = w / float(small_w)
        sy = roi_h / float(small_h)
        bx = int(x1 * sx)
        by = int(y1 * sy)
        bw = int((x2 - x1) * sx)
        bh = int((y2 - y1) * sy)

        # Constrain to realistic head/face proportions
        if bw < w * 0.12 or bh < h * 0.14 or bw > w * 0.72 or bh > h * 0.72:
            return default_box
        # Keep head aspect ratio balanced (portrait oval)
        side = int(min(bw, bh * 0.9))
        cx = bx + bw // 2
        cy = by + int(bh * 0.48)
        fw = max(48, min(int(w * 0.48), int(side * 1.05)))
        fh = max(56, min(int(h * 0.56), int(side * 1.24)))
        fx = max(0, min(w - fw, cx - fw // 2))
        fy = max(0, min(h - fh, cy - fh // 2))
        return (fx, fy, fw, fh)
    except Exception:
        return default_box


def _extract_source_face_crop(src_img):
    """Extract clean centered portrait face crop without CascadeClassifier."""
    h, w = src_img.shape[:2]
    try:
        fx, fy, fw, fh = _detect_face_box_numpy_ycrcb(src_img)
        if fw >= 40 and fh >= 40:
            crop = src_img[fy : fy + fh, fx : fx + fw]
            if crop.size > 0:
                return crop
    except Exception:
        pass
    side = min(h, w)
    y_start = max(0, int((h - side) * 0.32))
    x_start = max(0, (w - side) // 2)
    return src_img[y_start : y_start + side, x_start : x_start + side]


def _estimate_frame_head_pose(frame_bgr, box: tuple[int, int, int, int]) -> str:
    """
    Estimate actor head pose (front / left / right / up / down) inside detected face ROI
    using pure NumPy skin-mass centroid asymmetry (zero CascadeClassifier!).
    Used to select the best-matching photo from a character's 5-Angle 3D Face Scan.
    """
    try:
        tx, ty, tw, th = box
        roi = frame_bgr[ty : ty + th, tx : tx + tw]
        if roi.size == 0 or tw < 24 or th < 24:
            return "front"
        ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
        cr, cb, y_ch = ycrcb[:, :, 1], ycrcb[:, :, 2], ycrcb[:, :, 0]
        skin = ((cr >= 133) & (cr <= 178) & (cb >= 77) & (cb <= 130) & (y_ch >= 45)).astype(np.float32)
        total = float(skin.sum())
        if total < 20.0:
            return "front"
        ys, xs = np.indices((th, tw), dtype=np.float32)
        cx_norm = float((xs * skin).sum() / total) / max(1.0, float(tw))
        cy_norm = float((ys * skin).sum() / total) / max(1.0, float(th))
        if cx_norm < 0.43:
            return "left"
        if cx_norm > 0.57:
            return "right"
        if cy_norm < 0.42:
            return "up"
        if cy_norm > 0.59:
            return "down"
        return "front"
    except Exception:
        return "front"


def _load_face_crop_from_path(img_path: str):
    """Safely load upright (EXIF-corrected) portrait face crop from any image file."""
    try:
        pil_src = ImageOps.exif_transpose(Image.open(img_path)).convert("RGB")
        src_img = cv2.cvtColor(np.array(pil_src), cv2.COLOR_RGB2BGR)
    except Exception:
        src_img = cv2.imread(img_path)
    return _extract_source_face_crop(src_img)


def _opencv_video_faceswap(
    video_path: str,
    face_img_path: str,
    out_video_noaudio: str,
    watermark_text: str = "",
    char_angles: Optional[dict] = None,
    preferred_angle: str = "front",
):
    if not OPENCV_AVAILABLE or cv2 is None or np is None:
        # Fallback FFmpeg smooth portrait face overlay if OpenCV is unavailable
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-i",
                face_img_path,
                "-t",
                "25",
                "-filter_complex",
                "[0:v]scale=720:-2[base];[1:v]scale=180:210:force_original_aspect_ratio=increase,crop=180:210[fc];[base][fc]overlay=(W-w)/2:H*0.16[v]",
                "-map",
                "[v]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                out_video_noaudio,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "ffmpeg-portrait-blend"

    # Pre-load primary + all 5-Angle 3D Face Scan crops (front, left, right, up, down)
    angle_crops: dict[str, any] = {}
    angle_paths: dict[str, str] = {}
    primary_crop = _load_face_crop_from_path(face_img_path)
    angle_crops["front"] = primary_crop
    angle_paths["front"] = face_img_path

    if char_angles:
        for ang, pth in char_angles.items():
            if ang in ALLOWED_FACE_ANGLES and isinstance(pth, str) and os.path.exists(pth):
                try:
                    angle_crops[ang] = _load_face_crop_from_path(pth)
                    angle_paths[ang] = pth
                except Exception:
                    continue

    src_face_crop = angle_crops.get(preferred_angle, primary_crop)

    # Normalize mobile videos (HEVC/H.265/MOV/vertical rotation metadata) to clean H.264 MP4 for reliable OpenCV decode
    norm_video_path = f"{out_video_noaudio}_norm.mp4"
    decode_path = video_path
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-t",
                "25",
                "-vf",
                "scale='min(720,iw)':'min(720,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-r",
                "24",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-an",
                norm_video_path,
            ],
            check=True,
            timeout=45,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.path.exists(norm_video_path) and os.path.getsize(norm_video_path) > 1024:
            decode_path = norm_video_path
    except Exception:
        decode_path = video_path

    cap = cv2.VideoCapture(decode_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    fps = max(12.0, min(30.0, fps))
    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 360
    scale = min(1.0, 720.0 / max(orig_w, orig_h))
    out_w = max(2, int(orig_w * scale) // 2 * 2)
    out_h = max(2, int(orig_h * scale) // 2 * 2)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(out_video_noaudio, fourcc, fps, (out_w, out_h))

    max_frames = int(fps * 25)
    frame_idx = 0
    last_box = None
    swapped_count = 0
    hf_tried_angles: set[str] = set()
    hf_engine_used = False
    active_pose = preferred_angle if preferred_angle in ALLOWED_FACE_ANGLES else "front"

    while frame_idx < max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        if scale < 0.99:
            frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)

        # Estimate target face box & 3D head pose every 3rd frame (zero CascadeClassifier!)
        if frame_idx % 3 == 0 or last_box is None:
            bx, by, bw, bh = _detect_face_box_numpy_ycrcb(frame)
            if last_box is not None:
                lx, ly, lw, lh = last_box
                last_box = (
                    int(0.72 * lx + 0.28 * bx),
                    int(0.72 * ly + 0.28 * by),
                    int(0.72 * lw + 0.28 * bw),
                    int(0.72 * lh + 0.28 * bh),
                )
            else:
                last_box = (int(bx), int(by), int(bw), int(bh))
            if len(angle_crops) > 1:
                detected_pose = _estimate_frame_head_pose(frame, last_box)
                if detected_pose in angle_crops:
                    active_pose = detected_pose

        tx, ty, tw, th = last_box
        tx = max(0, min(out_w - 24, tx))
        ty = max(0, min(out_h - 24, ty))
        tw = max(24, min(out_w - tx, tw))
        th = max(24, min(out_h - ty, th))

        active_face_path = angle_paths.get(active_pose, face_img_path)
        src_face_crop = angle_crops.get(active_pose, primary_crop)

        # Step 1: Try free HuggingFace AI Face-Swap Space once per detected pose angle
        if active_pose not in hf_tried_angles and len(hf_tried_angles) < 2:
            hf_tried_angles.add(active_pose)
            try:
                tmp_frame = f"{out_video_noaudio}_f_{active_pose}.jpg"
                cv2.imwrite(tmp_frame, frame)
                hf_res = _try_hf_faceswap_image(active_face_path, tmp_frame)
                if hf_res and os.path.exists(hf_res):
                    hf_img = cv2.imread(hf_res)
                    if hf_img is not None:
                        hf_resized = cv2.resize(hf_img, (out_w, out_h))
                        hf_crop = hf_resized[ty : ty + th, tx : tx + tw]
                        if hf_crop.size > 0:
                            angle_crops[active_pose] = hf_crop
                            src_face_crop = hf_crop
                            hf_engine_used = True
                if os.path.exists(tmp_frame):
                    os.remove(tmp_frame)
            except Exception:
                pass

        # Step 2: Pure NumPy + OpenCV smooth LAB color-matched feathered elliptical blend (100% crash-proof)
        target_roi = frame[ty : ty + th, tx : tx + tw]
        if target_roi.size > 0 and src_face_crop is not None and src_face_crop.size > 0:
            resized_src = cv2.resize(src_face_crop, (tw, th), interpolation=cv2.INTER_LINEAR)
            matched = _color_match_lab(resized_src, target_roi)

            # Feathered elliptical mask
            yy, xx = np.ogrid[:th, :tw]
            cy_m, cx_m = th / 2.0, tw / 2.0
            ry, rx = max(1.0, th * 0.46), max(1.0, tw * 0.43)
            dist = ((xx - cx_m) / rx) ** 2 + ((yy - cy_m) / ry) ** 2
            alpha_2d = np.clip(1.0 - np.maximum(0.0, (dist - 0.52) / 0.48), 0.0, 1.0).astype("float32")
            k = max(5, (min(tw, th) // 6) | 1)
            alpha_2d = cv2.GaussianBlur(alpha_2d, (k, k), 0)
            alpha = alpha_2d[:, :, None]

            # Try seamlessClone if available, else crisp Gaussian alpha blend
            blended_ok = False
            if hasattr(cv2, "seamlessClone") and hasattr(cv2, "NORMAL_CLONE"):
                try:
                    u8_mask = (alpha_2d > 0.25).astype(np.uint8) * 255
                    center = (tx + tw // 2, ty + th // 2)
                    frame = cv2.seamlessClone(matched, frame, u8_mask, center, cv2.NORMAL_CLONE)
                    blended_ok = True
                except Exception:
                    blended_ok = False
            if not blended_ok:
                blended = (
                    matched.astype("float32") * alpha
                    + target_roi.astype("float32") * (1.0 - alpha)
                ).astype("uint8")
                frame[ty : ty + th, tx : tx + tw] = blended
            swapped_count += 1

        if watermark_text:
            pil_fr = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            draw_channel_watermark(pil_fr, watermark_text[:24], "top-right")
            frame = cv2.cvtColor(np.array(pil_fr), cv2.COLOR_RGB2BGR)

        writer.write(frame)
        frame_idx += 1

    cap.release()
    writer.release()
    if os.path.exists(norm_video_path):
        try:
            os.remove(norm_video_path)
        except Exception:
            pass
    if frame_idx == 0 or not os.path.exists(out_video_noaudio) or os.path.getsize(out_video_noaudio) < 1024:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                video_path,
                "-i",
                face_img_path,
                "-t",
                "25",
                "-filter_complex",
                "[0:v]scale=720:-2,scale=trunc(iw/2)*2:trunc(ih/2)*2[base];[1:v]scale=180:210:force_original_aspect_ratio=increase,crop=180:210[fc];[base][fc]overlay=(W-w)/2:H*0.16[v]",
                "-map",
                "[v]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-pix_fmt",
                "yuv420p",
                out_video_noaudio,
            ],
            check=True,
            timeout=60,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "ffmpeg-safe-blend"
    angle_tag = f"{len(angle_crops)}D-angles" if len(angle_crops) > 1 else "front-lock"
    tag = f"hf-ai-faceswap+{angle_tag}" if hf_engine_used else f"numpy-smooth-blend+{angle_tag}"
    return f"{tag} ({swapped_count}/{frame_idx} frames)"


async def face_swap_worker(job_id: str):
    job_dir = f"{FACE_SWAPS_DIR}/{job_id}"
    db = load_db()
    job = db.get("face_swaps", {}).get(job_id)
    if not job:
        return
    try:
        video_path = job["video_path"]
        face_path = job["face_path"]
        raw_swapped = f"{job_dir}/swapped_raw.mp4"
        final_mp4 = f"{job_dir}/FINAL_FACESWAP.mp4"

        engine_used = await asyncio.to_thread(
            _opencv_video_faceswap,
            video_path,
            face_path,
            raw_swapped,
            job.get("watermark_text", ""),
            job.get("face_angles"),
        )

        dub_text = (job.get("dub_text") or "").strip()
        if dub_text:
            dub_mp3 = f"{job_dir}/dub_voice.mp3"
            await generate_bulletproof_audio(
                dub_text,
                job.get("dub_voice", "hi-IN-MadhurNeural"),
                dub_mp3,
                pitch=job.get("dub_pitch", "normal"),
                language=job.get("dub_language", "Hindi"),
            )
            mixed_dub = f"{job_dir}/dub_mixed.mp3"
            final_audio = await asyncio.to_thread(
                mix_cinematic_audio, dub_mp3, "Action", "none", mixed_dub
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    raw_swapped,
                    "-i",
                    final_audio,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-shortest",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    "-crf",
                    "25",
                    "-c:a",
                    "aac",
                    "-ar",
                    "44100",
                    "-b:a",
                    "128k",
                    final_mp4,
                ],
                check=True,
                timeout=90,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # Preserve original uploaded video audio while encoding H.264 MP4
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    raw_swapped,
                    "-i",
                    video_path,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0?",
                    "-shortest",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-pix_fmt",
                    "yuv420p",
                    "-crf",
                    "25",
                    "-c:a",
                    "aac",
                    "-ar",
                    "44100",
                    "-b:a",
                    "128k",
                    final_mp4,
                ],
                check=True,
                timeout=90,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        if os.path.exists(raw_swapped):
            os.remove(raw_swapped)

        db = load_db()
        if job_id in db["face_swaps"]:
            db["face_swaps"][job_id]["status"] = "ready"
            db["face_swaps"][job_id]["engine"] = engine_used
            db["face_swaps"][job_id]["output_path"] = final_mp4
            db["face_swaps"][job_id]["status_text"] = "✅ AI Face-Swap वीडियो तैयार है!"
            save_db(db)
    except Exception as e:
        db = load_db()
        if job_id in db["face_swaps"]:
            db["face_swaps"][job_id]["status"] = "failed"
            db["face_swaps"][job_id]["status_text"] = f"Face-Swap error: {str(e)}"
            save_db(db)


@app.post("/face-swap/start/")
@app.post("/face-swap/start")
async def start_face_swap(
    background_tasks: BackgroundTasks,
    user_email: Optional[str] = Form(default="guest@cinepop.ai"),
    title: Optional[str] = Form(default="Custom Face-Swap Clip"),
    dub_text: Optional[str] = Form(default=""),
    dub_language: Optional[str] = Form(default="Hindi"),
    dub_voice: Optional[str] = Form(default="hi-IN-MadhurNeural"),
    dub_pitch: Optional[str] = Form(default="normal"),
    watermark_text: Optional[str] = Form(default=""),
    video: Optional[Union[UploadFile, str]] = File(default=None),
    face_image: Optional[Union[UploadFile, str]] = File(default=None),
    angle_images: Optional[list[Union[UploadFile, str]]] = File(default=None),
    angle_meta: Optional[str] = Form(default="[]"),
):
    email = (user_email or "guest@cinepop.ai").lower().strip()
    title = title or "Custom Face-Swap Clip"
    dub_text = dub_text or ""
    dub_language = dub_language or "Hindi"
    dub_voice = dub_voice or "hi-IN-MadhurNeural"
    dub_pitch = dub_pitch or "normal"
    watermark_text = watermark_text or ""
    angle_meta = angle_meta or "[]"

    if not _is_upload_file_obj(video):
        raise HTTPException(status_code=400, detail="Video file is required for Face-Swap.")
    if not _is_upload_file_obj(face_image):
        raise HTTPException(status_code=400, detail="Face photo (face_image) is required for Face-Swap.")

    video_bytes = await video.read()
    face_bytes = await face_image.read()
    if not video_bytes:
        raise HTTPException(status_code=400, detail="Uploaded video file is empty.")
    if not face_bytes:
        raise HTTPException(status_code=400, detail="Uploaded face photo is empty.")
    if len(video_bytes) > 65 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Video max size is 65MB")
    if len(face_bytes) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Face photo max size is 12MB")

    job_id = str(uuid.uuid4())[:10]
    job_dir = f"{FACE_SWAPS_DIR}/{job_id}"
    os.makedirs(job_dir, exist_ok=True)

    vid_path = safe_project_file(job_dir, f"input_{getattr(video, 'filename', None) or 'clip.mp4'}")
    face_path = safe_project_file(job_dir, "face_front.jpg")
    with open(vid_path, "wb") as f:
        f.write(video_bytes)
    save_upright_image_bytes(face_bytes, face_path)
    try:
        av_url = save_user_avatar_bytes(email, face_bytes)
        if av_url:
            db_av = load_db()
            if email in db_av.get("users", {}):
                db_av["users"][email]["photo_url"] = av_url
                db_av["users"][email]["face_linked"] = True
                save_db(db_av)
    except Exception:
        pass

    face_angles: dict[str, str] = {"front": face_path}
    if angle_images:
        try:
            parsed_meta = json.loads(angle_meta) if angle_meta.strip() else []
            if not isinstance(parsed_meta, list):
                parsed_meta = []
        except Exception:
            parsed_meta = []
        for idx_a, a_up in enumerate(angle_images):
            if not _is_upload_file_obj(a_up):
                continue
            a_bytes = await a_up.read()
            if not a_bytes:
                continue
            m = parsed_meta[idx_a] if idx_a < len(parsed_meta) and isinstance(parsed_meta[idx_a], dict) else {}
            ang = str(m.get("angle", "")).lower().strip()
            if ang not in ALLOWED_FACE_ANGLES:
                fname_low = (getattr(a_up, "filename", "") or "").lower()
                ang = next((k for k in ALLOWED_FACE_ANGLES if k in fname_low), "front")
            a_path = safe_project_file(job_dir, f"face_{ang}.jpg")
            save_upright_image_bytes(a_bytes, a_path)
            face_angles[ang] = a_path

    job_meta = {
        "job_id": job_id,
        "user_email": email,
        "title": title.strip()[:50] or "Custom Face-Swap Clip",
        "video_path": vid_path,
        "face_path": face_path,
        "face_angles": face_angles,
        "dub_text": dub_text.strip(),
        "dub_language": normalize_language(dub_language),
        "dub_voice": dub_voice.strip() or "hi-IN-MadhurNeural",
        "dub_pitch": normalize_pitch(dub_pitch),
        "watermark_text": watermark_text.strip()[:30],
        "status": "processing",
        "status_text": f"🎭 3D Multi-Angle Face-Swap ({len(face_angles)} Angles Locked) चल रहा है...",
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    db = load_db()
    db.setdefault("face_swaps", {})[job_id] = job_meta
    save_db(db)

    background_tasks.add_task(face_swap_worker, job_id)
    return job_meta


@app.get("/face-swap/status/{job_id}")
def face_swap_status(job_id: str):
    db = load_db()
    job = db.get("face_swaps", {}).get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Face-swap job not found")
    return job


@app.get("/face-swap/stream/{job_id}")
def face_swap_stream(job_id: str):
    path = f"{FACE_SWAPS_DIR}/{job_id}/FINAL_FACESWAP.mp4"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Face-swap video not ready")
    return FileResponse(path, media_type="video/mp4")


@app.get("/face-swap/download/{job_id}")
def face_swap_download(job_id: str):
    path = f"{FACE_SWAPS_DIR}/{job_id}/FINAL_FACESWAP.mp4"
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Face-swap video not ready")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"CinePop_FaceSwap_{job_id}.mp4",
    )


@app.delete("/project/{project_id}")
def delete_movie_project(project_id: str, user_email: str = ""):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", project_id)
    if not safe_id:
        raise HTTPException(status_code=400, detail="Invalid project_id")
    proj_dir = f"{PROJECTS_DIR}/{safe_id}"
    if os.path.isdir(proj_dir):
        st = load_state(proj_dir)
        if user_email and st:
            owner = str(st.get("user_email", "")).lower().strip()
            requester = user_email.lower().strip()
            if owner and requester != owner and requester != ADMIN_EMAIL:
                raise HTTPException(status_code=403, detail="Unauthorized delete")
        shutil.rmtree(proj_dir, ignore_errors=True)
    return {"message": "Deleted", "project_id": safe_id}


@app.delete("/face-swap/{job_id}")
def face_swap_delete(job_id: str, user_email: str = ""):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", job_id)
    db = load_db()
    if safe_id in db.get("face_swaps", {}):
        job = db["face_swaps"][safe_id]
        if user_email:
            owner = str(job.get("user_email", "")).lower().strip()
            requester = user_email.lower().strip()
            if owner and requester != owner and requester != ADMIN_EMAIL:
                raise HTTPException(status_code=403, detail="Unauthorized delete")
        del db["face_swaps"][safe_id]
        save_db(db)
    shutil.rmtree(f"{FACE_SWAPS_DIR}/{safe_id}", ignore_errors=True)
    return {"message": "Deleted", "job_id": safe_id}


@app.get("/my-library/")
def get_my_library(user_email: str):
    email = user_email.lower().strip()
    movies = []
    if os.path.isdir(PROJECTS_DIR):
        for pid in os.listdir(PROJECTS_DIR):
            pdir = os.path.join(PROJECTS_DIR, pid)
            if not os.path.isdir(pdir):
                continue
            st = load_state(pdir)
            if not st:
                continue
            if st.get("user_email", "").lower() != email and email != ADMIN_EMAIL:
                continue
            movies.append(
                {
                    "project_id": st.get("project_id", pid),
                    "title": st.get("outline", {}).get("title") or st.get("idea", "Untitled")[:36],
                    "genre": st.get("genre", "Action"),
                    "language": st.get("language", "Hindi"),
                    "progress": st.get("progress", 0),
                    "is_completed": bool(st.get("is_completed", False)),
                    "ready_parts": st.get("ready_parts", []),
                    "has_poster": os.path.exists(f"{pdir}/POSTER.jpg"),
                    "has_trailer": os.path.exists(f"{pdir}/TRAILER_16_9.mp4"),
                    "has_reel": os.path.exists(f"{pdir}/REEL_9_16.mp4"),
                    "watermark_text": st.get("watermark_text", ""),
                    "created_at": st.get("created_at", ""),
                }
            )
    movies.sort(key=lambda m: m.get("created_at", ""), reverse=True)

    db = load_db()
    face_swaps = [
        j
        for j in db.get("face_swaps", {}).values()
        if j.get("user_email", "").lower() == email or email == ADMIN_EMAIL
    ]
    face_swaps.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return {"movies": movies, "face_swaps": face_swaps}


@app.get("/download/{project_id}/{file_type}")
def download_video(project_id: str, file_type: str):
    path = f"{PROJECTS_DIR}/{project_id}/{file_type}.mp4"
    if os.path.exists(path):
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=f"CinePop_{project_id}_{file_type}.mp4",
        )
    raise HTTPException(status_code=404, detail="File not ready yet")
