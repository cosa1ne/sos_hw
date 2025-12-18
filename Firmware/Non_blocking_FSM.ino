/*****************************************************************
 * sos_hw_v8 — 10채널 시향/제조 일체형 펌웨어 (로드셀 없음)
 *  · QR(Serial1: RX1=19) 문자열 → USB 시리얼 패스스루
 *  · 시향 센서 Low 감지 → 대응 SG90로 분사(1회)
 *  · 제조 요청(10개 mL) 수신 → 펌프 타임 제어(무게 미확인)
 *  · 스텝모터 Z축(노즐) 리프팅, 공병 덮개 서보 자동화
 *  · 완료 후 "#DONE\n" 전송 → 라즈베리파이 영수증 프린터 트리거
 *  · DHT22 온도센서 5초마다 읽기
 *  · [컴파일 토글] ENABLE_TEST_INPUT: "채널,ml" 테스트 입력 허용/차단
 *  · [컴파일 토글] DEBUG_MODE: DHT22 온도 시리얼 출력 제어
 *****************************************************************/
#include <Arduino.h>
#include <Servo.h>
#include <DHT.h>

/* ===== 컴파일 타임 토글 ===== */
#define ENABLE_TEST_INPUT 0   // 1=테스트 입력 허용, 0=무시
#define DEBUG_MODE 0          // 1=DHT22 온도 시리얼 출력 활성화, 0=비활성화

/* ───── 핀 매핑 ───── */
// 시향 SG90 서보
const uint8_t SERVO_PIN[10] = {3,4,5,6,7,8,9,10,11,12};
// 제조(믹싱) 서보
#define BOTTLE_SERVO_PIN 2
// DHT22
#define DHTPIN 48
#define DHTTYPE DHT22
DHT dht(DHTPIN, DHTTYPE);

// 시향 센서(TCRT5000, LOW=감지)
const uint8_t PROX_PIN[10] = {26,28,30,32,34,36,38,40,42,44};
// 공병 감지
#define BOTTLE_PROX_PIN  A0
// NEMA17 (A4988)
#define STEP_PIN 43
#define DIR_PIN  45
#define LIM_TOP  22
#define LIM_BOT  24
#define DIR_UP   false
#define DIR_DOWN true
// 워터펌프 MOSFET (IRLZ44N) — 로우사이드, HIGH=ON
const uint8_t PUMP_PIN[10] = {23,25,27,29,31,33,35,37,39,41};

/* ───── 펌프 기준(ms/mL, 기준온도에서) ───── */
const unsigned int PUMP_MS_PER_ML[10] = {
  588, 550, 520, 580, 570, 580, 550, 560, 570, 550
};

/* ───── 온도 보정(클램핑 + 선형) ─────
   24°C 이하 보정 0, 32.8°C 이상 최대 보정 유지
   2~6번: 최대 -30 ms/mL, 나머지(1,7,8,9,10): 최대 -9 ms/mL
*/
const float TEMP_MIN = 24.0f;
const float TEMP_MAX = 32.8f;
const int16_t MAX_COMPENSATION[10] = {
  -9,  -30, -30, -30, -30, -30,  -9,  -9,  -9,  -9
};
const unsigned long PUMP_TIME_MIN_MS = 50;   // 안전 하한
const float V_MIN = 0.0f, V_MAX = 30.0f;     // 볼륨 제한

/* ───── 동작 파라미터 ───── */
const uint8_t  START_DEG = 140;
const uint8_t  END_DEG[10] = {102,93,80,100,95,100,85,100,94,83};
const uint32_t HOLD_MS = 600;
const uint32_t DEBOUNCE_MS = 30;
const uint32_t RETRIGGER_GUARD_MS = 500;
const uint8_t  BOTTLE_OPEN_DEG  = 0;
const uint8_t  BOTTLE_CLOSE_DEG = 70;
const uint32_t BOTTLE_HOLD_MS   = 800;

/* ───── QR 리더기 ───── */
#define QR_BAUD 9600  // Serial1 (RX1=19)

/* ───── 전역 상태 ───── */
float pumpVolume[10] = {0.0f};
bool  recipeReady    = false;   // CSV 10개 수신 시 true
bool  isTestMode     = false;   // "채널,ml" 입력 처리 중

Servo servos[10], bottleServo;
bool     servoBusy[10]    = {false};
uint32_t servoStartMs[10] = {0};
bool     proxStable[10]   = {false};
bool     proxPrevLow[10]  = {false};
bool     proxTriggered[10]= {false};
uint32_t proxEdgeMs[10]   = {0};
uint32_t proxLastMs[10]   = {0};

