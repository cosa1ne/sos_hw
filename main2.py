#!/usr/bin/env python3
"""
main3.py ― FastAPI + QR bridge + Realtime receipt printer (분리 구조/리팩토링)
---------------------------------------------------------------------------

1. escpos.printer.Usb 만 사용해 이미지 인쇄
2. /api/production 수락 → 콜백 URL·productionId 를 전역에 저장
3. Arduino 가 '#DONE' 전송 → 콜백 후 영수증 인쇄
"""

from __future__ import annotations
import time, uuid, threading, asyncio, re
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

import serial
from escpos.printer import Usb
from PIL import Image, ImageDraw, ImageFont
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ───── 사용자-환경 설정 ─────────────────────────────────────────
SERIAL_PORT, BAUD_RATE = "/dev/ttyACM0", 115200

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "img"
TIME_DIR = TEMPLATE_DIR / "time"; TIME_DIR.mkdir(parents=True, exist_ok=True)
KEEP_IMAGES = 10

USB_VID, USB_PID = 0x0FE6, 0x811E
USB_TIMEOUT = 2000  # ms
PRN_MAX_WIDTH = 384  # 58 mm

X, Y = 122, 160
FONT_PATH = BASE_DIR / "fonts" / "HakgyoansimBareondotumB.ttf"
FONT_SIZE = 15
TEXT_COLOR, BG_COLOR, PAD = (0,0,0,255), (255,255,255,255), 4

CALLBACK_URL = "http://3.39.64.81:8080/api/v1/hardware/callback"

QR_URL = "http://3.39.64.81:8080/api/v1/hardware/qr/lookup"
QR_HEAD = {"Content-Type":"application/json"}
VALID_QR = {"배롱나무","감나무","은행나무","경포대","차수국",
            "태백산맥","밤장미","벚꽃","안목해변","소나무"}
QR_PAT = re.compile(r"^[\u3131-\uD79D\w ]+$")
QR_RET_SEC, PHYSICAL_ID = 3, "MPQ03KH/A"
PUMP_MAP = {"배롱나무":0,"감나무":1,"은행나무":2,"경포대":3,"차수국":4,
            "태백산맥":5,"밤장미":6,"벚꽃":7,"안목해변":8,"소나무":9}

ser_lock = threading.Lock()
serial_ready = threading.Event()
usb_lock = threading.Lock()

current_template: Path | None = None
current_callback_url: str | None = None
current_production_id: str | None = None
last_qr_sent: dict[str, float] = {}

# ───── 템플릿 찾기 ───────────────────────────────────────
def find_template(n):
    p = TEMPLATE_DIR / f"{n}.png"
    return p if p.exists() else None

# ───── 프린터 헬퍼 ────────────────────────────────────────────
'''def printer_ready() -> bool:
    try:
        p = Usb(USB_VID, USB_PID, out_ep=0x03, timeout=USB_TIMEOUT, auto_detach=True)
        val = p.query_status(1)
        p.close()
        if val is None:
            return True
        return not (val & 0x24)
    except Exception as e:
        print("[WARN] printer_ready 실패:", e)
        return True'''

def overlay_now(template: Path) -> Path:
    im = Image.open(template).convert("RGBA")
    draw = ImageDraw.Draw(im)
    ts = datetime.now().strftime("%Y.%m.%d %H:%M")
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE) if Path(FONT_PATH).exists() \
        else ImageFont.load_default()
    try:
        l, t, r, b = draw.textbbox((0,0), ts, font=font)
        w, h = r - l, b - t
    except AttributeError:
        w, h = draw.textsize(ts, font=font)
    rect = [X-PAD, Y-PAD, X+w+PAD, Y+h+PAD]
    draw.rectangle(rect, fill=BG_COLOR)
    draw.text((X,Y), ts, font=font, fill=TEXT_COLOR)
    if im.width > PRN_MAX_WIDTH:
        nh = int(im.height * PRN_MAX_WIDTH / im.width)
        im = im.resize((PRN_MAX_WIDTH, nh), Image.LANCZOS)
    out = TIME_DIR / f"receipt_{uuid.uuid4().hex}.png"
    im.save(out)
    _prune_old(TIME_DIR, KEEP_IMAGES)
    return out

