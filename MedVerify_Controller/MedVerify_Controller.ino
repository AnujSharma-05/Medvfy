/*
  MedVerify - ESP32 indicator and buzzer test firmware
  Revised architecture: NO servo, NO HC-SR04.

  Serial Monitor commands (115200 baud, Newline):
    A / B / C / D : simulate a scheduled reminder for that compartment
    TEST          : test all four LEDs and the buzzer
    STOP          : turn all compartment LEDs and buzzer off
    HELP          : show commands

  Hardware assumptions:
    LED A -> GPIO16 via 220-330 ohm resistor
    LED B -> GPIO17 via 220-330 ohm resistor
    LED C -> GPIO18 via 220-330 ohm resistor
    LED D -> GPIO19 via 220-330 ohm resistor
    buzzer module S/signal -> GPIO25
    buzzer module +/VCC -> 3V3 (only if the module label/spec supports 3.3V)
    buzzer module -/GND -> ESP32 GND

  Connect all grounds together. Verify the buzzer module's pin labels before powering it.
*/

#include <Arduino.h>

const uint8_t LED_A = 16;
const uint8_t LED_B = 17;
const uint8_t LED_C = 18;
const uint8_t LED_D = 19;
const uint8_t BUZZER_PIN = 25;

const uint8_t LED_PINS[] = {LED_A, LED_B, LED_C, LED_D};
const char* SLOT_NAMES[] = {"A", "B", "C", "D"};
const size_t SLOT_COUNT = sizeof(LED_PINS) / sizeof(LED_PINS[0]);

void allLedsOff() {
  for (size_t i = 0; i < SLOT_COUNT; i++) {
    digitalWrite(LED_PINS[i], LOW);
  }
}

void beepPattern() {
  // Intended for a passive buzzer/module that accepts a tone signal.
  for (int i = 0; i < 3; i++) {
    tone(BUZZER_PIN, 2000, 160);
    delay(230);
  }
  noTone(BUZZER_PIN);
}

void printHelp() {
  Serial.println();
  Serial.println("=== MedVerify ESP32 controller ===");
  Serial.println("A/B/C/D : simulate reminder for compartment");
  Serial.println("TEST    : test LEDs and buzzer");
  Serial.println("STOP    : turn all LEDs and buzzer off");
  Serial.println("HELP    : show this message");
  Serial.println("==================================");
}

void triggerReminder(size_t slotIndex) {
  if (slotIndex >= SLOT_COUNT) return;

  allLedsOff();
  digitalWrite(LED_PINS[slotIndex], HIGH);

  Serial.print("REMINDER: compartment ");
  Serial.print(SLOT_NAMES[slotIndex]);
  Serial.println(" selected; LED ON, buzzer sounding.");
  beepPattern();
  Serial.println("LED remains on until STOP or another compartment is selected.");
}

void runTest() {
  Serial.println("Starting hardware test...");
  allLedsOff();

  for (size_t i = 0; i < SLOT_COUNT; i++) {
    allLedsOff();
    digitalWrite(LED_PINS[i], HIGH);
    Serial.print("Testing LED for compartment ");
    Serial.println(SLOT_NAMES[i]);
    tone(BUZZER_PIN, 1800 + (int)i * 150, 120);
    delay(500);
    noTone(BUZZER_PIN);
  }

  allLedsOff();
  beepPattern();
  Serial.println("Test finished. All LEDs are now OFF.");
}

void handleCommand(String command) {
  command.trim();
  command.toUpperCase();

  if (command == "A") triggerReminder(0);
  else if (command == "B") triggerReminder(1);
  else if (command == "C") triggerReminder(2);
  else if (command == "D") triggerReminder(3);
  else if (command == "TEST") runTest();
  else if (command == "STOP") {
    allLedsOff();
    noTone(BUZZER_PIN);
    Serial.println("Reminder cleared. All LEDs and buzzer OFF.");
  }
  else if (command == "HELP" || command.length() == 0) printHelp();
  else Serial.println("Unknown command. Type HELP.");
}

void setup() {
  Serial.begin(115200);
  delay(500);

  for (size_t i = 0; i < SLOT_COUNT; i++) {
    pinMode(LED_PINS[i], OUTPUT);
    digitalWrite(LED_PINS[i], LOW);
  }
  pinMode(BUZZER_PIN, OUTPUT);
  noTone(BUZZER_PIN);

  Serial.println("MedVerify controller booted.");
  Serial.println("No servo or ultrasonic sensor is used in this version.");
  printHelp();
}

void loop() {
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    handleCommand(command);
  }
}
