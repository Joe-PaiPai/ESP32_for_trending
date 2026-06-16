#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <SPIFFS.h>
#include <Wire.h>
#include <time.h>

#include "ST7305_U8g2.h"

#define LCD_WIDTH 400
#define LCD_HEIGHT 300

#define RLCD_SCK_PIN 11
#define RLCD_MOSI_PIN 12
#define RLCD_DC_PIN 5
#define RLCD_CS_PIN 40
#define RLCD_RST_PIN 41

#define KEY_PIN 18
#define BOOT_PIN 0
#define BATTERY_ADC_PIN 4
#define I2C_SDA_PIN 13
#define I2C_SCL_PIN 14
#define SHTC3_ADDR 0x70

// Change these values before uploading to the board.
const char *WIFI_SSID = "YOUR_WIFI_NAME";
const char *WIFI_PASS = "YOUR_WIFI_PASSWORD";
const char *SCHEDULE_URL = "http://YOUR_SERVER_IP:8080/schedule.csv";

const char *AP_SSID = "TradeCalendar";
const char *AP_PASS = "12345678";
const char *SCHEDULE_PATH = "/schedule.csv";

struct TradeEvent {
  char date[11];
  char start[6];
  char end[6];
  char item[64];
  char target[32];
  char tag[8];
};

struct ButtonState {
  uint8_t pin;
  bool wasPressed;
  bool longHandled;
  uint32_t pressedAt;
};

static TradeEvent events[180];
static int eventCount = 0;
static int selectedYear = 2026;
static int selectedMonth = 5;
static int selectedDay = 1;
static bool showCoverPage = true;
static uint32_t lastInteractionMs = 0;

static ST7305_U8g2 lcd(RLCD_SCK_PIN, RLCD_MOSI_PIN, RLCD_DC_PIN, RLCD_CS_PIN, RLCD_RST_PIN);
static U8G2 *u8g2 = nullptr;
static WebServer server(80);
static File uploadFile;

static bool isLeapYear(int year) {
  return (year % 4 == 0 && year % 100 != 0) || (year % 400 == 0);
}

static int daysInMonth(int year, int month) {
  static const int days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  if (month == 2 && isLeapYear(year)) {
    return 29;
  }
  return days[month - 1];
}

static int dayOfWeek(int year, int month, int day) {
  if (month < 3) {
    month += 12;
    year -= 1;
  }
  int k = year % 100;
  int j = year / 100;
  int h = (day + (13 * (month + 1)) / 5 + k + k / 4 + j / 4 + 5 * j) % 7;
  return (h + 6) % 7;
}

static void trim(String &s) {
  s.trim();
  if (s.startsWith("\"") && s.endsWith("\"") && s.length() >= 2) {
    s = s.substring(1, s.length() - 1);
  }
}

static void copyField(char *dest, size_t destSize, const String &src) {
  String s = src;
  trim(s);
  s.toCharArray(dest, destSize);
}

static int splitCsvLine(const String &line, String fields[], int maxFields) {
  int count = 0;
  bool inQuotes = false;
  String current;
  for (size_t i = 0; i < line.length(); i++) {
    char c = line[i];
    if (c == '"') {
      inQuotes = !inQuotes;
    } else if (c == ',' && !inQuotes) {
      if (count < maxFields) {
        fields[count++] = current;
      }
      current = "";
    } else {
      current += c;
    }
  }
  if (count < maxFields) {
    fields[count++] = current;
  }
  return count;
}

static bool parseDate(const char *date, int &year, int &month, int &day) {
  return sscanf(date, "%d-%d-%d", &year, &month, &day) == 3;
}

static int minutesOfDay(const char *timeText) {
  int h = 0;
  int m = 0;
  if (sscanf(timeText, "%d:%d", &h, &m) != 2) {
    return 0;
  }
  return h * 60 + m;
}

static time_t eventEndTime(const TradeEvent &event) {
  int y, mo, d;
  if (!parseDate(event.date, y, mo, d)) {
    return 0;
  }
  int endMinutes = minutesOfDay(event.end);
  struct tm t = {};
  t.tm_year = y - 1900;
  t.tm_mon = mo - 1;
  t.tm_mday = d;
  t.tm_hour = endMinutes / 60;
  t.tm_min = endMinutes % 60;
  t.tm_sec = 0;
  return mktime(&t);
}

static bool hasTime() {
  time_t now = time(nullptr);
  return now > 1700000000;
}

static bool eventIsUpcoming(const TradeEvent &event) {
  return true;
  if (!hasTime()) {
    return true;
  }
  return eventEndTime(event) >= time(nullptr);
}

static bool eventVisibleOnBoard(const TradeEvent &event) {
  if (strstr(event.item, "多日发电合同转让交易")) return false;
  if (strstr(event.item, "多日代购合同转让交易")) return false;
  if (strstr(event.item, "多日代购交易")) return false;
  if (strstr(event.item, "多日投放交易")) return false;
  return true;
}

static bool eventOnSelectedDate(const TradeEvent &event) {
  int y, mo, d;
  return parseDate(event.date, y, mo, d) && y == selectedYear && mo == selectedMonth && d == selectedDay;
}

