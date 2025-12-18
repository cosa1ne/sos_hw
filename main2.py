#!/usr/bin/env python3
"""
main3.py ― FastAPI + QR bridge + Realtime receipt printer (분리 구조/리팩토링)

기능 요약
1) original(고정 템플릿) : 날짜만 중앙정렬로 오버레이 후 인쇄
2) custom(1~7개 레시피) : img/custom/N.jpg(.png)에
   - 날짜(중앙정렬)
   - name(Hello Perfumers 아래, 표 위 — 48mm 폭 자동 폰트 축소)
   - 표(향료명/비율, 비율 내림차순·동률은 입력순) 를 합성 후 인쇄
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
PRN_MAX_WIDTH = 384  # 58 mm 열프린터 유효 폭(px)
PAPER_WIDTH_MM = 58.0  # 프린터 용지 폭(mm) → 384px과 매칭됨

# ── mm → px 변환 (58mm=384px 가정) ─────────────────────────────
DOTS_PER_MM = PRN_MAX_WIDTH / PAPER_WIDTH_MM  # ≈ 6.62 px/mm
def mm(v: float) -> int:
    return int(round(v * DOTS_PER_MM))

# ── 날짜(시간) 오버레이 ────────────────────────────────────────
# *이미 중앙정렬로 바뀐 상태* : X는 무의미, Y만 사용 (원하시면 mm()로 바꿔도 됨)
X, Y = 122, 160  # Y만 유효(세로 위치)

FONT_PATH = BASE_DIR / "fonts" / "HakgyoansimBareondotumB.ttf"
FONT_SIZE = 15   # 날짜 폰트 px 크기 (원하시면 mm(2.2) 등으로 조정)
TEXT_COLOR = (0,0,0,255)
BG_COLOR   = (255,255,255,255)
PAD        = 4   # 날짜 배경 패딩(px)

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
NAME_SET = set(PUMP_MAP.keys())  # ← original 여부 판별용

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
current_perfume_name: str | None = None
last_qr_sent: dict[str, float] = {}

# ── 커스텀(레시피 표·타이틀) 레이아웃 (mm기반) ──────────────────
# 표(“향료명/비율”) — 1줄 높이 5mm (측정값 반영)
RECIPE_X          = mm(6.0)    # 표 좌측 시작 X
RECIPE_Y          = mm(35.0)   # 머리글 Y (원하시는 위치로 미세조정)
RECIPE_LH         = mm(5.0)    # 1줄 높이(=5mm)
NAME_COL_W        = mm(30.0)   # 향료명 칼럼폭
GAP_COL           = mm(2.0)    # 칼럼 사이 간격
PCT_COL_W         = mm(12.0)   # 비율 칼럼폭
RECIPE_TITLE_FS   = mm(3.2)    # 머리글 폰트크기(≈3.2mm)
RECIPE_ITEM_FS    = mm(3.5)    # 항목 폰트크기(≈3.5mm)
RECIPE_HEADER_GAP = mm(1.2)    # 머리글과 첫 항목 사이 간격

# name(타이틀) — Hello Perfumers 아래, 표 위 / 가로폭 48mm 제한
NAME_MAX_CHARS    = 12
NAME_Y            = mm(30.0)   # name 텍스트 Y (Hello Perfumers와 표 사이로 조정)
NAME_SIDE_MARGIN  = mm(2.0)    # 좌우 여백
NAME_FS_MAX       = mm(5.0)    # 최대 폰트
NAME_FS_MIN       = mm(2.5)    # 최소 폰트
NAME_MAX_WIDTH_PX = mm(48.0)   # 48mm 폭 제한

# 커스텀 상태
current_is_custom: bool = False
current_recipe: Dict[str, float] | None = None

# ───── 템플릿 찾기 ───────────────────────────────────────
def _count_used_ingredients(recipe: Dict[str, float]) -> int:
    n = len(recipe)
    if n < 1: n = 1
    if n > 7: n = 7
    return n

def select_template(name: str, recipe: Dict[str, float]) -> Optional[Path]:
    """
    name이 10개 고정 향료 집합에 포함되면: img/original/<name>.png
    아니면: img/custom/<N>.jpg(.png) (N = len(recipe), 1~7)
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
        p = CUSTOM_DIR / f"{n}.jpg"
        if p.exists():
            print(f"[TPL] custom 선택(jpg): {p} (N={n})")
            return p
        p = CUSTOM_DIR / f"{n}.png"
        if p.exists():
            print(f"[TPL] custom 선택(png): {p} (N={n})")
            return p
        print(f"[TPL] custom 파일 없음: {CUSTOM_DIR}/{n}.jpg|.png")
        return None

