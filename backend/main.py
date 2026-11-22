import os
import io
import re
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
import urllib.parse
from typing import Optional
import requests
import edge_tts
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps, ImageEnhance
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from gradio_client import Client, handle_file

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
    r"^\s*(?:title|शीर्षक|scene\s*\d*|सीन\s*\d*|location|लोकेशन|action|एक्शन|mood|मूड|sfx|camera|कैमरा|int\.|ext\.|cut\s+to|fade\s+(?:in|out)|narrator|सूत्रधार|background|बैकग्राउंड)\s*[:：\-]?",
    re.IGNORECASE,
)


def clean_spoken_dialogue(raw_text: str) -> str:
    """
    Strip ALL script/stage metadata tags (`Title:`, `Scene 1:`, `Location:`, `Action:`, `[...]`, `(...)`, markdown)
    so TTS characters NEVER read stage directions or headers aloud!
    """
    txt = str(raw_text or "").strip()
    if not txt:
        return ""
    # Remove bracketed & parenthetical stage cues like [Location: ...] or (angrily punches)
    txt = re.sub(r"\[[^\]]*\]", " ", txt)
    txt = re.sub(r"\([^)]*(?:action|scene|location|camera|mood|sfx|angry|shouts|runs|punches|laugh|एक्शन|सीन|लोकेशन)[^)]*\)", " ", txt, flags=re.IGNORECASE)
    # Remove inline script metadata markers
    txt = re.sub(
        r"\b(?:Title|Scene\s*\d*|Location|Action|Mood|SFX|Camera|Visual|शीर्षक|सीन\s*\d*|लोकेशन|एक्शन|मूड)\s*[:：]\s*[^\n.।!?]*",
        " ",
        txt,
        flags=re.IGNORECASE,
    )
    # Remove markdown formatting & quotes
    txt = txt.replace("**", "").replace("__", "").replace("##", "").replace("#", "")
    txt = re.sub(r"^[\-\*•\d\.\)\s]+", "", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" \"'“”‘’")
    return txt


def detect_speaker_role(speaker_name: str, char_map: dict) -> str:
    """Classify speaker as 'narrator', 'hero', 'villain', 'saas', 'female', or 'crowd' for distinct voices & visuals."""
    low = (speaker_name or "").lower().strip()
    char_names = list(char_map.keys())
    if any(k in low for k in ("narrator", "sutradhar", "voiceover", "storyteller", "सूत्रधार", "कथावाचक", "नैरेटर")):
        return "narrator"
    if any(k in low for k in ("log", "crowd", "public", "villager", "people", "लोग", "भीड़", "जनता", "गाँव वाले", "गांव वाले", "police", "पुलिस")):
        return "crowd"
    if any(k in low for k in ("saas", "mother-in-law", "stepmother", "aunty", "chachi", "सास", "सौतेली", "बुआ")):
        return "saas"
    if any(k in low for k in ("villain", "don", "kaal", "boss", "gunda", "evil", "enemy", "rakshas", "shaitan", "विलेन", "डॉन", "गुंडा", "दुश्मन", "काल")):
        return "villain"
    if any(k in low for k in ("heroine", "priya", "pooja", "anjali", "rani", "maa", "mother", "wife", "bahu", "girl", "woman", "ladki", "lady", "लड़की", "माँ", "रानी", "बहन", "पत्नी", "बहू")):
        return "female"
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


def resolve_character_voice_profile(
    speaker_name: str,
    char_entry: Optional[dict],
    char_map: dict,
    language: str = "Hindi",
    scene_idx: int = 0,
) -> tuple[str, str, str]:
    """
    Return distinct (edge_tts_voice, pitch_hz, rate_pct) per Pocket FM / Bollywood drama role:
    - Dramatic Narrator: hi-IN-MadhurNeural, suspense storytelling pitch="-4Hz", rate="+4%"
    - Hero: hi-IN-MadhurNeural, bold heroic pitch="+3Hz", rate="+2%"
    - Villain: hi-IN-MadhurNeural, heavy dangerous bass pitch="-14Hz", rate="-6%"
    - Saas / Sharp Female Villain: hi-IN-SwaraNeural, sharp tone pitch="-4Hz", rate="+3%"
    - Female / Heroine: hi-IN-SwaraNeural, pitch="+4Hz", rate="+2%"
    - Crowd / Log: hi-IN-SwaraNeural / pitch variations
    """
    lang = normalize_language(language)
    role = detect_speaker_role(speaker_name, char_map)
    raw_pitch_key = normalize_pitch((char_entry or {}).get("pitch", "normal"))
    explicit_voice = str((char_entry or {}).get("voice", "") or "").strip()

    if lang == "English":
        if role == "narrator":
            return ("en-IN-PrabhatNeural", "-4Hz", "+4%")
        if role == "saas":
            return ("en-IN-NeerjaNeural", "-4Hz", "+3%")
        if role in ("female", "crowd") or "Swara" in explicit_voice or "Neerja" in explicit_voice:
            return ("en-IN-NeerjaNeural", "+4Hz" if role == "female" else "+8Hz", "+4%")
        if role == "villain":
            return ("en-IN-PrabhatNeural", "-14Hz", "-6%")
        return ("en-IN-PrabhatNeural", "+3Hz", "+2%")

    # Respect explicit non-normal user pitch preset if chosen
    if raw_pitch_key != "normal" and raw_pitch_key in PITCH_PRESETS:
        p = PITCH_PRESETS[raw_pitch_key]
        v = explicit_voice if explicit_voice else ("hi-IN-SwaraNeural" if role in ("female", "saas", "crowd") else "hi-IN-MadhurNeural")
        if v == "en-IN-PrabhatNeural":
            v = "hi-IN-MadhurNeural"
        return (v, p["pitch"], p["rate"])

    if role == "narrator":
        return ("hi-IN-MadhurNeural", "-4Hz", "+4%")
    if role == "villain":
        return ("hi-IN-MadhurNeural", "-14Hz", "-6%")
    if role == "saas":
        return ("hi-IN-SwaraNeural", "-4Hz", "+3%")
    if role == "crowd":
        crowd_variants = [
            ("hi-IN-SwaraNeural", "+6Hz", "+6%"),
            ("hi-IN-MadhurNeural", "+8Hz", "+8%"),
            ("hi-IN-SwaraNeural", "-4Hz", "+4%"),
        ]
        return crowd_variants[scene_idx % len(crowd_variants)]
    if role == "female" or "Swara" in explicit_voice:
        return ("hi-IN-SwaraNeural", "+4Hz", "+2%")
    # Default Hero bold commanding tone
    return ("hi-IN-MadhurNeural", "+3Hz", "+2%")


def _get_or_create_role_portrait(role_kind: str, proj_dir: str) -> Optional[str]:
    """
    Generate and cache a realistic Bollywood Villain or Crowd face portrait ONLY when
    the script introduces Villain or Log/Crowd speakers that the user did not upload a photo for.
    Uploaded user characters NEVER use this — they stay 100% locked to the user's 5-angle scan.
    """
    clean_role = "crowd" if role_kind == "crowd" else "villain"
    out_path = safe_project_file(proj_dir, f"auto_role_{clean_role}.jpg")
    if os.path.exists(out_path) and os.path.getsize(out_path) > 4000:
        return out_path

    prompt = (
        "Close-up cinematic portrait of dramatic Indian Bollywood villain man with intense eyes, dark beard, leather jacket, moody rim lighting, 4K film still, photorealistic, looking at camera"
        if clean_role == "villain"
        else "Cinematic Bollywood movie shot of expressive Indian street crowd people reacting excitedly in dramatic city night lighting, photorealistic 4K film still, clear faces"
    )
    # Fast photorealistic portrait fetch via Pollinations Flux
    try:
        encoded = urllib.parse.quote(prompt, safe="")
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=768&height=768&model=flux&nologo=true&seed={42 if clean_role == 'villain' else 99}"
        resp = requests.get(url, timeout=12)
        if resp.status_code == 200 and len(resp.content) > 5000:
            save_upright_image_bytes(resp.content, out_path, max_side=960)
            return out_path
    except Exception:
        pass
    return None


