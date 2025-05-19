from escpos.printer import Usb
from datetime import datetime

PRN = Usb(0x0416, 0x5011, out_ep=0x03, timeout=0)   # VID/PID 수정

def print_receipt(recipe: dict):
    PRN.set(align='center', text_type='B', width=2, height=2)
    PRN.text("센오사 향수\n")
    PRN.set(align='left')
    PRN.text(f"이름: {recipe['name']}\n")
    for i, ml in enumerate(recipe['ml']):
        if ml:
            PRN.text(f"채널 {i+1}: {ml} ml\n")
    PRN.text(f"완료: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    PRN.cut()