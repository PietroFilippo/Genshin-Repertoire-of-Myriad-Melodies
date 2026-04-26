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
    
    // Command format expected: "K:<code>:<action>" or "M:<code>:<action>"
    // Example: "K:97:DOWN" (Press 'a')
    // Example: "M:1:UP" (Release Left Mouse Button)
    
    if (command.length() >= 6 && command.charAt(1) == ':') {
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
        }
      }
    }
  }
}
