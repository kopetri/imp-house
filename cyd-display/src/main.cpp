#include <Arduino.h>
#include <stdlib.h>
#include <SPI.h>
#define LGFX_AUTODETECT
#include <LovyanGFX.hpp>
#include <TJpg_Decoder.h>
#include <LittleFS.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <XPT2046_Touchscreen.h>
#include "MjpegStream.h"

#if __has_include("secrets.h")
#include "secrets.h"
#endif

#if __has_include("device_config.h")
#include "device_config.h"
#endif

#ifndef CYD_WIFI_SSID
#define CYD_WIFI_SSID ""
#endif

#ifndef CYD_WIFI_PASSWORD
#define CYD_WIFI_PASSWORD ""
#endif

#ifndef IMP_SERVER_HOST
#define IMP_SERVER_HOST ""
#endif

#ifndef IMP_SERVER_PORT
#define IMP_SERVER_PORT 8000
#endif

#ifndef IMP_DEVICE_TOKEN
#define IMP_DEVICE_TOKEN ""
#endif

namespace {
constexpr char StaticImagePath[] = "/IMG_1422.bmp";
constexpr uint8_t BacklightPin = 21;
constexpr uint8_t TouchSclkPin = 25;
constexpr uint8_t TouchMisoPin = 39;
constexpr uint8_t TouchMosiPin = 32;
constexpr uint8_t TouchCsPin = 33;
constexpr uint8_t TouchIrqPin = 36;
constexpr size_t MaxJpegFrameSize = 64 * 1024;
constexpr uint16_t MaxJpegBlockDimension = 16;
constexpr uint16_t CalibrationSourceWidth = 320;
constexpr uint16_t CalibrationSourceHeight = 240;
constexpr uint32_t MaxBmpDimension = 320;
constexpr size_t MaxBmpRowBytes = MaxBmpDimension * 3 + 3;
constexpr uint32_t WifiRetryIntervalMs = 20000;
constexpr uint32_t CameraRetryIntervalMs = 5000;
constexpr uint32_t TouchDebounceMs = 300;
constexpr int32_t TouchRawXMin = 200;
constexpr int32_t TouchRawXMax = 3800;
constexpr int32_t TouchRawYMin = 200;
constexpr int32_t TouchRawYMax = 3800;
constexpr size_t MaxClipItems = 5;
constexpr bool StaticImageMode = false;
constexpr bool CalibrationMode = false;

enum class UiMode : uint8_t {
    Live,
    Menu,
    ClipList,
    Playback
};

struct ClipItem {
    char id[40];
    char timestamp[24];
    char prompt[28];
    uint16_t durationSeconds;
};

LGFX display;
XPT2046_Touchscreen touch(TouchCsPin, TouchIrqPin);
MjpegStream serverStream;
uint8_t jpegFrameBuffer[MaxJpegFrameSize];
uint16_t calibrationBlock[MaxJpegBlockDimension * MaxJpegBlockDimension];
uint16_t rotatedJpegBlock[MaxJpegBlockDimension * MaxJpegBlockDimension];
uint16_t *calibrationFrameBuffer = nullptr;
bool captureCalibrationFrame = false;
UiMode uiMode = UiMode::Live;
ClipItem clipItems[MaxClipItems];
size_t clipItemCount = 0;
uint32_t clipTotal = 0;
uint32_t clipOffset = 0;
uint32_t lastTouchMs = 0;
String clipListMessage;
int32_t scaledImageHeight = 0;
int32_t frameOriginX = 0;
int32_t frameOriginY = 0;
bool wasTouched = false;
bool wifiWasConnected = false;
bool streamHasRenderedFrame = false;
bool frameSizeWarningShown = false;
bool frameDimensionsLogged = false;
uint32_t lastWifiAttemptMs = 0;
uint32_t lastCameraAttemptMs = 0;

bool tftOutput(int16_t x, int16_t y, uint16_t width, uint16_t height, uint16_t *bitmap) {
    if (width == 0 || height == 0 ||
        width > MaxJpegBlockDimension || height > MaxJpegBlockDimension) {
        return false;
    }

    for (uint16_t row = 0; row < height; ++row) {
        for (uint16_t column = 0; column < width; ++column) {
            const uint16_t outputColumn = height - 1 - row;
            const uint16_t outputRow = column;
            rotatedJpegBlock[outputRow * height + outputColumn] = bitmap[row * width + column];
        }
    }

    const int32_t outputX = frameOriginX + scaledImageHeight - y - height;
    const int32_t outputY = frameOriginY + x;
    if (captureCalibrationFrame) {
        for (uint16_t row = 0; row < width; ++row) {
            uint16_t *outputRow = calibrationFrameBuffer +
                (outputY + row) * display.width() + outputX;
            for (uint16_t column = 0; column < height; ++column) {
                outputRow[column] = rotatedJpegBlock[row * height + column];
            }
        }
    } else {
        display.pushImage(outputX, outputY, height, width, rotatedJpegBlock);
    }
    return true;
}

bool staticImageOutput(int16_t x, int16_t y, uint16_t width, uint16_t height, uint16_t *bitmap) {
    if (width == 0 || height == 0) {
        return false;
    }

    display.pushImage(x, y, width, height, bitmap);
    return true;
}

void drawStatusScreen(const char *message, uint16_t color, const char *details = nullptr) {
    display.fillScreen(TFT_BLACK);
    display.setTextColor(color, TFT_BLACK);
    display.setTextSize(2);
    display.setCursor(8, display.height() / 2 - 16);
    display.println(message);

    if (details != nullptr) {
        display.setTextSize(1);
        display.setCursor(8, display.height() / 2 + 12);
        display.println(details);
    }
}

bool hasServerConfig() {
    return IMP_SERVER_HOST[0] != '\0' && IMP_DEVICE_TOKEN[0] != '\0';
}

void drawUiButton(int16_t x, int16_t y, int16_t width, int16_t height, const char *label,
                  uint16_t fill = TFT_DARKCYAN) {
    display.fillRoundRect(x, y, width, height, 6, fill);
    display.drawRoundRect(x, y, width, height, 6, TFT_WHITE);
    display.setTextColor(TFT_WHITE, fill);
    display.setTextSize(2);
    display.setCursor(x + 10, y + (height - 16) / 2);
    display.print(label);
}

void mapTouchPoint(const TS_Point &point, int32_t &screenX, int32_t &screenY) {
    screenX = map(point.y, TouchRawYMin, TouchRawYMax, display.width() - 1, 0);
    screenY = map(point.x, TouchRawXMin, TouchRawXMax, 0, display.height() - 1);
    screenX = constrain(screenX, 0, display.width() - 1);
    screenY = constrain(screenY, 0, display.height() - 1);
}

void drawMenu() {
    display.fillScreen(TFT_BLACK);
    display.setTextColor(TFT_WHITE, TFT_BLACK);
    display.setTextSize(2);
    display.setCursor(16, 24);
    display.println("IMP HOUSE");
    drawUiButton(14, 86, display.width() - 28, 64, "LIVE VIEW", TFT_DARKGREEN);
    drawUiButton(14, 174, display.width() - 28, 64, "RECORDED CLIPS");
    display.setTextSize(1);
    display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
    display.setCursor(16, display.height() - 28);
    display.print(WiFi.status() == WL_CONNECTED ? "Wi-Fi connected" : "Wi-Fi offline");
}

void drawClipList() {
    display.fillScreen(TFT_BLACK);
    display.setTextColor(TFT_WHITE, TFT_BLACK);
    display.setTextSize(2);
    display.setCursor(8, 8);
    display.print("CLIPS ");
    display.setTextSize(1);
    display.setCursor(112, 16);
    if (clipTotal == 0) {
        display.print("0 available");
    } else {
        display.printf("%lu-%lu of %lu", static_cast<unsigned long>(clipOffset + 1),
                       static_cast<unsigned long>(clipOffset + clipItemCount),
                       static_cast<unsigned long>(clipTotal));
    }

    if (clipListMessage.length() > 0) {
        display.setTextColor(TFT_YELLOW, TFT_BLACK);
        display.setCursor(10, 42);
        display.println(clipListMessage.substring(0, 30));
    }

    constexpr int16_t rowTop = 52;
    constexpr int16_t rowHeight = 42;
    for (size_t index = 0; index < clipItemCount; ++index) {
        const int16_t y = rowTop + static_cast<int16_t>(index) * rowHeight;
        display.drawRoundRect(5, y, display.width() - 10, rowHeight - 3, 4, TFT_DARKGREY);
        display.setTextColor(TFT_WHITE, TFT_BLACK);
        display.setTextSize(1);
        display.setCursor(12, y + 4);
        display.print(clipItems[index].timestamp);
        display.print("  ");
        display.print(clipItems[index].durationSeconds);
        display.print("s");
        display.setCursor(12, y + 20);
        display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
        display.print(clipItems[index].prompt);
    }

    if (clipItemCount == 0 && clipListMessage.length() == 0) {
        display.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
        display.setCursor(12, rowTop + 12);
        display.print("No augmented clips yet");
    }

    const int16_t buttonY = display.height() - 44;
    drawUiButton(4, buttonY, 74, 38, "MENU", TFT_DARKGREY);
    drawUiButton(83, buttonY, 70, 38, "PREV", TFT_DARKGREY);
    drawUiButton(158, buttonY, 78, 38, "NEXT", TFT_DARKGREY);
}

bool fetchClipList() {
    clipItemCount = 0;
    clipListMessage = "";
    if (WiFi.status() != WL_CONNECTED) {
        clipListMessage = "Wi-Fi unavailable";
        drawClipList();
        return false;
    }
    if (!hasServerConfig()) {
        clipListMessage = "Configure device_config.h";
        drawClipList();
        return false;
    }

    char url[192];
    snprintf(url, sizeof(url), "http://%s:%u/api/device/clips?limit=%u&offset=%lu",
             IMP_SERVER_HOST, static_cast<unsigned int>(IMP_SERVER_PORT),
             static_cast<unsigned int>(MaxClipItems), static_cast<unsigned long>(clipOffset));
    HTTPClient http;
    http.setTimeout(8000);
    if (!http.begin(url)) {
        clipListMessage = "Could not start request";
        drawClipList();
        return false;
    }
    String authorization = "Bearer ";
    authorization += IMP_DEVICE_TOKEN;
    http.addHeader("Authorization", authorization);
    const int responseCode = http.GET();
    if (responseCode != HTTP_CODE_OK) {
        clipListMessage = responseCode > 0 ? String("Server error ") + responseCode : "Server unavailable";
        http.end();
        drawClipList();
        return false;
    }

    JsonDocument document;
    const DeserializationError jsonError = deserializeJson(document, http.getStream());
    http.end();
    if (jsonError) {
        clipListMessage = "Invalid clip list";
        drawClipList();
        return false;
    }

    clipTotal = document["total"] | 0U;
    JsonArray clips = document["clips"].as<JsonArray>();
    for (JsonObject clip : clips) {
        if (clipItemCount >= MaxClipItems) {
            break;
        }
        ClipItem &item = clipItems[clipItemCount];
        strlcpy(item.id, clip["id"] | "", sizeof(item.id));
        strlcpy(item.timestamp, clip["t"] | "", sizeof(item.timestamp));
        strlcpy(item.prompt, clip["p"] | "", sizeof(item.prompt));
        item.durationSeconds = clip["d"] | 0;
        if (item.id[0] != '\0') {
            ++clipItemCount;
        }
    }
    drawClipList();
    return true;
}

void drawCalibrationImage() {
    const size_t framePixels = static_cast<size_t>(display.width()) * display.height();
    calibrationFrameBuffer = static_cast<uint16_t *>(malloc(framePixels * sizeof(uint16_t)));
    if (calibrationFrameBuffer == nullptr) {
        Serial.println("Callback calibration framebuffer allocation failed.");
        drawStatusScreen("Calibration failed", TFT_RED, "No framebuffer memory");
        return;
    }

    for (size_t pixel = 0; pixel < framePixels; ++pixel) {
        calibrationFrameBuffer[pixel] = TFT_MAGENTA;
    }

    scaledImageHeight = CalibrationSourceHeight;
    frameOriginX = (display.width() - CalibrationSourceHeight) / 2;
    frameOriginY = (display.height() - CalibrationSourceWidth) / 2;
    display.fillScreen(TFT_BLACK);
    captureCalibrationFrame = true;

    for (uint16_t blockY = 0; blockY < CalibrationSourceHeight; blockY += MaxJpegBlockDimension) {
        uint16_t blockHeight = CalibrationSourceHeight - blockY;
        if (blockHeight > MaxJpegBlockDimension) {
            blockHeight = MaxJpegBlockDimension;
        }

        for (uint16_t blockX = 0; blockX < CalibrationSourceWidth; blockX += MaxJpegBlockDimension) {
            uint16_t blockWidth = CalibrationSourceWidth - blockX;
            if (blockWidth > MaxJpegBlockDimension) {
                blockWidth = MaxJpegBlockDimension;
            }

            for (uint16_t row = 0; row < blockHeight; ++row) {
                const uint16_t sourceY = blockY + row;
                const uint8_t green = static_cast<uint8_t>(sourceY * 63 / (CalibrationSourceHeight - 1));
                for (uint16_t column = 0; column < blockWidth; ++column) {
                    const uint16_t sourceX = blockX + column;
                    const uint8_t red = static_cast<uint8_t>(sourceX * 31 / (CalibrationSourceWidth - 1));
                    const uint8_t blue = ((sourceX / 16 + sourceY / 16) & 1) ? 31 : 0;
                    uint16_t color = static_cast<uint16_t>((red << 11) | (green << 5) | blue);

                    if (sourceX < 32 && sourceY < 32) color = TFT_RED;
                    else if (sourceX >= CalibrationSourceWidth - 32 && sourceY < 32) color = TFT_GREEN;
                    else if (sourceX < 32 && sourceY >= CalibrationSourceHeight - 32) color = TFT_BLUE;
                    else if (sourceX >= CalibrationSourceWidth - 32 &&
                             sourceY >= CalibrationSourceHeight - 32) color = TFT_YELLOW;
                    else if (sourceX % 40 == 0 || sourceY % 30 == 0) color = TFT_WHITE;

                    calibrationBlock[row * blockWidth + column] = color;
                }
            }

            tftOutput(blockX, blockY, blockWidth, blockHeight, calibrationBlock);
        }
    }
    captureCalibrationFrame = false;
    display.pushImage(0, 0, display.width(), display.height(), calibrationFrameBuffer);
    free(calibrationFrameBuffer);
    calibrationFrameBuffer = nullptr;

    Serial.printf("Callback calibration full-frame blit: %ux%u -> %ux%u at rotation %u.\n",
                  CalibrationSourceWidth, CalibrationSourceHeight,
                  static_cast<unsigned int>(display.width()),
                  static_cast<unsigned int>(display.height()), display.getRotation());
}

bool readBmpUint16(fs::File &file, uint16_t &value) {
    const int lowByte = file.read();
    const int highByte = file.read();
    if (lowByte < 0 || highByte < 0) {
        return false;
    }

    value = static_cast<uint16_t>(lowByte | (highByte << 8));
    return true;
}

bool readBmpUint32(fs::File &file, uint32_t &value) {
    const int byte0 = file.read();
    const int byte1 = file.read();
    const int byte2 = file.read();
    const int byte3 = file.read();
    if (byte0 < 0 || byte1 < 0 || byte2 < 0 || byte3 < 0) {
        return false;
    }

    value = static_cast<uint32_t>(byte0) |
            (static_cast<uint32_t>(byte1) << 8) |
            (static_cast<uint32_t>(byte2) << 16) |
            (static_cast<uint32_t>(byte3) << 24);
    return true;
}

void drawStaticImage() {
    if (!LittleFS.begin(false)) {
        drawStatusScreen("Image storage failed", TFT_RED, "Upload LittleFS image");
        Serial.println("LittleFS mount failed.");
        return;
    }

    fs::File bitmap = LittleFS.open(StaticImagePath, FILE_READ);
    if (!bitmap) {
        drawStatusScreen("Image unavailable", TFT_RED, "Upload LittleFS image");
        Serial.printf("Could not open static BMP: %s\n", StaticImagePath);
        return;
    }

    uint16_t signature = 0;
    uint16_t planes = 0;
    uint16_t bitsPerPixel = 0;
    uint32_t pixelDataOffset = 0;
    uint32_t headerSize = 0;
    uint32_t imageWidth = 0;
    uint32_t rawImageHeight = 0;
    uint32_t compression = 0;
    const bool headerRead = readBmpUint16(bitmap, signature) &&
        signature == 0x4D42 && bitmap.seek(10) &&
        readBmpUint32(bitmap, pixelDataOffset) &&
        readBmpUint32(bitmap, headerSize) &&
        readBmpUint32(bitmap, imageWidth) &&
        readBmpUint32(bitmap, rawImageHeight) &&
        readBmpUint16(bitmap, planes) &&
        readBmpUint16(bitmap, bitsPerPixel) &&
        readBmpUint32(bitmap, compression);
    const bool topDown = (rawImageHeight & 0x80000000UL) != 0;
    const uint32_t imageHeight = topDown ? (~rawImageHeight + 1U) : rawImageHeight;
    if (!headerRead || headerSize < 40 || imageWidth == 0 || imageHeight == 0 ||
        (imageWidth & 0x80000000UL) != 0 || imageWidth > MaxBmpDimension ||
        imageHeight > MaxBmpDimension || planes != 1 || bitsPerPixel != 24 || compression != 0) {
        drawStatusScreen("Unsupported BMP", TFT_RED, "Expected 24-bit image");
        Serial.println("Static BMP header is invalid or unsupported.");
        return;
    }

    const uint32_t rowStride = ((imageWidth * 3U + 3U) / 4U) * 4U;
    const uint64_t imageEnd = static_cast<uint64_t>(pixelDataOffset) +
                              static_cast<uint64_t>(rowStride) * imageHeight;
    if (imageEnd > bitmap.size()) {
        drawStatusScreen("Truncated BMP", TFT_RED);
        Serial.println("Static BMP pixel data is incomplete.");
        return;
    }

    const uint32_t outputWidth = imageWidth < static_cast<uint32_t>(display.width())
        ? imageWidth : static_cast<uint32_t>(display.width());
    const uint32_t outputHeight = imageHeight < static_cast<uint32_t>(display.height())
        ? imageHeight : static_cast<uint32_t>(display.height());
    const uint32_t sourceLeft = (imageWidth - outputWidth) / 2;
    const uint32_t sourceTop = (imageHeight - outputHeight) / 2;
    const int32_t targetLeft = (display.width() - outputWidth) / 2;
    const int32_t targetTop = (display.height() - outputHeight) / 2;
    uint8_t rowBuffer[MaxBmpRowBytes];

    display.fillScreen(TFT_BLACK);
    for (uint32_t row = 0; row < outputHeight; ++row) {
        const uint32_t sourceRow = sourceTop + row;
        const uint32_t bitmapRow = topDown ? sourceRow : imageHeight - sourceRow - 1;
        const uint32_t rowOffset = pixelDataOffset + bitmapRow * rowStride;
        if (!bitmap.seek(rowOffset) || bitmap.read(rowBuffer, rowStride) != rowStride) {
            drawStatusScreen("BMP read failed", TFT_RED);
            Serial.println("Could not read a static BMP row.");
            return;
        }

        display.startWrite();
        display.setAddrWindow(targetLeft, targetTop + row, outputWidth, 1);
        for (uint32_t column = 0; column < outputWidth; ++column) {
            const size_t pixelOffset = static_cast<size_t>(sourceLeft + column) * 3;
            const uint8_t blue = rowBuffer[pixelOffset];
            const uint8_t green = rowBuffer[pixelOffset + 1];
            const uint8_t red = rowBuffer[pixelOffset + 2];
            display.writeColor(display.color565(red, green, blue), 1);
        }
        display.endWrite();
    }

    bitmap.close();
    Serial.printf("Static BMP %lux%lu streamed to the %ux%u display with LovyanGFX.\n",
                  static_cast<unsigned long>(imageWidth),
                  static_cast<unsigned long>(imageHeight),
                  static_cast<unsigned int>(display.width()),
                  static_cast<unsigned int>(display.height()));
}

bool hasWifiCredentials() {
    return CYD_WIFI_SSID[0] != '\0';
}

void beginWifiAttempt() {
    if (CYD_WIFI_PASSWORD[0] == '\0') {
        WiFi.begin(CYD_WIFI_SSID);
    } else {
        WiFi.begin(CYD_WIFI_SSID, CYD_WIFI_PASSWORD);
    }
    lastWifiAttemptMs = millis();
}

void startWifi() {
    if (!hasWifiCredentials()) {
        drawStatusScreen("Wi-Fi not configured", TFT_YELLOW, "Edit include/secrets.h");
        Serial.println("WiFi credentials are blank; configure include/secrets.h.");
        return;
    }

    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    beginWifiAttempt();
    drawStatusScreen("Connecting Wi-Fi", TFT_YELLOW);
    Serial.println("WiFi: connecting...");
}

void updateWifiStatus() {
    if (!hasWifiCredentials()) {
        return;
    }

    if (WiFi.status() == WL_CONNECTED) {
        if (!wifiWasConnected) {
            wifiWasConnected = true;
            const IPAddress ip = WiFi.localIP();
            char ipAddress[16];
            snprintf(ipAddress, sizeof(ipAddress), "%u.%u.%u.%u",
                     static_cast<unsigned int>(ip[0]), static_cast<unsigned int>(ip[1]),
                     static_cast<unsigned int>(ip[2]), static_cast<unsigned int>(ip[3]));
            if (uiMode == UiMode::Live) {
                drawStatusScreen("Wi-Fi connected", TFT_GREEN, ipAddress);
            }
            Serial.printf("WiFi connected. IP: %s\n", ipAddress);
        }
        return;
    }

    if (wifiWasConnected) {
        wifiWasConnected = false;
        if (uiMode == UiMode::Live) {
            drawStatusScreen("Wi-Fi disconnected", TFT_YELLOW, "Reconnecting...");
        }
        Serial.println("WiFi disconnected; reconnecting.");
    }

    if (millis() - lastWifiAttemptMs >= WifiRetryIntervalMs) {
        beginWifiAttempt();
        drawStatusScreen("Retrying Wi-Fi", TFT_YELLOW);
        Serial.println("WiFi: retrying connection.");
    }
}

bool drawJpegFrame(const uint8_t *frame, size_t frameSize) {
    uint16_t imageWidth = 0;
    uint16_t imageHeight = 0;
    if (TJpgDec.getJpgSize(&imageWidth, &imageHeight, frame, frameSize) != JDR_OK) {
        return false;
    }

    uint8_t scale = 1;
    while (scale < 8 &&
           ((imageHeight + scale - 1) / scale > display.width() ||
            (imageWidth + scale - 1) / scale > display.height())) {
        scale *= 2;
    }

    TJpgDec.setJpgScale(scale);
    const int32_t scaledImageWidth = (imageWidth + scale - 1) / scale;
    scaledImageHeight = (imageHeight + scale - 1) / scale;
    const int32_t rotatedWidth = scaledImageHeight;
    const int32_t rotatedHeight = scaledImageWidth;
    frameOriginX = (display.width() - rotatedWidth) / 2;
    frameOriginY = (display.height() - rotatedHeight) / 2;
    const JRESULT result = TJpgDec.drawJpg(0, 0, frame, frameSize);

    if (!frameDimensionsLogged) {
        Serial.printf("JPEG %ux%u rotated to %ldx%ld on %ux%u display at decoder scale %u; result=%d.\n",
                      static_cast<unsigned int>(imageWidth),
                      static_cast<unsigned int>(imageHeight),
                      static_cast<long>(rotatedWidth),
                      static_cast<long>(rotatedHeight),
                      static_cast<unsigned int>(display.width()),
                      static_cast<unsigned int>(display.height()),
                      static_cast<unsigned int>(scale),
                      static_cast<int>(result));
        frameDimensionsLogged = true;
    }

    return result == JDR_OK;
}

bool connectRelayStream(const char *path) {
    if (!hasServerConfig()) {
        drawStatusScreen("Server not configured", TFT_YELLOW, "Edit device_config.h");
        return false;
    }
    drawStatusScreen("Connecting server", TFT_YELLOW, IMP_SERVER_HOST);
    Serial.println("Connecting to imp-house MJPEG endpoint.");
    if (!serverStream.connect(IMP_SERVER_HOST, IMP_SERVER_PORT, path, IMP_DEVICE_TOKEN)) {
        drawStatusScreen("Server unavailable", TFT_RED, "Check address and token");
        Serial.println("Server MJPEG connection failed.");
        return false;
    }

    streamHasRenderedFrame = false;
    frameSizeWarningShown = false;
    frameDimensionsLogged = false;
    Serial.println("Server MJPEG stream connected.");
    return true;
}

void updateLiveStream() {
    if (uiMode != UiMode::Live) {
        return;
    }
    if (WiFi.status() != WL_CONNECTED) {
        if (serverStream.connected()) {
            serverStream.stop();
            streamHasRenderedFrame = false;
        }
        return;
    }

    if (!serverStream.connected()) {
        if (millis() - lastCameraAttemptMs < CameraRetryIntervalMs) {
            return;
        }
        lastCameraAttemptMs = millis();
        connectRelayStream("/api/device/live.mjpeg");
        return;
    }

    size_t frameSize = 0;
    const MjpegStream::FrameResult result = serverStream.readFrame(
        jpegFrameBuffer, sizeof(jpegFrameBuffer), frameSize);
    if (result == MjpegStream::FrameResult::Ready) {
        if (!streamHasRenderedFrame) {
            display.fillScreen(TFT_BLACK);
        }
        if (!drawJpegFrame(jpegFrameBuffer, frameSize)) {
            Serial.println("JPEG frame decode failed.");
            streamHasRenderedFrame = false;
        } else {
            streamHasRenderedFrame = true;
        }
        frameSizeWarningShown = false;
        return;
    }

    if (result == MjpegStream::FrameResult::TooLarge) {
        if (!frameSizeWarningShown) {
            drawStatusScreen("JPEG frame too large", TFT_RED, "Relay frame over 64 KiB");
            Serial.println("JPEG frame exceeds the 64 KiB buffer.");
            frameSizeWarningShown = true;
        }
        return;
    }

    serverStream.stop();
    streamHasRenderedFrame = false;
    lastCameraAttemptMs = millis();
    drawStatusScreen("Live stream lost", TFT_YELLOW, "Reconnecting...");
    Serial.println("Live stream lost; reconnecting.");
}

void enterLive() {
    serverStream.stop();
    uiMode = UiMode::Live;
    lastCameraAttemptMs = millis() - CameraRetryIntervalMs;
    streamHasRenderedFrame = false;
    display.fillScreen(TFT_BLACK);
}

void enterPlayback(size_t index) {
    if (index >= clipItemCount || WiFi.status() != WL_CONNECTED || !hasServerConfig()) {
        return;
    }
    char path[128];
    snprintf(path, sizeof(path), "/api/device/clips/%s/playback.mjpeg", clipItems[index].id);
    serverStream.stop();
    uiMode = UiMode::Playback;
    drawStatusScreen("Loading clip", TFT_YELLOW, clipItems[index].timestamp);
    if (!connectRelayStream(path)) {
        uiMode = UiMode::ClipList;
        clipListMessage = "Playback connection failed";
        drawClipList();
    }
}

void updatePlaybackStream() {
    if (uiMode != UiMode::Playback) {
        return;
    }
    size_t frameSize = 0;
    const MjpegStream::FrameResult result = serverStream.readFrame(
        jpegFrameBuffer, sizeof(jpegFrameBuffer), frameSize);
    if (result == MjpegStream::FrameResult::Ready) {
        if (!drawJpegFrame(jpegFrameBuffer, frameSize)) {
            clipListMessage = "Clip frame decode failed";
            serverStream.stop();
            uiMode = UiMode::ClipList;
            drawClipList();
        }
        return;
    }
    if (result == MjpegStream::FrameResult::EndOfStream) {
        serverStream.stop();
        uiMode = UiMode::ClipList;
        clipListMessage = "Playback finished";
        drawClipList();
        return;
    }
    if (result == MjpegStream::FrameResult::TooLarge) {
        clipListMessage = "Playback frame over 64 KiB";
    } else {
        clipListMessage = "Playback stream interrupted";
    }
    serverStream.stop();
    uiMode = UiMode::ClipList;
    drawClipList();
}

void handleTouch(int32_t x, int32_t y) {
    if (uiMode == UiMode::Live) {
        serverStream.stop();
        uiMode = UiMode::Menu;
        drawMenu();
        return;
    }

    if (uiMode == UiMode::Menu) {
        if (y >= 86 && y < 150) {
            enterLive();
        } else if (y >= 174 && y < 238) {
            uiMode = UiMode::ClipList;
            clipOffset = 0;
            clipTotal = 0;
            fetchClipList();
        }
        return;
    }

    if (uiMode == UiMode::ClipList) {
        const int16_t buttonY = display.height() - 44;
        if (y >= buttonY) {
            if (x < 80) {
                uiMode = UiMode::Menu;
                drawMenu();
            } else if (x < 158 && clipOffset >= MaxClipItems) {
                clipOffset -= MaxClipItems;
                fetchClipList();
            } else if (x >= 158 && clipOffset + MaxClipItems < clipTotal) {
                clipOffset += MaxClipItems;
                fetchClipList();
            }
            return;
        }
        constexpr int16_t rowTop = 52;
        constexpr int16_t rowHeight = 42;
        if (y >= rowTop) {
            const size_t index = static_cast<size_t>((y - rowTop) / rowHeight);
            enterPlayback(index);
        }
        return;
    }

    if (uiMode == UiMode::Playback) {
        serverStream.stop();
        uiMode = UiMode::ClipList;
        clipListMessage = "";
        drawClipList();
    }
}
}

