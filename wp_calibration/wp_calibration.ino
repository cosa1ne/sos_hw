// 센오사 – 워터펌프 개별 보정 스케치 (sec / ml 방식)
// ----------------------------------------------------
// ┏ 사용 방법 ┓
// 1. 시리얼 모니터(115200) 열기.
// 2. ▶  p,<채널 1~10>,<구동초>  : 펌프 가동 테스트.
// 3.       펌프가 멈추면 측정한 실제 분사량(ml)을 입력 → Enter.
// 4. ▶  s                       : 현재 보정 테이블(sec/ml) 확인.
// 측정이 끝나면 출력되는 배열을 perfumemaker/perfume.ino 의
// SEC_PER_ML[...] 초기값에 복사‑붙여넣기 하세요.

#define PUMP_COUNT 10
const uint8_t PUMP_PIN[PUMP_COUNT] = {23,25,27,29,31,33,35,37,39,41};
float secPerMl[PUMP_COUNT] = {1,1,1,1,1,1,1,1,1,1};

void setup() {
  Serial.begin(115200);
  for (uint8_t i = 0; i < PUMP_COUNT; i++) pinMode(PUMP_PIN[i], OUTPUT);
  Serial.println(F("\n=== Pump Calibration (sec / ml) ==="));
  Serial.println(F("명령: p,<채널>,<초>   |   s: 테이블"));
}

void loop() {
  static String line;
  if (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (line.length()) handleCmd(line);
      line = "";
    } else line += c;
  }
}}

void handleCmd(String cmd) {
  cmd.trim();
  if (cmd == "s") {
    Serial.println(F("-- Pump sec/ml --"));
    for (uint8_t i = 0; i < PUMP_COUNT; i++) {
      Serial.print(i+1); Serial.print(F(": "));
      Serial.println(secPerMl[i], 3);
    }
    return;
  }

  if (cmd.startsWith("p,")) {
    int c1 = cmd.indexOf(',',2);
    if (c1 < 0) return;
    uint8_t ch = cmd.substring(2, c1).toInt();
    uint16_t sec = cmd.substring(c1+1).toInt();
    if (ch < 1 || ch > PUMP_COUNT || sec == 0) {
      Serial.println(F("파라미터 오류")); return; }

    uint8_t idx = ch-1;
    Serial.print("CH"); Serial.print(ch);
    Serial.print(F(" 펌프 ")); Serial.print(sec);
    Serial.println(F(" s 가동"));

    digitalWrite(PUMP_PIN[idx], HIGH);
    delay(sec * 1000UL);
    digitalWrite(PUMP_PIN[idx], LOW);

    Serial.println(F("실제 분사량(ml)을 입력: "));
    String mlLine;
    while (true) {
      if (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') { if (mlLine.length()) break; }
        else mlLine += c;
      }
    }
    float ml = mlLine.toFloat();
    if (ml <= 0) { Serial.println(F("0 < ml")); return; }

    secPerMl[idx] = (float)sec / ml;
    Serial.print(F("CH")); Serial.print(ch);
    Serial.print(F(" → ")); Serial.print(secPerMl[idx], 3);
    Serial.println(F(" sec/ml"));
  }
}