def _prune_old(folder: Path, keep: int):
    for p in sorted(folder.glob("receipt_*.png"), key=lambda x: x.stat().st_mtime)[:-keep]:
        p.unlink(missing_ok=True)

def print_receipt(template: Path, retry: int = 5):
    try:
        png = overlay_now(template)
    except Exception as e:
        print("[ERR] overlay 실패:", e)
        return
    while True:
        '''if not printer_ready():
            print(f"[WAIT] 커버/용지 오류 — {retry}s 후 재시도")
            time.sleep(retry)
            continue'''
        try:
            with usb_lock:
                p = Usb(USB_VID, USB_PID, out_ep=0x03, timeout=USB_TIMEOUT, auto_detach=True)
                p.set(align="center")
                p.image(str(png), impl="bitImageRaster", fragment_height=128)
                p.cut()
                p.close()
                time.sleep(0.1)   # busy 방지
            print("[PRINT] escpos 인쇄 완료 →", png)
            break
        except Exception as e:
            print("[ERR] 인쇄 실패:", e)
            time.sleep(retry)

# ───── QR 헬퍼 ────────────────────────────────────────────────
def looks_like_qr(line: str) -> bool:
    return bool(line and line not in {"r", "#DONE"} and line[0] not in ("[", "▶", "#"))

def handle_qr(qr: str) -> None:
    if qr not in VALID_QR or not QR_PAT.match(qr): return
    now = time.time()
    if now - last_qr_sent.get(qr, 0) < QR_RET_SEC: return
    payload = {"fragranceName": qr, "physicalId": PHYSICAL_ID}
    try:
        r = httpx.post(QR_URL, json=payload, headers=QR_HEAD, timeout=3)
        if r.status_code < 400:
            last_qr_sent[qr] = now
            print(f"[QR]{qr}→{r.status_code}")
        else:
            print(f"[QR] 응답 {r.status_code}: {r.text}")
    except httpx.RequestError as e:
        print("[QR] 예외:", e)

# ───── FastAPI 및 시리얼 초기화 ───────────────────────────────
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
main_loop = asyncio.get_event_loop()
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.2)
    print("✅ Serial opened")
except serial.SerialException as e:
    print("⚠️ Serial open 실패:", e)
    ser = None

# ─── Pydantic 모델 ──────────────────────────────────────────
class CustomRecipe(BaseModel):
    top: Optional[List[Dict[str, float]]] = []
    middle: Optional[List[Dict[str, float]]] = []
    base: Optional[List[Dict[str, float]]] = []
class CustomRequest(BaseModel):
    name: str
    recipe: CustomRecipe
    callbackUrl: str
    productionId: Optional[str] = None
class OriginalRecipe(BaseModel):
    name: str
    ratio: float
class OriginalRequest(BaseModel):
    recipe: OriginalRecipe
    callbackUrl: str
    productionId: Optional[str] = None

def parse_recipe(recipe: dict, total: float = 15.0) -> List[float]:
    v = [0.0] * 10
    for layer in ("top", "middle", "base"):
        for itm in recipe.get(layer, []):
            for n, p in itm.items():
                idx = PUMP_MAP.get(n)
                if idx is not None: v[idx] += total * (p / 100.0)
    return v
fmt = lambda vs: [f"{x:.1f}" if x == int(x) else f"{x:.2f}".rstrip("0") for x in vs]


# ───── 시리얼 워커/처리자 ──────────────────────────────────────
def notify_cartridge_used(fragrance_name, physical_id):
    url = "http://3.39.64.81:8080/api/v1/hardware/cartridges"
    payload = {
        "fragranceName": fragrance_name,
        "physicalId": physical_id
    }
    try:
        r = httpx.patch(url, json=payload, timeout=3)
        print(f"[PATCH cartridges] 응답 {r.status_code}: {r.text[:120]}")
        return r.status_code == 200
    except Exception as e:
        print("[PATCH cartridges] 예외:", e)
        return False