# ───── 프린터 헬퍼 ────────────────────────────────────────────
def overlay_now(template: Path) -> Path:
    """original 템플릿에 날짜만 얹어 저장 (가로 중앙정렬)"""
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

    x = (im.width - w)//2
    y = Y  # 세로 위치만 조정
    draw.rectangle([x-PAD, y-PAD, x+w+PAD, y+h+PAD], fill=BG_COLOR)
    draw.text((x,y), ts, font=font, fill=TEXT_COLOR)

    if im.width > PRN_MAX_WIDTH:
        nh = int(im.height * PRN_MAX_WIDTH / im.width)
        im = im.resize((PRN_MAX_WIDTH, nh), Image.LANCZOS)
    out = TIME_DIR / f"receipt_{uuid.uuid4().hex}.jpg"
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

# ───── 커스텀 합성(타이틀 + 표) ─────────────────────────────────
def select_custom_template_by_count(n: int) -> Optional[Path]:
    p = CUSTOM_DIR / f"{n}.jpg"
    if p.exists(): return p
    p = CUSTOM_DIR / f"{n}.png"
    return p if p.exists() else None

def _stable_sort_by_amount(recipe: Dict[str, float]) -> list[tuple[str, float]]:
    """비율 내림차순, 동률은 입력 순서 유지"""
    items = list(recipe.items())
    order = sorted(range(len(items)), key=lambda i: (-float(items[i][1]), i))
    return [items[i] for i in order]

def _fmt_pct(x: float) -> str:
    return f"{x:.1f}%"

def draw_recipe_block(im: Image.Image, recipe: Dict[str, float]) -> None:
    """이미지 위에 '향료명  비율' 표를 그림 (머리글→간격→항목들)"""
    draw = ImageDraw.Draw(im)
    font_title = ImageFont.truetype(FONT_PATH, RECIPE_TITLE_FS) if Path(FONT_PATH).exists() else ImageFont.load_default()
    font_item  = ImageFont.truetype(FONT_PATH, RECIPE_ITEM_FS)  if Path(FONT_PATH).exists() else ImageFont.load_default()

    # 머리글
    y = RECIPE_Y
    draw.text((RECIPE_X, y), "향료명", font=font_title, fill=(0,0,0,255))
    pct_right = RECIPE_X + NAME_COL_W + GAP_COL + PCT_COL_W
    try:
        l,t,r,b = draw.textbbox((0,0), "비율", font=font_title)
        w_title = r - l; h_title = b - t
    except AttributeError:
        w_title, h_title = draw.textsize("비율", font=font_title)
    draw.text((pct_right - w_title, y), "비율", font=font_title, fill=(0,0,0,255))
    # 머리글 높이 반영 후 간격 추가
    y += h_title + RECIPE_HEADER_GAP

    # 데이터
    items = _stable_sort_by_amount(recipe)
    total = sum(float(v) for _, v in items) or 1.0

    for name, ml in items:
        ratio = (float(ml) / total) * 100.0
        # 좌: 향료명(좌정렬)
        draw.text((RECIPE_X, y), str(name), font=font_item, fill=(0,0,0,255))
        # 우: 비율(우정렬)
        pct_txt = _fmt_pct(ratio)
        try:
            l,t,r,b = draw.textbbox((0,0), pct_txt, font=font_item)
            tw = r - l
        except AttributeError:
            tw,_ = draw.textsize(pct_txt, font=font_item)
        draw.text((pct_right - tw, y), pct_txt, font=font_item, fill=(0,0,0,255))
        y += RECIPE_LH  # 1줄=5mm 간격

def draw_centered_fit_text(im: Image.Image, text: str, y: int,
                           max_width_px: int, fs_max: int, fs_min: int,
                           color=(0,0,0,255),
                           margin=NAME_SIDE_MARGIN,
                           bg_color=BG_COLOR):
    """가운데 정렬 텍스트를 폭 제한 내로 자동 폰트 축소하여 그림."""
    draw = ImageDraw.Draw(im)
    if len(text) > NAME_MAX_CHARS:
        text = text[:NAME_MAX_CHARS]

    size = fs_max
    while size >= fs_min:
        font = ImageFont.truetype(FONT_PATH, size) if Path(FONT_PATH).exists() else ImageFont.load_default()
        try:
            l,t,r,b = draw.textbbox((0,0), text, font=font)
            w,h = r-l, b-t
        except AttributeError:
            w,h = draw.textsize(text, font=font)
        allowed = max_width_px - 2*margin
        if w <= allowed:
            x = (im.width - w)//2
            # 배경 사각형(필요없으면 삭제 가능)
            draw.rectangle([x-4, y-4, x+w+4, y+h+4], fill=bg_color)
            draw.text((x, y), text, font=font, fill=color)
            return
        size -= 1

    # 최소 폰트로라도 출력
    font = ImageFont.truetype(FONT_PATH, fs_min) if Path(FONT_PATH).exists() else ImageFont.load_default()
    try:
        l,t,r,b = draw.textbbox((0,0), text, font=font)
        w,h = r-l, b-t
    except AttributeError:
        w,h = draw.textsize(text, font=font)
    x = (im.width - w)//2
    draw.rectangle([x-4, y-4, x+w+4, y+h+4], fill=bg_color)
    draw.text((x, y), text, font=font, fill=color)

