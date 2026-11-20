import os
import re
import json
import uuid
import asyncio
import datetime
import subprocess
import edge_tts
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from gradio_client import Client, handle_file

app = FastAPI(title="CinePop AI Complete Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "akashtyagi534@gmail.com").lower().strip()
UPI_ID = os.getenv("UPI_ID", "8269045143@ybl")
MOVIE_PRICE_INR = int(os.getenv("MOVIE_PRICE_INR", "50"))

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

PROJECTS_DIR = "all_movies"
DB_FILE = "cinepop_db.json"
os.makedirs(PROJECTS_DIR, exist_ok=True)


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


def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "promo_codes" not in data:
                    data["promo_codes"] = {}
                return data
        except Exception:
            pass
    return {"users": {}, "payment_requests": [], "promo_codes": {}, "total_revenue": 0}


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


@app.get("/")
def home():
    return {
        "app": "CinePop AI Studio",
        "status": "100% Live",
        "gemini_configured": bool(GEMINI_API_KEY),
        "price_per_movie": f"₹{MOVIE_PRICE_INR}",
    }


@app.post("/auth/google-login/")
def google_login(
    email: str = Form(...),
    name: str = Form("CinePop Creator"),
    photo_url: str = Form(""),
    device_id: str = Form("android"),
):
    email = email.lower().strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Valid Google Email required")

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
            "movies_created": 0,
            "total_paid": 0,
            "redeemed_codes": [],
        }
    else:
        db["users"][email]["last_active"] = now
        if name:
            db["users"][email]["name"] = name
        if photo_url:
            db["users"][email]["photo_url"] = photo_url

    save_db(db)
    return {
        "user": db["users"][email],
        "is_admin": (email == ADMIN_EMAIL),
        "upi_id": UPI_ID,
        "movie_price": MOVIE_PRICE_INR,
    }


@app.post("/admin/create-promo/")
def create_promo(
    admin_email: str = Form(...),
    code: str = Form(...),
    free_credits: int = Form(1),
    max_uses: int = Form(10),
):
    if admin_email.lower().strip() != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Unauthorized Admin")
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
def delete_promo(admin_email: str = Form(...), code: str = Form(...)):
    if admin_email.lower().strip() != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Unauthorized Admin")
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
def get_admin_stats(admin_email: str):
    if admin_email.lower().strip() != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Unauthorized Admin")
    db = load_db()
    users_list = list(db["users"].values())
    return {
        "total_installs": len(users_list),
        "total_revenue": db.get("total_revenue", 0),
        "total_movies_made": sum(u.get("movies_created", 0) for u in users_list),
        "users": users_list,
        "pending_payments": [
            p for p in db.get("payment_requests", []) if p["status"] == "PENDING"
        ],
        "promo_codes": list(db.get("promo_codes", {}).values()),
    }


@app.post("/admin/approve-payment/")
def approve_payment(admin_email: str = Form(...), req_id: str = Form(...)):
    if admin_email.lower().strip() != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Unauthorized Admin")
    db = load_db()
    for p in db["payment_requests"]:
        if p["req_id"] == req_id and p["status"] == "PENDING":
            p["status"] = "APPROVED"
            user_email = p["email"]
            if user_email in db["users"]:
                db["users"][user_email]["credits"] += p["movies_count"]
                db["users"][user_email]["total_paid"] += p["amount"]
                db["total_revenue"] = db.get("total_revenue", 0) + p["amount"]
            save_db(db)
            return {
                "message": f"Approved! {user_email} को {p['movies_count']} मूवी पास मिल गया।"
            }
    raise HTTPException(status_code=404, detail="Pending request not found")


@app.post("/admin/add-free-credit/")
def add_free_credit(
    admin_email: str = Form(...),
    target_email: str = Form(...),
    credits: int = Form(1),
):
    if admin_email.lower().strip() != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Unauthorized Admin")
    db = load_db()
    target_email = target_email.lower().strip()
    if target_email in db["users"]:
        db["users"][target_email]["credits"] += credits
        save_db(db)
        return {"message": f"{target_email} को {credits} मूवी पास दे दिया गया!"}
    raise HTTPException(status_code=404, detail="User not found")


