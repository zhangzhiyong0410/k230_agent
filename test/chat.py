import requests
import json
import socket
import threading
import websocket
import base64
import pyaudio
import time

chat_message = ""
shutdown_event = threading.Event()

bot_id = '7618103224301944847'
user_id = '123456789'
authorization = 'Bearer pat_JrYrSPfHItMZUfpFEuwp3GEqqPM5OQXI5ftAqbYGd3XNSCVkBnuMTTpxBw79DfDc'

# 建议不要用逗号分段，不然太碎
Paragraph_marks = ['。', '！', '？', '!', '?', '\n']

url = 'https://api.coze.cn/v3/chat?'
tts_url = "wss://ws.coze.cn/v1/audio/speech"

headers = {
    'Authorization': authorization,
    'Content-Type': 'application/json'
}

message_history = [
    # {
    #     'content': '你好',
    #     'content_type': 'text',
    #     'role': 'user',
    #     'type': 'question'
    # }
]

payload = {
    'bot_id': bot_id,
    'user_id': user_id,
    'stream': True,
    'additional_messages': message_history,
    'parameters': {}
}

# 连接超时、单次读超时
HTTP_TIMEOUT = (30, 120)

# ===================== TTS 配置 =====================
TTS_HEADERS = {"Authorization": authorization}

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 24000

p = pyaudio.PyAudio()
audio_stream = p.open(
    format=FORMAT,
    channels=CHANNELS,
    rate=RATE,
    output=True,
    frames_per_buffer=1024
)

tts_ws = None
tts_msg_id = 0
tts_lock = threading.Lock()
tts_ready_event = threading.Event()


def tts_on_open(ws):
    print("[TTS] WebSocket 已连接")
    tts_ready_event.set()


def tts_on_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("event_type") == "speech.audio.update":
            delta_b64 = data["data"]["delta"]
            pcm_data = base64.b64decode(delta_b64)
            audio_stream.write(pcm_data)
    except Exception as e:
        print("[TTS] on_message error:", e)


def tts_on_error(ws, error):
    print("[TTS] WebSocket error:", error)


def tts_on_close(ws, close_status_code, close_msg):
    print(f"[TTS] WebSocket 已关闭: code={close_status_code}, msg={close_msg}")
    tts_ready_event.clear()


def tts_connect():
    global tts_ws

    tts_ready_event.clear()

    try:
        if tts_ws is not None:
            tts_ws.close()
            time.sleep(0.2)
    except Exception:
        pass

    tts_ws = websocket.WebSocketApp(
        tts_url,
        header=TTS_HEADERS,
        on_open=tts_on_open,
        on_message=tts_on_message,
        on_error=tts_on_error,
        on_close=tts_on_close,
    )

    threading.Thread(target=tts_ws.run_forever, daemon=True).start()

    # 等待连接建立
    if not tts_ready_event.wait(timeout=2.0):
        print("[TTS] 连接超时")


def tts_reconnect():
    # complete 之后重新连一次，你特别提到的处理就在这里
    tts_connect()


def tts_send_append(text):
    global tts_msg_id

    if not text.strip():
        return

    if tts_ws is None or not tts_ready_event.is_set():
        tts_connect()

    msg = {
        "id": str(tts_msg_id),
        "event_type": "input_text_buffer.append",
        "data": {"delta": text}
    }
    tts_ws.send(json.dumps(msg))
    tts_msg_id += 1


def tts_send_complete():
    global tts_msg_id

    if tts_ws is None or not tts_ready_event.is_set():
        return

    msg = {
        "id": str(tts_msg_id),
        "event_type": "input_text_buffer.complete"
    }
    tts_ws.send(json.dumps(msg))
    tts_msg_id += 1


def tts_speak(text):
    """
    发送一段文本给 TTS：
    1. append
    2. complete
    3. complete 后立即重连 websocket
    """
    clean_text = text.strip()
    if not clean_text:
        return

    with tts_lock:
        try:
            tts_send_append(clean_text)
            tts_send_complete()

            # 稍微等一下，让服务端开始吐音频，避免太快断开
            time.sleep(0.15)

        except Exception as e:
            print("[TTS] speak error:", e)

        finally:
            # 你要求的：complete 之后重新连接一次
            try:
                tts_reconnect()
            except Exception as e:
                print("[TTS] reconnect error:", e)


def split_by_marks(buffer, marks):
    """
    从 buffer 中尽可能切出完整段落。
    返回: (segments, remain)
    """
    segments = []
    start = 0

    for i, ch in enumerate(buffer):
        if ch in marks:
            seg = buffer[start:i + 1].strip()
            if seg:
                segments.append(seg)
            start = i + 1

    remain = buffer[start:]
    return segments, remain