static bool hasEventOnDay(int year, int month, int day) {
  char date[11];
  snprintf(date, sizeof(date), "%04d-%02d-%02d", year, month, day);
  for (int i = 0; i < eventCount; i++) {
    if (strcmp(events[i].date, date) == 0 && eventIsUpcoming(events[i]) && eventVisibleOnBoard(events[i])) {
      return true;
    }
  }
  return false;
}

static int countUpcomingOnSelectedDate() {
  int count = 0;
  for (int i = 0; i < eventCount; i++) {
    if (eventOnSelectedDate(events[i]) && eventIsUpcoming(events[i]) && eventVisibleOnBoard(events[i])) {
      count++;
    }
  }
  return count;
}

static void drawCenteredText(int x, int y, int w, const char *text) {
  int textWidth = u8g2->getStrWidth(text);
  int textX = x + (w - textWidth) / 2;
  if (textX < x) {
    textX = x;
  }
  u8g2->drawStr(textX, y, text);
}

static const char *displayItemName(const char *item);

static const char *shortItemName(const char *item) {
  if (strstr(item, "发电合同转让")) return "发转";
  if (strstr(item, "代购合同转让")) return "代转";
  if (strstr(item, "用电合同转让")) return "用转";
  if (strstr(item, "绿电")) return "绿电";
  if (strstr(item, "代购")) return "代购";
  if (strstr(item, "投放")) return "投放";
  if (strstr(item, "直接")) return "直购";
  if (strstr(item, "月度")) return "月度";
  if (strstr(item, "多月")) return "多月";
  return displayItemName(item);
}

static void drawCenteredUTF8(int x, int y, int w, const char *text) {
  int textWidth = u8g2->getUTF8Width(text);
  int textX = x + (w - textWidth) / 2;
  if (textX < x) {
    textX = x;
  }
  u8g2->drawUTF8(textX, y, text);
}

static const char *eventTagText(const TradeEvent &event) {
  if (event.tag[0]) {
    return event.tag;
  }
  if (strstr(event.item, "集中") || strstr(event.item, "撮合")) return "集";
  if (strstr(event.item, "挂牌")) return "挂";
  if (strstr(event.item, "双边")) return "双";
  return "交";
}

static int collectEventsOnDay(int year, int month, int day, int indexes[], int maxIndexes) {
  char date[11];
  int count = 0;
  snprintf(date, sizeof(date), "%04d-%02d-%02d", year, month, day);
  for (int i = 0; i < eventCount; i++) {
    if (strcmp(events[i].date, date) == 0 && eventIsUpcoming(events[i]) && eventVisibleOnBoard(events[i])) {
      if (count < maxIndexes) {
        indexes[count] = i;
      }
      count++;
    }
  }
  return count;
}

static void compactTargetText(const char *target, char *out, size_t outSize) {
  int y1 = 0;
  int m1 = 0;
  int d1 = 0;
  int y2 = 0;
  int m2 = 0;
  int d2 = 0;
  if (!target[0]) {
    snprintf(out, outSize, "-");
  } else if (sscanf(target, "%d-%d-%d~%d-%d-%d", &y1, &m1, &d1, &y2, &m2, &d2) == 6) {
    snprintf(out, outSize, "%02d-%02d~%02d-%02d", m1, d1, m2, d2);
  } else if (sscanf(target, "%d-%d-%d", &y1, &m1, &d1) == 3) {
    snprintf(out, outSize, "%02d-%02d", m1, d1);
  } else {
    snprintf(out, outSize, "%s", target);
  }
}

static float readBatteryVoltage() {
  uint32_t mv = analogReadMilliVolts(BATTERY_ADC_PIN);
  return (mv * 3.0f) / 1000.0f;
}

static int readBatteryPercent(float voltage) {
  if (voltage <= 3.0f) {
    return 0;
  }
  if (voltage >= 4.12f) {
    return 100;
  }
  return (int)(((voltage - 3.0f) / 1.12f) * 100.0f + 0.5f);
}

static uint8_t shtc3Crc(uint8_t *data, uint8_t len) {
  uint8_t crc = 0xFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : (crc << 1);
    }
  }
  return crc;
}

static bool shtc3Command(uint16_t command) {
  Wire.beginTransmission(SHTC3_ADDR);
  Wire.write(command >> 8);
  Wire.write(command & 0xFF);
  return Wire.endTransmission() == 0;
}

static bool readShtc3(float &temperature, float &humidity) {
  if (!shtc3Command(0x3517)) {
    return false;
  }
  delay(2);
  if (!shtc3Command(0x7866)) {
    return false;
  }
  delay(20);
  if (Wire.requestFrom(SHTC3_ADDR, 6) != 6) {
    return false;
  }
  uint8_t data[6];
  for (uint8_t i = 0; i < 6; i++) {
    data[i] = Wire.read();
  }
  if (shtc3Crc(data, 2) != data[2] || shtc3Crc(data + 3, 2) != data[5]) {
    return false;
  }
  uint16_t rawTemp = (data[0] << 8) | data[1];
  uint16_t rawHum = (data[3] << 8) | data[4];
  temperature = 175.0f * rawTemp / 65536.0f - 45.0f - 4.0f;
  humidity = 100.0f * rawHum / 65536.0f;
  shtc3Command(0xB098);
  return true;
}

