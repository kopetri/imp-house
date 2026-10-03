# CYD Display

A PlatformIO starter for the common 2.8-inch ESP32-2432S028R CYD with an
ILI9341 display and XPT2046 touch controller.

The firmware displays the camera's live MJPEG stream by default. Touch the
display to open the menu and browse augmented clips. Set `StaticImageMode` to
`true` in `src/main.cpp` to show `IMG_1422.jpg` instead.

## Pin assumptions

These are common ESP32-2432S028R connections. Board revisions and clones may
differ.

| Signal | GPIO |
| --- | ---: |
| TFT MISO | 12 |
| TFT MOSI | 13 |
| TFT SCLK | 14 |
| TFT CS | 15 |
| TFT DC | 2 |
| TFT reset | Not connected (`-1`) |
| Backlight | 21 |
| Touch SCLK | 25 |
| Touch MOSI | 32 |
| Touch MISO | 39 |
| Touch CS | 33 |
| Touch IRQ | 36 |

LovyanGFX auto-detects the Sunton ESP32-2432S028 display controller. The
firmware also drives its GPIO 21 backlight high explicitly. Touch uses a
separate SPI bus with its pins defined near the top of `src/main.cpp`.

## Static image (optional)

Set `StaticImageMode` to `true` and leave `CalibrationMode` false to display
the bundled photo instead of the camera.

`data/IMG_1422.jpg` is the original photo. The 240x320, 24-bit BMP display
copy lives at `display-data/IMG_1422.bmp` and is stored in LittleFS. Upload
the filesystem with `pio run --target uploadfs` whenever you change that
display copy. The BMP is streamed a row at a time; no full-screen framebuffer
or SD card is required.

## Camera stream (default)

Camera mode runs when both `StaticImageMode` and `CalibrationMode` are false.
Set `CalibrationMode` to `true` with `StaticImageMode` false to show the
synthetic gradient/grid test image.

The sketch connects to the imp-house server relay, not directly to the camera.
The server holds the camera's single stream connection and relays it to the
CYD and browser. Live and recorded playback use authenticated multipart MJPEG;
decoded frames are rotated onto the 240x320 portrait panel. Playback returns
to the clip list when the response ends. Frames larger than 64 KiB are skipped
with a screen and serial warning.

Copy `include/device_config.example.h` to `include/device_config.h` and set
`IMP_SERVER_HOST`, `IMP_SERVER_PORT`, and `IMP_DEVICE_TOKEN`. Create a device
in the web UI and copy its token; it is only shown once. The local
`device_config.h` is git-ignored. Touch coordinates use the raw calibration
bounds near the top of `src/main.cpp`; the serial log prints both raw and
mapped coordinates if a board revision needs different bounds.

## Camera mode Wi-Fi

Enter credentials in the local, git-ignored `include/secrets.h`. The file is
created with blank values in this checkout; on a fresh checkout, use
`include/secrets.example.h` as its template. Keep real credentials out of
tracked files. The ESP32 connects to 2.4 GHz Wi-Fi only.

With a nonblank SSID, camera mode connects in station mode and retries if
disconnected. The screen and serial monitor show connection status and the
local IP address, but never the password. With blank credentials, camera mode
shows a setup reminder and does not connect to the camera.

## Build and upload

From this directory, build the firmware, upload the image filesystem and
firmware, then start the serial monitor:

```sh
pio run
pio run --target uploadfs
pio run --target upload
pio device monitor
```

The serial monitor uses 115200 baud. If upload needs an explicit serial port,
add `--upload-port /dev/cu.usbserial-...` to each upload command. With Wi-Fi
credentials configured, the camera stream should appear after startup.

If the display stays blank, verify the board revision and its pinout before
changing the assumptions above.