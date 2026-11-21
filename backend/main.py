import os
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
from typing import Optional
import edge_tts
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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

PITCH_PRESETS = {
    "normal": {"pitch": "+0Hz", "rate": "+0%", "label": "Normal"},
    "hero_deep": {"pitch": "-12Hz", "rate": "-5%", "label": "Heroic Deep"},
    "heavy_villain": {"pitch": "-24Hz", "rate": "-12%", "label": "Heavy Villain"},
    "cute_kid": {"pitch": "+30Hz", "rate": "+10%", "label": "Cute Kid"},
    "fast_comic": {"pitch": "+12Hz", "rate": "+18%", "label": "Fast Comedy"},
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
        "gemini_configured": bool(GEMINI_API_KEY),
        "opencv_faceswap": OPENCV_AVAILABLE,
        "video_engine": "LTX-Video / CogVideoX-5B / SVD + FFmpeg Pro Motion VFX",
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
):
    clean_text = text.strip() or "नमस्ते दोस्तों!"
    resolved_voice = _resolve_tts_voice(voice, language)
    preset = PITCH_PRESETS.get(normalize_pitch(pitch), PITCH_PRESETS["normal"])
    try:
        comm = edge_tts.Communicate(
            clean_text,
            resolved_voice,
            rate=preset["rate"],
            pitch=preset["pitch"],
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
            "3",
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
    dialogue_path: str, mood: str, sfx_type: str, out_mixed_mp3: str
) -> str:
    dur = _probe_audio_duration(dialogue_path)
    bgm_wav = f"{out_mixed_mp3}_bgm.wav"
    sfx_wav = f"{out_mixed_mp3}_sfx.wav"
    try:
        generate_bgm_wav(mood, dur + 0.25, bgm_wav)
        has_sfx = generate_sfx_wav(sfx_type, dur + 0.25, sfx_wav)
        if has_sfx and os.path.exists(sfx_wav):
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                dialogue_path,
                "-i",
                bgm_wav,
                "-i",
                sfx_wav,
                "-filter_complex",
                "[0:a]volume=1.0[d];[1:a]volume=0.20[b];[2:a]volume=0.35[s];[d][b][s]amix=inputs=3:duration=first:dropout_transition=2:normalize=0[aout]",
                "-map",
                "[aout]",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                out_mixed_mp3,
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                dialogue_path,
                "-i",
                bgm_wav,
                "-filter_complex",
                "[0:a]volume=1.0[d];[1:a]volume=0.20[b];[d][b]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]",
                "-map",
                "[aout]",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                out_mixed_mp3,
            ]
        subprocess.run(
            cmd,
            check=True,
            timeout=20,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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


def render_location_backdrop(location_key: str, mood: str) -> Image.Image:
    W, H = 1280, 720
    bg = Image.new("RGB", (W, H), (12, 9, 24))

    palettes = {
        "village_field": ((30, 58, 95), (231, 111, 81)),
        "haunted_mansion": ((6, 8, 18), (28, 32, 62)),
        "mumbai_rain": ((10, 18, 32), (26, 48, 72)),
        "deep_jungle": ((6, 26, 18), (18, 64, 46)),
        "police_station": ((15, 23, 42), (30, 41, 59)),
        "outer_space": ((4, 4, 14), (24, 12, 58)),
        "royal_palace": ((38, 11, 28), (168, 85, 16)),
        "mountains": ((15, 23, 42), (100, 116, 139)),
        "desert_fort": ((67, 20, 7), (217, 119, 6)),
        "courtroom": ((28, 17, 10), (74, 44, 24)),
        "cyber_lab": ((4, 12, 26), (12, 48, 78)),
        "city_street": ((11, 14, 29), (36, 28, 82)),
    }
    top_c, bot_c = palettes.get(location_key, palettes["city_street"])
    _draw_vertical_gradient(bg, top_c, bot_c)
    draw = ImageDraw.Draw(bg, "RGBA")

    if location_key == "village_field":
        # Golden sunset sun + mustard fields + village huts
        draw.ellipse([120, 80, 320, 280], fill=(255, 224, 130, 180))
        draw.rectangle([0, 420, W, H], fill=(56, 94, 18, 255))
        for row in range(8):
            y = 430 + row * 28
            draw.rectangle([0, y, W, y + 12], fill=(234, 179, 8, 95))
        for hx in [130, 260, 1020]:
            draw.rectangle([hx, 370, hx + 72, 425], fill=(48, 26, 20, 255))
            draw.polygon([(hx - 14, 370), (hx + 36, 330), (hx + 86, 370)], fill=(35, 18, 14, 255))
            draw.rectangle([hx + 28, 392, hx + 44, 412], fill=(251, 191, 36, 220))
    elif location_key == "haunted_mansion":
        draw.ellipse([930, 60, 1090, 220], fill=(226, 232, 240, 200))
        draw.rectangle([90, 220, 350, 560], fill=(10, 12, 24, 245))
        draw.polygon([(140, 220), (220, 110), (300, 220)], fill=(10, 12, 24, 245))
        draw.rectangle([150, 280, 180, 330], fill=(251, 146, 60, 190))
        draw.rectangle([260, 280, 290, 330], fill=(251, 146, 60, 190))
        draw.rectangle([0, 460, W, H], fill=(148, 163, 184, 42))
    elif location_key in ("mumbai_rain", "city_street"):
        for bx, bw, bh in [(20, 115, 360), (150, 130, 430), (295, 95, 310), (885, 115, 390), (1015, 135, 440), (1165, 100, 340)]:
            draw.rectangle([bx, 520 - bh, bx + bw, 560], fill=(10, 16, 32, 245))
            for wy in range(540 - bh, 500, 26):
                for wx in range(bx + 14, bx + bw - 18, 22):
                    col = (6, 182, 212, 185) if (wx + wy) % 4 == 0 else (251, 191, 36, 155)
                    draw.rectangle([wx, wy, wx + 10, wy + 12], fill=col)
        if location_key == "mumbai_rain":
            for i in range(130):
                rx = (i * 97) % W
                ry = (i * 53) % H
                draw.line([(rx, ry), (rx - 14, ry + 42)], fill=(186, 230, 253, 120), width=2)
    elif location_key == "deep_jungle":
        for tx in range(30, W, 145):
            draw.rectangle([tx, 0, tx + 42, H], fill=(6, 32, 22, 225))
            draw.ellipse([tx - 65, -40, tx + 115, 170], fill=(10, 48, 34, 215))
        for f in range(36):
            fx = (f * 103) % W
            fy = 120 + (f * 61) % 420
            draw.ellipse([fx, fy, fx + 7, fy + 7], fill=(250, 204, 21, 210))
    elif location_key == "outer_space":
        draw.ellipse([120, 90, 560, 490], fill=(192, 38, 211, 72))
        for s in range(160):
            sx = (s * 173) % W
            sy = (s * 83) % H
            sz = 3 if s % 5 == 0 else 2
            draw.rectangle([sx, sy, sx + sz, sy + sz], fill=(255, 255, 255, 210))
        draw.ellipse([960, 110, 1140, 290], fill=(37, 99, 235, 235))
        draw.arc([890, 165, 1210, 240], 0, 360, fill=(245, 184, 0, 190), width=7)
    elif location_key == "royal_palace":
        for px in range(40, W, 240):
            draw.rectangle([px, 0, px + 44, H], fill=(64, 18, 32, 230))
            draw.rectangle([px + 8, 0, px + 14, H], fill=(245, 184, 0, 150))
            draw.arc([px + 44, 60, px + 240, 260], 180, 360, fill=(251, 191, 36, 175), width=5)
    elif location_key == "police_station":
        draw.ellipse([-60, -40, 420, 420], fill=(239, 68, 68, 75))
        draw.ellipse([W - 420, -40, W + 60, 420], fill=(59, 130, 246, 75))
        for bx in range(70, 340, 36):
            draw.rectangle([bx, 70, bx + 8, 560], fill=(148, 163, 184, 90))
    elif location_key == "mountains":
        draw.polygon([(-40, H), (180, 210), (420, 390), (680, 170), (960, 370), (1180, 220), (W + 40, H)], fill=(51, 65, 85, 235))
        draw.polygon([(-40, H), (260, 340), (540, 460), (880, 310), (W + 40, H)], fill=(30, 41, 59, 245))
    elif location_key == "desert_fort":
        draw.rectangle([0, 420, W, H], fill=(154, 52, 18, 255))
        draw.rectangle([80, 260, 360, 430], fill=(67, 20, 7, 245))
        for bx in range(80, 350, 42):
            draw.rectangle([bx, 240, bx + 22, 265], fill=(67, 20, 7, 245))
    elif location_key == "cyber_lab":
        for gx in range(0, W, 64):
            draw.line([(gx, 0), (gx, H)], fill=(6, 182, 212, 65), width=1)
        for gy in range(0, H, 64):
            draw.line([(0, gy), (W, gy)], fill=(6, 182, 212, 65), width=1)
        draw.ellipse([110, 190, 290, 370], outline=(34, 211, 238, 165), width=3)
        draw.ellipse([990, 190, 1170, 370], outline=(34, 211, 238, 165), width=3)
    elif location_key == "courtroom":
        for px in range(40, W, 180):
            draw.rectangle([px, 40, px + 34, 580], fill=(92, 42, 14, 190))

    # Subtle mood atmosphere wash
    rim = MOOD_RIM_COLORS.get(mood, (255, 59, 92))
    wash = Image.new("RGBA", (W, H), (rim[0], rim[1], rim[2], 26))
    bg = Image.alpha_composite(bg.convert("RGBA"), wash).convert("RGB")
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
    try:
        font_wm = ImageFont.truetype(
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", 20
        )
    except Exception:
        font_wm = ImageFont.load_default()
    box_w = min(340, max(150, len(text) * 13 + 38))
    box_h = 38
    if watermark_position == "top-left":
        x1, y1 = 28, 76
    elif watermark_position == "bottom-right":
        x1, y1 = W - box_w - 28, H - 156
    else:
        x1, y1 = W - box_w - 28, 76
    odraw.rounded_rectangle(
        [x1, y1, x1 + box_w, y1 + box_h],
        radius=12,
        fill=(12, 8, 28, 175),
        outline=(255, 214, 0, 210),
        width=2,
    )
    odraw.text((x1 + 16, y1 + 8), f"★ {text}", fill=(255, 214, 0, 240), font=font_wm)
    composited = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    img.paste(composited)


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
):
    W, H = 1280, 720
    if custom_bg_path and os.path.exists(custom_bg_path):
        bg = Image.open(custom_bg_path).convert("RGB").resize((W, H), Image.Resampling.LANCZOS)
        dark = Image.new("RGB", (W, H), (10, 8, 22))
        bg = Image.blend(bg, dark, 0.25)
    else:
        bg = render_location_backdrop(location_key, mood)

    base = Image.open(raw_img_path).convert("RGB")
    aspect = base.width / max(base.height, 1)
    new_h = 500
    new_w = min(500, max(320, int(new_h * aspect)))
    portrait = base.resize((new_w, new_h), Image.Resampling.LANCZOS)
    x_offset = (W - new_w) // 2
    y_offset = 88

    # Mood-colored rim border + drop shadow around foreground actor
    rim_rgb = MOOD_RIM_COLORS.get(mood, (255, 59, 92))
    draw = ImageDraw.Draw(bg)
    draw.rounded_rectangle(
        [x_offset - 5, y_offset - 5, x_offset + new_w + 5, y_offset + new_h + 5],
        radius=18,
        outline=rim_rgb,
        width=4,
    )

    # Rounded mask for clean foreground character portrait
    mask = Image.new("L", (new_w, new_h), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, new_w, new_h], radius=16, fill=255)
    bg.paste(portrait, (x_offset, y_offset), mask)

    try:
        font_badge = ImageFont.truetype(
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", 24
        )
        font_env = ImageFont.truetype(
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", 19
        )
        font_sub = ImageFont.truetype(
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", 27
        )
    except Exception:
        font_badge = ImageFont.load_default()
        font_env = ImageFont.load_default()
        font_sub = ImageFont.load_default()

    # Top-left Speaker badge
    draw.rounded_rectangle([32, 22, 310, 68], radius=12, fill=(225, 29, 116))
    draw.text((52, 30), char_name[:18], fill=(255, 255, 255), font=font_badge)

    # Top-right Location + Mood badge
    env_label = f"{location_label[:26]} | {mood}"
    draw.rounded_rectangle([W - 440, 22, W - 32, 66], radius=18, fill=(14, 10, 28))
    draw.text((W - 420, 32), env_label, fill=(226, 232, 240), font=font_env)

    # Bottom cinema letterbox subtitle bar
    draw.rectangle([0, 608, W, H], fill=(9, 7, 18))
    short_diag = dialogue if len(dialogue) <= 84 else dialogue[:81] + "..."
    draw.text((48, 638), short_diag, fill=(255, 214, 0), font=font_sub)

    if watermark_text:
        draw_channel_watermark(bg, watermark_text, watermark_position)

    bg.save(out_frame_path, "JPEG", quality=92)


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
    Attempt real Image-to-Video motion clip generation using free HuggingFace Spaces:
    LTX-Video -> CogVideoX-5B -> Stable Video Diffusion (SVD).
    Returns local path to generated MP4 if available, else None (fast fallback to FFmpeg Pro Motion VFX).
    """
    prompt_txt = (
        visual_action_prompt.strip()
        if visual_action_prompt and visual_action_prompt.strip()
        else f"Cinematic Bollywood {mood} scene, dynamic tracking camera movement, realistic lighting"
    )
    i2v_spaces = [
        ("Lightricks/ltx-video-distilled", "ltx"),
        ("THUDM/CogVideoX-5B-Space", "cogvideo"),
        ("multimodalart/stable-video-diffusion", "svd"),
    ]
    for space_id, kind in i2v_spaces:
        try:
            hf_client = Client(space_id)
            if kind == "ltx":
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
                    image=handle_file(raw_img_path),
                    seed=random.randint(1, 99999),
                    randomize_seed=True,
                    motion_bucket_id=145 if mood == "Action" else 110,
                    fps_id=8,
                    api_name="/video",
                )
            vid_path = _extract_gradio_video_path(res)
            if vid_path and os.path.getsize(vid_path) > 2000:
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
) -> bool:
    """
    High-Octane FFmpeg + OpenCV Pro Motion & VFX Engine (ZERO static still frames):
    - Smooth zoompan camera tracking & directional dolly
    - Camera shake / impact jitter on Action / punch / explosion / thunder / sword
    - Radial action speed lines, dynamic lighting flashes, and glowing particle overlays
    """
    dur = _probe_audio_duration(final_audio)
    fps = 24
    total_frames = max(36, int(dur * fps))
    high_impact = (
        mood == "Action"
        or sfx_type in ("punch", "explosion", "thunder", "sword")
    )

    # Path A: OpenCV real-time animated VFX compositor (camera tracking + shake + speed lines + particles + dynamic lighting)
    if OPENCV_AVAILABLE and cv2 is not None and np is not None:
        raw_vfx_mp4 = f"{out_mp4}_vfx_raw.mp4"
        try:
            base = cv2.imread(framed_img_path)
            if base is not None:
                h_out, w_out = 720, 1280
                # Oversample source for smooth camera tracking & shake without black borders
                src = cv2.resize(base, (1536, 864), interpolation=cv2.INTER_LINEAR)
                sh, sw = src.shape[:2]

                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(raw_vfx_mp4, fourcc, fps, (w_out, h_out))

                rng = random.Random(hash(framed_img_path) & 0xFFFFFFFF)
                # Pre-seed particles (embers/sparks for Action, rain streaks for Rain, glowing bokeh for Romantic/Comedy)
                particles = [
                    {
                        "x": rng.uniform(0, w_out),
                        "y": rng.uniform(0, h_out),
                        "vx": rng.uniform(-7.5, -2.5) if sfx_type in ("rain", "thunder") else rng.uniform(-3.0, 3.0),
                        "vy": rng.uniform(16.0, 26.0) if sfx_type in ("rain", "thunder") else rng.uniform(-4.2, -1.2),
                        "r": rng.randint(2, 5),
                        "phase": rng.uniform(0, 6.28),
                    }
                    for _ in range(55 if high_impact or sfx_type in ("rain", "thunder") else 28)
                ]

                for f_idx in range(total_frames):
                    t = f_idx / max(1, total_frames - 1)
                    # 1. Mood-aware smooth camera tracking zoom + pan
                    if mood == "Action":
                        zoom = 1.04 + 0.14 * t + 0.015 * math.sin(f_idx * 0.45)
                        pan_x = math.sin(t * math.pi * 1.4) * 44.0
                        pan_y = math.cos(t * math.pi * 1.1) * 18.0
                    elif mood == "Suspense":
                        zoom = 1.02 + 0.11 * t
                        pan_x = math.sin(t * math.pi) * 28.0
                        pan_y = -14.0 * t
                    elif mood == "Comedy":
                        zoom = 1.04 + 0.05 * abs(math.sin(f_idx * 0.16))
                        pan_x = math.sin(f_idx * 0.14) * 32.0
                        pan_y = math.cos(f_idx * 0.12) * 10.0
                    elif mood == "Romantic":
                        zoom = 1.03 + 0.09 * t
                        pan_x = -34.0 + 68.0 * t
                        pan_y = math.sin(t * math.pi) * 14.0
                    else:
                        zoom = 1.03 + 0.10 * t
                        pan_x = math.sin(t * math.pi * 0.8) * 22.0
                        pan_y = -10.0 * t

                    # 2. Camera shake / impact burst on high-impact moments
                    shake_x = 0.0
                    shake_y = 0.0
                    if high_impact:
                        burst = 1.0 if (f_idx % 24) < 7 else 0.35
                        amp = (13.0 if sfx_type in ("explosion", "punch") else 7.5) * burst
                        shake_x = math.sin(f_idx * 1.9) * amp
                        shake_y = math.cos(f_idx * 2.3) * (amp * 0.72)

                    crop_w = int(sw / zoom)
                    crop_h = int(sh / zoom)
                    cx = int(sw / 2 + pan_x + shake_x)
                    cy = int(sh / 2 + pan_y + shake_y)
                    x1 = max(0, min(sw - crop_w, cx - crop_w // 2))
                    y1 = max(0, min(sh - crop_h, cy - crop_h // 2))
                    frame = cv2.resize(
                        src[y1 : y1 + crop_h, x1 : x1 + crop_w],
                        (w_out, h_out),
                        interpolation=cv2.INTER_LINEAR,
                    )

                    # 3. Dynamic lighting & storm / impact flash modulation
                    light_boost = 1.0
                    if sfx_type == "thunder" and (f_idx % 38) in (2, 3, 5):
                        light_boost = 1.32
                    elif sfx_type in ("explosion", "punch") and (f_idx % 28) in (1, 2):
                        light_boost = 1.24
                    elif mood == "Action":
                        light_boost = 1.04 + 0.04 * math.sin(f_idx * 0.3)

                    if abs(light_boost - 1.0) > 0.01:
                        frame = cv2.convertScaleAbs(frame, alpha=light_boost, beta=4 if light_boost > 1.15 else 0)

                    # 4. Speed lines overlay (Action / combat energy)
                    if high_impact:
                        overlay = frame.copy()
                        cx_o, cy_o = w_out // 2, int(h_out * 0.44)
                        num_lines = 26
                        for k in range(num_lines):
                            ang = (k / num_lines) * 2.0 * math.pi + (f_idx * 0.05)
                            r_in = 340 + int(45 * math.sin(f_idx * 0.4 + k))
                            r_out = 760
                            p1 = (int(cx_o + math.cos(ang) * r_in), int(cy_o + math.sin(ang) * r_in))
                            p2 = (int(cx_o + math.cos(ang) * r_out), int(cy_o + math.sin(ang) * r_out))
                            line_col = (120, 210, 255) if k % 2 == 0 else (90, 80, 255)
                            cv2.line(overlay, p1, p2, line_col, 2, cv2.LINE_AA)
                        cv2.addWeighted(overlay, 0.24, frame, 0.76, 0, frame)

                    # 5. Cinematic particles overlay (sparks, rain, bokeh, suspense dust)
                    p_overlay = frame.copy()
                    for p in particles:
                        p["x"] = (p["x"] + p["vx"]) % w_out
                        p["y"] = (p["y"] + p["vy"]) % h_out
                        px, py = int(p["x"]), int(p["y"])
                        if sfx_type in ("rain", "thunder"):
                            cv2.line(
                                p_overlay,
                                (px, py),
                                (px - 8, py + 24),
                                (245, 225, 195),
                                1,
                                cv2.LINE_AA,
                            )
                        elif high_impact:
                            # Glowing fiery orange/gold sparks
                            col = (60, 175, 255) if p["r"] > 3 else (90, 225, 255)
                            cv2.circle(p_overlay, (px, py), p["r"], col, -1, cv2.LINE_AA)
                        elif mood in ("Romantic", "Comedy"):
                            # Warm golden/pink bokeh
                            col = (180, 130, 255) if p["r"] % 2 == 0 else (110, 220, 255)
                            cv2.circle(p_overlay, (px, py), p["r"] + 2, col, -1, cv2.LINE_AA)
                        else:
                            # Atmospheric cyan/silver dust motes
                            cv2.circle(p_overlay, (px, py), max(2, p["r"] - 1), (220, 210, 170), -1, cv2.LINE_AA)
                    cv2.addWeighted(p_overlay, 0.38, frame, 0.62, 0, frame)

                    writer.write(frame)

                writer.release()

                if os.path.exists(raw_vfx_mp4) and os.path.getsize(raw_vfx_mp4) > 2000:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            raw_vfx_mp4,
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
                            "96k",
                            out_mp4,
                        ],
                        check=True,
                        timeout=60,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
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

    # Path B: Pure FFmpeg Pro Motion filtergraph (zoompan camera tracking + camera shake + dynamic lighting EQ)
    shake_expr_x = (
        "iw/2-(iw/zoom/2)+sin(on*1.7)*14"
        if high_impact
        else "iw/2-(iw/zoom/2)+sin(on*0.09)*22"
    )
    shake_expr_y = (
        "ih/2-(ih/zoom/2)+cos(on*2.1)*10"
        if high_impact
        else "ih/2-(ih/zoom/2)+cos(on*0.07)*10"
    )
    zoom_rate = "0.0018" if high_impact else "0.0010"
    zoom_cap = "1.16" if high_impact else "1.10"
    vf = (
        f"scale=1536:864,"
        f"zoompan=z='min(zoom+{zoom_rate},{zoom_cap})':x='{shake_expr_x}':y='{shake_expr_y}':d={total_frames}:s=1280x720:fps={fps},"
        f"eq=contrast=1.08:saturation=1.15:brightness=0.02,"
        f"vignette=PI/5"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            framed_img_path,
            "-i",
            final_audio,
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "25",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-b:a",
            "96k",
            out_mp4,
        ],
        check=True,
        timeout=60,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000


def render_exact_face_clip(
    raw_img_path: str,
    framed_img_path: str,
    audio_path: str,
    out_mp4: str,
    mood: str = "Action",
    sfx_type: str = "none",
    visual_action_prompt: str = "",
):
    mixed_tmp = f"{out_mp4}_mix.mp3"
    final_audio = mix_cinematic_audio(audio_path, mood, sfx_type, mixed_tmp)

    try:
        # 1. Try Real AI Action Image-to-Video (LTX-Video / CogVideoX-5B / SVD)
        ai_video_path = _try_ai_action_i2v_spaces(
            raw_img_path, visual_action_prompt, mood
        )
        if ai_video_path:
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-stream_loop",
                        "-1",
                        "-i",
                        ai_video_path,
                        "-i",
                        final_audio,
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-vf",
                        "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,eq=contrast=1.06:saturation=1.12,vignette=PI/5",
                        "-r",
                        "24",
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
                        "96k",
                        out_mp4,
                    ],
                    check=True,
                    timeout=45,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000:
                    return
            except Exception:
                pass

        # 2. Try Talking-Face AI Lip-Sync Spaces (Wav2Lip / SadTalker) for close-up dialogues
        hf_mirrors = ["camenduru/Wav2Lip", "vinthony/SadTalker"]
        for mirror in hf_mirrors:
            try:
                hf_client = Client(mirror)
                if "Wav2Lip" in mirror:
                    res = hf_client.predict(
                        face=handle_file(raw_img_path),
                        audio=handle_file(audio_path),
                        api_name="/predict",
                    )
                else:
                    res = hf_client.predict(
                        source_image=handle_file(raw_img_path),
                        driven_audio=handle_file(audio_path),
                        preprocess="crop",
                        still_mode=False,
                        use_enhancer=False,
                        batch_size=1,
                        size_of_image=256,
                        pose_style=0,
                        api_name="/test",
                    )
                talk_vid = _extract_gradio_video_path(res)
                if not talk_vid:
                    continue
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        talk_vid,
                        "-i",
                        final_audio,
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-vf",
                        "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                        "-r",
                        "24",
                        "-shortest",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "25",
                        "-c:a",
                        "aac",
                        "-ar",
                        "44100",
                        "-b:a",
                        "96k",
                        out_mp4,
                    ],
                    check=True,
                    timeout=35,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000:
                    return
            except Exception:
                continue

        # 3. High-Octane FFmpeg + OpenCV Pro Motion & VFX Engine (Camera tracking + shake + speed lines + particles + dynamic lighting — ZERO static stills!)
        _render_pro_motion_vfx_clip(
            framed_img_path, final_audio, out_mp4, mood=mood, sfx_type=sfx_type
        )
    finally:
        if os.path.exists(mixed_tmp):
            try:
                os.remove(mixed_tmp)
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
    total_parts = (total_ch + 2) // 3
    ready_parts = []
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
            "पूरी मूवी तैयार है! पहले Play करके चेक करें, फिर Download करें।"
        )


def parse_manual_script_lines(
    raw_text: str, char_names: list[str], default_location: str, default_mood: str
) -> list[dict]:
    parsed = []
    cur_loc_key, cur_loc_label = detect_location_meta(default_location, default_location)
    cur_mood = normalize_mood(default_mood, "Action")

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
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

        m = re.match(r"^([^:：]{1,28})[:：]\s*(.+)$", line)
        if m:
            speaker_raw = m.group(1).strip()
            diag = m.group(2).strip()
            if speaker_raw.lower() in ("शीर्षक", "title", "scene", "location", "mood"):
                continue
            matched = next(
                (c for c in char_names if c.lower() == speaker_raw.lower()),
                char_names[len(parsed) % len(char_names)],
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
        duration = state["duration_minutes"]
        total_chapters = max(1, duration // 10)
        story_mode = state.get("story_mode", "ai")
        language = normalize_language(state.get("language", "Hindi"))
        default_mood = normalize_mood(state.get("default_mood", "Action"))
        default_location = state.get("default_location", "")
        project_custom_bg = state.get("custom_bg_path")
        watermark_text = state.get("watermark_text", "")
        watermark_position = state.get("watermark_position", "top-right")

        # Manual verbatim script mode: preserve user's exact lines without changing story
        manual_lines = (
            parse_manual_script_lines(
                state["idea"], char_names, default_location, default_mood
            )
            if story_mode == "manual"
            else []
        )

        if not state.get("outline"):
            if story_mode == "manual" and len(manual_lines) >= 2:
                per_ch = max(1, math.ceil(len(manual_lines) / total_chapters))
                chunks = [
                    manual_lines[i : i + per_ch]
                    for i in range(0, len(manual_lines), per_ch)
                ]
                state["outline"] = {
                    "title": "Custom Script Movie",
                    "chapters": [
                        {
                            "no": i + 1,
                            "plot": f"सीन {i + 1}: {chunks[i][0]['location']}",
                        }
                        for i in range(len(chunks))
                    ],
                }
                state["manual_chunks"] = chunks
                state["scripts"] = {}
                save_state(proj_dir, state)
            else:
                state["status_text"] = (
                    "CinePop AI पूरी फिल्म की कहानी और रियल लोकेशंस तैयार कर रहा है..."
                )
                save_state(proj_dir, state)

                fallback_outline = {
                    "title": "CinePop Blockbuster",
                    "chapters": [
                        {"no": i + 1, "plot": f"{state['idea']} - भाग {i + 1}"}
                        for i in range(total_chapters)
                    ],
                }
                if client:
                    try:
                        strict_note = (
                            "महत्वपूर्ण: यूज़र की लिखी कहानी को बिल्कुल मत बदलो, उसी कहानी के आधार पर चैप्टर्स बनाओ।"
                            if story_mode == "manual"
                            else f"Genre: {state.get('genre', 'Action')}."
                        )
                        prompt = f"""
                        तुम एक बॉलीवुड मूवी डायरेक्टर हो। '{state['idea']}' पर {duration} मिनट की फिल्म की रूपरेखा बनाओ।
                        {strict_note}
                        कैरेक्टर्स: {char_names}
                        कहानी को ठीक {total_chapters} बराबर चैप्टर्स में बांटो।
                        सिर्फ शुद्ध JSON दो:
                        {{"title": "फिल्म का नाम", "chapters": [{{"no": 1, "plot": "विस्तार से कहानी और लोकेशन"}}]}}
                        """
                        res = client.models.generate_content(
                            model="gemini-2.5-flash", contents=prompt
                        )
                        state["outline"] = safe_extract_json(res.text, fallback_outline)
                    except Exception:
                        state["outline"] = fallback_outline
                else:
                    state["outline"] = fallback_outline

                state["scripts"] = {}
                save_state(proj_dir, state)

        chapters = state["outline"]["chapters"]
        manual_chunks = state.get("manual_chunks")
        prev_plot = "कहानी की शुरुआत"

        for ch_idx, ch in enumerate(chapters):
            ch_no = ch["no"]
            ch_mp4 = f"{proj_dir}/ch_{ch_no}.mp4"
            if ch_no in state["done_chapters"] and os.path.exists(ch_mp4):
                prev_plot = ch["plot"]
                continue

            percent = int(((ch_no - 1) / len(chapters)) * 90) + 5
            state["progress"] = percent
            state["status_text"] = (
                f"चैप्टर {ch_no}/{len(chapters)} सिनेमैटिक लोकेशन के साथ शूट हो रहा है ({percent}%)..."
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
                        "dialogue": f"{ch['plot']} - सीन {i+1} का दमदार संवाद।",
                        "location": loc_l,
                        "location_key": loc_k,
                        "mood": default_mood,
                        "sfx": detect_sfx_type(ch["plot"], loc_k, default_mood),
                        "visual_action_prompt": build_visual_action_prompt(
                            char_names[i % len(char_names)],
                            ch["plot"],
                            loc_l,
                            default_mood,
                            detect_sfx_type(ch["plot"], loc_k, default_mood),
                        ),
                    }
                    for i in range(15)
                ]

                if client:
                    try:
                        script_prompt = f"""
                        फिल्म: {state['outline']['title']} | पिछला सीन: {prev_plot}
                        वर्तमान चैप्टर {ch_no}: {ch['plot']}
                        कैरेक्टर्स: {char_names}
                        भाषा / Dialect: {language} (सभी डायलॉग्स असली {language} अंदाज़ और बोलचाल में लिखो!)
                        इस चैप्टर के लिए 50 से 60 दमदार {language} डायलॉग्स लिखो ताकि 10 मिनट का सीन बने।
                        हर डायलॉग के साथ उसकी रियल लोकेशन (location: जैसे Gaon ka Khet, Purani Haveli, Mumbai ki Barish, Deep Jungle, Police Station, Outer Space, Royal Palace), mood (Action, Emotional, Suspense, Comedy, Romantic), sfx (none, rain, thunder, punch, explosion, sword, footsteps), और हाई-ऑक्टेन English visual_action_prompt (जैसे: "character running through stormy street, camera tracking, sparks flying") भी दो।
                        सिर्फ JSON लिस्ट दो: [{{"character": "{char_names[0]}", "dialogue": "डायलॉग...", "location": "Mumbai ki Barish", "mood": "{default_mood}", "sfx": "rain", "visual_action_prompt": "hero running through stormy Mumbai street, fast tracking camera, sparks and rain"}}]
                        """
                        s_res = client.models.generate_content(
                            model="gemini-2.5-flash", contents=script_prompt
                        )
                        raw_list = safe_extract_json(s_res.text, fallback_dialogues)
                        dialogues = []
                        for item in raw_list:
                            c_name = (
                                item.get("character")
                                if item.get("character") in char_map
                                else char_names[0]
                            )
                            diag_text = str(item.get("dialogue", "कहानी आगे बढ़ती है..."))
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
                            dialogues.append(
                                {
                                    "character": c_name,
                                    "dialogue": diag_text,
                                    "location": ll,
                                    "location_key": lk,
                                    "mood": mood_val,
                                    "sfx": sfx_val,
                                    "visual_action_prompt": vap,
                                }
                            )
                    except Exception:
                        dialogues = fallback_dialogues
                else:
                    dialogues = fallback_dialogues

            state["scripts"][str(ch_no)] = dialogues
            save_state(proj_dir, state)

            clips = []
            for i, d in enumerate(dialogues):
                c_name = (
                    d.get("character")
                    if d.get("character") in char_map
                    else char_names[0]
                )
                diag_text = d.get("dialogue", "कहानी आगे बढ़ती है...")
                loc_key = d.get("location_key") or "city_street"
                loc_label = d.get("location") or default_location or "Mumbai City Street"
                scene_mood = normalize_mood(d.get("mood", ""), default_mood)
                scene_sfx = d.get("sfx") if d.get("sfx") in ALLOWED_SFX else detect_sfx_type(diag_text, loc_key, scene_mood)
                action_prompt = d.get("visual_action_prompt") or build_visual_action_prompt(
                    c_name, diag_text, loc_label, scene_mood, scene_sfx
                )
                bg_override = d.get("bg_path") or project_custom_bg
                char_pitch = char_map[c_name].get("pitch", "normal")

                a_file = f"{ch_temp}/a_{i}.mp3"
                f_img = f"{ch_temp}/f_{i}.jpg"
                v_file = f"{ch_temp}/v_{i}.mp4"

                if not os.path.exists(v_file):
                    await generate_bulletproof_audio(
                        diag_text,
                        char_map[c_name]["voice"],
                        a_file,
                        pitch=char_pitch,
                        language=language,
                    )
                    await asyncio.to_thread(
                        prepare_character_frame,
                        char_map[c_name]["image"],
                        c_name,
                        diag_text,
                        f_img,
                        loc_key,
                        loc_label,
                        scene_mood,
                        bg_override,
                        watermark_text,
                        watermark_position,
                    )
                    await asyncio.to_thread(
                        render_exact_face_clip,
                        char_map[c_name]["image"],
                        f_img,
                        a_file,
                        v_file,
                        scene_mood,
                        scene_sfx,
                        action_prompt,
                    )
                    if os.path.exists(a_file):
                        os.remove(a_file)
                    if os.path.exists(f_img):
                        os.remove(f_img)
                clips.append(v_file)

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
    story_mode: str = Form("ai"),
    genre: str = Form("Action"),
    language: str = Form("Hindi"),
    char_pitches: str = Form("{}"),
    default_mood: str = Form("Action"),
    default_location: str = Form(""),
    watermark_text: str = Form(""),
    watermark_position: str = Form("top-right"),
    admin_pin: str = Form(""),
    custom_bg: Optional[UploadFile] = File(None),
):
    email = user_email.lower().strip()
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

    names = [x.strip() for x in char_names.split(",")]
    v_list = [x.strip() for x in voices.split(",")]
    try:
        pitches_map = json.loads(char_pitches) if char_pitches.strip() else {}
        if not isinstance(pitches_map, dict):
            pitches_map = {}
    except Exception:
        pitches_map = {}

    char_map = {}
    for idx, img in enumerate(images):
        c_name = names[idx] if idx < len(names) else f"Actor_{idx+1}"
        c_voice = v_list[idx] if idx < len(v_list) else "hi-IN-MadhurNeural"
        c_pitch = normalize_pitch(str(pitches_map.get(c_name, pitches_map.get(str(idx), "normal"))))
        img_path = safe_project_file(proj_dir, f"char_{idx}_{c_name}.jpg")
        with open(img_path, "wb") as f:
            f.write(await img.read())
        char_map[c_name] = {"image": img_path, "voice": c_voice, "pitch": c_pitch}

    custom_bg_path = None
    if custom_bg is not None:
        raw_bytes = await custom_bg.read()
        if raw_bytes:
            custom_bg_path = safe_project_file(proj_dir, "custom_bg.jpg")
            with open(custom_bg_path, "wb") as f:
                f.write(raw_bytes)

    clean_lang = normalize_language(language)
    wm_pos = watermark_position if watermark_position in ("top-right", "bottom-right", "top-left") else "top-right"

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
        "custom_bg_path": custom_bg_path,
        "duration_minutes": duration_minutes,
        "characters": char_map,
        "done_chapters": [],
        "ready_parts": [],
        "scripts": {},
        "progress": 2,
        "status_text": f"प्रोजेक्ट शुरू! {clean_lang} भाषा, Auto-BGM + Foley SFX और रियल लोकेशंस के साथ मूवी बन रही है...",
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
                p_img = Image.open(img_p).convert("RGB")
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
    try:
        font_super = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", 72)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", 34)
        font_meta = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", 26)
    except Exception:
        font_super = ImageFont.load_default()
        font_sub = ImageFont.load_default()
        font_meta = ImageFont.load_default()

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
    try:
        f_big = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", 54 if not vertical else 44)
        f_sm = ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", 28 if not vertical else 24)
    except Exception:
        f_big = ImageFont.load_default()
        f_sm = ImageFont.load_default()

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


# ==================== CUSTOM VIDEO AI FACE-SWAP ENGINE ====================

def _try_hf_faceswap_image(source_face_path: str, target_frame_path: str) -> Optional[str]:
    hf_spaces = ["tonyassi/face-swap", "ezioruan/inswapper"]
    for space in hf_spaces:
        try:
            hf = Client(space)
            res = hf.predict(
                handle_file(source_face_path),
                handle_file(target_frame_path),
                api_name="/predict",
            )
            if isinstance(res, str) and os.path.exists(res):
                return res
            if isinstance(res, (list, tuple)) and res and isinstance(res[0], str) and os.path.exists(res[0]):
                return res[0]
        except Exception:
            continue
    return None


def _color_match_lab(src_bgr, dst_bgr):
    src_lab = cv2.cvtColor(src_bgr, cv2.COLOR_BGR2LAB).astype("float32")
    dst_lab = cv2.cvtColor(dst_bgr, cv2.COLOR_BGR2LAB).astype("float32")
    for ch in range(3):
        s_mean, s_std = src_lab[:, :, ch].mean(), max(src_lab[:, :, ch].std(), 1.0)
        d_mean, d_std = dst_lab[:, :, ch].mean(), max(dst_lab[:, :, ch].std(), 1.0)
        src_lab[:, :, ch] = ((src_lab[:, :, ch] - s_mean) * (d_std / s_std)) + d_mean
    src_lab = np.clip(src_lab, 0, 255).astype("uint8")
    return cv2.cvtColor(src_lab, cv2.COLOR_LAB2BGR)


def _opencv_video_faceswap(
    video_path: str,
    face_img_path: str,
    out_video_noaudio: str,
    watermark_text: str = "",
):
    if not OPENCV_AVAILABLE:
        # Fallback FFmpeg PiP face overlay if OpenCV is unavailable
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
                "[0:v]scale=720:-2[base];[1:v]scale=150:150[fc];[base][fc]overlay=W-w-24:24[v]",
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
        return "ffmpeg-overlay"

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    face_cascade = cv2.CascadeClassifier(cascade_path)

    src_img = cv2.imread(face_img_path)
    if src_img is None:
        raise ValueError("Uploaded face image could not be decoded")

    src_gray = cv2.cvtColor(src_img, cv2.COLOR_BGR2GRAY)
    src_faces = face_cascade.detectMultiScale(src_gray, 1.1, 4, minSize=(40, 40))
    if len(src_faces) > 0:
        sx, sy, sw, sh = max(src_faces, key=lambda b: b[2] * b[3])
        pad_x, pad_y = int(sw * 0.12), int(sh * 0.15)
        x1 = max(0, sx - pad_x)
        y1 = max(0, sy - pad_y)
        x2 = min(src_img.shape[1], sx + sw + pad_x)
        y2 = min(src_img.shape[0], sy + sh + pad_y)
        src_face_crop = src_img[y1:y2, x1:x2]
    else:
        h, w = src_img.shape[:2]
        side = min(h, w)
        src_face_crop = src_img[(h - side) // 2 : (h + side) // 2, (w - side) // 2 : (w + side) // 2]

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
    hf_tried = False

    while frame_idx < max_frames:
        ok, frame = cap.read()
        if not ok:
            break
        if scale < 0.99:
            frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)

        # Detect every 2nd frame for smooth speed + stable tracking
        if frame_idx % 2 == 0 or last_box is None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, 1.12, 4, minSize=(36, 36))
            if len(faces) > 0:
                bx, by, bw, bh = max(faces, key=lambda b: b[2] * b[3])
                if last_box is not None:
                    # Smooth exponential tracking to eliminate jitter
                    lx, ly, lw, lh = last_box
                    last_box = (
                        int(0.6 * lx + 0.4 * bx),
                        int(0.6 * ly + 0.4 * by),
                        int(0.6 * lw + 0.4 * bw),
                        int(0.6 * lh + 0.4 * bh),
                    )
                else:
                    last_box = (int(bx), int(by), int(bw), int(bh))

        if last_box is not None:
            tx, ty, tw, th = last_box
            tx = max(0, min(out_w - 20, tx))
            ty = max(0, min(out_h - 20, ty))
            tw = max(20, min(out_w - tx, tw))
            th = max(20, min(out_h - ty, th))

            # Optional 1-shot HuggingFace Space refine on first detected frame
            if not hf_tried and frame_idx == 0:
                hf_tried = True
                try:
                    tmp_frame = f"{out_video_noaudio}_f0.jpg"
                    cv2.imwrite(tmp_frame, frame)
                    hf_res = _try_hf_faceswap_image(face_img_path, tmp_frame)
                    if hf_res and os.path.exists(hf_res):
                        hf_img = cv2.imread(hf_res)
                        if hf_img is not None:
                            hf_crop = cv2.resize(hf_img, (out_w, out_h))[ty : ty + th, tx : tx + tw]
                            if hf_crop.size > 0:
                                src_face_crop = hf_crop
                    if os.path.exists(tmp_frame):
                        os.remove(tmp_frame)
                except Exception:
                    pass

            target_roi = frame[ty : ty + th, tx : tx + tw]
            if target_roi.size > 0:
                resized_src = cv2.resize(src_face_crop, (tw, th), interpolation=cv2.INTER_LINEAR)
                matched = _color_match_lab(resized_src, target_roi)

                # Smooth elliptical feathered mask
                mask = np.zeros((th, tw), dtype=np.uint8)
                cv2.ellipse(
                    mask,
                    (tw // 2, th // 2),
                    (int(tw * 0.44), int(th * 0.47)),
                    0,
                    0,
                    360,
                    255,
                    -1,
                )
                try:
                    center = (tx + tw // 2, ty + th // 2)
                    frame = cv2.seamlessClone(matched, frame, mask, center, cv2.NORMAL_CLONE)
                except Exception:
                    k = max(5, (min(tw, th) // 6) | 1)
                    alpha = (cv2.GaussianBlur(mask, (k, k), 0).astype("float32") / 255.0)[:, :, None]
                    blended = (matched.astype("float32") * alpha + target_roi.astype("float32") * (1.0 - alpha)).astype("uint8")
                    frame[ty : ty + th, tx : tx + tw] = blended
                swapped_count += 1

        if watermark_text:
            cv2.rectangle(frame, (out_w - 230, 14), (out_w - 14, 48), (18, 12, 36), -1)
            cv2.putText(
                frame,
                watermark_text[:20],
                (out_w - 218, 37),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 214, 255),
                2,
                cv2.LINE_AA,
            )

        writer.write(frame)
        frame_idx += 1

    cap.release()
    writer.release()
    return f"opencv-seamless ({swapped_count}/{frame_idx} frames)"


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
    face_path = safe_project_file(job_dir, f"face_{face_image.filename or 'face.jpg'}")
    with open(vid_path, "wb") as f:
        f.write(video_bytes)
    with open(face_path, "wb") as f:
        f.write(face_bytes)

    job_meta = {
        "job_id": job_id,
        "user_email": email,
        "title": title.strip()[:50] or "Custom Face-Swap Clip",
        "video_path": vid_path,
        "face_path": face_path,
        "dub_text": dub_text.strip(),
        "dub_language": normalize_language(dub_language),
        "dub_voice": dub_voice.strip() or "hi-IN-MadhurNeural",
        "dub_pitch": normalize_pitch(dub_pitch),
        "watermark_text": watermark_text.strip()[:30],
        "status": "processing",
        "status_text": "🎭 AI Face-Swap (HuggingFace + OpenCV Seamless Blend) चल रहा है...",
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
