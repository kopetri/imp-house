#pragma once

#include <Arduino.h>
#include <WiFiClient.h>

class MjpegStream {
public:
    enum class FrameResult : uint8_t {
        Ready,
        TooLarge,
        EndOfStream,
        Error
    };

    bool connect(const char *host, uint16_t port, const char *path, const char *bearerToken = nullptr);
    FrameResult readFrame(uint8_t *buffer, size_t capacity, size_t &frameSize);
    bool connected();
    void stop();

private:
    bool readClientLine(String &line);
    bool readBodyLine(String &line);
    int readClientByte();
    int readBodyByte();
    bool readBodyBytes(uint8_t *buffer, size_t length);
    bool discardBodyBytes(size_t length);
    bool parseBoundary(const String &contentType);

    WiFiClient client_;
    String boundaryLine_;
    size_t chunkRemaining_ = 0;
    bool chunkedTransfer_ = false;
    bool needsChunkTerminator_ = false;
    bool transferEnded_ = false;
};