static int firstVisibleEventOnSelectedDate() {
  for (int i = 0; i < eventCount; i++) {
    if (eventOnSelectedDate(events[i]) && eventIsUpcoming(events[i]) && eventVisibleOnBoard(events[i])) {
      return i;
    }
  }
  return -1;
}

static void drawCoverCard(int x, int y, int w, int h) {
  u8g2->setDrawColor(1);
  u8g2->drawRBox(x, y, w, h, 12);
  u8g2->setDrawColor(0);
  u8g2->drawRFrame(x + 5, y + 5, w - 10, h - 10, 8);
}

static void drawCoverPage() {
  char text[128];
  float batteryVoltage = readBatteryVoltage();
  int batteryPercent = readBatteryPercent(batteryVoltage);
  float temperature = 0.0f;
  float humidity = 0.0f;
  bool hasWeather = readShtc3(temperature, humidity);
  struct tm nowInfo = {};
  bool timeReady = false;
  if (hasTime()) {
    time_t now = time(nullptr);
    localtime_r(&now, &nowInfo);
    timeReady = true;
  }

  u8g2->clearBuffer();
  u8g2->setDrawColor(1);

  const int pad = 8;
  const int gap = 6;
  const int topY = 36;
  const int topH = 128;
  const int bottomY = topY + topH + gap;
  const int bottomH = LCD_HEIGHT - bottomY - pad;
  const int leftW = 248;
  const int rightX = pad + leftW + gap;
  const int rightW = LCD_WIDTH - pad - rightX;
  const int bottomLeftW = 252;
  const int bottomRightX = pad + bottomLeftW + gap;
  const int bottomRightW = LCD_WIDTH - pad - bottomRightX;

  u8g2->setFont(u8g2_font_7x14B_tf);
  if (hasWeather) {
    snprintf(text, sizeof(text), "%.1fC  %.0f%%", temperature, humidity);
  } else {
    snprintf(text, sizeof(text), "--.-C  --%%");
  }
  u8g2->drawStr(10, 20, text);

  u8g2->drawRBox(278, 4, 114, 26, 13);
  u8g2->setDrawColor(0);
  if (WiFi.status() == WL_CONNECTED) {
    u8g2->drawStr(287, 22, "WiFi");
  } else {
    u8g2->drawStr(287, 22, "OFF");
  }
  u8g2->drawFrame(330, 10, 22, 12);
  u8g2->drawBox(352, 13, 3, 6);
  int topFillW = map(batteryPercent, 0, 100, 0, 18);
  if (topFillW > 0) {
    u8g2->drawBox(332, 12, topFillW, 8);
  }
  snprintf(text, sizeof(text), "%d%%", batteryPercent);
  u8g2->drawStr(360, 22, text);

  drawCoverCard(pad, topY, leftW, topH);
  if (timeReady) {
    snprintf(text, sizeof(text), "%02d:%02d", nowInfo.tm_hour, nowInfo.tm_min);
  } else {
    snprintf(text, sizeof(text), "--:--");
  }
  u8g2->setFont(u8g2_font_logisoso78_tn);
  drawCenteredText(pad, topY + 100, leftW, text);

  drawCoverCard(rightX, topY, rightW, topH);
  u8g2->setFont(u8g2_font_helvB18_tf);
  if (timeReady) {
    const char *weeks[] = {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};
    drawCenteredText(rightX, topY + 32, rightW, weeks[nowInfo.tm_wday]);
    u8g2->drawRBox(rightX + 16, topY + 42, rightW - 32, 48, 8);
    u8g2->setDrawColor(1);
    snprintf(text, sizeof(text), "%d", nowInfo.tm_mday);
    u8g2->setFont(u8g2_font_logisoso38_tn);
    drawCenteredText(rightX, topY + 83, rightW, text);
  } else {
    drawCenteredText(rightX, topY + 32, rightW, "---");
    u8g2->drawRBox(rightX + 16, topY + 42, rightW - 32, 48, 8);
    u8g2->setDrawColor(1);
    u8g2->setFont(u8g2_font_logisoso38_tn);
    drawCenteredText(rightX, topY + 83, rightW, "--");
  }
  u8g2->setFont(u8g2_font_7x14B_tf);
  if (hasWeather) {
    snprintf(text, sizeof(text), "%.0fC", temperature);
  } else {
    snprintf(text, sizeof(text), "--C");
  }
  drawCenteredText(rightX, topY + 118, rightW, text);

  drawCoverCard(pad, bottomY, bottomLeftW, bottomH);
  u8g2->setFont(u8g2_font_wqy12_t_gb2312a);
  u8g2->drawUTF8(pad + 16, bottomY + 26, "交易摘要");
  u8g2->drawHLine(pad + 14, bottomY + 32, bottomLeftW - 28);
  int visibleTotal = 0;
  int todayTotal = 0;
  int nextIndex = -1;
  for (int i = 0; i < eventCount; i++) {
    if (!eventIsUpcoming(events[i]) || !eventVisibleOnBoard(events[i])) {
      continue;
    }
    visibleTotal++;
    if (eventOnSelectedDate(events[i])) {
      todayTotal++;
    }
    if (nextIndex < 0) {
      nextIndex = i;
    }
  }
  snprintf(text, sizeof(text), "本月已载入 %d 条", visibleTotal);
  u8g2->drawUTF8(pad + 18, bottomY + 56, text);
  snprintf(text, sizeof(text), "当前选中日 %d 条", todayTotal);
  u8g2->drawUTF8(pad + 18, bottomY + 78, text);
  if (nextIndex >= 0) {
    snprintf(text, sizeof(text), "下一项 %s-%s %s", events[nextIndex].start, events[nextIndex].end, shortItemName(events[nextIndex].item));
    u8g2->drawUTF8(pad + 18, bottomY + 101, text);
  } else {
    u8g2->drawUTF8(pad + 18, bottomY + 101, "暂无可显示交易");
  }

  drawCoverCard(bottomRightX, bottomY, bottomRightW, bottomH);
  u8g2->setFont(u8g2_font_7x14B_tf);
  drawCenteredText(bottomRightX, bottomY + 28, bottomRightW, "SYSTEM");
  u8g2->drawHLine(bottomRightX + 14, bottomY + 34, bottomRightW - 28);
  u8g2->setFont(u8g2_font_6x13_tf);
  snprintf(text, sizeof(text), "%d%% %.2fV", batteryPercent, batteryVoltage);
  drawCenteredText(bottomRightX, bottomY + 60, bottomRightW, text);
  drawCenteredText(bottomRightX, bottomY + 82, bottomRightW, WiFi.status() == WL_CONNECTED ? "WiFi OK" : "WiFi OFF");
  snprintf(text, sizeof(text), "%d rows", eventCount);
  drawCenteredText(bottomRightX, bottomY + 104, bottomRightW, text);

  u8g2->setDrawColor(1);
  u8g2->sendBuffer();
}

