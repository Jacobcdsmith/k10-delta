---
name: Voice IO
description: Real-time audio streaming with Opus codec over WebSocket for speech-to-text and text-to-speech.
---

# Voice IO Skill

Handles bidirectional audio streaming for the voice assistant.

## Capabilities

- Opus audio encoding/decoding
- Real-time WebSocket streaming
- Microphone input capture
- Speaker output playback

## Protocol

Uses `type: "audio"` messages with binary Opus frames over WebSocket. See `docs/websocket.md` for wire format.