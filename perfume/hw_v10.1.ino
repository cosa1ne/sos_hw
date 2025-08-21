#include <Arduino.h>
#include <Servo.h>
#include <DHT.h>

/* ───── 매크로 설정 ───── */
#define DEBUG 0              // 1=디버그 출력, 0=출력 안 함
#define PUMP_TEST_MODE 0     // 1=시리얼 입력 테스트 모드, 0=라즈베리파이 레시피 모드

/* ───── 핀 매핑 ───── */
const uint8_t PUMP_PIN[10] = {23,25,27,29,31,33,35,37,39,41};
const uint8_t SERVO_PIN[10] = {3,4,5,6,7,8,9,10,11,12};

/* ───── DHT22 ───── */
#define DHTPIN A6
#define DHTTYPE DHT22
DHT dht(DHTPIN, DHTTYPE);

float g_tempC = 25.0;
float g_hum   = 50.0;
unsigned long lastDhtReadMs = 0;
#define DHT_PERIOD_MS 2500  // DHT 읽기 주기 (ms)

/* ───── 펌프 설정 ───── */
const unsigned int PUMP_MS_PER_ML[10] = {
  588, 550, 520, 580, 570, 580, 550, 560, 570, 550
};

#define T_COLD   24.0   // 기준 낮은 온도
#define T_HOT    32.8   // 기준 높은 온도
#define MAX_COMP 9      // 최대 보정치 (ms)

/* ───── 함수: 온도 읽기 ───── */
inline void readDhtIfDue(unsigned long now) {
  if (now - lastDhtReadMs >= DHT_PERIOD_MS) {
    lastDhtReadMs = now;
    float t = dht.readTemperature();   // °C
    float h = dht.readHumidity();      // % (안쓰지만 보관)

    if (!isnan(t)) g_tempC = t;
    if (!isnan(h)) g_hum   = h;

    #if DEBUG
    Serial.print(F("[DHT22] T="));
    Serial.print(g_tempC);
    Serial.print(F("°C, RH="));
    Serial.println(g_hum);
    #endif
  }
}

/* ───── 함수: 보정 적용 ───── */
int applyTempComp(int base_ms, int pumpIndex) {
  float t = g_tempC;
  int comp = 0;

  if (t <= T_COLD) {
    comp = 0;
  } else if (t >= T_HOT) {
    comp = MAX_COMP;
  } else {
    comp = (int)round((t - T_COLD) / (T_HOT - T_COLD) * MAX_COMP);
  }

  int corrected = base_ms - comp;

  #if DEBUG
  Serial.print("펌프 ");
  Serial.print(pumpIndex+1);
  Serial.print(" 가동! -> 현재온도 ");
  Serial.print(t, 1);
  Serial.print("°C --> 보정값 [-");
  Serial.print(comp);
  Serial.println(" ms]");
  #endif

  return corrected;
}

/* ───── 펌프 구동 ───── */
void runPump(int pumpIndex, int ml) {
  if (pumpIndex < 0 || pumpIndex >= 10) return;
  if (ml <= 0 || ml > 30) return;

  int base_ms = PUMP_MS_PER_ML[pumpIndex] * ml;
  int corrected_ms = applyTempComp(base_ms, pumpIndex);

  digitalWrite(PUMP_PIN[pumpIndex], HIGH);
  delay(corrected_ms);
  digitalWrite(PUMP_PIN[pumpIndex], LOW);
}

/* ───── 시리얼 파싱 (테스트 모드) ───── */
void handleSerial() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    int commaIndex = line.indexOf(',');
    if (commaIndex > 0) {
      int pump = line.substring(0, commaIndex).toInt();
      int ml   = line.substring(commaIndex + 1).toInt();
      runPump(pump - 1, ml);
    }
  }
}

/* ───── 초기화 ───── */
void setup() {
  Serial.begin(115200);
  for (int i = 0; i < 10; i++) {
    pinMode(PUMP_PIN[i], OUTPUT);
    digitalWrite(PUMP_PIN[i], LOW);
  }
  dht.begin();

  #if DEBUG
  Serial.println(F("[INIT] 시스템 시작"));
  #endif
}

/* ───── 루프 ───── */
void loop() {
  unsigned long now = millis();
  readDhtIfDue(now);

  #if PUMP_TEST_MODE
  handleSerial();
  #else
  // 여기서 라즈베리파이 레시피 수신 처리
  #endif
}