enum class JobState {IDLE, MOVING_DOWN, AT_BOTTOM, DISPENSING, MOVING_UP};
JobState job = JobState::IDLE;

const char* RECEIPT_DONE = "#DONE\n";

/* ───── DHT22 상태 ───── */
volatile float g_tempC = NAN, g_hum = NAN;
uint32_t lastDhtReadMs = 0;
const uint32_t DHT_PERIOD_MS = 5000; // 5초

/* ───── 헬퍼 ───── */
inline void stepPulse() {
  digitalWrite(STEP_PIN, HIGH); delayMicroseconds(450);
  digitalWrite(STEP_PIN, LOW);  delayMicroseconds(450);
}
void moveUntilLimit(bool dir, uint8_t limitPin) {
  digitalWrite(DIR_PIN, dir);
  while (digitalRead(limitPin) == HIGH) stepPulse();
}
inline void readDhtIfDue(uint32_t now) {
  if (now - lastDhtReadMs >= DHT_PERIOD_MS) {
    lastDhtReadMs = now;
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (!isnan(t)) g_tempC = t;
    if (!isnan(h)) g_hum   = h;
    
    #if DEBUG_MODE
    Serial.print(F("[DHT22] T=")); Serial.print(g_tempC,1);
    Serial.print(F("°C, RH=")); Serial.println(g_hum,1);
    #endif
  }
}

/* ───── 보정된 펌프 시간 계산 ─────
 * base = volume * PUMP_MS_PER_ML[ch]
 * ratio = 0 (T<=24), (T-24)/(32.8-24) (중간), 1 (T>=32.8)
 * Δ(ms) = (MAX_COMP[ch] * ratio) * volume
 * 최종 = base + Δ,  최종 하한 = PUMP_TIME_MIN_MS
 */
unsigned long getCompensatedPumpTime(uint8_t channel, float volume) {
  if (channel >= 10) return 0;

  // 볼륨 클램프
  if (volume < V_MIN) volume = V_MIN;
  if (volume > V_MAX) volume = V_MAX;

  unsigned long baseTime = (unsigned long)(volume * PUMP_MS_PER_ML[channel]);

  // 온도 유효성
  float Traw = g_tempC;
  if (isnan(Traw)) {
    // 온도 못 읽었으면 보정 0
    Serial.print(F("[CH")); Serial.print(channel+1); Serial.print(F("] 펌프 가동! -> 현재온도 N/A → 보정값 [0 ms]  최종 "));
    Serial.print(baseTime); Serial.println(F(" ms"));
    return baseTime < PUMP_TIME_MIN_MS ? PUMP_TIME_MIN_MS : baseTime;
  }

  // 클램핑
  float Tc = Traw;
  if (Tc < TEMP_MIN) Tc = TEMP_MIN;
  if (Tc > TEMP_MAX) Tc = TEMP_MAX;

  // 비율(0~1)
  float ratio = 0.0f;
  if (TEMP_MAX > TEMP_MIN) ratio = (Tc - TEMP_MIN) / (TEMP_MAX - TEMP_MIN);
  if (ratio < 0.0f) ratio = 0.0f;
  if (ratio > 1.0f) ratio = 1.0f;

  // ms/mL 보정 → 총 ms 보정
  float delta_ms_per_ml = (float)MAX_COMPENSATION[channel] * ratio; // 음수면 시간 줄이기
  long  delta_total_ms  = (long)(delta_ms_per_ml * volume);         // 이번 명령 전체 보정 ms
  long  compensated     = (long)baseTime + delta_total_ms;

  if (compensated < (long)PUMP_TIME_MIN_MS) compensated = (long)PUMP_TIME_MIN_MS;

  // 디버그 출력
  Serial.print(F("[CH")); Serial.print(channel+1); Serial.print(F("] 펌프 가동! -> 현재온도 "));
  Serial.print(Traw, 1); Serial.print(F("도  → 적용온도 "));
  Serial.print(Tc, 1);   Serial.print(F("도  보정값 ["));
  Serial.print(delta_total_ms); Serial.print(F(" ms]  최종 "));
  Serial.print(compensated); Serial.println(F(" ms"));

  return (unsigned long)compensated;
}

/* ───── 레시피 파싱 ─────
 *  1) "채널,ml"  → 즉시 펌프(테스트 모드, ENABLE_TEST_INPUT=1일 때만)
 *  2) CSV 10개  → recipeReady=true (일반 제조 모드)
 */
