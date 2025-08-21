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
from pydantic import BaseModel, Field

# ───── 사용자-환경 설정 ─────────────────────────────────────────
SERIAL_PORT, BAUD_RATE = "/dev/ttyACM0", 115200

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "img"
ORIG_DIR   = TEMPLATE_DIR / "original"
CUSTOM_DIR = TEMPLATE_DIR / "custom"
TIME_DIR   = TEMPLATE_DIR / "time"
for _d in (ORIG_DIR, CUSTOM_DIR, TIME_DIR):
    _d.mkdir(parents=True, exist_ok=True)

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

# --- 워터펌프 관련 파라미터 ------------------------------------#
PUMP_MAP = {
    "오미자": 0, "감나무": 1, "은행나무": 2, "경포대": 3, "차수국": 4,
    "태백산맥": 5, "메밀꽃": 6, "감자꽃": 7, "안목해변": 8, "소나무": 9
}

NAME_SET = set(PUMP_MAP.keys())  # ← name 판별용 (JSON과 직접 비교)

ALLOWED_MIN_ML = 14.0
ALLOWED_MAX_ML = 15.1
MIN_ING = 1
MAX_ING = 7

ser_lock = threading.Lock()
serial_ready = threading.Event()
usb_lock = threading.Lock()

current_template: Path | None = None
current_callback_url: str | None = None
current_production_id: str | None = None
last_qr_sent: dict[str, float] = {}

# ───── 템플릿 찾기 ───────────────────────────────────────
'''def find_template(n):
    p = TEMPLATE_DIR / f"{n}.png"
    return p if p.exists() else None'''
def _count_used_ingredients(recipe: Dict[str, float]) -> int:
     # 커스텀 요청은 0 ml가 안 오므로 길이로 결정
     n = len(recipe)
     if n < 1: n = 1
     if n > 7: n = 7
     return n

def select_template(name: str, recipe: Dict[str, float]) -> Optional[Path]:
    """
    name이 10개 고정 향료 집합에 포함되면: img/original/<name>.png
    아니면: img/custom/<N>.png (N = len(recipe), 1~7)
    """
    clean = name.replace(" ", "")
    if name in NAME_SET:
        p = ORIG_DIR / f"{clean}.png"
        if p.exists():
            print(f"[TPL] original 선택: {p}")
            return p
        print(f"[TPL] original 지정이나 파일 없음: {p}")
        return None
    else:
        n = _count_used_ingredients(recipe)
        p = CUSTOM_DIR / f"{n}.png"
        if p.exists():
            print(f"[TPL] custom 선택: {p} (N={n})")
            return p
        print(f"[TPL] custom 파일 없음: {p}")
        return None



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
app = FastAPI(title="Scenti Piano - production API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
main_loop = asyncio.get_event_loop()
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.2)
    print("✅ Serial opened")
except serial.SerialException as e:
    print("⚠️ Serial open 실패:", e)
    ser = None


def _fmt(vs: List[float]) -> List[str]:
    out = []
    for x in vs:
        if float(int(x)) == float(x):
            out.append(f"{x:.1f}")
        else:
            s = f"{x:.2f}".rstrip("0")
            out.append(s if "." in s else s + ".0")
    return out

class PerfumeRequest(BaseModel):
    name: str = Field(..., description="향수 이름")
    recipe: Dict[str, float] = Field(..., description="향료명→ml (절대량)")
    callbackUrl: str
    productionId: str


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
            handle_production_done(current_callback_url, current_production_id, success=True)
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
    print("[STARTUP] FastAPI 서버 시작")
    if ser:
        print("[STARTUP] Serial 워커 스레드 시작")
        threading.Thread(target=serial_worker, daemon=True).start()
    else:
        print("[STARTUP] Serial 포트 없음, 워커 미시작")

@app.get("/")
def root():
    return {"message": "production API is running"}

@app.post("/api/production")
def production(req: PerfumeRequest):

    print("==== /api/production 호출됨 ====")
    print("req.name =", req.name)
    print("req.recipe =", req.recipe)
    print("req.callbackUrl =", req.callbackUrl)
    print("req.productionId =", req.productionId)

    # 0) 키/값 기초 검증
    print(req)
    if not isinstance(req.recipe, dict) or len(req.recipe) == 0:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "recipe는 최소 1개 이상의 항목이 필요합니다."},
        )

    # 1) 알 수 없는 향료명 검증 (엄격 모드: 있으면 에러)
    unknowns = [k for k in req.recipe.keys() if k not in PUMP_MAP]
    if unknowns:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": "정의되지 않은 향료명이 포함되어 있습니다.",
                "details": {"unknown_ingredients": unknowns}
            },
        )

    # 2) 개수 제한 (1~7개) — 커스텀은 0ml가 안 오므로 len(recipe) 그대로 사용
    if not (MIN_ING <= len(req.recipe) <= MAX_ING):
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": f"향료 개수는 {MIN_ING}~{MAX_ING}개여야 합니다.",
                "details": {"count": len(req.recipe)}
            },
        )

    # 3) 값 검증 및 총합 계산 (음수/NaN/None 금지)
    total = 0.0
    pumps = [0.0] * 10
    for name, ml in req.recipe.items():
        try:
            val = float(ml)
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": f"'{name}'의 값이 숫자가 아닙니다."},
            )
        if val < 0:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": f"'{name}'의 양은 음수가 될 수 없습니다."},
            )
        pumps[PUMP_MAP[name]] += val
        total += val

    # 4) 총합 제한 (14.0~15.1 ml)
    if not (ALLOWED_MIN_ML <= total <= ALLOWED_MAX_ML):
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": f"총합은 {ALLOWED_MIN_ML}~{ALLOWED_MAX_ML} ml 범위여야 합니다.",
                "details": {"total_ml": round(total, 3)}
            },
        )

    # 5) 템플릿/콜백/프로덕션ID/향수명 세팅 (영수증/콜백에 필요)
    global current_template, current_callback_url, current_production_id, current_perfume_name
    current_template = select_template(req.name, req.recipe)
    current_callback_url = req.callbackUrl
    current_production_id = req.productionId
    current_perfume_name = req.name

    # 6) 아두이노 전송 (개행 포함)
    payload = ",".join(_fmt(pumps))
    line = payload + "\n"
    print("[REQ] name:", req.name)
    print("[REQ] productionId:", req.productionId)
    print("[REQ] callbackUrl:", req.callbackUrl)
    print(f"[REQ] recipe_count={len(req.recipe)}, total_ml={total:.3f}")
    print("[SER 준비] payload =", payload)

    if ser is None:
        print("[SER] ser is None")
    elif not ser.is_open:
        print("[SER] ser is not open")
    else:
        try:
            with ser_lock:
                ser.write(line.encode())  # ← 개행 포함해 인코딩
            print("[SER 전송 완료]", payload)
        except Exception as e:
            print("[SER] 전송 실패]", e)
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": "아두이노 통신 실패"},
            )

    # 7) 성공 응답
    return {
        "status": "success",
        "message": "향수 제작 요청이 수락되었습니다",
        "details": {"production_id": req.productionId}
    }