def resolve_character_name(raw_name: object, char_map: dict, fallback_idx: int = 0) -> str:
    """Resolve dialogue speaker accurately: preserves Villain/Crowd roles if distinct, and strictly locks user characters."""
    char_names = list(char_map.keys())
    if not char_names:
        return "Hero"
    raw = str(raw_name or "").strip()
    if raw in char_map:
        return raw
    raw_low = raw.lower()
    if raw_low:
        for cn in char_names:
            if cn.lower() == raw_low:
                return cn
        for cn in char_names:
            cn_low = cn.lower()
            if cn_low in raw_low or raw_low in cn_low:
                return cn
            cn_first = cn_low.split()[0]
            raw_first = raw_low.split()[0]
            if len(cn_first) >= 2 and cn_first == raw_first:
                return cn
        # If user uploaded 2+ characters, map villain mentions to character #2
        if len(char_names) >= 2 and any(k in raw_low for k in ("villain", "don", "boss", "enemy", "विलेन", "डॉन", "दुश्मन")):
            return char_names[1]
        # If user uploaded only 1 hero photo and script explicitly has Villain or Crowd/Log, keep distinct speaker label!
        if any(k in raw_low for k in ("villain", "don", "kaal", "boss", "विलेन", "डॉन", "दुश्मन")):
            return "Villain"
        if any(k in raw_low for k in ("log", "crowd", "public", "people", "लोग", "भीड़", "जनता", "गाँव वाले")):
            return "Log (Crowd)"
    return char_names[fallback_idx % len(char_names)]

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
                for em, u in data.get("users", {}).items():
                    u.setdefault("wallet_balance", 0)
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
    return {
        "app": "CinePop AI — Pocket Bollywood & Face-Swap Studio",
        "status": "100% Live",
        "engine_version": "v5-pocketfm-fullscreen-faceswap-pil-subtitles",
        "devanagari_font": find_devanagari_font_path(bold=True) or "not-found",
        "gemini_configured": bool(GEMINI_API_KEY),
        "opencv_faceswap": OPENCV_AVAILABLE,
        "video_engine": "Pollinations Flux + HF Face-Swap + SadTalker/Wav2Lip + PIL Hindi Subtitles",
        "admin_pin_enabled": True,
        "price_per_movie": f"₹{MOVIE_PRICE_INR}",
    }


@app.post("/auth/google-login/")
def google_login(
    email: str = Form(...),
    name: str = Form("CinePop Creator"),
    photo_url: str = Form(""),
    device_id: str = Form("android"),
    admin_pin: str = Form(""),
    referral_code: str = Form(""),
):
    email = email.lower().strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Valid Google Email required")

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
            "photo_url": photo_url,
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
        if not db["users"][email].get("referral_code"):
            db["users"][email]["referral_code"] = make_referral_code(email)
        db["users"][email].setdefault("referred_by", "")
        db["users"][email].setdefault("referrals_count", 0)
        db["users"][email].setdefault("referral_earnings", 0)
        if name:
            db["users"][email]["name"] = name
        if photo_url:
            db["users"][email]["photo_url"] = photo_url

    referral_message = ""
    if referral_code.strip():
        ok, msg = apply_referral_internal(db, email, referral_code.strip())
        if ok:
            referral_message = msg

    save_db(db)
    return {
        "user": db["users"][email],
        "is_admin": admin_verified,
        "is_admin_email": is_admin_email,
        "requires_admin_pin": is_admin_email and not admin_verified,
        "referral_message": referral_message,
        "upi_id": UPI_ID,
        "movie_price": MOVIE_PRICE_INR,
    }


@app.post("/auth/instant-login/")
def instant_guest_login(
    device_id: str = Form("android"),
    guest_name: str = Form(""),
    referral_code: str = Form(""),
):
    """1-Click Guest / Instant Start with 1 Free Trial Movie Pass so any new user can start immediately."""
    clean_dev = re.sub(r"[^a-zA-Z0-9_-]", "", (device_id or "guest").strip())[:24] or "guest"
    guest_id = str(uuid.uuid4())[:8]
    email = f"guest_{clean_dev}_{guest_id}@cinepop.guest".lower()
    display_name = (guest_name or "").strip()[:32] or f"Guest Creator {guest_id[:4].upper()}"

    db = load_db()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db["users"][email] = {
        "email": email,
        "name": display_name,
        "photo_url": "",
        "device_id": clean_dev,
        "installed_at": now,
        "last_active": now,
        "credits": 1,  # 1 Free Trial Movie Pass for Instant Start
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
        "message": "⚡ Instant Start तैयार है! आपको 1 Free Trial Video Pass मिल गया है।",
        "upi_id": UPI_ID,
        "movie_price": MOVIE_PRICE_INR,
    }


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
    users_list = list(db["users"].values())
    referrals_list = db.get("referrals", [])
    face_swaps_map = db.get("face_swaps", {})
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


@app.post("/ai/generate-story/")
def ai_generate_story(
    topic: str = Form(...),
    genre: str = Form("Action"),
    char_names: str = Form("Vikram, Kaal"),
    duration_minutes: int = Form(30),
):
    clean_topic = topic.strip()
    if len(clean_topic) < 4:
        raise HTTPException(status_code=400, detail="कृपया कम से कम 1 लाइन का टॉपिक लिखें।")

    names = [n.strip() for n in char_names.split(",") if n.strip()] or ["Vikram", "Kaal"]
    hero = names[0]
    rival = names[1] if len(names) > 1 else "Villain"

    fallback_story = f"""शीर्षक: {clean_topic[:36]} ({genre})

[Location: Gaon ka Khet | Mood: Emotional]
{hero}: यह कहानी हमारे गाँव और अपनों की हिफ़ाज़त से शुरू होती है। {clean_topic}।
{rival}: तुम जो सोच रहे हो वह आसान नहीं होगा, असली इम्तिहान अब शुरू होगा!

[Location: Mumbai ki Barish | Mood: Suspense]
{hero}: तेज़ बारिश और अंधेरे में भी सच छुप नहीं सकता। मुझे हर हाल में जवाब ढूंढना होगा।
{rival}: तुम्हारे हर कदम पर मेरी नज़र है, देखते हैं इस खेल में कौन टिकता है!

[Location: Royal Palace | Mood: Action]
{hero}: अब वक़्त आ गया है इस कहानी का सबसे बड़ा फैसला करने का!
{rival}: तो आओ, आज पूरी दुनिया देखेगी कि किसके इरादों में ज़्यादा दम है!"""

    if not client:
        return {"story": fallback_story, "genre": genre}

    try:
        prompt = f"""तुम एक अवॉर्ड-विनिंग बॉलीवुड स्टोरी और स्क्रिप्ट राइटर हो।
Genre: {genre}
Topic: "{clean_topic}"
Characters: {", ".join(names)}
Duration Target: {duration_minutes} minutes

इस 1-लाइन टॉपिक पर पूरी डिटेल्ड, रोमांचक हिंदी कहानी और सीन-बाय-सीन स्क्रिप्ट लिखो जिसे यूज़र पढ़कर एडिट भी कर सके।
कम से कम 4 अलग-अलग रियल सिनेमैटिक लोकेशंस (जैसे Gaon ka Khet, Purani Haveli, Mumbai ki Barish, Deep Jungle, Police Station, Outer Space, Royal Palace) शामिल करो।
फॉर्मेट:
शीर्षक: <फिल्म का नाम>
[Location: <Location Name> | Mood: <Action/Emotional/Suspense/Comedy/Romantic>]
{hero}: <दमदार हिंदी डायलॉग>
{rival}: <दमदार हिंदी डायलॉग>
सीधे हिंदी कहानी और स्क्रिप्ट लिखो।"""
        res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        story = (res.text or "").strip() or fallback_story
        return {"story": story, "genre": genre}
    except Exception:
        return {"story": fallback_story, "genre": genre}


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


def _try_hf_scene_faceswap(scene_img_path: str, actor_face_path: str, out_path: str) -> Optional[str]:
    """
    Seamlessly merge the user's scanned face onto the full-screen AI drama scene's hero using
    100% FREE HuggingFace Face-Swap spaces (`tonyassi/face-swap`, `ezioruan/inswapper`).
    Returns out_path on success, or None to fall back to seamless skin-tone feathered compositing.
    """
    if not scene_img_path or not actor_face_path or not os.path.exists(scene_img_path) or not os.path.exists(actor_face_path):
        return None
    for space_id in ("tonyassi/face-swap", "ezioruan/inswapper"):
        try:
            hf_client = Client(space_id)
            res = hf_client.predict(
                handle_file(actor_face_path),
                handle_file(scene_img_path),
                api_name="/predict",
            )
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
                open_upright_image(swapped).save(out_path, "JPEG", quality=93)
                return out_path
        except Exception:
            continue
    return None


