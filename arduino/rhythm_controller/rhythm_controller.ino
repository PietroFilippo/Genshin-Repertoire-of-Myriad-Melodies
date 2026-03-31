#include <Keyboard.h>

void setup() {
  // Serial interface at a high baud rate for minimum latency
  Serial.begin(115200);
  
  // HID keyboard control
  Keyboard.begin();
}

void loop() {
  // Check if there is any data available to read from the PC
  if (Serial.available() > 0) {
    // Read the incoming string until a newline character is encountered
    String command = Serial.readStringUntil('\n');
    command.trim(); // Remove any leftover carriage returns or spaces
    
    // Command format expected: "<KEY>_DOWN" or "<KEY>_UP"
    
    if (command.length() >= 4) {
      char targetKey = command.charAt(0);
      String action = command.substring(2);
      
      // Convert character to a valid lowercase char for Keyboard API if it's alphanumeric
      char pressChar = toLowerCase(targetKey);
      
      if (action == "DOWN") {
        Keyboard.press(pressChar);
      } else if (action == "UP") {
        Keyboard.release(pressChar);
      }
    }
  }
}