def handle_production_done(callback_url, production_id, success=True, error_reason=""):
    print("[콜백부분 pruduction_id -> ] : ", production_id)
    payload = {
        "productionId": production_id,
        "status": "COMPLETED" if success else "FAILED",
        "errorReason": error_reason if not success else ""
    }
    try:
        r= httpx.post(callback_url, json=payload, timeout=3)
        if r.status_code < 400:
            print(f"[prod_callback] {callback_url} -> {r.status_code}")
        else :
            print(f"[prod_callback] 응답 {r.status_code}: {r.text}")
    except httpx.RequestError as e:
        print("[prod_callback] 예외:", e)

def serial_worker():
    if ser is None:
        return
    serial_ready.set()
    while True:
        try:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode(errors='ignore').strip()
            print(f"[serial_worker] 라인: {repr(line)}")

            if looks_like_qr(line):
                handle_qr(line)
                continue

            if line == "#DONE":

                serial_done_worker()
        except Exception as e:
            print("[ERR] serial_worker:", e)
            time.sleep(0.5)

def serial_done_worker():
    # 콜백을 먼저 비동기로 전송
    print("[DBG] serial_done_worker 진입")
    try:
        #PATCH 콜백
        fragrance_name = current_template.stem if current_template else None
        physical_id = PHYSICAL_ID
        print(fragrance_name)
        if fragrance_name:
            notify_cartridge_used(fragrance_name, physical_id)
        else:
            print("[PATCH cartridges] fragrance_name 없음")
        
        #제조 완료 콜백
        print("[DBG] 콜백 URL : ", current_callback_url, "PID: ", current_production_id)
        if current_callback_url and current_production_id:
            handle_production_done("http://3.39.64.81:8080/api/v1/hardware/callback", current_production_id, success=True)
        else:
            print("[DEG] 콜백 미존재, 실행 안함")
    except Exception as e:
        print("[ERR] simulate_callback:", e)
    finally:
        try:
            if current_template:
                print_receipt(current_template)
            else:
                print("[DONE] 템플릿 정보 없음")
        except Exception as e:
            print("[ERR] print_receipt:", e)

# ─── FastAPI 이벤트 및 라우터 ──────────────────────────────
@app.on_event("startup")
def _start():
    if ser:
        threading.Thread(target=serial_worker, daemon=True).start()

@app.get("/")
def root():
    return {"message": "FastAPI is running!"}

@app.post("/api/production")
async def production(req: Request):
    global current_template, current_callback_url, current_production_id
    try:
        body = await req.json(); print("[REQ]", body)
        # --- Custom
        if "name" in body and "recipe" in body:
            data = CustomRequest(**body)
            fname = data.name.replace(" ", "")
            pumps = parse_recipe(data.recipe.dict())
            cid = data.productionId or f"{fname}_{datetime.now():%Y%m%d%H%M%S}"
            cb_url = data.callbackUrl
        # --- Original
        elif "recipe" in body and "name" in body["recipe"]:
            data = OriginalRequest(**body)
            fname = data.recipe.name.replace(" ", "")
            pumps = parse_recipe({"base": [{data.recipe.name: data.recipe.ratio}]})
            cid = data.productionId or f"{fname}_{datetime.now():%Y%m%d%H%M%S}"
            cb_url = data.callbackUrl
        else:
            return JSONResponse(400, {"status": "error", "code": "INVALID_FORMAT", "message": "지원되지 않는 형식"})

        tpl = find_template(fname)
        current_template = tpl
        current_callback_url = cb_url
        current_production_id = cid

        if ser and ser.is_open:
            s = ",".join(fmt(pumps))
            with ser_lock:
                ser.write((s + "\n").encode())
            print("[SER] →", s)

        return {"status": "success", "message": "향수 제작 요청이 수락되었습니다",
                "details": {"production_id": cid}}
    except Exception as e:
        print("[ERR] production:", e)

        