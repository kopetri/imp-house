# CYD Display PlatformIO Starter Design

## Goal

Create an independent PlatformIO project for the common 2.8-inch
ESP32-2432S028R CYD without changing the existing Python project.

## Structure and behavior

- Use the Arduino framework on PlatformIO's generic `esp32dev` board target.
- Configure `TFT_eSPI` for the ILI9341 display on HSPI and common CYD display pins.
- Use `XPT2046_Touchscreen` for raw readings on the separate VSPI touch pins.
- Read Wi-Fi credentials from a local ignored header and keep an example template tracked.
- Show Wi-Fi state and local IP on the display and serial monitor without logging credentials.
- Retry disconnected Wi-Fi connections without blocking touch handling.
- Connect directly to the camera's chunked multipart MJPEG endpoint, rotate
  decoded blocks 90 degrees clockwise, and render at native resolution on the
  240x320 portrait panel through `TJpg_Decoder` and TFT_eSPI.
- Keep a bounded frame buffer and reconnect to the camera if the stream drops.
- Leave touch calibration to a later application feature.
- Keep generated PlatformIO build files ignored within the subproject.

## Validation

Build with `pio run`, upload to the CYD, confirm live MJPEG frames render,
then touch the panel and confirm one raw X/Y/Z line per press at 115200 baud.
With local Wi-Fi credentials configured, verify the connected state and IP
on the display and serial monitor. The pin map is a common-board assumption
and must be checked against the physical board if behavior differs.