async def generate_bulletproof_audio(text: str, voice: str, out_mp3: str):
    clean_text = text.strip() or "नमस्ते दोस्तों!"
    try:
        comm = edge_tts.Communicate(clean_text, voice)
        await asyncio.wait_for(comm.save(out_mp3), timeout=15.0)
        if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 500:
            return
    except Exception:
        pass

    try:
        def _run_gtts():
            tts = gTTS(text=clean_text, lang="hi")
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


def prepare_character_frame(
    raw_img_path: str, char_name: str, dialogue: str, out_frame_path: str
):
    base = Image.open(raw_img_path).convert("RGB")
    bg = base.resize((1280, 720))
    overlay = Image.new("RGB", (1280, 720), (15, 10, 30))
    bg = Image.blend(bg, overlay, 0.72)

    aspect = base.width / max(base.height, 1)
    new_h = 620
    new_w = min(1100, int(new_h * aspect))
    portrait = base.resize((new_w, new_h), Image.Resampling.LANCZOS)
    x_offset = (1280 - new_w) // 2
    bg.paste(portrait, (x_offset, 20))

    draw = ImageDraw.Draw(bg)
    draw.rounded_rectangle([40, 25, 320, 75], radius=15, fill=(255, 0, 127))
    try:
        font_badge = ImageFont.truetype(
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", 26
        )
        font_sub = ImageFont.truetype(
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", 28
        )
    except Exception:
        font_badge = ImageFont.load_default()
        font_sub = ImageFont.load_default()

    draw.text((60, 35), char_name[:18], fill=(255, 255, 255), font=font_badge)
    draw.rectangle([0, 635, 1280, 720], fill=(10, 8, 22))
    short_diag = dialogue if len(dialogue) <= 85 else dialogue[:82] + "..."
    draw.text((50, 655), short_diag, fill=(255, 214, 0), font=font_sub)

    bg.save(out_frame_path, "JPEG", quality=92)