def render_location_backdrop(
    location_key: str,
    mood: str,
    prompt_hint: str = "",
    aspect_ratio: str = "16:9",
    include_hero: bool = False,
    hero_role: str = "hero",
) -> Image.Image:
    """
    Generate a FULL-SCREEN Pocket FM / Moj 3D Graphic-Novel / Realistic Bollywood drama scene
    at exact 9:16 (720x1280) or 16:9 (1280x720) via 100% FREE `image.pollinations.ai` Flux/Turbo model.
    ZERO cartoon rectangles/huts and ZERO paid APIs!
    """
    W, H = get_aspect_dimensions(aspect_ratio)
    loc_descriptions = {
        "village_field": "golden sunset Indian mustard wheat field rural cinematic landscape, dramatic clouds, warm lens flare, 3D graphic novel Bollywood drama scene",
        "haunted_mansion": "ancient spooky Indian royal haveli mansion at night in blue fog and moonlight, dramatic horror thriller cinema atmosphere, photorealistic",
        "mumbai_rain": "Mumbai city street in heavy monsoon rain at night, wet asphalt neon reflections, dramatic Bollywood action cinema scene, bokeh",
        "deep_jungle": "dense misty Indian forest jungle with dramatic volumetric sunbeams through trees, cinematic adventure drama scene",
        "police_station": "dramatic dark Indian police headquarters interior at night with red and blue siren bokeh reflections, crime thriller cinema scene",
        "outer_space": "deep cosmic nebula and glowing planet in outer space, realistic IMAX sci-fi drama backdrop",
        "royal_palace": "grand Rajasthani Indian royal palace hall lit with golden chandeliers and torches, epic Pocket FM Bollywood drama set",
        "mountains": "dramatic snow-capped Himalayan mountain peaks in twilight mist, epic Bollywood cinema landscape",
        "desert_fort": "majestic Rajasthan desert sandstone fort at fiery sunset, golden dust atmosphere, cinema background",
        "courtroom": "dramatic wood-paneled Indian courtroom hall with warm directional sunlight beams, high-stakes Bollywood drama set",
        "cyber_lab": "dark futuristic high-tech holographic command center lab with cyan rim lighting, sci-fi thriller set",
        "city_street": "dramatic Indian metropolitan city highway at night with glowing neon bokeh lights and wet road, Bollywood action thriller scene",
    }
    desc = loc_descriptions.get(location_key, loc_descriptions["city_street"])
    hint_clean = re.sub(r"[^a-zA-Z0-9 ,.-]", "", (prompt_hint or "")).strip()[:90]
    hero_tag = f"hero_{hero_role}" if include_hero else "bgonly"
    cache_seed_str = f"{location_key}_{mood}_{W}x{H}_{hero_tag}_{hint_clean[:28]}"
    cache_key = re.sub(r"[^a-zA-Z0-9_]", "_", cache_seed_str)[:76]
    cache_path = f"/tmp/cinepop_scene_{cache_key}.jpg"
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 8000:
        try:
            return open_upright_image(cache_path).resize((W, H), Image.Resampling.LANCZOS)
        except Exception:
            pass

    role_subject_map = {
        "villain": "intimidating Indian Bollywood mafia villain man in dark tailored coat looking intense, medium close-up portrait facing camera",
        "saas": "dramatic Indian wealthy matriarch woman in royal silk saree with gold jewelry, intense expression, medium close-up facing camera",
        "female": "expressive young Indian Bollywood actress in stylish ethnic cinematic attire, dramatic rim light, medium close-up facing camera",
        "narrator": "charismatic Indian Bollywood action hero in dramatic lighting looking forward, medium close-up portrait facing camera",
        "crowd": "dramatic Indian cinema character reacting intensely in cinematic crowd lighting, medium close-up facing camera",
        "hero": "handsome Indian Bollywood action drama hero wearing stylish jacket, intense cinematic expression, medium close-up facing camera",
    }
    subject_prefix = (
        f"{role_subject_map.get(hero_role, role_subject_map['hero'])}, "
        if include_hero
        else ""
    )
    scene_detail = f"{hint_clean}, " if hint_clean else ""
    full_prompt = (
        f"{subject_prefix}{scene_detail}{desc}, {mood} dramatic lighting, "
        f"Pocket FM graphic novel meets 4K photorealistic Bollywood cinema still, "
        f"shallow depth of field, anamorphic bokeh, ultra detailed, no text, no watermark"
    )
    encoded = urllib.parse.quote(full_prompt, safe="")
    seed_val = abs(hash(cache_key)) % 99999
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    candidate_urls = [
        (f"https://image.pollinations.ai/prompt/{encoded}?width={W}&height={H}&model=flux&nologo=true&seed={seed_val}", 28),
        (f"https://image.pollinations.ai/prompt/{encoded}?width={W}&height={H}&model=turbo&nologo=true&seed={seed_val}", 20),
        (f"https://image.pollinations.ai/prompt/{encoded}?width={W}&height={H}&nologo=true&seed={seed_val + 7}", 20),
    ]
    for url, t_sec in candidate_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=t_sec)
            ctype = str(resp.headers.get("content-type", "")).lower()
            if resp.status_code == 200 and len(resp.content) > 8000 and ("image" in ctype or resp.content[:2] in (b"\xff\xd8", b"\x89P")):
                bg_img = open_upright_image(resp.content).resize((W, H), Image.Resampling.LANCZOS)
                bg_img.save(cache_path, "JPEG", quality=92)
                return bg_img
        except Exception:
            continue

    # Last-resort photorealistic atmospheric lighting backdrop (ZERO cartoon buildings/boxes!)
    bg = Image.new("RGB", (W, H), (14, 10, 18))
    palettes = {
        "village_field": ((42, 28, 18), (138, 72, 26)),
        "haunted_mansion": ((8, 14, 24), (24, 36, 52)),
        "mumbai_rain": ((10, 20, 34), (22, 52, 76)),
        "deep_jungle": ((8, 26, 18), (22, 62, 40)),
        "police_station": ((14, 16, 30), (36, 22, 46)),
        "outer_space": ((6, 8, 22), (24, 18, 56)),
        "royal_palace": ((38, 14, 18), (118, 56, 20)),
        "mountains": ((16, 26, 44), (62, 78, 102)),
        "desert_fort": ((48, 20, 10), (142, 70, 20)),
        "courtroom": ((28, 18, 12), (72, 44, 24)),
        "cyber_lab": ((6, 18, 32), (14, 58, 84)),
        "city_street": ((14, 16, 28), (44, 26, 48)),
    }
    top_c, bot_c = palettes.get(location_key, palettes["city_street"])
    _draw_vertical_gradient(bg, top_c, bot_c)

    bokeh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(bokeh)
    rim = MOOD_RIM_COLORS.get(mood, (255, 88, 60))
    rng = random.Random(hash(cache_key) & 0xFFFFFFFF)
    for _ in range(18):
        bx = rng.randint(-60, W + 60)
        by = rng.randint(60, H - 60)
        br = rng.randint(50, 175)
        col = (rim[0], rim[1], rim[2], rng.randint(26, 68)) if rng.random() > 0.45 else (255, 190, 100, rng.randint(20, 50))
        bdraw.ellipse([bx - br, by - br, bx + br, by + br], fill=col)
    bokeh = bokeh.filter(ImageFilter.GaussianBlur(radius=46))
    bg = Image.alpha_composite(bg.convert("RGBA"), bokeh).convert("RGB")
    return bg


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
    full_line = f"{speaker_name.strip()}: {clean_diag}" if speaker_name.strip() else clean_diag

    is_vertical = width < 900
    if is_vertical and height < 220:
        height = 240

    strip = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(strip)

    # Smooth translucent cinema gradient band at bottom (no harsh blocky box!)
    for y in range(height):
        alpha = int(218 * ((y / max(1, height - 1)) ** 1.25))
        draw.line([(0, y), (width, y)], fill=(6, 6, 14, alpha))

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
    bottom_pad = 64 if is_vertical else 22  # Extra safe-zone above Moj/Reels bottom UI
    start_y = max(18, height - (len(lines) * line_h) - bottom_pad)
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
):
    """
    POCKET FM / MOJ FULL-SCREEN CINEMATIC DRAMA FRAME (720x1280 9:16 OR 1280x720 16:9):
    1. 100% DELETED square photo boxes, sticker borders, and cartoon buildings!
    2. Generates a full-screen 3D/Graphic-Novel/Realistic Bollywood hero scene via FREE Pollinations Flux.
    3. Straightens user's scanned face via EXIF transpose (`open_upright_image`) and seamlessly merges
       it onto the AI Bollywood hero via FREE HuggingFace Face-Swap (`tonyassi/face-swap` / `ezioruan/inswapper`).
    4. If HF Face-Swap is temporarily busy, blends the upright actor full-bleed edge-to-edge with
       atmospheric lighting match (ZERO square box, ZERO floating borders!).
    5. Clean Hindi subtitles are overlaid separately as rock-steady PIL PNG strips AFTER camera motion!
    """
    W, H = get_aspect_dimensions(aspect_ratio)
    is_vertical = H > W
    role = detect_speaker_role(char_name)

    # Always enforce EXIF-upright portrait orientation on user's scanned photo
    actor = open_upright_image(raw_img_path)
    upright_actor_tmp = f"{out_frame_path}_upright_actor.jpg"
    scene_tmp = f"{out_frame_path}_ai_scene.jpg"
    swapped_tmp = f"{out_frame_path}_swapped.jpg"

    try:
        actor.save(upright_actor_tmp, "JPEG", quality=92)

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
            )
            use_hf_swap = True

        # Step A: Try 100% FREE HuggingFace AI Face-Swap onto the Full-Screen Bollywood Hero Scene
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

        # Step B: Seamless Full-Bleed Edge-to-Edge Environmental Portrait Blend (ZERO square photo box!)
        aw, ah = actor.size
        # Cover-scale actor so subject spans full frame naturally with zero hard edges or floating square
        scale = max((W * 1.08) / max(1, aw), (H * 0.92) / max(1, ah)) if is_vertical else max((W * 0.84) / max(1, aw), (H * 1.12) / max(1, ah))
        target_w = max(W, int(aw * scale)) if is_vertical else max(int(W * 0.78), int(aw * scale))
        target_h = max(int(H * 0.85), int(ah * scale))

        actor_resized = actor.resize((target_w, target_h), Image.Resampling.LANCZOS)
        actor_resized = ImageEnhance.Contrast(actor_resized).enhance(1.10)
        actor_resized = ImageEnhance.Color(actor_resized).enhance(1.06)

        # Ambient scene lighting color match onto actor
        bg_small = bg.resize((16, 16), Image.Resampling.BILINEAR)
        bg_avg = tuple(int(sum(p[c] for p in bg_small.getdata()) / 256) for c in range(3))
        tint_layer = Image.new("RGB", actor_resized.size, bg_avg)
        actor_graded = Image.blend(actor_resized, tint_layer, 0.16)

        bg_cin = bg.filter(ImageFilter.GaussianBlur(radius=4.5))
        bg_cin = ImageEnhance.Brightness(bg_cin).enhance(0.78)
        bg_cin = ImageEnhance.Contrast(bg_cin).enhance(1.10)

        canvas_actor = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        px = (W - target_w) // 2
        py = max(-40, min(0, (H - target_h) // 4))
        canvas_actor.paste(actor_graded.convert("RGBA"), (px, py))

        # Wide organic radial fade spanning the full frame (NO box edges anywhere!)
        mask = Image.new("L", (W, H), 0)
        mdraw = ImageDraw.Draw(mask)
        rx = int(W * (0.62 if is_vertical else 0.48))
        ry = int(H * (0.52 if is_vertical else 0.72))
        cx, cy = W // 2, int(H * 0.44)
        mdraw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(radius=88))

        rim_rgb = MOOD_RIM_COLORS.get(mood, (255, 88, 60))
        rim_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        rdraw = ImageDraw.Draw(rim_layer)
        rdraw.ellipse([cx - rx - 40, cy - ry, cx + rx + 40, cy + ry], fill=(rim_rgb[0], rim_rgb[1], rim_rgb[2], 66))
        rim_layer = rim_layer.filter(ImageFilter.GaussianBlur(radius=84))

        composed = Image.alpha_composite(bg_cin.convert("RGBA"), rim_layer)
        composed = Image.composite(canvas_actor, composed, mask).convert("RGB")

        # Subtle cinema vignette
        vignette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        vdraw = ImageDraw.Draw(vignette)
        vdraw.rectangle([0, 0, W, H], fill=(8, 10, 20, 34))
        vdraw.ellipse([int(W * 0.08), int(H * 0.04), int(W * 0.92), int(H * 0.96)], fill=(0, 0, 0, 0))
        vignette = vignette.filter(ImageFilter.GaussianBlur(radius=72))
        composed = Image.alpha_composite(composed.convert("RGBA"), vignette).convert("RGB")

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
) -> Optional[str]:
    """
    Attempt real Image-to-Video motion clip generation using 100% FREE HuggingFace Spaces:
    Wan-AI/Wan2.1 -> Lightricks/LTX-Video -> THUDM/CogVideoX-5B -> KwaiVGI/LivePortrait.
    Returns local path to generated MP4 if available, else None (fast fallback to FFmpeg Pro Motion VFX).
    """
    prompt_txt = (
        visual_action_prompt.strip()
        if visual_action_prompt and visual_action_prompt.strip()
        else f"Cinematic Bollywood {mood} scene, dynamic tracking camera movement, realistic lighting"
    )
    i2v_spaces = [
        ("Wan-AI/Wan2.1", "wan"),
        ("Lightricks/ltx-video-distilled", "ltx"),
        ("THUDM/CogVideoX-5B-Space", "cogvideo"),
        ("KwaiVGI/LivePortrait", "liveportrait"),
    ]
    for space_id, kind in i2v_spaces:
        try:
            hf_client = Client(space_id)
            if kind == "wan":
                res = hf_client.predict(
                    image=handle_file(raw_img_path),
                    prompt=prompt_txt,
                    api_name="/predict",
                )
            elif kind == "ltx":
                res = hf_client.predict(
                    prompt=prompt_txt,
                    negative_prompt="static image, blurry, distorted face, low quality",
                    input_image_filepath=handle_file(raw_img_path),
                    api_name="/image_to_video",
                )
            elif kind == "cogvideo":
                res = hf_client.predict(
                    prompt=prompt_txt,
                    image_input=handle_file(raw_img_path),
                    api_name="/generate",
                )
            else:
                res = hf_client.predict(
                    handle_file(raw_img_path),
                    api_name="/gpu_wrapped_execute_video",
                )
            vid_path = _extract_gradio_video_path(res)
            if vid_path and os.path.getsize(vid_path) > 2000:
                return vid_path
        except Exception:
            continue
    return None


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


