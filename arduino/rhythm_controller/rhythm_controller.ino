#include <Keyboard.h>
#include <Mouse.h>
#include <stdlib.h>
#include <string.h>

// Existing realtime protocol (unchanged):
//   K:<code>:DOWN|UP  keyboard
//   M:<code>:DOWN|UP  mouse button
//   P:<dx>:<dy>        relative mouse movement
//
// Optional scheduled-macro protocol v3:
//   V                        -> V:3:TM:<capacity>
//   L:<count>:<crc16>        -> L:OK
//   E:<at_ms>:<op>:<code>    (one per event; no per-event response)
//   C                        -> R:<count>:<crc16> after verification
//   S                        -> S:OK, then run from the board's micros()
//                              T:<count>:<late_us>... after the last event
//   A                        -> immediate abort + input safety release
//   Q                        -> diagnostic state
//
// Event ops: 1 key-down, 2 key-up, 3 mouse-down, 4 mouse-up. CRC is
// CRC-16/CCITT-FALSE over each event as six canonical bytes:
// little-endian uint32 at_ms, uint8 op, uint8 code.

static const uint8_t PROTOCOL_VERSION = 3;
static const uint16_t MAX_MACRO_EVENTS = 160;
static const uint32_t MAX_MACRO_DURATION_MS = 30UL * 60UL * 1000UL;
static const uint32_t START_LEAD_US = 5000UL;
static const uint8_t SERIAL_BYTES_PER_LOOP = 64;
static const uint8_t LINE_CAPACITY = 48;

enum TimedOp : uint8_t {
  OP_KEY_DOWN = 1,
  OP_KEY_UP = 2,
  OP_MOUSE_DOWN = 3,
  OP_MOUSE_UP = 4,
};

struct __attribute__((packed)) TimedEvent {
  uint32_t atMs;
  uint8_t op;
  uint8_t code;
};

TimedEvent macroEvents[MAX_MACRO_EVENTS];
uint16_t expectedCount = 0;
uint16_t expectedCrc = 0;
uint16_t loadCount = 0;
uint16_t loadCrc = 0xFFFF;
bool loadingMacro = false;
bool macroLoaded = false;

bool macroRunning = false;
uint16_t macroIndex = 0;
uint32_t macroStartUs = 0;
uint32_t maxLateUs = 0;
uint32_t sumLateUs = 0;
uint16_t overOneMs = 0;
uint16_t overFiveMs = 0;
// Captured after each HID call and transmitted only after the whole macro.
// uint16 is ample for healthy runs and costs 320 bytes at full capacity;
// aggregate maxLateUs still preserves the true value if one saturates.
uint16_t eventLateUs[MAX_MACRO_EVENTS];

char lineBuffer[LINE_CAPACITY];
uint8_t lineLength = 0;
bool lineOverflow = false;


uint16_t crc16Update(uint16_t crc, uint8_t value) {
  crc ^= (uint16_t)value << 8;
  for (uint8_t i = 0; i < 8; ++i) {
    if (crc & 0x8000) {
      crc = (uint16_t)((crc << 1) ^ 0x1021);
    } else {
      crc <<= 1;
    }
  }
  return crc;
}


void updateLoadCrc(const TimedEvent &event) {
  loadCrc = crc16Update(loadCrc, (uint8_t)(event.atMs));
  loadCrc = crc16Update(loadCrc, (uint8_t)(event.atMs >> 8));
  loadCrc = crc16Update(loadCrc, (uint8_t)(event.atMs >> 16));
  loadCrc = crc16Update(loadCrc, (uint8_t)(event.atMs >> 24));
  loadCrc = crc16Update(loadCrc, event.op);
  loadCrc = crc16Update(loadCrc, event.code);
}


void releaseAllInputs() {
  Keyboard.releaseAll();
  Mouse.release(MOUSE_LEFT);
  Mouse.release(MOUSE_RIGHT);
  Mouse.release(MOUSE_MIDDLE);
}


bool parseUnsigned(const char *text, uint32_t &value) {
  if (text == NULL || *text == '\0' || *text == '-') {
    return false;
  }
  char *end = NULL;
  unsigned long parsed = strtoul(text, &end, 10);
  if (end == text || *end != '\0') {
    return false;
  }
  value = (uint32_t)parsed;
  return true;
}


bool parseSigned(const char *text, long &value) {
  if (text == NULL || *text == '\0') {
    return false;
  }
  char *end = NULL;
  long parsed = strtol(text, &end, 10);
  if (end == text || *end != '\0') {
    return false;
  }
  value = parsed;
  return true;
}


void sendVersion() {
  Serial.print(F("V:"));
  Serial.print(PROTOCOL_VERSION);
  Serial.print(F(":TM:"));
  Serial.println(MAX_MACRO_EVENTS);
}