def compose_custom_receipt(name: str, recipe: Dict[str, float]) -> Optional[Path]:
    """커스텀(레시피 1~7개) 템플릿을 골라 날짜 + name 타이틀 + 레시피 표 저장."""
    n = max(1, min(7, len(recipe)))
    tpl = select_custom_template_by_count(n)
    if not tpl:
        print(f"[TPL] custom 템플릿 없음: {n}.jpg/.png")
        return None

    im = Image.open(tpl).convert("RGBA")
    draw = ImageDraw.Draw(im)

    # 1) 날짜(가운데 정렬)
    ts = datetime.now().strftime("%Y.%m.%d %H:%M")
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE) if Path(FONT_PATH).exists() else ImageFont.load_default()
    try:
        l,t,r,b = draw.textbbox((0,0), ts, font=font)
        w,h = r-l, b-t
    except AttributeError:
        w,h = draw.textsize(ts, font=font)
    x = (im.width - w)//2
    y = Y
    draw.rectangle([x-PAD, y-PAD, x+w+PAD, y+h+PAD], fill=BG_COLOR)
    draw.text((x,y), ts, font=font, fill=TEXT_COLOR)

    # 2) name 타이틀(Hello Perfumers와 표 사이)
    draw_centered_fit_text(
        im=im, text=name, y=NAME_Y,
        max_width_px=NAME_MAX_WIDTH_PX,
        fs_max=NAME_FS_MAX, fs_min=NAME_FS_MIN,
        color=(0,0,0,255),
        margin=NAME_SIDE_MARGIN,
        bg_color=BG_COLOR
    )

    # 3) 레시피 표
    draw_recipe_block(im, recipe)

    # 4) 프린터 폭 맞춤 + 저장
    if im.width > PRN_MAX_WIDTH:
        nh = int(im.height * PRN_MAX_WIDTH / im.width)
        im = im.resize((PRN_MAX_WIDTH, nh), Image.LANCZOS)

    out = TIME_DIR / f"receipt_{uuid.uuid4().hex}.png"
    im.save(out)
    _prune_old(TIME_DIR, KEEP_IMAGES)
    print("[PRINT-ASSET] custom receipt composed →", out)
    return out

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
    print("[DBG] serial_done_worker 진입]")
    try:
        # PATCH 콜백
        fragrance_name = current_template.stem if current_template else None
        physical_id = PHYSICAL_ID
        print(fragrance_name)
        if fragrance_name:
            notify_cartridge_used(fragrance_name, physical_id)
        else:
            print("[PATCH cartridges] fragrance_name 없음")
        
        # 제조 완료 콜백
        print("[DBG] 콜백 URL : ", current_callback_url, "PID: ", current_production_id)
        if current_callback_url and current_production_id:
            handle_production_done(current_callback_url, current_production_id, success=True)
        else:
            print("[DEG] 콜백 미존재, 실행 안함")
    except Exception as e:
        print("[ERR] simulate_callback:", e)
    finally:
        # 인쇄
        try:
            if current_is_custom and current_recipe:
                png = compose_custom_receipt(current_perfume_name or "", current_recipe)
                if png:
                    with usb_lock:
                        p = Usb(USB_VID, USB_PID, out_ep=0x03, timeout=USB_TIMEOUT, auto_detach=True)
                        p.set(align="center")
                        p.image(str(png), impl="bitImageRaster", fragment_height=128)
                        p.cut()
                        p.close()
                        time.sleep(0.1)
                    print("[PRINT] custom receipt 인쇄 완료 →", png)
                else:
                    print("[WARN] custom receipt 생성 실패 → fallback")
                    if current_template:
                        print_receipt(current_template)
                    else:
                        print("[DONE] 템플릿 정보 없음")
            else:
                if current_template:
                    print_receipt(current_template)
                else:
                    print("[DONE] 템플릿 정보 없음")
        except Exception as e:
            print("[ERR] print_receipt/custom:", e)

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

    # 1) 알 수 없는 향료명 검증
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

    # 2) 개수 제한 (1~7개)
    if not (MIN_ING <= len(req.recipe) <= MAX_ING):
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "message": f"향료 개수는 {MIN_ING}~{MAX_ING}개여야 합니다.",
                "details": {"count": len(req.recipe)}
            },
        )

    # 3) 값 검증 및 총합 계산
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

    # 5) 템플릿/콜백/프로덕션ID/향수명 세팅
    global current_template, current_callback_url, current_production_id, current_perfume_name
    current_template = select_template(req.name, req.recipe)
    current_callback_url = req.callbackUrl
    current_production_id = req.productionId
    current_perfume_name = req.name

    # 커스텀 여부/레시피 저장
    global current_is_custom, current_recipe
    current_is_custom = (req.name not in NAME_SET)
    current_recipe = dict(req.recipe)

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
                ser.write(line.encode())  # ← 개행 포함
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