static void drawCalendarWithList() {
  char text[96];
  char targetText[32];
  const int calendarX = 4;
  const int calendarY = 45;
  const int cellW = 37;
  const int cellH = 34;
  const int sideX = 270;
  const int sideW = LCD_WIDTH - sideX - 2;
  const char *weekdays[] = {"日", "一", "二", "三", "四", "五", "六"};

  u8g2->clearBuffer();
  u8g2->setDrawColor(1);
  u8g2->setFont(u8g2_font_wqy12_t_gb2312a);

  snprintf(text, sizeof(text), "%04d-%02d", selectedYear, selectedMonth);
  u8g2->drawUTF8(8, 14, text);
  snprintf(text, sizeof(text), "%02d日", selectedDay);
  u8g2->drawUTF8(sideX + 8, 14, text);
  u8g2->drawHLine(0, 20, LCD_WIDTH);
  u8g2->drawVLine(sideX - 7, 0, LCD_HEIGHT);

  for (int col = 0; col < 7; col++) {
    drawCenteredUTF8(calendarX + col * cellW, 40, cellW, weekdays[col]);
  }

  int firstDow = dayOfWeek(selectedYear, selectedMonth, 1);
  int totalDays = daysInMonth(selectedYear, selectedMonth);
  for (int day = 1; day <= totalDays; day++) {
    int index = firstDow + day - 1;
    int row = index / 7;
    int col = index % 7;
    int x = calendarX + col * cellW;
    int y = calendarY + row * cellH;
    int eventIndexes[4];
    int dayEventCount = collectEventsOnDay(selectedYear, selectedMonth, day, eventIndexes, 4);

    u8g2->drawFrame(x, y, cellW, cellH);
    if (day == selectedDay) {
      u8g2->drawFrame(x + 1, y + 1, cellW - 2, cellH - 2);
    }

    snprintf(text, sizeof(text), "%d", day);
    drawCenteredUTF8(x, y + 13, cellW, text);

    int badges = dayEventCount < 3 ? dayEventCount : 3;
    for (int b = 0; b < badges; b++) {
      int bx = x + 3 + b * 10;
      int by = y + 22;
      u8g2->drawBox(bx, by, 8, 8);
      u8g2->setDrawColor(0);
      drawCenteredUTF8(bx - 2, by + 8, 12, eventTagText(events[eventIndexes[b]]));
      u8g2->setDrawColor(1);
    }
  }

  int selectedIndexes[6];
  int selectedCount = 0;
  for (int i = 0; i < eventCount && selectedCount < 6; i++) {
    if (!eventOnSelectedDate(events[i]) || !eventIsUpcoming(events[i]) || !eventVisibleOnBoard(events[i])) {
      continue;
    }
    selectedIndexes[selectedCount++] = i;
  }

  int shown = 0;
  int blockStep = selectedCount > 0 ? 246 / selectedCount : 0;
  if (blockStep > 78) {
    blockStep = 78;
  }
  if (blockStep < 42) {
    blockStep = 42;
  }
  for (int n = 0; n < selectedCount; n++) {
    int i = selectedIndexes[n];
    int y = 42 + n * blockStep;
    snprintf(text, sizeof(text), "%s-%s", events[i].start, events[i].end);
    u8g2->drawUTF8(sideX, y, text);
    u8g2->drawUTF8(sideX, y + 15, events[i].item);
    compactTargetText(events[i].target, targetText, sizeof(targetText));
    snprintf(text, sizeof(text), "标的%s", targetText);
    u8g2->drawUTF8(sideX, y + 30, text);
    shown++;
  }

  if (shown == 0) {
    u8g2->drawUTF8(sideX, 42, "暂无交易");
  }

  u8g2->setFont(u8g2_font_6x13_tf);
  u8g2->drawStr(6, 294, "KEY next  BOOT prev  hold month");
  u8g2->sendBuffer();
}