def print_and_tts_segment(segment):
    """
    期望效果：
    你好！
    audio[0:20]
    很高兴和你聊天~
    audio[0:20]
    """
    seg = segment.strip()
    if not seg:
        return

    print(seg)
    print("audio[0:20]")
    tts_speak(seg)


def chat():
    global chat_message

    # 先建立一次 TTS 连接
    tts_connect()

    while not shutdown_event.is_set():
        if chat_message == "":
            shutdown_event.wait(0.1)
            continue

        message = chat_message
        chat_message = ""

        message_history.append({
            'content': message,
            'content_type': 'text',
            'role': 'user',
            'type': 'question'
        })

        print('--------------------------------')

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                stream=True,
                timeout=HTTP_TIMEOUT
            )
        except requests.RequestException as e:
            if not shutdown_event.is_set():
                print(f"[chat] 请求失败: {e}")
            continue

        data_type = ""
        reply_buffer = ""

        try:
            for line in response.iter_lines():
                if shutdown_event.is_set():
                    break

                if not line:
                    continue

                raw = line.decode('utf-8')

                if raw[0:5] == "event":
                    data_type = raw[6:]
                    continue

                if raw[0:4] != "data":
                    continue

                if data_type == "conversation.message.delta":
                    data = json.loads(raw[5:])
                    chunk = data.get('content', '')
                    if not chunk:
                        continue

                    # 这里只做缓冲，不直接逐字打印
                    reply_buffer += chunk

                    # 按 Paragraph_marks 分段
                    segments, reply_buffer = split_by_marks(reply_buffer, Paragraph_marks)

                    for seg in segments:
                        print_and_tts_segment(seg)

                elif data_type == "conversation.message.completed":
                    data = json.loads(raw[5:])

                    if data.get('type') == "answer":
                        full_answer = data.get('content', '')

                        message_history.append({
                            'content': full_answer,
                            'content_type': data.get('content_type', 'text'),
                            'role': data.get('role', 'assistant'),
                            'type': data.get('type', 'answer')
                        })

                        # 把最后没遇到标点的残余也播掉
                        if reply_buffer.strip():
                            print_and_tts_segment(reply_buffer)
                            reply_buffer = ""

                        print()

                elif data_type == "conversation.chat.requires_action":
                    data = json.loads(raw[5:])
                    tool_calls = data.get('required_action', {}).get('submit_tool_outputs', {}).get('tool_calls', [])

                    for tool_call in tool_calls:
                        function = tool_call.get('function', {})
                        if function.get('name') == 'print':
                            args = function.get('arguments', '{}')
                            try:
                                args_json = json.loads(args) if isinstance(args, str) else args
                            except Exception:
                                args_json = {}

                            text = args_json.get('text', '')
                            if text:
                                print(text)

        finally:
            response.close()


def handle_client(conn, addr):
    global chat_message
    print(f"[新连接] {addr} 已连接")
    conn.settimeout(1.0)

    try:
        while not shutdown_event.is_set():
            try:
                data = conn.recv(1024)
            except socket.timeout:
                continue

            if not data:
                break

            recv_msg = data.decode('utf-8')
            chat_message = recv_msg
            print(f"[{addr}] {recv_msg}")

            conn.send(f"服务端回复：{chat_message}".encode('utf-8'))

    finally:
        conn.close()
        print(f"[断开连接] {addr} 已退出")


def start_server():
    HOST = ''
    PORT = 8888

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST, PORT))
    server_socket.listen(5)
    server_socket.settimeout(1.0)

    print(f"[启动] 多线程TCP服务端，端口 {PORT}（Ctrl+C 退出）")

    chat_thread = threading.Thread(target=chat, daemon=True, name="chat")
    chat_thread.start()

    try:
        while not shutdown_event.is_set():
            try:
                conn, addr = server_socket.accept()
            except socket.timeout:
                continue

            client_thread = threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True
            )
            client_thread.start()
            print(f"[活跃连接数] {threading.active_count() - 1}")

    except KeyboardInterrupt:
        print("\n[退出] 收到 Ctrl+C", flush=True)

    finally:
        shutdown_event.set()

        try:
            if tts_ws is not None:
                tts_ws.close()
        except Exception:
            pass

        try:
            audio_stream.stop_stream()
            audio_stream.close()
            p.terminate()
        except Exception:
            pass

        server_socket.close()


if __name__ == "__main__":
    start_server()