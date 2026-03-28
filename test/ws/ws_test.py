import json
import websocket
import threading
import base64
import pyaudio
import time

# ===================== 配置 =====================
ACCESS_TOKEN = "Bearer pat_JrYrSPfHItMZUfpFEuwp3GEqqPM5OQXI5ftAqbYGd3XNSCVkBnuMTTpxBw79DfDc"
URL = "wss://ws.coze.cn/v1/audio/speech"

HEADERS = {"Authorization": ACCESS_TOKEN}

# 音频参数（Coze 语音返回的标准格式）
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 24000  # 大部分AI语音都是24000采样率

# 初始化音频播放
p = pyaudio.PyAudio()
stream = p.open(
    format=FORMAT,
    channels=CHANNELS,
    rate=RATE,
    output=True,
    frames_per_buffer=1024  # 越小延迟越低
)

# ===================== WebSocket 消息处理 =====================
def on_open(ws):
    print("✅ 连接成功")

def on_message(ws, message):
    try:
        data = json.loads(message)
        
        # 只处理音频数据
        if data.get("event_type") == "speech.audio.update":
            delta_b64 = data["data"]["delta"]
            # 1. Base64 解码 → PCM 二进制音频
            pcm_data = base64.b64decode(delta_b64)
            
            # 2. 实时播放
            stream.write(pcm_data)

    except Exception as e:
        print("on_message error: ", e)

# ===================== 启动 WebSocket =====================
ws = websocket.WebSocketApp(
    URL,
    header=HEADERS,
    on_open=on_open,
    on_message=on_message,
)

def ws_thread():
    ws.run_forever()

threading.Thread(target=ws_thread, daemon=True).start()

def connect():
    global ws
    try:
        ws.close()
        time.sleep(0.5)
    except:
        pass

    ws = websocket.WebSocketApp(
        URL, header=HEADERS,
        on_open=on_open, on_message=on_message
    )
    threading.Thread(target=ws.run_forever, daemon=True).start()
    time.sleep(0.5)
    print("✅ 连接成功")

# ===================== 输入发送 =====================
msg_id = 0
print("🎤 输入文字回车发送，输入 exit 结束")
while True:
    text = input("输入：")
    if text == "exit":
        # 发送结束标记
        end_msg = {
            "id": str(msg_id),
            "event_type": "input_text_buffer.complete"
        }
        ws.send(json.dumps(end_msg))
        msg_id += 1
        continue
    elif text == "connect":
        connect()
        continue

    # 发送文本
    send_msg = {
        "id": str(msg_id),
        "event_type": "input_text_buffer.append",
        "data": {"delta": text}
    }
    ws.send(json.dumps(send_msg))
    