static void drawTradeListOnly() {
  char text[96];
  u8g2->clearBuffer();
  u8g2->setDrawColor(1);
  u8g2->setFont(u8g2_font_wqy12_t_gb2312a);

  int y = 18;
  int shown = 0;
  for (int i = 0; i < eventCount && shown < 13; i++) {
    if (!eventOnSelectedDate(events[i]) || !eventIsUpcoming(events[i]) || !eventVisibleOnBoard(events[i])) {
      continue;
    }
    snprintf(text, sizeof(text), "%s-%s %s", events[i].start, events[i].end, shortItemName(events[i].item));
    u8g2->drawUTF8(4, y, text);
    y += 21;
    shown++;
  }

  if (shown == 0) {
    snprintf(text, sizeof(text), "%04d-%02d-%02d 暂无交易", selectedYear, selectedMonth, selectedDay);
    u8g2->drawUTF8(4, 28, text);
  }

  u8g2->sendBuffer();
}

static void drawCalendarPage() {
  char text[64];
  const int left = 18;
  const int top = 64;
  const int cellW = 52;
  const int cellH = 30;
  const int headerH = 24;
  const char *weekdays[] = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"};

  u8g2->clearBuffer();
  u8g2->setDrawColor(1);
  u8g2->drawFrame(8, 8, LCD_WIDTH - 16, LCD_HEIGHT - 16);

  u8g2->setFont(u8g2_font_helvB18_tf);
  snprintf(text, sizeof(text), "Trade Calendar %04d-%02d", selectedYear, selectedMonth);
  drawCenteredText(0, 34, LCD_WIDTH, text);

  u8g2->setFont(u8g2_font_6x13_tf);
  if (hasTime()) {
    struct tm nowInfo;
    time_t now = time(nullptr);
    localtime_r(&now, &nowInfo);
    snprintf(text, sizeof(text), "Now %04d-%02d-%02d %02d:%02d",
             nowInfo.tm_year + 1900, nowInfo.tm_mon + 1, nowInfo.tm_mday,
             nowInfo.tm_hour, nowInfo.tm_min);
  } else {
    snprintf(text, sizeof(text), "Time syncing... AP: %s", AP_SSID);
  }
  u8g2->drawStr(22, 56, text);

  for (int col = 0; col < 7; col++) {
    int x = left + col * cellW;
    u8g2->drawFrame(x, top, cellW, headerH);
    drawCenteredText(x, top + 16, cellW, weekdays[col]);
  }

  int firstDow = dayOfWeek(selectedYear, selectedMonth, 1);
  int firstCol = firstDow == 0 ? 6 : firstDow - 1;
  int totalDays = daysInMonth(selectedYear, selectedMonth);

  u8g2->setFont(u8g2_font_helvB12_tf);
  for (int day = 1; day <= totalDays; day++) {
    int index = firstCol + day - 1;
    int row = index / 7;
    int col = index % 7;
    int x = left + col * cellW;
    int y = top + headerH + row * cellH;
    bool selected = day == selectedDay;
    bool hasEvent = hasEventOnDay(selectedYear, selectedMonth, day);

    if (hasEvent) {
      u8g2->drawBox(x + 2, y + 2, cellW - 4, cellH - 4);
      u8g2->setDrawColor(0);
    } else {
      u8g2->drawFrame(x + 2, y + 2, cellW - 4, cellH - 4);
      u8g2->setDrawColor(1);
    }

    snprintf(text, sizeof(text), "%d", day);
    drawCenteredText(x, y + 21, cellW, text);
    u8g2->setDrawColor(1);

    if (selected) {
      u8g2->drawFrame(x, y, cellW, cellH);
      u8g2->drawFrame(x + 4, y + 4, cellW - 8, cellH - 8);
    }
  }

  u8g2->setFont(u8g2_font_6x13_tf);
  snprintf(text, sizeof(text), "Selected %04d-%02d-%02d  upcoming:%d",
           selectedYear, selectedMonth, selectedDay, countUpcomingOnSelectedDate());
  u8g2->drawStr(22, 276, text);
  u8g2->sendBuffer();
}

