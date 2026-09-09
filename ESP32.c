#include <Wire.h>

// ============================================================
// KneeGrow - ESP32-C3 Firmware
// Biomechanical Telemetry & Rehabilitation System
//
// Serial protocol:
// ANGLE=35.6,FSR=27,MOTOR=0,REP=4,SEQ=123,TIME=4567
//
// Python application expects:
// ANGLE, FSR, MOTOR, REP
// Optional: SEQ, TIME
// ============================================================


// ------------------------------------------------------------
// Pin configuration
// ------------------------------------------------------------

#define FSR_PIN       0
#define MOTOR_PIN     4

#define SDA_PIN       8
#define SCL_PIN       9

#define MPU_ADDR      0x68


// ------------------------------------------------------------
// Serial configuration
// ------------------------------------------------------------

#define BAUD_RATE     115200


// ------------------------------------------------------------
// Sampling
// ------------------------------------------------------------

#define SAMPLE_INTERVAL_MS 100


// ------------------------------------------------------------
// MPU6050 registers
// ------------------------------------------------------------

#define MPU_PWR_MGMT_1     0x6B
#define MPU_ACCEL_XOUT_H   0x3B


// ------------------------------------------------------------
// FSR configuration
// ------------------------------------------------------------

#define FSR_MIN_VALUE     20
#define FSR_ACTIVE_VALUE  150


// ------------------------------------------------------------
// Repetition detection
// ------------------------------------------------------------
//
// A repetition is detected when the knee moves:
//
// LOW -> HIGH -> LOW
//
// Adjust these two values after observing your actual ROM.
// ------------------------------------------------------------

#define ANGLE_LOW_THRESHOLD   35.0
#define ANGLE_HIGH_THRESHOLD  70.0


// ------------------------------------------------------------
// Global state
// ------------------------------------------------------------

float angle = 0.0;

int fsrValue = 0;

int motorState = 0;

int repetitionCount = 0;

unsigned long sequenceNumber = 0;

unsigned long lastSampleTime = 0;


// ------------------------------------------------------------
// Repetition state machine
// ------------------------------------------------------------

enum RepState
{
  REP_READY,
  REP_EXTENDED
};

RepState repState = REP_READY;


// ------------------------------------------------------------
// MPU6050 initialization
// ------------------------------------------------------------

void initMPU6050()
{
  Wire.beginTransmission(MPU_ADDR);

  Wire.write(MPU_PWR_MGMT_1);
  Wire.write(0x00);

  Wire.endTransmission(true);

  delay(100);
}


// ------------------------------------------------------------
// Read accelerometer
// ------------------------------------------------------------

bool readAccelerometer(
  int16_t &accelX,
  int16_t &accelY,
  int16_t &accelZ
)
{
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(MPU_ACCEL_XOUT_H);

  if (Wire.endTransmission(false) != 0)
  {
    return false;
  }

  uint8_t received = Wire.requestFrom(
    MPU_ADDR,
    (uint8_t)6,
    (uint8_t)true
  );

  if (received != 6)
  {
    return false;
  }

  accelX = (Wire.read() << 8) | Wire.read();
  accelY = (Wire.read() << 8) | Wire.read();
  accelZ = (Wire.read() << 8) | Wire.read();

  return true;
}


// ------------------------------------------------------------
// Calculate angle from accelerometer
// ------------------------------------------------------------
//
// Uses the Y/Z acceleration components to estimate orientation.
//
// This is an engineering prototype measurement, not a
// clinically validated knee-angle measurement.
// ------------------------------------------------------------

float calculateAngle()
{
  int16_t axRaw;
  int16_t ayRaw;
  int16_t azRaw;

  if (!readAccelerometer(axRaw, ayRaw, azRaw))
  {
    return angle;
  }

  float ay = (float)ayRaw;
  float az = (float)azRaw;

  float calculatedAngle = atan2(
    fabs(ay),
    fabs(az)
  ) * 180.0 / PI;

  calculatedAngle = constrain(
    calculatedAngle,
    0.0,
    180.0
  );

  return calculatedAngle;
}


// ------------------------------------------------------------
// Read FSR
// ------------------------------------------------------------

int readFSR()
{
  int raw = analogRead(FSR_PIN);

  return constrain(raw, 0, 4095);
}


// ------------------------------------------------------------
// Determine motor state
// ------------------------------------------------------------

int calculateMotorState(int fsr)
{
  if (fsr >= FSR_ACTIVE_VALUE)
  {
    return 1;
  }

  return 0;
}


// ------------------------------------------------------------
// Repetition detection
// ------------------------------------------------------------

void updateRepetitionCount(float currentAngle)
{
  switch (repState)
  {
    case REP_READY:

      if (currentAngle >= ANGLE_HIGH_THRESHOLD)
      {
        repState = REP_EXTENDED;
      }

      break;


    case REP_EXTENDED:

      if (currentAngle <= ANGLE_LOW_THRESHOLD)
      {
        repetitionCount++;

        repState = REP_READY;
      }

      break;
  }
}


// ------------------------------------------------------------
// Send telemetry packet
// ------------------------------------------------------------

void sendTelemetry()
{
  Serial.print("ANGLE=");
  Serial.print(angle, 1);

  Serial.print(",FSR=");
  Serial.print(fsrValue);

  Serial.print(",MOTOR=");
  Serial.print(motorState);

  Serial.print(",REP=");
  Serial.print(repetitionCount);

  // Optional fields supported by Python telemetry parser
  Serial.print(",SEQ=");
  Serial.print(sequenceNumber);

  Serial.print(",TIME=");
  Serial.print(millis());

  Serial.println();
}


// ------------------------------------------------------------
// Setup
// ------------------------------------------------------------

void setup()
{
  Serial.begin(BAUD_RATE);

  delay(1000);

  // I2C
  Wire.begin(
    SDA_PIN,
    SCL_PIN
  );

  Wire.setClock(400000);

  // MPU6050
  initMPU6050();

  // FSR
  pinMode(
    FSR_PIN,
    INPUT
  );

  // Motor
  pinMode(
    MOTOR_PIN,
    OUTPUT
  );

  digitalWrite(
    MOTOR_PIN,
    LOW
  );

  Serial.println(
    "KneeGrow ESP32-C3 telemetry started"
  );
}


// ------------------------------------------------------------
// Main loop
// ------------------------------------------------------------

void loop()
{
  unsigned long currentTime = millis();

  if (
    currentTime - lastSampleTime
    >= SAMPLE_INTERVAL_MS
  )
  {
    lastSampleTime = currentTime;

    // 1. Read sensors
    angle = calculateAngle();

    fsrValue = readFSR();

    // 2. Motor control
    motorState = calculateMotorState(
      fsrValue
    );

    digitalWrite(
      MOTOR_PIN,
      motorState ? HIGH : LOW
    );

    // 3. Repetition detection
    updateRepetitionCount(
      angle
    );

    // 4. Sequence number
    sequenceNumber++;

    // 5. Send telemetry
    sendTelemetry();
  }
}
