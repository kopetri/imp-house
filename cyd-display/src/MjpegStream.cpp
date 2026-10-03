#include "MjpegStream.h"

#include <stdlib.h>

namespace {
constexpr uint32_t IoTimeoutMs = 5000;
constexpr size_t MaxLineLength = 512;
constexpr size_t MaxDiscardSize = 1024 * 1024;
}

bool MjpegStream::connect(const char *host, uint16_t port, const char *path, const char *bearerToken) {
    stop();

    if (!client_.connect(host, port)) {
        Serial.printf("Camera TCP connection failed: %s:%u\n", host,
                      static_cast<unsigned int>(port));
        return false;
    }
    client_.setNoDelay(true);

    client_.print("GET ");
    client_.print(path);
    client_.println(" HTTP/1.1");
    client_.print("Host: ");
    client_.print(host);
    client_.print(':');
    client_.println(static_cast<unsigned int>(port));
    client_.println("Accept: multipart/x-mixed-replace");
    if (bearerToken != nullptr && bearerToken[0] != '\0') {
        client_.print("Authorization: Bearer ");
        client_.println(bearerToken);
    }
    client_.println("Connection: keep-alive");
    client_.println();

    String line;
    if (!readClientLine(line)) {
        Serial.println("Camera returned no HTTP status line.");
        stop();
        return false;
    }

    const int statusSeparator = line.indexOf(' ');
    if (!line.startsWith("HTTP/1.") || statusSeparator < 0 ||
        line.substring(statusSeparator + 1).toInt() != 200) {
        Serial.printf("Camera returned an unexpected response: %s\n", line.c_str());
        stop();
        return false;
    }

    String contentType;
    bool headersComplete = false;
    while (readClientLine(line)) {
        if (line.length() == 0) {
            headersComplete = true;
            break;
        }

        const int separator = line.indexOf(':');
        if (separator < 0) {
            continue;
        }

        String name = line.substring(0, separator);
        String value = line.substring(separator + 1);
        name.trim();
        value.trim();
        name.toLowerCase();

        if (name == "content-type") {
            contentType = value;
        } else if (name == "transfer-encoding") {
            value.toLowerCase();
            chunkedTransfer_ = value.indexOf("chunked") >= 0;
        }
    }

    if (!headersComplete) {
        Serial.println("Camera response headers timed out.");
        stop();
        return false;
    }
    if (!parseBoundary(contentType)) {
        Serial.printf("Camera returned an unsupported content type: %s\n", contentType.c_str());
        stop();
        return false;
    }

    return true;
}

MjpegStream::FrameResult MjpegStream::readFrame(uint8_t *buffer, size_t capacity, size_t &frameSize) {
    frameSize = 0;
    String line;
    bool foundBoundary = false;

    while (readBodyLine(line)) {
        line.trim();
        if (line == boundaryLine_ + "--") {
            transferEnded_ = true;
            return FrameResult::EndOfStream;
        }
        if (line == boundaryLine_) {
            foundBoundary = true;
            break;
        }
    }

    if (!foundBoundary) {
        return transferEnded_ || !client_.connected() ? FrameResult::EndOfStream : FrameResult::Error;
    }

    size_t contentLength = 0;
    bool hasContentLength = false;
    while (readBodyLine(line)) {
        if (line.length() == 0) {
            break;
        }

        const int separator = line.indexOf(':');
        if (separator < 0) {
            continue;
        }

        String name = line.substring(0, separator);
        name.trim();
        name.toLowerCase();
        if (name != "content-length") {
            continue;
        }

        String value = line.substring(separator + 1);
        value.trim();
        const char *valueText = value.c_str();
        char *end = nullptr;
        const unsigned long parsedLength = strtoul(valueText, &end, 10);
        if (end == valueText || *end != '\0') {
            return FrameResult::Error;
        }
        contentLength = static_cast<size_t>(parsedLength);
        hasContentLength = true;
    }

    if (!hasContentLength || contentLength == 0 || contentLength > MaxDiscardSize) {
        return FrameResult::Error;
    }

    if (contentLength > capacity) {
        return discardBodyBytes(contentLength) ? FrameResult::TooLarge : FrameResult::Error;
    }

    if (!readBodyBytes(buffer, contentLength)) {
        return FrameResult::Error;
    }

    frameSize = contentLength;
    return FrameResult::Ready;
}

