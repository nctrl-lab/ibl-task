// ======================================
// Teensy firmware for IBL task
// Laboratory of Neural Control, POSTECH
// Authors: Nahyun Kim, Dohoung Kim
// 2026. 05. 11
// ======================================
//
// Teensy commands (host -> Teensy)
//   s / e     start / end streaming
//   t / T     trial onset / offset
//   c / C     cue onset / offset
//   n         noise (incorrect trial)
//   r         reward (correct trial)
//   w{ms}     set reward duration in ms
//   S         stream encoder position (human-readable, starts at 0), end with 'e'
//   h / ?     print this help
//
// Teensy -> host packet (3 bytes, 1 kHz)
//   byte 0   : 0 (start marker)
//   byte 1   : counter, cycles 1..255
//   byte 2   : wheel-position delta + 128,
//              clamped so byte ∈ [1, 255]
//              max value (127) can be achieved when 512 ppr rotary joint (2048 counts/rev with 4x quadrature)
//              rotates at 127*1000/2048 = 62 rps = 3723 rpm

#include <Encoder.h>
#include <Audio.h>
#include <Wire.h>

#define ENCA        0
#define ENCB        1
#define TRIAL       2
#define CUE         3
#define NOISE       4
#define REWARD      5

// Audio shield reserved (do not use):
//   6 7 8 10 11 12 13 15 18 19 20 21 23

#define trialOn()   digitalWriteFast(TRIAL,  HIGH)
#define trialOff()  digitalWriteFast(TRIAL,  LOW)
#define cueOn()     digitalWriteFast(CUE,    HIGH)
#define cueOff()    digitalWriteFast(CUE,    LOW)
#define rewardOn()  digitalWriteFast(REWARD, HIGH)
#define rewardOff() digitalWriteFast(REWARD, LOW)
#define allOff()    { trialOff(); cueOff(); rewardOff(); \
                      toneOff(); noiseOff(); }

AudioSynthWaveformSine  sineOsc;
AudioEffectEnvelope     sineEnv;
AudioSynthNoiseWhite    noiseGen;
AudioMixer4             mixer;
AudioOutputI2S          i2sOut;
AudioControlSGTL5000    audioShield;
AudioConnection c1(sineOsc,  sineEnv);
AudioConnection c2(sineEnv,  0, mixer, 0);
AudioConnection c3(noiseGen, 0, mixer, 1);
AudioConnection c4(mixer, 0, i2sOut, 0);
AudioConnection c5(mixer, 0, i2sOut, 1);

Encoder myEnc(ENCA, ENCB);

// Time constants in µs.
const unsigned long ENC_INTERVAL    = 1000;     // 1 kHz wheel sampling
const unsigned long HUMAN_INTERVAL  = 100000;   // 10 Hz human-readable print
const unsigned long TONE_DURATION   = 100000;
const unsigned long NOISE_DURATION  = 500000;
const unsigned long MAX_REWARD      = 1000000;  // valve safety clamp (1 s)

unsigned long now;
unsigned long encTime, humanTime;
unsigned long rewardTime, toneTime, noiseTime;
unsigned long rewardDuration = 50000;

bool streaming      = false;
bool humanStreaming = false;
bool valveState     = false;
bool toneState      = false;
bool noiseState     = false;

uint8_t txCounter = 1;
int32_t lastEnc = 0;
int32_t humanBase = 0;

void sendDelta(int32_t pos) {
  int32_t delta = pos - lastEnc;
  if (delta >  127) delta =  127;
  if (delta < -127) delta = -127;
  lastEnc += delta;                             // catches up over multiple ms if clamped
  uint8_t buf[3] = { 0, txCounter, (uint8_t)(delta + 128) };
  Serial.write(buf, 3);
  txCounter = (txCounter == 255) ? 1 : txCounter + 1;
}

void toneOn() {
  mixer.gain(0, 0.7);
  sineEnv.noteOn();
  toneTime  = now;
  toneState = true;
}

void toneOff() {
  mixer.gain(0, 0.0);
  sineEnv.noteOff();
  toneState = false;
}

void noiseOn() {
  digitalWriteFast(NOISE, HIGH);
  mixer.gain(1, 0.5);
  noiseTime  = now;
  noiseState = true;
}

