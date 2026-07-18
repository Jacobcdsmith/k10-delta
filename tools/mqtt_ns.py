"""mqtt.* — publish / subscribe helpers."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from typing import Any

from .schema import _p, tool

try:
    import paho.mqtt.client as _paho_mqtt

    _HAS_PAHO = True
except ImportError:
    _paho_mqtt = None
    _HAS_PAHO = False


def _mqtt_encode_remaining_length(length: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length > 0:
            digit |= 0x80
        encoded.append(digit)
        if length == 0:
            break
    return bytes(encoded)


def _mqtt_encode_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def _mqtt_decode_remaining_length(sock: socket.socket) -> int:
    multiplier = 1
    value = 0
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("MQTT socket closed")
        digit = chunk[0]
        value += (digit & 127) * multiplier
        multiplier *= 128
        if (digit & 128) == 0:
            break
    return value


def _mqtt_recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("MQTT socket closed")
        data.extend(chunk)
    return bytes(data)


def _mqtt_connect(sock: socket.socket, client_id: str) -> None:
    payload = (
        _mqtt_encode_string("MQTT")
        + bytes([4])
        + bytes([2])
        + struct.pack(">H", 60)
        + _mqtt_encode_string(client_id)
    )
    packet = bytes([0x10]) + _mqtt_encode_remaining_length(len(payload)) + payload
    sock.sendall(packet)
    header = sock.recv(1)
    if not header or header[0] != 0x20:
        raise ConnectionError("MQTT broker did not send CONNACK")
    remaining = _mqtt_decode_remaining_length(sock)
    _mqtt_recv_exact(sock, remaining)


def _mqtt_publish_packet(topic: str, payload: str | bytes, qos: int = 0) -> bytes:
    body = payload.encode("utf-8") if isinstance(payload, str) else payload
    variable = _mqtt_encode_string(topic)
    if qos > 0:
        variable += struct.pack(">H", 1)
    remaining = variable + body
    flags = (qos & 0x03) << 1
    return bytes([0x30 | flags]) + _mqtt_encode_remaining_length(len(remaining)) + remaining


def _mqtt_subscribe_packet(topic: str, packet_id: int = 1, qos: int = 0) -> bytes:
    variable = struct.pack(">H", packet_id)
    payload = _mqtt_encode_string(topic) + bytes([qos & 0x03])
    remaining = variable + payload
    return bytes([0x82]) + _mqtt_encode_remaining_length(len(remaining)) + remaining


def _mqtt_parse_publish(flags: int, data: bytes) -> tuple[str, str]:
    qos = (flags >> 1) & 0x03
    offset = 0
    topic_len = struct.unpack(">H", data[offset : offset + 2])[0]
    offset += 2
    topic = data[offset : offset + topic_len].decode("utf-8", errors="replace")
    offset += topic_len
    if qos > 0:
        offset += 2
    message = data[offset:].decode("utf-8", errors="replace")
    return topic, message


def _mqtt_wait_publish(sock: socket.socket, timeout: float) -> tuple[str, str] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())
        sock.settimeout(remaining)
        try:
            header = sock.recv(1)
            if not header:
                return None
            msg_type = (header[0] >> 4) & 0x0F
            packet_len = _mqtt_decode_remaining_length(sock)
            data = _mqtt_recv_exact(sock, packet_len)
            if msg_type == 3:
                return _mqtt_parse_publish(header[0], data)
        except socket.timeout:
            continue
        except OSError:
            break
    return None


def _mqtt_publish_stdlib(broker: str, port: int, topic: str, payload: str, qos: int) -> dict:
    sock = socket.create_connection((broker, port), timeout=10)
    try:
        _mqtt_connect(sock, f"k10d-pub-{int(time.time())}")
        sock.sendall(_mqtt_publish_packet(topic, payload, qos=qos))
        return {"ok": True, "broker": broker, "topic": topic}
    finally:
        sock.close()


def _mqtt_subscribe_stdlib(broker: str, port: int, topic: str, timeout: float) -> dict:
    sock = socket.create_connection((broker, port), timeout=10)
    try:
        _mqtt_connect(sock, f"k10d-sub-{int(time.time())}")
        sock.sendall(_mqtt_subscribe_packet(topic))
        header = sock.recv(1)
        if not header or header[0] != 0x90:
            return {
                "ok": False,
                "message": None,
                "topic": topic,
                "error": "MQTT SUBACK missing",
            }
        remaining = _mqtt_decode_remaining_length(sock)
        _mqtt_recv_exact(sock, remaining)
        received = _mqtt_wait_publish(sock, timeout)
        if not received:
            return {
                "ok": False,
                "message": None,
                "topic": topic,
                "error": f"timed out after {timeout}s",
            }
        recv_topic, message = received
        return {"ok": True, "message": message, "topic": recv_topic}
    finally:
        sock.close()


def _mqtt_publish_paho(broker: str, port: int, topic: str, payload: str, qos: int) -> dict:
    try:
        client = _paho_mqtt.Client(_paho_mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        client = _paho_mqtt.Client()
    try:
        client.connect(broker, int(port), 60)
        client.publish(topic, payload, qos=qos)
        client.loop(timeout=1.0)
        return {"ok": True, "broker": broker, "topic": topic}
    finally:
        client.disconnect()


def _mqtt_subscribe_paho(broker: str, port: int, topic: str, timeout: float) -> dict:
    result: dict[str, Any] = {"ok": False, "message": None, "topic": topic}
    done = threading.Event()

    def on_message(_client, _userdata, msg) -> None:
        result["message"] = msg.payload.decode("utf-8", errors="replace")
        result["topic"] = msg.topic
        result["ok"] = True
        done.set()

    try:
        client = _paho_mqtt.Client(_paho_mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        client = _paho_mqtt.Client()
    client.on_message = on_message
    try:
        client.connect(broker, int(port), 60)
        client.subscribe(topic)
        client.loop_start()
        if not done.wait(timeout):
            result["error"] = f"timed out after {timeout}s"
        return result
    finally:
        client.loop_stop()
        client.disconnect()


def register(registry, ctx) -> None:
    def mqtt_publish(args: dict) -> str:
        broker = (args.get("broker") or "localhost").strip()
        port = int(args.get("port", 1883))
        topic = (args.get("topic") or "").strip()
        if not topic:
            return json.dumps({"ok": False, "error": "topic is required"})
        payload = args.get("payload", "")
        if payload is None:
            payload = ""
        if not isinstance(payload, str):
            payload = json.dumps(payload)
        qos = max(0, min(int(args.get("qos", 0)), 2))
        try:
            if _HAS_PAHO:
                result = _mqtt_publish_paho(broker, port, topic, payload, qos)
            else:
                result = _mqtt_publish_stdlib(broker, port, topic, payload, qos)
            return json.dumps(result, indent=2)
        except Exception as exc:
            return json.dumps(
                {"ok": False, "broker": broker, "topic": topic, "error": str(exc)},
                indent=2,
            )

    def mqtt_subscribe(args: dict) -> str:
        broker = (args.get("broker") or "localhost").strip()
        port = int(args.get("port", 1883))
        topic = (args.get("topic") or "").strip()
        if not topic:
            return json.dumps(
                {
                    "ok": False,
                    "message": None,
                    "topic": "",
                    "error": "topic is required",
                }
            )
        timeout = max(1, min(float(args.get("timeout", 10)), 120))
        try:
            if _HAS_PAHO:
                result = _mqtt_subscribe_paho(broker, port, topic, timeout)
            else:
                result = _mqtt_subscribe_stdlib(broker, port, topic, timeout)
            return json.dumps(result, indent=2)
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "message": None,
                    "topic": topic,
                    "error": str(exc),
                },
                indent=2,
            )

    registry.register_from_def(
        tool(
            "mqtt.publish",
            "Publish a message to an MQTT broker.",
            {
                "broker": _p(
                    "string", "Broker host (default localhost)", required=False
                ),
                "port": _p(
                    "integer", "Broker port (default 1883)", required=False
                ),
                "topic": _p("string", "MQTT topic"),
                "payload": _p("string", "Message payload", required=False),
                "qos": _p(
                    "integer", "QoS level 0-2 (default 0)", required=False
                ),
            },
        ),
        mqtt_publish,
    )
    registry.register_from_def(
        tool(
            "mqtt.subscribe",
            "Subscribe to a topic and wait for one message.",
            {
                "broker": _p(
                    "string", "Broker host (default localhost)", required=False
                ),
                "port": _p(
                    "integer", "Broker port (default 1883)", required=False
                ),
                "topic": _p("string", "MQTT topic"),
                "timeout": _p(
                    "integer", "Seconds to wait (default 10)", required=False
                ),
            },
        ),
        mqtt_subscribe,
    )