bool parseRecipeLine(char *line) {
  // 공백 트림
  while (*line==' '||*line=='\t') ++line;
  int n = strlen(line);
  while (n>0 && (line[n-1]==' '||line[n-1]=='\t')) line[--n]='\0';

  // 쉼표 개수 세기
  int commaCount = 0;
  for (char *p=line; *p; ++p) if (*p==',') ++commaCount;

  if (commaCount == 1) {
    // "채널,ml"
    #if ENABLE_TEST_INPUT
      int ch = -1;
      float ml = -1;
      if (sscanf(line, "%d,%f", &ch, &ml) == 2) {
        if (ch >= 1 && ch <= 10 && ml > 0.0f) {
          isTestMode = true;
          // 볼륨 클램프
          if (ml > V_MAX) ml = V_MAX;
          if (ml < V_MIN) ml = V_MIN;

          // 해당 채널만 즉시 펌프
          uint8_t idx = (uint8_t)(ch - 1);
          unsigned long runMs = getCompensatedPumpTime(idx, ml);
          Serial.print(F("[TEST] CH")); Serial.print(ch);
          Serial.print(F(" ")); Serial.print(ml,2); Serial.print(F("mL → "));
          Serial.print(runMs); Serial.println(F("ms"));

          // 안전: 다른 채널 OFF
          for (uint8_t i=0;i<10;++i) digitalWrite(PUMP_PIN[i], LOW);
          delay(2);

          digitalWrite(PUMP_PIN[idx], HIGH);
          delay(runMs);
          digitalWrite(PUMP_PIN[idx], LOW);

          Serial.println(F("[TEST] done"));

          // ✅ 테스트 1회 완료 후 즉시 정상 모드 복귀
          isTestMode = false;
          return true;
        }
      }
      Serial.println(F("[TEST][ERR] Usage: <ch 1-10>,<ml>"));
      return false;
    #else
      Serial.println(F("[TEST] 비활성화됨 (ENABLE_TEST_INPUT=0)"));
      return false;
    #endif
  }

  // CSV 10개
  if (commaCount == 9) {
    char *tok = strtok(line, ",");
    uint8_t i = 0;
    while (tok && i < 10) {
      float v = atof(tok);
      if (v < V_MIN) v = V_MIN;
      if (v > V_MAX) v = V_MAX;
      pumpVolume[i++] = v;
      tok = strtok(NULL, ",");
    }
    if (i == 10) {
      isTestMode = false;
      recipeReady = true;
      Serial.println(F("[RECIPE] 수신 완료 → 공병 대기"));
      return true;
    }
  }

  Serial.println(F("[ERR] 파싱 실패"));
  return false;
}

/* ───── 레시피 펌프 동작 ───── */
void dispenseRecipe() {
  Serial.println(F("  ▶ 펌프 조제 시작"));
  unsigned long start = millis();
  unsigned long pumpStopAt[10] = {0};

  for (uint8_t i=0;i<10;++i) {
    if (pumpVolume[i] <= 0.0f) continue;
    unsigned long runMs = getCompensatedPumpTime(i, pumpVolume[i]);
    digitalWrite(PUMP_PIN[i], HIGH);         // HIGH=ON
    pumpStopAt[i] = start + runMs;
  }

  bool anyRunning;
  do {
    anyRunning = false;
    unsigned long now = millis();
    for (uint8_t i=0;i<10;++i) {
      if (pumpStopAt[i] && now >= pumpStopAt[i]) {
        digitalWrite(PUMP_PIN[i], LOW);
        pumpStopAt[i] = 0;
      }
      if (pumpStopAt[i]) anyRunning = true;
    }
  } while (anyRunning);

  Serial.println(F("  ▶ 펌프 조제 완료"));
}

/* ───── 초기 Homing ───── */
void systemHoming() {
  Serial.println(F("\n[INIT] 스텝모터 ↑ Homing"));
  if (digitalRead(LIM_TOP) == HIGH) moveUntilLimit(DIR_UP, LIM_TOP);

  bottleServo.attach(BOTTLE_SERVO_PIN);
  bottleServo.write(BOTTLE_CLOSE_DEG);

  for (uint8_t i=0;i<10;++i) {
    servos[i].attach(SERVO_PIN[i]);
    servos[i].write(START_DEG);
  }
  delay(300);
  bottleServo.detach();
  for (uint8_t i=0;i<10;++i) servos[i].detach();

  Serial.println(F("[INIT] 서보 초기화 완료"));
}

