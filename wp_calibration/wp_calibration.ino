// 센오사 – 워터펌프 개별 유량 보정용 스케치
// -------------------------------------------
// 각 펌프를 지정 시간만큼 동작시키고, 사용자가 계량컵으로 직접 잰
// 실제 분사량(ml)을 입력하면 ml/s 유량을 자동 계산‑저장합니다.
// 1) 시리얼 모니터(115200 baud) 열기
// 2) `p,<채널번호 1~10>,<초>` 입력 → 펌프 구동
// 3) 동작 종료 후 "ml?" 프롬프트가 나오면 측정값 입력 후 Enter
// 4) `s` 입력 시 현재 보정 테이블 확인
// 펌핑 핀 매핑은 메인 펌웨어와 동일합니다.

#define PUMP_COUNT 10
const uint8_t PUMP_PIN[PUMP_COUNT] = {23,25,27,29,31,33,35,37,39,41};

float calib[PUMP_COUNT] = {1,1,1,1,1,1,1,1,1,1}; // ml/s 초기값

void setup() {
  Serial.begin(115200);
  for (uint8_t i = 0; i < PUMP_COUNT; i++) pinMode(PUMP_PIN[i], OUTPUT);
  Serial.println(F("
=== Water Pump Calibration ==="));
  Serial.println(F("명령: p,<채널 1~10>,<구동초수>  /  s: 테이블 보기"));
}

void loop() {
  static String line;
  if (Serial.available()) {
    char c = Serial.read();
    if (c == '
' || c == '
') {
      if (line.length()) processCmd(line); line = "";
    } else line += c;
  }
}

void processCmd(String cmd) {
  cmd.trim();
  if (cmd == "s") {
    Serial.println(F("-- Calibration Table (ml/s) --"));
    for (uint8_t i = 0; i < PUMP_COUNT; i++) {
      Serial.print(i+1); Serial.print(F(": "));
      Serial.println(calib[i], 3);
    }
    return;
  }

  if (cmd.startsWith("p,")) {
    int comma1 = cmd.indexOf(',',2);
    int ch = cmd.substring(2, comma1).toInt();
    int sec = cmd.substring(comma1+1).toInt();
    if (ch < 1 || ch > PUMP_COUNT || sec <= 0) {
      Serial.println(F("잘못된 파라미터")); return; }

    uint8_t idx = ch-1;
    Serial.print(F("채널 ")); Serial.print(ch);
    Serial.print(F(" 펌프 ")); Serial.print(sec);
    Serial.println(F("초 구동 시작..."));

    digitalWrite(PUMP_PIN[idx], HIGH);
    unsigned long start = millis();
    while (millis() - start < (unsigned long)sec*1000UL) {
      // 간단히 블로킹 – 캘리브 전용
    }
    digitalWrite(PUMP_PIN[idx], LOW);
    Serial.println(F("=> 완료. 컵에 담긴 액체 양(ml)을 입력하세요:"));

    // 사용자로부터 ml 입력 대기
    String mlLine;
    while (true) {
      if (Serial.available()) {
        char c = Serial.read();
        if (c == '
' || c == '
') {
          if (mlLine.length()) break;  // 입력 완료
        } else mlLine += c;
      }
    }
    float ml = mlLine.toFloat();
    if (ml <= 0) { Serial.println(F("0보다 큰 값을 입력하세요.")); return; }

    calib[idx] = ml / sec;
    Serial.print(F("채널 ")); Serial.print(ch);
    Serial.print(F(" 유량 → ")); Serial.print(calib[idx], 3);
    Serial.println(F(" ml/s 로 갱신"));
  }
}