def _try_talking_lipsync_spaces(
    face_img_path: str,
    dialogue_audio_path: str,
) -> Optional[str]:
    """
    Real AI Talking Lip-Sync via free HuggingFace Spaces:
    vinthony/SadTalker -> camenduru/Wav2Lip -> KwaiVGI/LivePortrait
    Returns path to real lip-synced talking-head video if space responds, else None.
    """
    if not face_img_path or not os.path.exists(face_img_path) or not dialogue_audio_path or not os.path.exists(dialogue_audio_path):
        return None

    mirrors = [
        ("vinthony/SadTalker", "sadtalker"),
        ("camenduru/Wav2Lip", "wav2lip"),
        ("KwaiVGI/LivePortrait", "liveportrait"),
    ]
    for space_id, kind in mirrors:
        try:
            hf_client = Client(space_id)
            if kind == "sadtalker":
                res = hf_client.predict(
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
            elif kind == "wav2lip":
                res = hf_client.predict(
                    face=handle_file(face_img_path),
                    audio=handle_file(dialogue_audio_path),
                    api_name="/predict",
                )
            else:
                res = hf_client.predict(
                    source_image=handle_file(face_img_path),
                    driving_audio=handle_file(dialogue_audio_path),
                    api_name="/predict",
                )
            vid_path = _extract_gradio_video_path(res)
            if vid_path and os.path.exists(vid_path) and os.path.getsize(vid_path) > 2000:
                return vid_path
        except Exception:
            continue
    return None


def _render_pro_motion_vfx_clip(
    framed_img_path: str,
    final_audio: str,
    out_mp4: str,
    mood: str = "Action",
    sfx_type: str = "none",
    dialogue_audio_path: Optional[str] = None,
    subtitle_png_path: Optional[str] = None,
    aspect_ratio: str = "16:9",
) -> bool:
    """
    Full-Screen Cinematic Talking Lip-Sync + Pro Motion VFX Engine (Supports both 9:16 720x1280 & 16:9 1280x720):
    - Real audio-envelope synchronized mouth lip movement + syllable head-nod micro-motion
    - Fast-paced camera tracking zoom/pan + combat shake on Action/punch/explosion/thunder/sword
    - Rain streaks, fiery sparks, atmospheric bokeh particles & dynamic lighting flashes
    - Rock-steady PIL Noto Sans Devanagari PNG Hindi subtitle strip overlaid cleanly at bottom
    """
    w_out, h_out = get_aspect_dimensions(aspect_ratio)
    src_w, src_h = int(w_out * 1.2), int(h_out * 1.2)
    dur = _probe_audio_duration(final_audio)
    fps = 24
    total_frames = max(36, int(dur * fps))
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

                # Track the speaker's face region once on the oversampled frame (ZERO CascadeClassifier!)
                face_box_src = _detect_skin_face_box(src)

                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(raw_vfx_mp4, fourcc, fps, (w_out, h_out))

                rng = random.Random(hash(framed_img_path) & 0xFFFFFFFF)
                particles = [
                    {
                        "x": rng.uniform(0, w_out),
                        "y": rng.uniform(0, h_out),
                        "vx": rng.uniform(-7.5, -2.5) if sfx_type in ("rain", "thunder") else rng.uniform(-3.0, 3.0),
                        "vy": rng.uniform(16.0, 26.0) if sfx_type in ("rain", "thunder") else rng.uniform(-4.2, -1.2),
                        "r": rng.randint(2, 5),
                    }
                    for _ in range(52 if high_impact or sfx_type in ("rain", "thunder") else 24)
                ]

                for f_idx in range(total_frames):
                    t = f_idx / max(1, total_frames - 1)
                    talk_amp = speech_env[f_idx] if f_idx < len(speech_env) else 0.0

                    # 1. Apply live speech-synchronized mouth & jaw talking animation on source frame
                    work_src = _apply_speech_lipsync_mouth_motion(src.copy(), talk_amp, face_box_src)

                    # 2. Natural speaking head-nod micro-motion + active-speaker subtle push-in zoom
                    nod_y = math.sin(f_idx * 0.55) * (4.8 * talk_amp)
                    nod_x = math.cos(f_idx * 0.32) * (2.6 * talk_amp)
                    talk_zoom_boost = 0.014 * talk_amp

                    if mood == "Action":
                        zoom = 1.05 + 0.12 * t + talk_zoom_boost
                        pan_x = math.sin(t * math.pi * 1.3) * 34.0 + nod_x
                        pan_y = math.cos(t * math.pi * 1.1) * 14.0 + nod_y
                    elif mood == "Suspense":
                        zoom = 1.03 + 0.10 * t + talk_zoom_boost
                        pan_x = math.sin(t * math.pi) * 24.0 + nod_x
                        pan_y = -12.0 * t + nod_y
                    elif mood == "Comedy":
                        zoom = 1.04 + 0.05 * abs(math.sin(f_idx * 0.16)) + talk_zoom_boost
                        pan_x = math.sin(f_idx * 0.14) * 26.0 + nod_x
                        pan_y = math.cos(f_idx * 0.12) * 10.0 + nod_y
                    elif mood == "Romantic":
                        zoom = 1.03 + 0.08 * t + talk_zoom_boost
                        pan_x = -28.0 + 56.0 * t + nod_x
                        pan_y = math.sin(t * math.pi) * 12.0 + nod_y
                    else:
                        zoom = 1.03 + 0.09 * t + talk_zoom_boost
                        pan_x = math.sin(t * math.pi * 0.8) * 18.0 + nod_x
                        pan_y = -8.0 * t + nod_y

                    shake_x = 0.0
                    shake_y = 0.0
                    if high_impact:
                        burst = 1.0 if (f_idx % 24) < 7 else 0.30
                        amp_s = (11.5 if sfx_type in ("explosion", "punch") else 6.5) * burst
                        shake_x = math.sin(f_idx * 1.9) * amp_s
                        shake_y = math.cos(f_idx * 2.3) * (amp_s * 0.7)

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

                    # 3. Dynamic storm / impact / active-speaker rim lighting pulse
                    light_boost = 1.0 + 0.03 * talk_amp
                    if sfx_type == "thunder" and (f_idx % 38) in (2, 3, 5):
                        light_boost = 1.30
                    elif sfx_type in ("explosion", "punch") and (f_idx % 28) in (1, 2):
                        light_boost = 1.22
                    if abs(light_boost - 1.0) > 0.01:
                        frame = cv2.convertScaleAbs(frame, alpha=light_boost, beta=3 if light_boost > 1.15 else 0)

                    # 4. Atmospheric particles (rain streaks, fiery combat sparks, cinema bokeh)
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
                    cv2.addWeighted(p_overlay, 0.34, frame, 0.66, 0, frame)

                    writer.write(frame)

                writer.release()

                if os.path.exists(raw_vfx_mp4) and os.path.getsize(raw_vfx_mp4) > 2000:
                    if subtitle_png_path and os.path.exists(subtitle_png_path):
                        cmd = [
                            "ffmpeg", "-y",
                            "-i", raw_vfx_mp4,
                            "-i", subtitle_png_path,
                            "-i", final_audio,
                            "-filter_complex", "[0:v][1:v]overlay=0:H-h[vout]",
                            "-map", "[vout]",
                            "-map", "2:a:0",
                            "-shortest",
                            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "24",
                            "-c:a", "aac", "-ar", "44100", "-b:a", "96k",
                            out_mp4,
                        ]
                    else:
                        cmd = [
                            "ffmpeg", "-y",
                            "-i", raw_vfx_mp4,
                            "-i", final_audio,
                            "-map", "0:v:0",
                            "-map", "1:a:0",
                            "-shortest",
                            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "24",
                            "-c:a", "aac", "-ar", "44100", "-b:a", "96k",
                            out_mp4,
                        ]
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

    # Path B: Pure FFmpeg Pro Motion + Clean PIL Hindi Subtitle Overlay
    shake_expr_x = "iw/2-(iw/zoom/2)+sin(on*1.7)*12" if high_impact else "iw/2-(iw/zoom/2)+sin(on*0.09)*18"
    shake_expr_y = "ih/2-(ih/zoom/2)+cos(on*2.1)*8" if high_impact else "ih/2-(ih/zoom/2)+cos(on*0.07)*8"
    zoom_rate = "0.0016" if high_impact else "0.0010"
    zoom_cap = "1.14" if high_impact else "1.10"
    vf_base = (
        f"scale={src_w}:{src_h},"
        f"zoompan=z='min(zoom+{zoom_rate},{zoom_cap})':x='{shake_expr_x}':y='{shake_expr_y}':d={total_frames}:s={w_out}x{h_out}:fps={fps},"
        f"eq=contrast=1.06:saturation=1.12:brightness=0.01,vignette=PI/5"
    )
    if subtitle_png_path and os.path.exists(subtitle_png_path):
        cmd_fb = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", framed_img_path,
            "-i", subtitle_png_path,
            "-i", final_audio,
            "-filter_complex", f"[0:v]{vf_base}[vm];[vm][1:v]overlay=0:H-h[vout]",
            "-map", "[vout]", "-map", "2:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-shortest", "-pix_fmt", "yuv420p", "-crf", "24",
            "-c:a", "aac", "-ar", "44100", "-b:a", "96k",
            out_mp4,
        ]
    else:
        cmd_fb = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", framed_img_path,
            "-i", final_audio,
            "-vf", vf_base,
            "-c:v", "libx264", "-preset", "veryfast", "-shortest", "-pix_fmt", "yuv420p", "-crf", "24",
            "-c:a", "aac", "-ar", "44100", "-b:a", "96k",
            out_mp4,
        ]
    subprocess.run(cmd_fb, check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000


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
):
    """
    POCKET FM / MOJ FULL-SCREEN TALKING LIP-SYNC + 5-ANGLE FACE-LOCK PIPELINE (9:16 OR 16:9):
    1. Renders a crisp transparent PIL Devanagari Hindi subtitle strip (`Speaker: Dialogue`) — ZERO □□□ boxes!
    2. Mixes character's Hindi voice with Bollywood BGM + Rain/Fight SFX using automatic audio ducking.
    3. Tries 100% FREE HuggingFace Real AI Talking Lip-Sync (`vinthony/SadTalker` -> `camenduru/Wav2Lip` -> `KwaiVGI/LivePortrait`)
       at the exact chosen ratio (720x1280 or 1280x720).
    4. Guaranteed fallback runs our Audio-Envelope Talking Mouth Lip-Sync + Head-Nod + Full-Screen Pro Motion VFX engine
       directly on the speaker's angle-matched full-screen drama frame so characters ALWAYS talk visibly!
    """
    w_out, h_out = get_aspect_dimensions(aspect_ratio)
    mixed_tmp = f"{out_mp4}_mix.mp3"
    sub_png = f"{out_mp4}_sub.png"
    final_audio = mix_cinematic_audio(
        audio_path, mood, sfx_type, mixed_tmp, target_duration_sec=target_duration_sec
    )
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

    effective_angles = dict(char_angles or {})
    if raw_img_path and os.path.exists(raw_img_path):
        effective_angles.setdefault("front", raw_img_path)
    angle_face_path = (
        effective_angles.get(scene_angle)
        if effective_angles.get(scene_angle) and os.path.exists(effective_angles[scene_angle])
        else raw_img_path
    )

    try:
        # 1. Try Real AI Talking Lip-Sync (SadTalker / Wav2Lip / LivePortrait) on the speaker's exact face + Hindi audio
        if angle_face_path and os.path.exists(angle_face_path):
            talk_vid = _try_talking_lipsync_spaces(angle_face_path, audio_path)
            if talk_vid and os.path.exists(talk_vid):
                try:
                    scale_crop = f"scale={w_out}:{h_out}:force_original_aspect_ratio=increase,crop={w_out}:{h_out}"
                    if rendered_sub and os.path.exists(rendered_sub):
                        fc = (
                            f"[0:v]{scale_crop},eq=contrast=1.06:saturation=1.10,vignette=PI/5[vbase];"
                            f"[vbase][1:v]overlay=0:H-h[vout]"
                        )
                        cmd = [
                            "ffmpeg", "-y",
                            "-stream_loop", "-1", "-i", talk_vid,
                            "-i", rendered_sub,
                            "-i", final_audio,
                            "-filter_complex", fc,
                            "-map", "[vout]", "-map", "2:a:0",
                            "-r", "24", "-shortest",
                            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "24",
                            "-c:a", "aac", "-ar", "44100", "-b:a", "96k",
                            out_mp4,
                        ]
                    else:
                        cmd = [
                            "ffmpeg", "-y",
                            "-stream_loop", "-1", "-i", talk_vid,
                            "-i", final_audio,
                            "-map", "0:v:0", "-map", "1:a:0",
                            "-vf", f"{scale_crop},eq=contrast=1.06:saturation=1.10,vignette=PI/5",
                            "-r", "24", "-shortest",
                            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "24",
                            "-c:a", "aac", "-ar", "44100", "-b:a", "96k",
                            out_mp4,
                        ]
                    subprocess.run(cmd, check=True, timeout=45, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000:
                        return
                except Exception:
                    pass

        # 2. Guaranteed Audio-Synchronized Talking Lip-Sync + Full-Screen Cinematic Pro Motion VFX
        _render_pro_motion_vfx_clip(
            framed_img_path,
            final_audio,
            out_mp4,
            mood=mood,
            sfx_type=sfx_type,
            dialogue_audio_path=audio_path,
            subtitle_png_path=rendered_sub,
            aspect_ratio=aspect_ratio,
        )
    finally:
        for tmp_p in (mixed_tmp, sub_png):
            if os.path.exists(tmp_p):
                try:
                    os.remove(tmp_p)
                except Exception:
                    pass


def ffmpeg_concat(file_list: list, out_path: str):
    valid_files = [
        f for f in file_list if os.path.exists(f) and os.path.getsize(f) > 500
    ]
    if not valid_files:
        return
    txt = f"{out_path}.txt"
    with open(txt, "w", encoding="utf-8") as f:
        for item in valid_files:
            f.write(f"file '{os.path.abspath(item)}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", txt, "-c", "copy", out_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if os.path.exists(txt):
        os.remove(txt)


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
        "background", "बैकग्राउंड", "int", "ext", "narrator", "सूत्रधार",
    }

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
            speaker_raw = re.sub(r"[\*\#\_]", "", m.group(1)).strip()
            speaker_low = speaker_raw.lower()
            if speaker_low in skip_tags or re.match(r"^(?:scene|सीन)\s*\d+$", speaker_low):
                continue
            diag = clean_spoken_dialogue(m.group(2))
            if len(diag) < 2:
                continue
            matched = next(
                (c for c in char_names if c.lower() == speaker_low),
                speaker_raw if any(k in speaker_low for k in ("villain", "don", "log", "crowd", "विलेन", "लोग", "भीड़"))
                else char_names[len(parsed) % len(char_names)],
            )
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
            # Pocket FM / Moj fast-paced cuts: 14 scenes (~4.2s each) for 1 Min/Part, 22 scenes for 2 Min/Part
            scenes_per_chapter = 14 if part_duration <= 1 else 22
        else:
            total_chapters = 1 if duration <= 10 else max(1, duration // 10)
            if duration <= 1:
                scenes_per_chapter = 14
            elif duration <= 2:
                scenes_per_chapter = 20
            elif duration <= 5:
                scenes_per_chapter = 24
            elif duration <= 10:
                scenes_per_chapter = 28
            else:
                scenes_per_chapter = 30

        story_mode = state.get("story_mode", "ai")
        language = normalize_language(state.get("language", "Hindi"))
        default_mood = normalize_mood(state.get("default_mood", "Action"))
        default_location = state.get("default_location", "")
        project_custom_bg = state.get("custom_bg_path")
        watermark_text = state.get("watermark_text", "")
        watermark_position = state.get("watermark_position", "top-right")

        manual_lines = (
            parse_manual_script_lines(
                state["idea"], char_names, default_location, default_mood
            )
            if story_mode == "manual"
            else []
        )

        if not state.get("outline"):
            state["progress"] = 5
            if story_mode == "manual" and len(manual_lines) >= 2:
                per_ch = max(1, math.ceil(len(manual_lines) / total_chapters))
                chunks = [
                    manual_lines[i : i + per_ch]
                    for i in range(0, len(manual_lines), per_ch)
                ]
                state["outline"] = {
                    "title": "Custom Script Series",
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
                if client:
                    try:
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
                        prompt = f"""
                        तुम एक Pocket FM / Moj और बॉलीवुड डायरेक्टर हो। '{state['idea']}' पर कहानी की रूपरेखा बनाओ।
                        {strict_note}
                        {cliff_note}
                        कैरेक्टर्स: {char_names}
                        कहानी को ठीक {total_chapters} बराबर Parts/Chapters में बांटो।
                        सिर्फ शुद्ध JSON दो:
                        {{"title": "सीरीज़/फिल्म का दमदार नाम", "chapters": [{{"no": 1, "title": "Part 1: धमाकेदार शुरुआत", "plot": "विस्तार से कहानी, ट्विस्ट और अंत का सस्पेंस"}}]}}
                        """
                        res = client.models.generate_content(
                            model="gemini-2.5-flash", contents=prompt
                        )
                        parsed_out = safe_extract_json(res.text, fallback_outline)
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
                    except Exception:
                        state["outline"] = fallback_outline
                else:
                    state["outline"] = fallback_outline

                state["scripts"] = {}
                state["progress"] = 12
                state["status_text"] = "कहानी तैयार! अब Part 1 के तेज़-रफ़्तार डायलॉग्स और एक्शन सीन्स बन रहे हैं (12%)..."
                save_state(proj_dir, state)

        chapters = state["outline"]["chapters"]
        manual_chunks = state.get("manual_chunks")
        prev_plot = "कहानी की शुरुआत"
        total_ch_count = max(1, len(chapters))

        for ch_idx, ch in enumerate(chapters):
            ch_no = ch["no"]
            ch_mp4 = f"{proj_dir}/ch_{ch_no}.mp4"
            if ch_no in state["done_chapters"] and os.path.exists(ch_mp4):
                prev_plot = ch["plot"]
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
            else:
                loc_k, loc_l = detect_location_meta(ch["plot"], default_location)
                fallback_dialogues = [
                    {
                        "character": char_names[i % len(char_names)],
                        "dialogue": (
                            f"आगे क्या होगा? इस राज़ से पर्दा उठेगा अगले एपिसोड में — देखिए Part {ch_no + 1} में!"
                            if (series_mode == "series" and ch_no < total_ch_count and i == scenes_per_chapter - 1)
                            else f"{ch['plot']} - सीन {i+1} का दमदार संवाद।"
                        ),
                        "location": loc_l,
                        "location_key": loc_k,
                        "mood": default_mood,
                        "sfx": detect_sfx_type(ch["plot"], loc_k, default_mood),
                        "camera_angle": ["front", "left", "right", "up", "down"][i % 5],
                        "visual_action_prompt": build_visual_action_prompt(
                            char_names[i % len(char_names)],
                            ch["plot"],
                            loc_l,
                            default_mood,
                            detect_sfx_type(ch["plot"], loc_k, default_mood),
                        ),
                    }
                    for i in range(scenes_per_chapter)
                ]

                if client:
                    try:
                        cliff_instruction = (
                            f"अंतिम सीन (आखिरी डायलॉग) में Narrator की आवाज़ में ज़बरदस्त सस्पेंस Cliffhanger ज़रूर डालो: "
                            f"जैसे 'आगे क्या होगा? देखिए Part {ch_no + 1} में...'"
                            if (series_mode == "series" and ch_no < total_ch_count)
                            else "अंतिम सीन में धमाकेदार क्लाइमैक्स डायलॉग रखो।"
                        )
                        script_prompt = f"""
                        सीरीज़/फिल्म: {state['outline']['title']} | पिछला सीन: {prev_plot}
                        वर्तमान Part {ch_no}/{total_ch_count}: {ch['plot']}
                        मुख्य अपलोड किए गए कैरेक्टर: {char_names} (ज़रूरत पड़ने पर Narrator, Villain, Saas या Log/Crowd भी बोल सकते हैं)।
                        भाषा / Dialect: {language} (सभी डायलॉग्स असली {language} अंदाज़ में छोटे, तेज़-रफ़्तार और पंचदार हों — हर डायलॉग 3-4 सेकंड का हो!)
                        सख्त नियम: डायलॉग के अंदर कभी भी 'Title:', 'Scene 1:', 'Location:', 'Action:' जैसे स्क्रिप्ट टैग मत लिखो — सिर्फ बोले जाने वाले डायलॉग लिखो!
                        {cliff_instruction}
                        ठीक {scenes_per_chapter} फास्ट-कट सीन्स लिखो (हर सीन में अलग कैमरा एंगल: Hero close-up, Villain angry reaction, Ring/Car zoom, Action cut)।
                        हर सीन के साथ location, mood (Action, Emotional, Suspense, Comedy, Romantic), sfx (none, rain, thunder, punch, explosion, sword, footsteps), camera_angle (front, left, right, up, down), और dramatic English visual_action_prompt दो।
                        सिर्फ JSON लिस्ट दो: [{{"character": "{char_names[0]}", "dialogue": "डायलॉग...", "location": "Mumbai ki Barish", "mood": "{default_mood}", "sfx": "rain", "camera_angle": "front", "visual_action_prompt": "dramatic close-up in rain, neon reflections, intense thriller lighting"}}]
                        """
                        s_res = client.models.generate_content(
                            model="gemini-2.5-flash", contents=script_prompt
                        )
                        raw_list = safe_extract_json(s_res.text, fallback_dialogues)
                        raw_list = raw_list[: max(6, scenes_per_chapter + 2)]
                        dialogues = []
                        for idx_d, item in enumerate(raw_list):
                            raw_spk = str(item.get("character", "")).strip()
                            low_spk = raw_spk.lower()
                            if any(k in low_spk for k in ("narrator", "sutradhar", "villain", "saas", "don", "log", "crowd", "सूत्रधार", "विलेन", "सास", "लोग", "भीड़")):
                                c_name = raw_spk
                            else:
                                c_name = resolve_character_name(raw_spk, char_map, idx_d)
                            diag_text = clean_spoken_dialogue(str(item.get("dialogue", "")))
                            if len(diag_text) < 2:
                                continue
                            lk, ll = detect_location_meta(
                                f"{item.get('location', '')} {diag_text}",
                                item.get("location") or default_location,
                            )
                            mood_val = normalize_mood(item.get("mood", ""), default_mood)
                            raw_sfx = str(item.get("sfx", "")).lower().strip()
                            sfx_val = raw_sfx if raw_sfx in ALLOWED_SFX else detect_sfx_type(diag_text, lk, mood_val)
                            vap = str(item.get("visual_action_prompt", "")).strip() or build_visual_action_prompt(
                                c_name, diag_text, ll, mood_val, sfx_val
                            )
                            raw_ang = str(item.get("camera_angle", "")).lower().strip()
                            cam_angle = (
                                raw_ang
                                if raw_ang in ALLOWED_FACE_ANGLES
                                else detect_scene_angle(vap, diag_text, mood_val, sfx_val, idx_d)
                            )
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
                                }
                            )
                        if not dialogues:
                            dialogues = fallback_dialogues
                    except Exception:
                        dialogues = fallback_dialogues
                else:
                    dialogues = fallback_dialogues

                # Guarantee Cliffhanger ending on Part k < N in Series Mode
                if series_mode == "series" and ch_no < total_ch_count and dialogues:
                    last_line = dialogues[-1]["dialogue"]
                    if f"Part {ch_no + 1}" not in last_line and "पार्ट" not in last_line:
                        dialogues.append(
                            {
                                "character": "Narrator",
                                "dialogue": f"आगे क्या होने वाला है? यह राज़ खुलेगा अगले एपिसोड में — देखिए Part {ch_no + 1} में!",
                                "location": dialogues[-1].get("location", "Mumbai City Street"),
                                "location_key": dialogues[-1].get("location_key", "city_street"),
                                "mood": "Suspense",
                                "sfx": "thunder",
                                "camera_angle": "front",
                                "visual_action_prompt": "dramatic cliffhanger freeze-frame suspense thriller lighting, dark stormy atmosphere",
                            }
                        )

            state["scripts"][str(ch_no)] = dialogues
            state["progress"] = min(94, ch_base_pct + 5)
            state["status_text"] = (
                f"Part {ch_no}/{total_ch_count} स्क्रिप्ट तैयार ({len(dialogues)} फास्ट-कट सीन्स)! फुल-स्क्रीन AI Lip-Sync वीडियो रेंडर हो रहा है ({state['progress']}%)..."
            )
            save_state(proj_dir, state)

            clips = []
            total_scenes = max(1, len(dialogues))
            # Fast-paced Pocket FM / Moj cut pacing: ~60s for 1-Min Part, ~120s for 2-Min Part
            if series_mode == "series":
                target_scene_sec = max(3.2, min(5.5, (part_duration * 60.0) / total_scenes))
            elif duration == 1:
                target_scene_sec = max(3.5, 60.0 / total_scenes)
            else:
                target_scene_sec = 0.0

            for i, d in enumerate(dialogues):
                raw_spk = str(d.get("character", "")).strip()
                c_name = (
                    raw_spk
                    if raw_spk in char_map or any(k in raw_spk.lower() for k in ("narrator", "sutradhar", "villain", "saas", "log", "crowd", "सूत्रधार", "विलेन", "सास", "लोग", "भीड़"))
                    else resolve_character_name(raw_spk, char_map, i)
                )
                diag_text = clean_spoken_dialogue(d.get("dialogue", "")) or "अब असली खेल शुरू होगा!"
                loc_key = d.get("location_key") or "city_street"
                loc_label = d.get("location") or default_location or "Mumbai City Street"
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

                # Uploaded user characters strictly use their 5-angle scanned photo; Narrator uses lead hero dramatic shot; unuploaded Villain/Saas/Crowd get realistic role portraits
                char_entry = char_map.get(c_name)
                if char_entry:
                    exact_char_photo = select_character_angle_photo(char_entry, scene_angle)
                    char_angles_dict = char_entry.get("angles")
                else:
                    role_kind = detect_speaker_role(c_name, char_map)
                    if role_kind == "narrator":
                        primary_hero = char_map[char_names[0]]
                        exact_char_photo = select_character_angle_photo(primary_hero, scene_angle)
                        char_angles_dict = primary_hero.get("angles")
                    else:
                        role_photo = await asyncio.to_thread(_get_or_create_role_portrait, role_kind, proj_dir)
                        primary_fallback = char_map[char_names[0]]
                        exact_char_photo = role_photo or select_character_angle_photo(primary_fallback, scene_angle)
                        char_angles_dict = {"front": exact_char_photo}

                bg_override = d.get("bg_path") or project_custom_bg
                tts_voice, tts_pitch, tts_rate = resolve_character_voice_profile(
                    c_name, char_entry, char_map, language=language, scene_idx=i
                )

                a_file = f"{ch_temp}/a_{i}.mp3"
                f_img = f"{ch_temp}/f_{i}.jpg"
                v_file = f"{ch_temp}/v_{i}.mp4"

                if not os.path.exists(v_file):
                    await generate_bulletproof_audio(
                        diag_text,
                        tts_voice,
                        a_file,
                        pitch=(char_entry or {}).get("pitch", "normal"),
                        language=language,
                        pitch_override=tts_pitch,
                        rate_override=tts_rate,
                    )
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
                    )
                    await asyncio.to_thread(
                        render_exact_face_clip,
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
                    )
                    if os.path.exists(a_file):
                        os.remove(a_file)
                    if os.path.exists(f_img):
                        os.remove(f_img)
                clips.append(v_file)

                scene_pct = min(
                    94,
                    ch_base_pct + 5 + int(((i + 1) / total_scenes) * max(1, ch_span_pct - 5)),
                )
                state["progress"] = max(state.get("progress", 15), scene_pct)
                ready_hint = (
                    " (Part 1 तैयार — अभी Play करें!)"
                    if (series_mode == "series" and 1 in state.get("ready_parts", []) and ch_no > 1)
                    else ""
                )
                state["status_text"] = (
                    f"Part {ch_no}/{total_ch_count} · सीन {i + 1}/{total_scenes} रेंडर पूरा ({state['progress']}%){ready_hint}"
                )
                save_state(proj_dir, state)

            ffmpeg_concat(clips, ch_mp4)
            state["done_chapters"].append(ch_no)
            prev_plot = ch["plot"]
            rebuild_parts_and_full(proj_dir, state)
            save_state(proj_dir, state)

    except Exception as e:
        state["status_text"] = f"रुकावट आई (रीस्टार्ट पर यहीं से चलेगा): {str(e)}"
        save_state(proj_dir, state)


