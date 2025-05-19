#include <Arduino.h>
#include <Servo.h>

/* ---------------- 핀 정의 ---------------- */
#define PROX_PIN            A6    // 중앙 광학 센서
#define LIM_TOP             13
#define LIM_BOT             22
#define STEP_PIN            43
#define DIR_PIN             45
#define SNIFF_PROX_PIN       4    // 시향지 감지 TCRT5000
#define SNIFF_SERVO_PIN      5    // MG996R
#define BOTTLE_PROX_PIN      2    // 공병 감지 TCRT5000
#define FILL_SERVO_PIN       6    // MG90S (필링 가이드)

const uint8_t WL_PIN[10]       = {24,30,32,36,40,48,44,55,57,59};
const uint8_t SERVO_PIN[10]    = {3, 7,8,9,10,11,12,14,15,16};   // 여유·예시
const uint8_t PROX_PIN_ARR[10] = {26,28,34,38,42,50,46,54,56,58};
const uint8_t PUMP_PIN[10]     = {23,25,27,29,31,33,35,37,39,41};

/* ---------------- 동작 상수 ---------------- */
const float    ML_PER_SEC    = 1.0;      // 펌프 유량 (ml/s)
const uint16_t STEP_DELAY_US = 400;      // 스테퍼 최고속도 (토크 모자라면 ↑)

/* 서보 각도 */
const uint8_t SNIFF_OPEN  = 90;
const uint8_t SNIFF_CLOSE = 0;
const uint8_t FILL_OPEN   = 90;
const uint8_t FILL_CLOSE  = 0;

/* ---------------- 데이터 구조 ---------------- */
struct Recipe {
  uint16_t ml[10];
  uint8_t  idx;             // 향수 이름 인덱스 0‑9
};

enum State {
  IDLE,
  /* 시향 */      SNIFF_ROTATE, SNIFF_WAIT,
  /* 제작 */     WAIT_RECIPE, DESCEND, FILL_OPEN_STATE, PUMPING, ASCEND, DONE
};

State state = IDLE;
Recipe cur;
uint8_t curChan = 0;
unsigned long stepTimer = 0, pumpTimer = 0, sniffTimer = 0;
volatile bool curRecipeReady = false;

/* 이름 테이블 (UTF‑8) */
const char* const NAMES[10] = {
  u8"감나무", u8"경포대", u8"밤장미", u8"배롱나무", u8"벛꽃",
  u8"소나무", u8"안목해변", u8"은행나무", u8"차수국", u8"태백산맥"
};

Servo sniffServo;   // MG996R
Servo fillServo;    // MG90S

/* ---------------- 초기화 ---------------- */
void setup() {
  Serial.begin(115200);      // Pi USB CDC
  Serial1.begin(9600);     // QR 스캐너 TTL (TX→19)

  pinMode(LIM_TOP, INPUT_PULLUP);
  pinMode(LIM_BOT, INPUT_PULLUP);
  pinMode(PROX_PIN, INPUT_PULLUP);
  pinMode(SNIFF_PROX_PIN, INPUT_PULLUP);
  pinMode(BOTTLE_PROX_PIN, INPUT_PULLUP);

  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  digitalWrite(DIR_PIN, HIGH); // 기본 상승 방향

  for (uint8_t i = 0; i < 10; i++) {
    pinMode(PUMP_PIN[i], OUTPUT);
  }

  sniffServo.attach(SNIFF_SERVO_PIN);
  fillServo.attach(FILL_SERVO_PIN);
  sniffServo.write(SNIFF_CLOSE);
  fillServo.write(FILL_CLOSE);
}

/* ---------------- 헬퍼 ---------------- */
void stepperPulse() {
  if (micros() - stepTimer >= STEP_DELAY_US) {
    digitalWrite(STEP_PIN, HIGH);
    delayMicroseconds(2);
    digitalWrite(STEP_PIN, LOW);
    stepTimer = micros();
  }
}

/* QR → Pi 브리지 */
void bridgeQR() {
  static char buf[64];
  static uint8_t idx = 0;
  while (Serial1.available()) {
    char c = Serial1.read();
    if (c == '\n' || c == '\r') {
      if (idx) { buf[idx] = '\0'; Serial.println(buf); idx = 0; }
    } else if (idx < sizeof(buf) - 1) {
      buf[idx++] = c;
    }
  }
}

/* 레시피 수신 */
void recvRecipe() {
  static char buf[64];
  static uint8_t idx = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      buf[idx] = '\0';
      char* tok = strtok(buf, ",");
      for (uint8_t i = 0; i < 10 && tok; i++) { cur.ml[i] = atoi(tok); tok = strtok(NULL, ","); }
      cur.idx = tok ? atoi(tok) : 0;
      curRecipeReady = true;
      idx = 0;
    } else if (idx < sizeof(buf) - 1) {
      buf[idx++] = c;
    }
  }
}

/* ---------------- 메인 루프 ---------------- */
void loop() {
  bridgeQR();
  recvRecipe();

  /* 시향 이벤트 */
  if (digitalRead(SNIFF_PROX_PIN) == HIGH && state == IDLE) {
    state = SNIFF_ROTATE;
  }

  /* 제작 조건 (레시피+공병) */
  if (curRecipeReady && digitalRead(BOTTLE_PROX_PIN) == HIGH && state == IDLE) {
    state = WAIT_RECIPE;
    curRecipeReady = false;    // 소비
  }

  switch (state) {
    case IDLE:
      break;

    /* ===== 시향 ===== */
    case SNIFF_ROTATE:
      sniffServo.write(SNIFF_OPEN);
      sniffTimer = millis();
      state = SNIFF_WAIT;
      break;

    case SNIFF_WAIT:
      if (millis() - sniffTimer >= 3000) {
        sniffServo.write(SNIFF_CLOSE);
        state = IDLE;
      }
      break;

    /* ===== 향수 제작 ===== */
    case WAIT_RECIPE:
      digitalWrite(DIR_PIN, LOW); // ↓
      state = DESCEND;
      break;

    case DESCEND:
      if (digitalRead(LIM_BOT) == LOW) {
        fillServo.write(FILL_OPEN);
        delay(300);
        curChan = 0;
        pumpTimer = millis();
        state = PUMPING;
      } else {
        stepperPulse();
      }
      break;

    case PUMPING:
      if (curChan >= 10) {
        fillServo.write(FILL_CLOSE);
        digitalWrite(DIR_PIN, HIGH); // ↑
        state = ASCEND;
      } else if (cur.ml[curChan] == 0) {
        curChan++;
      } else {
        digitalWrite(PUMP_PIN[curChan], HIGH);
        if (millis() - pumpTimer >= cur.ml[curChan] * 1000 / ML_PER_SEC) {
          digitalWrite(PUMP_PIN[curChan], LOW);
          curChan++;
          pumpTimer = millis();
        }
      }
      break;

    case ASCEND:
      if (digitalRead(LIM_TOP) == LOW) {
        stepperPulse();
      } else {
        Serial.println("OK");
        state = DONE;
      }
      break;

    case DONE:
      state = IDLE;
      break;
  }
}