static void drawDayListPage() {
  char text[80];
  u8g2->clearBuffer();
  u8g2->setDrawColor(1);
  u8g2->drawFrame(8, 8, LCD_WIDTH - 16, LCD_HEIGHT - 16);

  u8g2->setFont(u8g2_font_helvB14_tf);
  snprintf(text, sizeof(text), "%04d-%02d-%02d Upcoming Trades", selectedYear, selectedMonth, selectedDay);
  u8g2->drawStr(18, 34, text);

  u8g2->setFont(u8g2_font_6x13_tf);
  int y = 58;
  int shown = 0;
  for (int i = 0; i < eventCount && shown < 8; i++) {
    if (!eventOnSelectedDate(events[i]) || !eventIsUpcoming(events[i]) || !eventVisibleOnBoard(events[i])) {
      continue;
    }
    snprintf(text, sizeof(text), "%s-%s %s", events[i].start, events[i].end, displayItemName(events[i].item));
    u8g2->drawStr(18, y, text);
    y += 17;
    snprintf(text, sizeof(text), "Target: %s", events[i].target[0] ? events[i].target : "-");
    u8g2->drawStr(30, y, text);
    y += 19;
    shown++;
  }

  if (shown == 0) {
    u8g2->drawStr(18, 80, "No upcoming trade for this date.");
  }

  u8g2->drawStr(18, 278, "KEY next day, BOOT prev day, hold=month");
  u8g2->sendBuffer();
}

static void drawScreen() {
  if (showCoverPage) {
    drawCoverPage();
  } else {
    drawCalendarWithList();
  }
}

static void normalizeSelectedDay() {
  int dim = daysInMonth(selectedYear, selectedMonth);
  if (selectedDay < 1) {
    selectedMonth--;
    if (selectedMonth < 1) {
      selectedMonth = 12;
      selectedYear--;
    }
    selectedDay = daysInMonth(selectedYear, selectedMonth);
  } else if (selectedDay > dim) {
    selectedDay = 1;
    selectedMonth++;
    if (selectedMonth > 12) {
      selectedMonth = 1;
      selectedYear++;
    }
  }
}

static void changeDay(int delta) {
  selectedDay += delta;
  normalizeSelectedDay();
}

static void changeMonth(int delta) {
  selectedMonth += delta;
  while (selectedMonth < 1) {
    selectedMonth += 12;
    selectedYear--;
  }
  while (selectedMonth > 12) {
    selectedMonth -= 12;
    selectedYear++;
  }
  int dim = daysInMonth(selectedYear, selectedMonth);
  if (selectedDay > dim) {
    selectedDay = dim;
  }
}

static bool loadSchedule() {
  eventCount = 0;
  if (!SPIFFS.exists(SCHEDULE_PATH)) {
    return false;
  }

  File file = SPIFFS.open(SCHEDULE_PATH, FILE_READ);
  if (!file) {
    return false;
  }

  bool firstLine = true;
  while (file.available() && eventCount < (int)(sizeof(events) / sizeof(events[0]))) {
    String line = file.readStringUntil('\n');
    line.trim();
    if (!line.length()) {
      continue;
    }
    if (firstLine) {
      firstLine = false;
      if (line.indexOf("date") >= 0 && line.indexOf("start") >= 0) {
        continue;
      }
    }
    String fields[6];
    int fieldCount = splitCsvLine(line, fields, 6);
    if (fieldCount < 5) {
      continue;
    }
    copyField(events[eventCount].date, sizeof(events[eventCount].date), fields[0]);
    copyField(events[eventCount].start, sizeof(events[eventCount].start), fields[1]);
    copyField(events[eventCount].end, sizeof(events[eventCount].end), fields[2]);
    copyField(events[eventCount].item, sizeof(events[eventCount].item), fields[3]);
    copyField(events[eventCount].target, sizeof(events[eventCount].target), fields[4]);
    if (fieldCount >= 6) {
      copyField(events[eventCount].tag, sizeof(events[eventCount].tag), fields[5]);
    } else {
      events[eventCount].tag[0] = '\0';
    }
    eventCount++;
  }
  file.close();
  if (eventCount > 0 && countUpcomingOnSelectedDate() == 0) {
    for (int i = 0; i < eventCount; i++) {
      if (!eventVisibleOnBoard(events[i]) || !eventIsUpcoming(events[i])) {
        continue;
      }
      int y, m, d;
      if (parseDate(events[i].date, y, m, d)) {
        selectedYear = y;
        selectedMonth = m;
        selectedDay = d;
        break;
      }
    }
  }
  return eventCount > 0;
}

static const char *displayItemName(const char *item) {
  if (strstr(item, "多日发电合同转让")) return "D-GenTransfer";
  if (strstr(item, "多日代购合同转让")) return "D-AgencyTransfer";
  if (strstr(item, "多日用电合同转让")) return "D-UserTransfer";
  if (strstr(item, "多日绿电")) return "D-Green";
  if (strstr(item, "多日代购交易")) return "D-Agency";
  if (strstr(item, "多日投放")) return "D-Release";
  if (strstr(item, "多日直接")) return "D-Direct";
  if (strstr(item, "月度绿电发电合同转让")) return "M-GreenGenTransfer";
  if (strstr(item, "月度绿电用电合同转让")) return "M-GreenUserTransfer";
  if (strstr(item, "月度发电合同转让")) return "M-GenTransfer";
  if (strstr(item, "月度用电合同转让")) return "M-UserTransfer";
  if (strstr(item, "月度代购合同转让")) return "M-AgencyTransfer";
  if (strstr(item, "多月绿电")) return "Multi-Green";
  if (strstr(item, "集中竞价")) return "Multi-DirectAuction";
  if (strstr(item, "滚动撮合")) return "Multi-DirectMatch";
  if (strstr(item, "挂牌")) return "Multi-DirectListing";
  if (strstr(item, "月度代购")) return "M-Agency";
  if (strstr(item, "月度投放")) return "M-Release";
  return item;
}