bool MjpegStream::connected() {
    return client_.connected();
}

void MjpegStream::stop() {
    client_.stop();
    boundaryLine_ = "";
    chunkRemaining_ = 0;
    chunkedTransfer_ = false;
    needsChunkTerminator_ = false;
    transferEnded_ = false;
}

bool MjpegStream::readClientLine(String &line) {
    line = "";
    while (true) {
        const int value = readClientByte();
        if (value < 0) {
            return false;
        }
        if (value == '\n') {
            if (line.endsWith("\r")) {
                line.remove(line.length() - 1);
            }
            return true;
        }
        if (line.length() >= MaxLineLength) {
            return false;
        }
        line += static_cast<char>(value);
    }
}

bool MjpegStream::readBodyLine(String &line) {
    line = "";
    while (true) {
        const int value = readBodyByte();
        if (value < 0) {
            return false;
        }
        if (value == '\n') {
            if (line.endsWith("\r")) {
                line.remove(line.length() - 1);
            }
            return true;
        }
        if (line.length() >= MaxLineLength) {
            return false;
        }
        line += static_cast<char>(value);
    }
}

int MjpegStream::readClientByte() {
    const uint32_t startedAt = millis();
    while (millis() - startedAt < IoTimeoutMs) {
        if (client_.available() > 0) {
            return client_.read();
        }
        if (!client_.connected()) {
            return -1;
        }
        delay(1);
    }
    return -1;
}

int MjpegStream::readBodyByte() {
    if (transferEnded_) {
        return -1;
    }
    if (!chunkedTransfer_) {
        return readClientByte();
    }

    if (chunkRemaining_ == 0) {
        if (needsChunkTerminator_) {
            if (readClientByte() != '\r' || readClientByte() != '\n') {
                return -1;
            }
            needsChunkTerminator_ = false;
        }

        String chunkHeader;
        if (!readClientLine(chunkHeader)) {
            return -1;
        }
        const int extension = chunkHeader.indexOf(';');
        if (extension >= 0) {
            chunkHeader.remove(extension);
        }
        chunkHeader.trim();

        const char *chunkText = chunkHeader.c_str();
        char *end = nullptr;
        const unsigned long chunkSize = strtoul(chunkText, &end, 16);
        if (end == chunkText) {
            return -1;
        }
        if (chunkSize == 0) {
            transferEnded_ = true;
            return -1;
        }
        chunkRemaining_ = static_cast<size_t>(chunkSize);
    }

    const int value = readClientByte();
    if (value < 0) {
        return -1;
    }

    --chunkRemaining_;
    if (chunkRemaining_ == 0) {
        needsChunkTerminator_ = true;
    }
    return value;
}

bool MjpegStream::readBodyBytes(uint8_t *buffer, size_t length) {
    for (size_t index = 0; index < length; ++index) {
        const int value = readBodyByte();
        if (value < 0) {
            return false;
        }
        buffer[index] = static_cast<uint8_t>(value);
    }
    return true;
}

bool MjpegStream::discardBodyBytes(size_t length) {
    while (length-- > 0) {
        if (readBodyByte() < 0) {
            return false;
        }
    }
    return true;
}

bool MjpegStream::parseBoundary(const String &contentType) {
    String lowerContentType = contentType;
    lowerContentType.toLowerCase();
    const int boundaryStart = lowerContentType.indexOf("boundary=");
    if (!lowerContentType.startsWith("multipart/") || boundaryStart < 0) {
        return false;
    }

    String boundary = contentType.substring(boundaryStart + 9);
    const int parameterEnd = boundary.indexOf(';');
    if (parameterEnd >= 0) {
        boundary.remove(parameterEnd);
    }
    boundary.trim();
    if (boundary.startsWith("\"") && boundary.endsWith("\"")) {
        boundary = boundary.substring(1, boundary.length() - 1);
    }

    boundaryLine_ = boundary.startsWith("--") ? boundary : "--" + boundary;
    return boundaryLine_.length() > 2;
}