void noiseOff() {
  digitalWriteFast(NOISE, LOW);
  mixer.gain(1, 0.0);
  noiseState = false;
}

void checkEncoder() {
  if (now - encTime >= ENC_INTERVAL) {
    encTime += ENC_INTERVAL;
    sendDelta((int32_t)myEnc.read());
  }
}

void checkHumanEncoder() {
  if (now - humanTime >= HUMAN_INTERVAL) {
    humanTime += HUMAN_INTERVAL;
    Serial.print("Encoder: ");
    Serial.println((int32_t)myEnc.read() - humanBase);
  }
}

void checkReward() {
  if (valveState && now - rewardTime >= rewardDuration) {
    rewardOff();
    valveState = false;
  }
}

void checkTone() {
  if (toneState && now - toneTime >= TONE_DURATION) toneOff();
}

void checkNoise() {
  if (noiseState && now - noiseTime >= NOISE_DURATION) noiseOff();
}

void printHelp() {
  Serial.println("Teensy commands:");
  Serial.println("  s / e     start / end streaming");
  Serial.println("  t / T     trial onset / offset");
  Serial.println("  c / C     cue onset / offset");
  Serial.println("  n         noise (incorrect trial)");
  Serial.println("  r         reward (correct trial)");
  Serial.println("  w{ms}     set reward duration in ms");
  Serial.println("  S         stream encoder position (end with 'e')");
  Serial.println("  h / ?     print this help");
}

void checkCommand() {
  if (!Serial.available()) return;
  char c = (char)Serial.read();

  switch (c) {
    case 'h':
    case '?':
      printHelp();
      return;

    case 'S':
      if (!streaming && !humanStreaming) {
        now = micros();
        humanStreaming = true;
        humanTime = now;
        humanBase = (int32_t)myEnc.read();
        Serial.println("Streaming encoder (press 'e' to stop)");
      }
      return;

    case 's':
      if (!streaming && !humanStreaming) {
        now = micros();
        streaming = true;
        encTime   = now;
        txCounter = 1;
        lastEnc   = (int32_t)myEnc.read();    // host position resets to 0 here
      }
      return;

    case 'e':
      if (humanStreaming) {
        humanStreaming = false;
      }
      if (streaming) {
        streaming = false;
        if (valveState) { rewardOff(); valveState = false; }
        if (toneState)  toneOff();
        if (noiseState) noiseOff();
        cueOff();
        trialOff();
      }
      return;

    case 'w': {
      long ms = Serial.parseInt();
      if (ms > 0) {
        unsigned long us = (unsigned long)ms * 1000UL;
        rewardDuration = (us > MAX_REWARD) ? MAX_REWARD : us;
      }
      Serial.print("Reward duration: ");
      Serial.print(rewardDuration / 1000);
      Serial.println(" ms");
      return;
    }
  }

  if (c == 'r') {
    rewardOn();
    rewardTime = now;
    valveState = true;
    return;
  }

  if (!streaming) return;

  switch (c) {
    case 't': trialOn();  break;
    case 'T': trialOff(); break;
    case 'c': cueOn(); toneOn();          break;
    case 'C': cueOff();                   break;
    case 'n': if (!noiseState) noiseOn(); break;
  }
}

void setup() {
  pinMode(TRIAL,  OUTPUT);
  pinMode(CUE,    OUTPUT);
  pinMode(NOISE,  OUTPUT);
  pinMode(REWARD, OUTPUT);

  Serial.begin(0);
  Serial.setTimeout(10);

  AudioMemory(20);
  audioShield.enable();
  audioShield.volume(0.8);
  sineEnv.attack(10);
  sineEnv.decay(0);
  sineEnv.sustain(1.0);
  sineEnv.release(10);
  sineOsc.frequency(5000);
  sineOsc.amplitude(0.8);
  noiseGen.amplitude(0.5);

  allOff();
}

void loop() {
  now = micros();
  checkReward();
  if (streaming) {
    checkEncoder();
    checkTone();
    checkNoise();
  } else if (humanStreaming) {
    checkHumanEncoder();
  }
  checkCommand();
}