static bool fetchScheduleFromServer() {
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  HTTPClient http;
  if (!http.begin(SCHEDULE_URL)) {
    return false;
  }

  int status = http.GET();
  if (status != HTTP_CODE_OK) {
    http.end();
    return false;
  }

  File file = SPIFFS.open(SCHEDULE_PATH, FILE_WRITE);
  if (!file) {
    http.end();
    return false;
  }

  WiFiClient *stream = http.getStreamPtr();
  uint8_t buffer[512];
  int total = http.getSize();
  int remaining = total;

  while (http.connected() && (remaining > 0 || total == -1)) {
    size_t available = stream->available();
    if (available) {
      int readLen = stream->readBytes(buffer, min((int)available, (int)sizeof(buffer)));
      file.write(buffer, readLen);
      if (remaining > 0) {
        remaining -= readLen;
      }
    } else {
      delay(1);
    }
  }

  file.close();
  http.end();
  return loadSchedule();
}

static String jsonEscape(const String &value) {
  String escaped;
  escaped.reserve(value.length() + 8);
  for (size_t i = 0; i < value.length(); i++) {
    char c = value[i];
    if (c == '"' || c == '\\') {
      escaped += '\\';
      escaped += c;
    } else if (c == '\n') {
      escaped += "\\n";
    } else if (c == '\r') {
      escaped += "\\r";
    } else {
      escaped += c;
    }
  }
  return escaped;
}

static String selectedDateString() {
  char dateText[16];
  snprintf(dateText, sizeof(dateText), "%04d-%02d-%02d", selectedYear, selectedMonth, selectedDay);
  return String(dateText);
}

static void sendJson(const String &body, int statusCode = 200) {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(statusCode, "application/json; charset=utf-8", body);
}

static String statusJson(bool ok = true, const String &message = "ok") {
  String json = "{";
  json += "\"ok\":";
  json += ok ? "true" : "false";
  json += ",\"message\":\"" + jsonEscape(message) + "\"";
  json += ",\"page\":\"";
  json += showCoverPage ? "cover" : "calendar";
  json += "\",\"selected_date\":\"" + selectedDateString() + "\"";
  json += ",\"event_count\":" + String(eventCount);
  json += ",\"selected_event_count\":" + String(countUpcomingOnSelectedDate());
  json += ",\"wifi_connected\":";
  json += WiFi.status() == WL_CONNECTED ? "true" : "false";
  json += ",\"sta_ip\":\"" + WiFi.localIP().toString() + "\"";
  json += ",\"ap_ip\":\"" + WiFi.softAPIP().toString() + "\"";
  json += ",\"schedule_url\":\"" + jsonEscape(String(SCHEDULE_URL)) + "\"";
  json += ",\"uptime_ms\":" + String(millis());
  json += "}";
  return json;
}

static void handleRefresh() {
  bool ok = fetchScheduleFromServer();
  if (!ok) {
    loadSchedule();
  }
  drawScreen();
  sendJson(statusJson(ok, ok ? "refreshed" : "refresh failed, kept local schedule"), ok ? 200 : 502);
}

static void handleCover() {
  showCoverPage = true;
  lastInteractionMs = millis();
  drawScreen();
  sendJson(statusJson(true, "cover"));
}

static void handleCalendar() {
  showCoverPage = false;
  lastInteractionMs = millis();
  drawScreen();
  sendJson(statusJson(true, "calendar"));
}

static void handleNextDay() {
  showCoverPage = false;
  lastInteractionMs = millis();
  changeDay(1);
  drawScreen();
  sendJson(statusJson(true, "next"));
}

static void handlePrevDay() {
  showCoverPage = false;
  lastInteractionMs = millis();
  changeDay(-1);
  drawScreen();
  sendJson(statusJson(true, "prev"));
}

static void handleStatus() {
  sendJson(statusJson(true, "status"));
}

static String htmlPage() {
  String page;
  page += "<!doctype html><meta charset='utf-8'><title>Trade Calendar</title>";
  page += "<style>body{font-family:Arial,sans-serif;max-width:760px;margin:32px auto;line-height:1.5}code,pre{background:#f4f4f4;padding:8px;display:block}button{padding:10px 18px}</style>";
  page += "<h2>Trade Calendar Upload</h2>";
  page += "<p>Upload <code>schedule.csv</code>. Fields must be:</p>";
  page += "<pre>date,start,end,item,target\n2026-05-06,09:00,11:30,D-UserTransfer,2026-05-08~2026-05-12</pre>";
  page += "<form method='POST' action='/upload' enctype='multipart/form-data'>";
  page += "<input type='file' name='file' accept='.csv'> <button>Upload</button></form>";
  page += "<p>Loaded rows: " + String(eventCount) + "</p>";
  page += "<p><a href='/schedule.csv'>Download current CSV</a></p>";
  page += "<p>WiFi IP: " + WiFi.localIP().toString() + " / AP IP: " + WiFi.softAPIP().toString() + "</p>";
  page += "<p>Control: <a href='/refresh'>refresh</a> · <a href='/cover'>cover</a> · <a href='/calendar'>calendar</a> · <a href='/next'>next</a> · <a href='/prev'>prev</a> · <a href='/status'>status</a></p>";
  return page;
}