void setup() {
    Serial.begin(115200);

    display.init();
    display.setRotation(3);
    if (display.width() > display.height()) {
        display.setRotation(2);
    }
    display.setBrightness(255);
    pinMode(BacklightPin, OUTPUT);
    digitalWrite(BacklightPin, HIGH);
    TJpgDec.setSwapBytes(true);
    TJpgDec.setCallback(tftOutput);
    if (StaticImageMode) {
        drawStaticImage();
    } else if (CalibrationMode) {
        drawCalibrationImage();
    } else {
        drawStatusScreen("Starting CYD", TFT_WHITE);
    }

    SPI.begin(TouchSclkPin, TouchMisoPin, TouchMosiPin, -1);
    touch.begin();
    if (!StaticImageMode && !CalibrationMode) {
        startWifi();
    }
    Serial.println("CYD camera display ready. Touch the screen to open the menu.");
}

void loop() {
    if (StaticImageMode || CalibrationMode) {
        delay(100);
        return;
    }

    updateWifiStatus();
    const bool isTouched = touch.touched();
    if (isTouched && !wasTouched && millis() - lastTouchMs >= TouchDebounceMs) {
        lastTouchMs = millis();
        const TS_Point point = touch.getPoint();
        int32_t screenX = 0;
        int32_t screenY = 0;
        mapTouchPoint(point, screenX, screenY);
        Serial.printf("Touch raw: X=%d Y=%d Z=%d; screen: X=%ld Y=%ld\n",
                      point.x, point.y, point.z, static_cast<long>(screenX), static_cast<long>(screenY));
        handleTouch(screenX, screenY);
    }

    wasTouched = isTouched;
    if (uiMode == UiMode::Live) {
        updateLiveStream();
    } else if (uiMode == UiMode::Playback) {
        updatePlaybackStream();
    }
    delay(10);
}