---
name: Sensor Poll
description: Poll temperature, humidity, light, and accelerometer sensors on the UNIHIKER K10 device and return structured readings.
---

# Sensor Poll Skill

Polls all available hardware sensors on the K10 device.

## Capabilities

- Temperature and humidity via SHT30 sensor
- Light intensity via BH1750 sensor  
- Accelerometer X/Y/Z axis data

## Usage

Call via MCP: `sensor.poll` - returns JSON with all sensor readings.