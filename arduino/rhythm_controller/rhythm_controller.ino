#include <Keyboard.h>
#include <Mouse.h>

void setup() {
  // Serial interface at a high baud rate for minimum latency
  Serial.begin(115200);
  
  // HID control
  Keyboard.begin();
  Mouse.begin();
}

void loop() {
  // Check if there is any data available to read from the PC
  if (Serial.available() > 0) {
    // Read the incoming string until a newline character is encountered
    String command = Serial.readStringUntil('\n');
    command.trim(); // Remove any leftover carriage returns or spaces
    
    // Command format expected:
    //   "K:<code>:<action>"   keyboard, action = DOWN | UP
    //   "M:<code>:<action>"   mouse button, action = DOWN | UP
    //   "P:<dx>:<dy>"         mouse pointer relative move (signed pixels)
    // Examples: "K:97:DOWN", "M:1:UP", "P:-40:120"

    if (command.length() >= 5 && command.charAt(1) == ':') {
      char type = command.charAt(0);
      int sepIndex = command.lastIndexOf(':');

      if (sepIndex > 2) {
        String codeStr = command.substring(2, sepIndex);
        String action = command.substring(sepIndex + 1);

        int code = codeStr.toInt();

        if (type == 'K') {
          if (action == "DOWN") {
            Keyboard.press(code);
          } else if (action == "UP") {
            Keyboard.release(code);
          }
        } else if (type == 'M') {
          if (action == "DOWN") {
            Mouse.press(code);
          } else if (action == "UP") {
            Mouse.release(code);
          }
        } else if (type == 'P') {
          // Relative mouse move. Mouse.move takes signed char (-127..127),
          // so chunk larger deltas into multiple HID reports.
          int dx = code;
          int dy = action.toInt();
          while (dx != 0 || dy != 0) {
            int sx = constrain(dx, -127, 127);
            int sy = constrain(dy, -127, 127);
            Mouse.move(sx, sy, 0);
            dx -= sx;
            dy -= sy;
          }
        }
      }
    }
  }
}