static void setupWebServer() {
  server.on("/", HTTP_GET, []() {
    server.send(200, "text/html; charset=utf-8", htmlPage());
  });

  server.on("/schedule.csv", HTTP_GET, []() {
    if (SPIFFS.exists(SCHEDULE_PATH)) {
      File file = SPIFFS.open(SCHEDULE_PATH, FILE_READ);
      server.streamFile(file, "text/csv");
      file.close();
    } else {
      server.send(404, "text/plain", "No schedule uploaded");
    }
  });

  server.on("/refresh", HTTP_GET, handleRefresh);
  server.on("/refresh", HTTP_POST, handleRefresh);
  server.on("/cover", HTTP_GET, handleCover);
  server.on("/cover", HTTP_POST, handleCover);
  server.on("/calendar", HTTP_GET, handleCalendar);
  server.on("/calendar", HTTP_POST, handleCalendar);
  server.on("/next", HTTP_GET, handleNextDay);
  server.on("/next", HTTP_POST, handleNextDay);
  server.on("/prev", HTTP_GET, handlePrevDay);
  server.on("/prev", HTTP_POST, handlePrevDay);
  server.on("/status", HTTP_GET, handleStatus);

  server.on(
    "/upload",
    HTTP_POST,
    []() {
      loadSchedule();
      drawScreen();
      server.sendHeader("Location", "/");
      server.send(303);
    },
    []() {
      HTTPUpload &upload = server.upload();
      if (upload.status == UPLOAD_FILE_START) {
        uploadFile = SPIFFS.open(SCHEDULE_PATH, FILE_WRITE);
      } else if (upload.status == UPLOAD_FILE_WRITE) {
        if (uploadFile) {
          uploadFile.write(upload.buf, upload.currentSize);
        }
      } else if (upload.status == UPLOAD_FILE_END) {
        if (uploadFile) {
          uploadFile.close();
        }
      }
    });

  server.begin();
}

static void setupWifiAndTime() {
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(AP_SSID, AP_PASS);

  if (strcmp(WIFI_SSID, "YOUR_WIFI_NAME") != 0) {
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    uint32_t startMs = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - startMs < 12000) {
      delay(250);
    }
  }

  configTime(8 * 3600, 0, "ntp.aliyun.com", "ntp.tencent.com", "pool.ntp.org");
}

static ButtonState keyButton = {KEY_PIN, false, false, 0};
static ButtonState bootButton = {BOOT_PIN, false, false, 0};

static bool updateButton(ButtonState &button, int shortDelta, int longMonthDelta) {
  bool pressed = digitalRead(button.pin) == LOW;
  uint32_t now = millis();

  if (pressed && !button.wasPressed) {
    button.wasPressed = true;
    button.longHandled = false;
    button.pressedAt = now;
  }

  if (pressed && button.wasPressed && !button.longHandled && now - button.pressedAt >= 900) {
    if (button.pin == BOOT_PIN && !showCoverPage) {
      showCoverPage = true;
      button.longHandled = true;
      return true;
    }
    lastInteractionMs = now;
    changeMonth(longMonthDelta);
    button.longHandled = true;
    return true;
  }

  if (!pressed && button.wasPressed) {
    bool wasLong = button.longHandled;
    uint32_t duration = now - button.pressedAt;
    button.wasPressed = false;
    button.longHandled = false;
    if (!wasLong && duration >= 30) {
      if (showCoverPage) {
        showCoverPage = false;
        lastInteractionMs = now;
        return true;
      }
      lastInteractionMs = now;
      changeDay(shortDelta);
      return true;
    }
  }
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(KEY_PIN, INPUT_PULLUP);
  pinMode(BOOT_PIN, INPUT_PULLUP);
  analogReadResolution(12);
  analogSetPinAttenuation(BATTERY_ADC_PIN, ADC_11db);
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.setClock(400000);

  SPIFFS.begin(true);
  lcd.begin(0, U8G2_R1);
  u8g2 = lcd.getU8g2();

  setupWifiAndTime();
  if (!fetchScheduleFromServer()) {
    loadSchedule();
  }
  setupWebServer();

  Serial.println("Trade calendar web uploader started");
  Serial.print("STA IP: ");
  Serial.println(WiFi.localIP());
  Serial.print("AP IP: ");
  Serial.println(WiFi.softAPIP());

  drawScreen();
}

void loop() {
  server.handleClient();

  bool changed = false;
  changed |= updateButton(keyButton, 1, 1);
  changed |= updateButton(bootButton, -1, -1);
  if (changed) {
    drawScreen();
  }

  if (!showCoverPage && millis() - lastInteractionMs > 300000UL) {
    showCoverPage = true;
    drawScreen();
  }

  static uint32_t lastRefresh = 0;
  if (millis() - lastRefresh > 60000) {
    lastRefresh = millis();
    static uint8_t refreshCount = 0;
    refreshCount++;
    if (refreshCount >= 10) {
      refreshCount = 0;
      fetchScheduleFromServer();
    }
    drawScreen();
  }

  delay(10);
}