def render_exact_face_clip(
    raw_img_path: str, framed_img_path: str, audio_path: str, out_mp4: str
):
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
                    still_mode=True,
                    use_enhancer=False,
                    batch_size=1,
                    size_of_image=256,
                    pose_style=0,
                    api_name="/test",
                )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    res,
                    "-vf",
                    "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                    "-r",
                    "24",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "26",
                    "-c:a",
                    "aac",
                    "-ar",
                    "44100",
                    "-b:a",
                    "96k",
                    out_mp4,
                ],
                check=True,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1000:
                return
        except Exception:
            continue

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            framed_img_path,
            "-i",
            audio_path,
            "-c:v",
            "libx264",
            "-tune",
            "stillimage",
            "-shortest",
            "-r",
            "24",
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
            out_mp4,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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

        if not state.get("outline"):
            state["status_text"] = (
                "CinePop AI पूरी फिल्म की कहानी और चैप्टर्स लिख रहा है..."
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
                    prompt = f"""
                    तुम एक बॉलीवुड मूवी डायरेक्टर हो। '{state['idea']}' पर {duration} मिनट की फिल्म की रूपरेखा बनाओ।
                    कैरेक्टर्स: {char_names}
                    कहानी को ठीक {total_chapters} बराबर चैप्टर्स में बांटो।
                    सिर्फ शुद्ध JSON दो:
                    {{"title": "फिल्म का नाम", "chapters": [{{"no": 1, "plot": "विस्तार से कहानी"}}]}}
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
        prev_plot = "कहानी की शुरुआत"

        for ch in chapters:
            ch_no = ch["no"]
            ch_mp4 = f"{proj_dir}/ch_{ch_no}.mp4"
            if ch_no in state["done_chapters"] and os.path.exists(ch_mp4):
                prev_plot = ch["plot"]
                continue

            percent = int(((ch_no - 1) / len(chapters)) * 90) + 5
            state["progress"] = percent
            state["status_text"] = (
                f"चैप्टर {ch_no}/{len(chapters)} शूट हो रहा है ({percent}%)..."
            )
            save_state(proj_dir, state)

            ch_temp = f"{proj_dir}/ch_{ch_no}_clips"
            os.makedirs(ch_temp, exist_ok=True)

            fallback_dialogues = [
                {
                    "character": char_names[i % len(char_names)],
                    "dialogue": f"{ch['plot']} - सीन {i+1} का दमदार संवाद।",
                }
                for i in range(15)
            ]

            if client:
                try:
                    script_prompt = f"""
                    फिल्म: {state['outline']['title']} | पिछला सीन: {prev_plot}
                    वर्तमान चैप्टर {ch_no}: {ch['plot']}
                    कैरेक्टर्स: {char_names}
                    इस चैप्टर के लिए 50 से 60 दमदार हिंदी डायलॉग्स लिखो ताकि 10 मिनट का सीन बने।
                    सिर्फ JSON लिस्ट दो: [{{"character": "{char_names[0]}", "dialogue": "डायलॉग..."}}]
                    """
                    s_res = client.models.generate_content(
                        model="gemini-2.5-flash", contents=script_prompt
                    )
                    dialogues = safe_extract_json(s_res.text, fallback_dialogues)
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
                a_file = f"{ch_temp}/a_{i}.mp3"
                f_img = f"{ch_temp}/f_{i}.jpg"
                v_file = f"{ch_temp}/v_{i}.mp4"

                if not os.path.exists(v_file):
                    await generate_bulletproof_audio(
                        diag_text, char_map[c_name]["voice"], a_file
                    )
                    await asyncio.to_thread(
                        prepare_character_frame,
                        char_map[c_name]["image"],
                        c_name,
                        diag_text,
                        f_img,
                    )
                    await asyncio.to_thread(
                        render_exact_face_clip,
                        char_map[c_name]["image"],
                        f_img,
                        a_file,
                        v_file,
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
            "movies_created": 0,
            "total_paid": 0,
            "redeemed_codes": [],
        }

    if email != ADMIN_EMAIL:
        if db["users"][email]["credits"] < 1:
            raise HTTPException(status_code=402, detail="INSUFFICIENT_CREDITS")
        db["users"][email]["credits"] -= 1

    db["users"][email]["movies_created"] += 1
    save_db(db)

    project_id = str(uuid.uuid4())[:10]
    proj_dir = f"{PROJECTS_DIR}/{project_id}"
    os.makedirs(proj_dir, exist_ok=True)

    names = [x.strip() for x in char_names.split(",")]
    v_list = [x.strip() for x in voices.split(",")]

    char_map = {}
    for idx, img in enumerate(images):
        c_name = names[idx] if idx < len(names) else f"Actor_{idx+1}"
        c_voice = v_list[idx] if idx < len(v_list) else "hi-IN-MadhurNeural"
        img_path = f"{proj_dir}/{c_name}.jpg"
        with open(img_path, "wb") as f:
            f.write(await img.read())
        char_map[c_name] = {"image": img_path, "voice": c_voice}

    initial_state = {
        "project_id": project_id,
        "user_email": email,
        "idea": idea,
        "duration_minutes": duration_minutes,
        "characters": char_map,
        "done_chapters": [],
        "ready_parts": [],
        "scripts": {},
        "progress": 2,
        "status_text": "प्रोजेक्ट शुरू! अब आप इंटरनेट बंद कर सकते हैं।",
        "is_completed": False,
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

    state["scripts"][ch_key][dialogue_index]["dialogue"] = new_dialogue

    ch_temp = f"{proj_dir}/ch_{chapter_no}_clips"
    os.makedirs(ch_temp, exist_ok=True)
    a_file = f"{ch_temp}/a_{dialogue_index}_new.mp3"
    f_img = f"{ch_temp}/f_{dialogue_index}_new.jpg"
    v_file = f"{ch_temp}/v_{dialogue_index}.mp4"

    await generate_bulletproof_audio(new_dialogue, char_info["voice"], a_file)
    await asyncio.to_thread(
        prepare_character_frame, char_info["image"], char_name, new_dialogue, f_img
    )
    await asyncio.to_thread(
        render_exact_face_clip, char_info["image"], f_img, a_file, v_file
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