void sendState() {
  Serial.print(F("Q:"));
  if (macroRunning) {
    Serial.print(F("RUNNING:"));
  } else if (loadingMacro) {
    Serial.print(F("LOADING:"));
  } else if (macroLoaded) {
    Serial.print(F("READY:"));
  } else {
    Serial.print(F("IDLE:"));
  }
  Serial.print(macroIndex);
  Serial.print(':');
  Serial.print(expectedCount);
  Serial.print(':');
  Serial.println(expectedCrc);
}


void beginMacroLoad(char *countText, char *crcText) {
  if (macroRunning) {
    Serial.println(F("X:BUSY"));
    return;
  }
  uint32_t count = 0;
  uint32_t crc = 0;
  if (!parseUnsigned(countText, count) || count == 0 ||
      count > MAX_MACRO_EVENTS || !parseUnsigned(crcText, crc) ||
      crc > 0xFFFFUL) {
    Serial.println(F("X:LOAD"));
    return;
  }
  expectedCount = (uint16_t)count;
  expectedCrc = (uint16_t)crc;
  loadCount = 0;
  loadCrc = 0xFFFF;
  macroIndex = 0;
  loadingMacro = true;
  macroLoaded = false;
  Serial.println(F("L:OK"));
}


void appendMacroEvent(char *timeText, char *opText, char *codeText) {
  if (!loadingMacro || loadCount >= expectedCount) {
    Serial.println(F("X:EVENT"));
    loadingMacro = false;
    macroLoaded = false;
    return;
  }
  uint32_t atMs = 0;
  uint32_t op = 0;
  uint32_t code = 0;
  if (!parseUnsigned(timeText, atMs) || atMs > MAX_MACRO_DURATION_MS ||
      !parseUnsigned(opText, op) || op < OP_KEY_DOWN || op > OP_MOUSE_UP ||
      !parseUnsigned(codeText, code) || code == 0 || code > 255) {
    Serial.println(F("X:EVENT"));
    loadingMacro = false;
    macroLoaded = false;
    return;
  }
  TimedEvent &event = macroEvents[loadCount++];
  event.atMs = atMs;
  event.op = (uint8_t)op;
  event.code = (uint8_t)code;
  updateLoadCrc(event);
}


void commitMacroLoad() {
  if (!loadingMacro || loadCount != expectedCount || loadCrc != expectedCrc) {
    Serial.print(F("X:COMMIT:"));
    Serial.print(loadCount);
    Serial.print(':');
    Serial.println(loadCrc);
    loadingMacro = false;
    macroLoaded = false;
    return;
  }
  loadingMacro = false;
  macroLoaded = true;
  Serial.print(F("R:"));
  Serial.print(loadCount);
  Serial.print(':');
  Serial.println(loadCrc);
}


void startMacro() {
  if (macroRunning) {
    Serial.println(F("X:BUSY"));
    return;
  }
  if (!macroLoaded || expectedCount == 0) {
    Serial.println(F("X:NOLOAD"));
    return;
  }
  // Start from a known neutral HID state. The 5 ms lead begins after the
  // acknowledgement has been queued, so response traffic cannot skew event 0.
  releaseAllInputs();
  Serial.println(F("S:OK"));
  macroIndex = 0;
  maxLateUs = 0;
  sumLateUs = 0;
  overOneMs = 0;
  overFiveMs = 0;
  macroStartUs = micros() + START_LEAD_US;
  macroRunning = true;
}


void abortMacro() {
  macroRunning = false;
  macroIndex = 0;
  releaseAllInputs();
  Serial.println(F("A:OK"));
}


void handleLegacyCommand(char *typeText, char *first, char *second) {
  if (typeText == NULL || first == NULL || second == NULL ||
      typeText[1] != '\0') {
    return;
  }
  char type = typeText[0];
  if (type == 'K' || type == 'M') {
    uint32_t code = 0;
    if (!parseUnsigned(first, code) || code == 0 || code > 255) {
      return;
    }
    bool down = strcmp(second, "DOWN") == 0;
    bool up = strcmp(second, "UP") == 0;
    if (!down && !up) {
      return;
    }
    if (type == 'K') {
      down ? Keyboard.press((uint8_t)code) : Keyboard.release((uint8_t)code);
    } else {
      down ? Mouse.press((uint8_t)code) : Mouse.release((uint8_t)code);
    }
    return;
  }
  if (type == 'P') {
    long dx = 0;
    long dy = 0;
    if (!parseSigned(first, dx) || !parseSigned(second, dy)) {
      return;
    }
    // Mouse.move takes signed char (-127..127), so chunk larger deltas.
    while (dx != 0 || dy != 0) {
      int sx = dx > 127 ? 127 : (dx < -127 ? -127 : (int)dx);
      int sy = dy > 127 ? 127 : (dy < -127 ? -127 : (int)dy);
      Mouse.move((signed char)sx, (signed char)sy, 0);
      dx -= sx;
      dy -= sy;
    }
  }
}