@app.post("/start-autonomous-movie/")
async def start_movie(
    background_tasks: BackgroundTasks,
    user_email: str = Form(...),
    idea: str = Form(...),
    duration_minutes: int = Form(...),
    char_names: str = Form(...),
    voices: str = Form(...),
    images: list[UploadFile] = File(...),
    angle_images: Optional[list[UploadFile]] = File(default=None),
    angle_meta: str = Form("[]"),
    story_mode: str = Form("ai"),
    genre: str = Form("Action"),
    language: str = Form("Hindi"),
    char_pitches: str = Form("{}"),
    default_mood: str = Form("Action"),
    default_location: str = Form(""),
    watermark_text: str = Form(""),
    watermark_position: str = Form("top-right"),
    aspect_ratio: str = Form("9:16"),
    platform: str = Form("instagram"),
    series_mode: str = Form("series"),
    part_duration_minutes: int = Form(1),
    total_parts: int = Form(3),
    admin_pin: str = Form(""),
    custom_bg: Optional[UploadFile] = File(None),
):
    email = user_email.lower().strip()
    if not images:
        raise HTTPException(
            status_code=400,
            detail="कम से कम 1 कैरेक्टर की फोटो (या 5-Angle 3D Face Scan) ज़रूरी है!",
        )
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

    names = [x.strip() for x in char_names.split(",") if x.strip()]
    v_list = [x.strip() for x in voices.split(",")]
    try:
        pitches_map = json.loads(char_pitches) if char_pitches.strip() else {}
        if not isinstance(pitches_map, dict):
            pitches_map = {}
    except Exception:
        pitches_map = {}

    char_map = {}
    ordered_names: list[str] = []
    for idx, img in enumerate(images):
        c_name = names[idx] if idx < len(names) else f"Actor_{idx+1}"
        c_voice = v_list[idx] if idx < len(v_list) else "hi-IN-MadhurNeural"
        c_pitch = normalize_pitch(str(pitches_map.get(c_name, pitches_map.get(str(idx), "normal"))))
        raw_img_bytes = await img.read()
        if not raw_img_bytes:
            raise HTTPException(status_code=400, detail=f"Character '{c_name}' की फोटो खाली है।")
        img_path = safe_project_file(proj_dir, f"char_{idx}_front.jpg")
        save_upright_image_bytes(raw_img_bytes, img_path)
        char_map[c_name] = {
            "image": img_path,
            "voice": c_voice,
            "pitch": c_pitch,
            "angles": {"front": img_path},
            "face_profile_3d": False,
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
            raw_a = await a_file.read()
            if not raw_a:
                continue
            meta_item = parsed_angle_meta[a_idx] if a_idx < len(parsed_angle_meta) and isinstance(parsed_angle_meta[a_idx], dict) else {}
            angle_key = str(meta_item.get("angle", "")).lower().strip()
            if angle_key not in ALLOWED_FACE_ANGLES:
                fname_low = (a_file.filename or "").lower()
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
        raw_bytes = await custom_bg.read()
        if raw_bytes:
            custom_bg_path = safe_project_file(proj_dir, "custom_bg.jpg")
            save_upright_image_bytes(raw_bytes, custom_bg_path, max_side=1600)

    clean_lang = normalize_language(language)
    wm_pos = watermark_position if watermark_position in ("top-right", "bottom-right", "top-left") else "top-right"
    clean_ratio = "9:16" if str(aspect_ratio).strip() in ("9:16", "vertical", "instagram", "moj") else "16:9"
    clean_platform = "instagram" if clean_ratio == "9:16" else "youtube"
    clean_series_mode = "series" if str(series_mode).strip().lower() == "series" else "single"
    clean_part_dur = 2 if int(part_duration_minutes or 1) >= 2 else 1
    clean_total_parts = max(1, min(15, int(total_parts or 3))) if clean_series_mode == "series" else 1

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
        "custom_bg_path": custom_bg_path,
        "duration_minutes": (clean_part_dur * clean_total_parts) if clean_series_mode == "series" else duration_minutes,
        "characters": char_map,
        "done_chapters": [],
        "ready_parts": [],
        "scripts": {},
        "progress": 2,
        "status_text": (
            f"📱 {clean_total_parts}-Part Micro-Drama ({clean_ratio}) शुरू! Part 1 सबसे पहले तैयार हो रहा है..."
            if clean_series_mode == "series"
            else f"प्रोजेक्ट शुरू! {clean_lang} भाषा ({clean_ratio}) में फुल-स्क्रीन मूवी बन रही है..."
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
    # Collect best action/dialogue clips across chapters
    candidate_clips = []
    for ch_no in sorted(state.get("done_chapters", [])):
        ch_temp = f"{proj_dir}/ch_{ch_no}_clips"
        ch_scripts = state.get("scripts", {}).get(str(ch_no), [])
        for i, d in enumerate(ch_scripts):
            v_file = f"{ch_temp}/v_{i}.mp4"
            if os.path.exists(v_file) and os.path.getsize(v_file) > 1000:
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
    _make_title_card_clip("OFFICIAL TRAILER", f"{title} • CINEPOP AI", intro_16, 2.4, vertical=False)
    _make_title_card_clip(title, "RELEASING NOW • DIRECTED BY CINEPOP AI", outro_16, 2.6, vertical=False)

    # Trim each chosen clip to snappy 3.2s fast-cut segment for high-energy trailer
    fast_cuts_16 = [intro_16]
    for idx, src in enumerate(chosen):
        cut_out = f"{t_dir}/cut_{idx}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                src,
                "-t",
                "3.2",
                "-vf",
                "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
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
                cut_out,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        fast_cuts_16.append(cut_out)
    fast_cuts_16.append(outro_16)

    trailer_16 = f"{proj_dir}/TRAILER_16_9.mp4"
    ffmpeg_concat(fast_cuts_16, trailer_16)

    # Create 9:16 Vertical Instagram Reel / YouTube Short (720x1280) with blurred cinema backdrop
    reel_9_16 = f"{proj_dir}/REEL_9_16.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            trailer_16,
            "-filter_complex",
            "[0:v]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,boxblur=16:2[bg];[0:v]scale=720:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[vout]",
            "-map",
            "[vout]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "26",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-b:a",
            "96k",
            reel_9_16,
        ],
        check=True,
        timeout=90,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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
    """Try free HuggingFace Face-Swap spaces (tonyassi/face-swap, ezioruan/inswapper, roop)."""
    hf_spaces = [
        ("tonyassi/face-swap", "/predict"),
        ("ezioruan/inswapper", "/predict"),
        ("tonyassi/face-swap", "/swap"),
        ("felixrosberg/face-swap", "/predict"),
    ]
    for space, api_ep in hf_spaces:
        try:
            hf = Client(space)
            res = hf.predict(
                handle_file(source_face_path),
                handle_file(target_frame_path),
                api_name=api_ep,
            )
            if isinstance(res, str) and os.path.exists(res):
                return res
            if isinstance(res, (list, tuple)) and res:
                first = res[0]
                if isinstance(first, str) and os.path.exists(first):
                    return first
                if isinstance(first, dict) and first.get("path") and os.path.exists(first["path"]):
                    return first["path"]
        except Exception:
            continue
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
    """Safely load and extract portrait face crop from any image file (OpenCV + PIL fallback)."""
    src_img = cv2.imread(img_path)
    if src_img is None:
        pil_src = Image.open(img_path).convert("RGB")
        src_img = cv2.cvtColor(np.array(pil_src), cv2.COLOR_RGB2BGR)
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

    cap = cv2.VideoCapture(video_path)
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
async def start_face_swap(
    background_tasks: BackgroundTasks,
    user_email: str = Form(...),
    title: str = Form("Custom Face-Swap Clip"),
    dub_text: str = Form(""),
    dub_language: str = Form("Hindi"),
    dub_voice: str = Form("hi-IN-MadhurNeural"),
    dub_pitch: str = Form("normal"),
    watermark_text: str = Form(""),
    video: UploadFile = File(...),
    face_image: UploadFile = File(...),
    angle_images: Optional[list[UploadFile]] = File(default=None),
    angle_meta: str = Form("[]"),
):
    email = user_email.lower().strip()
    video_bytes = await video.read()
    face_bytes = await face_image.read()
    if len(video_bytes) > 65 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Video max size is 65MB")
    if len(face_bytes) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Face photo max size is 12MB")

    job_id = str(uuid.uuid4())[:10]
    job_dir = f"{FACE_SWAPS_DIR}/{job_id}"
    os.makedirs(job_dir, exist_ok=True)

    vid_path = safe_project_file(job_dir, f"input_{video.filename or 'clip.mp4'}")
    face_path = safe_project_file(job_dir, "face_front.jpg")
    with open(vid_path, "wb") as f:
        f.write(video_bytes)
    save_upright_image_bytes(face_bytes, face_path)

    face_angles: dict[str, str] = {"front": face_path}
    if angle_images:
        try:
            parsed_meta = json.loads(angle_meta) if angle_meta.strip() else []
            if not isinstance(parsed_meta, list):
                parsed_meta = []
        except Exception:
            parsed_meta = []
        for idx_a, a_up in enumerate(angle_images):
            a_bytes = await a_up.read()
            if not a_bytes:
                continue
            m = parsed_meta[idx_a] if idx_a < len(parsed_meta) and isinstance(parsed_meta[idx_a], dict) else {}
            ang = str(m.get("angle", "")).lower().strip()
            if ang not in ALLOWED_FACE_ANGLES:
                fname_low = (a_up.filename or "").lower()
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


@app.delete("/face-swap/{job_id}")
def face_swap_delete(job_id: str):
    db = load_db()
    if job_id in db.get("face_swaps", {}):
        del db["face_swaps"][job_id]
        save_db(db)
    shutil.rmtree(f"{FACE_SWAPS_DIR}/{job_id}", ignore_errors=True)
    return {"message": "Deleted"}


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
