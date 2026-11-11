// UNO Q Internet Radio - MCU side (STM32U585, Zephyr core)
//
// Responsibilities
//   * read the three Modulino Buttons on the Qwiic connector (Wire1)
//   * turn presses into events (short press / long press / hold-repeat)
//     and send them to Python with Bridge.notify("button_event", code)
//   * show frames sent from Python on the 13x8 LED matrix
//   * drive the button LEDs as requested by Python, plus instant press feedback
//
// Button map
//   A  short: previous station     hold: volume down (repeats)
//   B  short: play / pause         hold: mute toggle
//   C  short: next station         hold: volume up   (repeats)
//
// SPDX-License-Identifier: MIT

#include <Arduino_RouterBridge.h>
#include <Arduino_Modulino.h>
#include <Arduino_LED_Matrix.h>

// Event codes - must match main.py
#define EV_PREV      1
#define EV_PLAY      2
#define EV_NEXT      3
#define EV_VOL_DOWN  4
#define EV_VOL_UP    5
#define EV_MUTE      6

const unsigned long LONG_PRESS_MS = 600;   // hold time before "hold" action
const unsigned long REPEAT_MS     = 250;   // volume repeat interval while held
const unsigned long POLL_MS       = 10;

ModulinoButtons buttons;
Arduino_LED_Matrix matrix;

uint32_t frame[4] = {0, 0, 0, 0};
volatile bool frameDirty = false;
uint8_t ledMask = 0;           // LED state requested by Python (bit0=A, bit1=B, bit2=C)
uint8_t ledShown = 0xFF;       // what is currently on the module

struct ButtonState {
  bool down = false;
  bool holdFired = false;
  unsigned long pressedAt = 0;
  unsigned long lastRepeat = 0;
};
ButtonState btn[3];

// ---------------------------------------------------------------------------
// Functions callable from Python
// ---------------------------------------------------------------------------

// 32 hex characters = four 32-bit words, row-major, MSB first (104 bits used)
void showFrame(String hex) {
  if (hex.length() < 32) return;
  for (int w = 0; w < 4; w++) {
    uint32_t v = 0;
    for (int i = 0; i < 8; i++) {
      char c = hex.charAt(w * 8 + i);
      uint8_t n = (c >= '0' && c <= '9') ? c - '0'
                : (c >= 'a' && c <= 'f') ? c - 'a' + 10
                : (c >= 'A' && c <= 'F') ? c - 'A' + 10 : 0;
      v = (v << 4) | n;
    }
    frame[w] = v;
  }
  frameDirty = true;
}

void setLeds(int mask) {
  ledMask = (uint8_t)(mask & 0x07);
}

// ---------------------------------------------------------------------------
void sendEvent(int code) {
  Bridge.notify("button_event", code);
  Monitor.print("event ");
  Monitor.println(code);
}

void pollButtons() {
  buttons.update();
  unsigned long now = millis();

  for (int i = 0; i < 3; i++) {
    bool pressed = buttons.isPressed(i);
    ButtonState &b = btn[i];

    if (pressed && !b.down) {                       // press started
      b.down = true;
      b.holdFired = false;
      b.pressedAt = now;
    } else if (pressed && b.down) {                 // still held
      unsigned long held = now - b.pressedAt;
      if (held >= LONG_PRESS_MS) {
        if (i == 1) {                               // B: mute, once per hold
          if (!b.holdFired) { sendEvent(EV_MUTE); b.holdFired = true; }
        } else if (!b.holdFired || now - b.lastRepeat >= REPEAT_MS) {
          sendEvent(i == 0 ? EV_VOL_DOWN : EV_VOL_UP);
          b.holdFired = true;
          b.lastRepeat = now;
        }
      }
    } else if (!pressed && b.down) {                // released
      b.down = false;
      if (!b.holdFired) {
        sendEvent(i == 0 ? EV_PREV : (i == 1 ? EV_PLAY : EV_NEXT));
      }
    }
  }
}

void updateLeds() {
  // A pressed button is always lit for instant feedback
  uint8_t shown = ledMask;
  for (int i = 0; i < 3; i++) if (btn[i].down) shown |= (1 << i);
  if (shown != ledShown) {
    buttons.setLeds(shown & 0x01, shown & 0x02, shown & 0x04);
    ledShown = shown;
  }
}

// ---------------------------------------------------------------------------
void setup() {
  Bridge.begin();
  Monitor.begin();

  matrix.begin();
  Modulino.begin(Wire1);      // Qwiic connector on the UNO Q is Wire1
  buttons.begin();

  // provide_safe runs the handler between loop() passes, so it never
  // interrupts an I2C transaction with the Modulino
  Bridge.provide_safe("show_frame", showFrame);
  Bridge.provide_safe("set_leds", setLeds);

  // Boot pattern until Python sends its first frame: a single centre dot
  frame[1] = 0x00400000;
  frameDirty = true;
}

void loop() {
  pollButtons();
  updateLeds();
  if (frameDirty) {
    frameDirty = false;
    matrix.loadFrame(frame);
  }
  delay(POLL_MS);
}