void handleCommand(char *line) {
  char *save = NULL;
  char *command = strtok_r(line, ":", &save);
  if (command == NULL) {
    return;
  }
  if (strcmp(command, "V") == 0) {
    sendVersion();
    return;
  }
  if (strcmp(command, "Q") == 0) {
    sendState();
    return;
  }
  if (strcmp(command, "A") == 0) {
    abortMacro();
    return;
  }
  if (strcmp(command, "C") == 0) {
    commitMacroLoad();
    return;
  }
  if (strcmp(command, "S") == 0) {
    startMacro();
    return;
  }

  char *first = strtok_r(NULL, ":", &save);
  char *second = strtok_r(NULL, ":", &save);
  if (strcmp(command, "L") == 0) {
    beginMacroLoad(first, second);
    return;
  }
  if (strcmp(command, "E") == 0) {
    char *third = strtok_r(NULL, ":", &save);
    appendMacroEvent(first, second, third);
    return;
  }
  handleLegacyCommand(command, first, second);
}


void processSerial() {
  uint8_t budget = SERIAL_BYTES_PER_LOOP;
  while (budget-- > 0 && Serial.available() > 0) {
    char value = (char)Serial.read();
    if (value == '\n') {
      if (lineOverflow) {
        Serial.println(F("X:LINE"));
      } else if (lineLength > 0) {
        lineBuffer[lineLength] = '\0';
        handleCommand(lineBuffer);
      }
      lineLength = 0;
      lineOverflow = false;
    } else if (value != '\r') {
      if (lineLength < LINE_CAPACITY - 1) {
        lineBuffer[lineLength++] = value;
      } else {
        lineOverflow = true;
      }
    }
  }
}


void executeTimedEvent(const TimedEvent &event) {
  switch (event.op) {
    case OP_KEY_DOWN:
      Keyboard.press(event.code);
      break;
    case OP_KEY_UP:
      Keyboard.release(event.code);
      break;
    case OP_MOUSE_DOWN:
      Mouse.press(event.code);
      break;
    case OP_MOUSE_UP:
      Mouse.release(event.code);
      break;
  }
}


void finishMacro() {
  macroRunning = false;
  releaseAllInputs();
  uint32_t runtimeMs = (uint32_t)(micros() - macroStartUs) / 1000UL;
  uint32_t averageLateUs = expectedCount > 0 ? sumLateUs / expectedCount : 0;
  // Do not print while the macro is running: filling the CDC serial endpoint
  // could block the scheduler. Emit the completed trace before DONE so the
  // host can associate it with this run without affecting a single HID event.
  Serial.print(F("T:"));
  Serial.print(expectedCount);
  for (uint16_t i = 0; i < expectedCount; ++i) {
    Serial.print(':');
    Serial.print(eventLateUs[i]);
  }
  Serial.println();
  Serial.print(F("D:"));
  Serial.print(expectedCount);
  Serial.print(':');
  Serial.print(maxLateUs);
  Serial.print(':');
  Serial.print(averageLateUs);
  Serial.print(':');
  Serial.print(overOneMs);
  Serial.print(':');
  Serial.print(overFiveMs);
  Serial.print(':');
  Serial.println(runtimeMs);
}


void runMacroScheduler() {
  if (!macroRunning) {
    return;
  }
  while (macroIndex < expectedCount) {
    const TimedEvent &event = macroEvents[macroIndex];
    uint32_t deadlineUs = macroStartUs + event.atMs * 1000UL;
    int32_t due = (int32_t)(micros() - deadlineUs);
    if (due < 0) {
      break;
    }
    executeTimedEvent(event);
    // Measure after Keyboard/Mouse returns. This includes time spent waiting
    // for room in the USB HID endpoint, not just scheduler-loop wakeup.
    uint32_t lateUs = (uint32_t)(micros() - deadlineUs);
    eventLateUs[macroIndex] = lateUs > 0xFFFFUL
        ? 0xFFFFU : (uint16_t)lateUs;
    if (lateUs > maxLateUs) {
      maxLateUs = lateUs;
    }
    sumLateUs += lateUs;
    if (lateUs > 1000UL) {
      ++overOneMs;
    }
    if (lateUs > 5000UL) {
      ++overFiveMs;
    }
    ++macroIndex;
  }
  if (macroIndex >= expectedCount) {
    finishMacro();
  }
}


void setup() {
  // Leonardo native USB CDC ignores the numeric baud rate, but keep 115200
  // for compatibility with the host configuration and clone boards.
  Serial.begin(115200);
  Keyboard.begin();
  Mouse.begin();
}


void loop() {
  // Parsing is nonblocking and byte-budgeted so an upload/diagnostic burst
  // cannot starve a macro deadline. ABORT is still noticed within a few loops.
  processSerial();
  runMacroScheduler();
}