/* ───── Arduino 기본 ───── */
void setup() {
  Serial.begin(115200);
  Serial1.begin(QR_BAUD);

  for (uint8_t i=0;i<10;++i) {
    pinMode(PROX_PIN[i], INPUT);
    pinMode(SERVO_PIN[i], OUTPUT);
    pinMode(PUMP_PIN[i], OUTPUT);
    digitalWrite(PUMP_PIN[i], LOW);
  }
  pinMode(BOTTLE_PROX_PIN, INPUT);
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(LIM_TOP, INPUT_PULLUP);
  pinMode(LIM_BOT, INPUT_PULLUP);

  dht.begin();
  systemHoming();

  Serial.print(F("[CONFIG] TEST INPUT: "));
  Serial.println(ENABLE_TEST_INPUT ? F("ENABLED") : F("DISABLED"));
  Serial.print(F("[CONFIG] DEBUG MODE: "));
  Serial.println(DEBUG_MODE ? F("ENABLED") : F("DISABLED"));
  Serial.println(F("<대기> CSV(10개 mL) 또는 '채널,ml' 입력"));
  Serial.println(F("예) 1,15  /  10,30  /  15,0,0,0,0,0,0,0,0,0"));
}

void loop() {
  uint32_t now = millis();
  readDhtIfDue(now);

  // 1) QR pass-through
  while (Serial1.available()) {
    char c = Serial1.read();
    Serial.write(c);
    if (c == '\r') Serial.write('\n');
  }

  // 2) 입력 수신
  static char lineBuf[96];
  static uint8_t bufIdx = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (bufIdx) {
        lineBuf[bufIdx] = '\0';
        parseRecipeLine(lineBuf);
        bufIdx = 0;
      }
    } else if (bufIdx < sizeof(lineBuf) - 1) {
      lineBuf[bufIdx++] = c;
    }
  }

  // 3) 시향 센서 → SG90 (1회 분사)
  for (uint8_t ch=0; ch<10; ++ch) {
    bool rawLow = !digitalRead(PROX_PIN[ch]);
    if (rawLow && !proxPrevLow[ch]) proxEdgeMs[ch] = now;

    if (rawLow && !proxStable[ch] && !proxTriggered[ch] &&
        now - proxEdgeMs[ch] >= DEBOUNCE_MS &&
        now - proxLastMs[ch] >= RETRIGGER_GUARD_MS) {
      proxStable[ch]    = true;
      proxLastMs[ch]    = now;
      proxTriggered[ch] = true;

      servos[ch].attach(SERVO_PIN[ch]);
      servos[ch].write(END_DEG[ch]);
      servoBusy[ch]     = true;
      servoStartMs[ch]  = now;
    }

    if (!rawLow) {
      proxStable[ch]    = false;
      proxTriggered[ch] = false;
    }
    proxPrevLow[ch] = rawLow;
  }

  // 4) 공병 감지 + 제조 FSM (테스트 입력 중이 아니고, 레시피 준비됐을 때)
  if (!isTestMode) {
    bool bottleLow = !digitalRead(BOTTLE_PROX_PIN);

    switch (job) {
      case JobState::IDLE:
        if (bottleLow && recipeReady) job = JobState::MOVING_DOWN;
        break;

      case JobState::MOVING_DOWN:
        moveUntilLimit(DIR_DOWN, LIM_BOT);
        bottleServo.attach(BOTTLE_SERVO_PIN);
        bottleServo.write(BOTTLE_OPEN_DEG); delay(BOTTLE_HOLD_MS);
        job = JobState::AT_BOTTOM;
        break;

      case JobState::AT_BOTTOM:
        job = JobState::DISPENSING;
        break;

      case JobState::DISPENSING:
        dispenseRecipe();
        delay(2000);
        bottleServo.write(BOTTLE_CLOSE_DEG); delay(1000);
        bottleServo.detach();
        job = JobState::MOVING_UP;
        break;

      case JobState::MOVING_UP:
        moveUntilLimit(DIR_UP, LIM_TOP);
        digitalWrite(DIR_PIN, DIR_DOWN);
        for (uint16_t i=0;i<100;++i) stepPulse();

        Serial.print(RECEIPT_DONE); Serial.flush();

        recipeReady = false;
        memset(pumpVolume, 0, sizeof(pumpVolume));
        job = JobState::IDLE;
        break;
    }
  }

  // 5) 시향 서보 복귀
  for (uint8_t ch=0; ch<10; ++ch) {
    if (servoBusy[ch] && now - servoStartMs[ch] >= HOLD_MS) {
      servos[ch].write(START_DEG); delay(100);
      servos[ch].detach();
      servoBusy[ch] = false;
    }
  }
}