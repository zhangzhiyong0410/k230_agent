import socket

# K230/CanMV 平台：每次请求使用新 socket，避免复用已关闭的连接
def _parse_url(url):
    """解析 URL，返回 (scheme, host, port, path)"""
    url = url.strip()
    if url.startswith('https://'):
        scheme = 'https'
        rest = url[8:]
        default_port = 443
    elif url.startswith('http://'):
        scheme = 'http'
        rest = url[7:]
        default_port = 80
    else:
        scheme = 'http'
        rest = url
        default_port = 80

    if '/' in rest:
        host, path = rest.split('/', 1)
        path = '/' + path
    else:
        host = rest
        path = '/'

    if ':' in host and host.rfind(':') > 0:
        host, port_str = host.rsplit(':', 1)
        try:
            port = int(port_str)
        except ValueError:
            port = default_port
    else:
        port = default_port

    return scheme, host, port, path


def _sock_recv(sock, n, _retries=50):
    """兼容普通 socket(recv) 与 MicroPython SSLSocket(read/流接口)
    K230 SSL read() 数据未就绪时可能返回 None 或 b''，需重试。
    """
    if hasattr(sock, 'read'):
        for _ in range(_retries):
            d = sock.read(n)
            if d is not None and len(d) > 0:
                return d
            try:
                import time
                time.sleep_ms(100)
            except Exception:
                import time
                time.sleep(0.1)
        return b''
    return sock.recv(n)


def _sock_send(sock, data):
    """兼容普通 socket(send) 与 MicroPython SSLSocket(write/流接口)"""
    if hasattr(sock, 'write'):
        sock.write(data)
    else:
        sock.send(data)


def _read_chunked(sock, initial=b''):
    """解析 Transfer-Encoding: chunked 格式的 body"""
    body = b''
    buf = initial
    while True:
        # 读取到 \r\n 得到 chunk size 行
        while b'\r\n' not in buf:
            c = _sock_recv(sock, 64)
            if not c:
                return body
            buf += c
        line, buf = buf.split(b'\r\n', 1)
        size_hex = line.decode('ascii', 'ignore').split(';')[0].strip()
        try:
            chunk_size = int(size_hex, 16)
        except ValueError:
            return body
        if chunk_size == 0:
            break
        # 读取 chunk 数据
        got = 0
        while got < chunk_size:
            if buf:
                take = min(len(buf), chunk_size - got)
                body += buf[:take]
                buf = buf[take:]
                got += take
            else:
                need = min(512, chunk_size - got)
                c = _sock_recv(sock, need)
                if not c:
                    return body
                buf = c
        # 吃掉 chunk 后的 \r\n
        while len(buf) < 2:
            c = _sock_recv(sock, 2 - len(buf))
            if not c:
                return body
            buf += c
        buf = buf[2:]
    return body


def _read_response(sock):
    """读取 HTTP 响应，支持 Content-Length 与 Transfer-Encoding: chunked"""
    buf = b''
    while True:
        chunk = _sock_recv(sock, 256)
        if not chunk:
            break
        buf += chunk
        if b'\r\n\r\n' in buf:
            break

    parts = buf.split(b'\r\n\r\n', 1)
    headers_str = parts[0].decode('utf-8', 'ignore')
    body = parts[1] if len(parts) > 1 else b''

    # 解析 Transfer-Encoding
    chunked = False
    for line in headers_str.split('\r\n'):
        if line.lower().startswith('transfer-encoding:'):
            if 'chunked' in line.lower():
                chunked = True
            break

    if chunked:
        return _read_chunked(sock, body)

    # 解析 Content-Length
    content_length = None
    for line in headers_str.split('\r\n'):
        if line.lower().startswith('content-length:'):
            content_length = int(line.split(':', 1)[1].strip())
            break

    while content_length is not None and len(body) < content_length:
        need = content_length - len(body)
        chunk = _sock_recv(sock, min(512, need))
        if not chunk:
            break
        body += chunk

    return body


def _urlencode(params):
    """将 dict 转为 URL 查询字符串，K230 无 urllib"""
    parts = []
    for k, v in params.items():
        if v is True:
            s = 'true'
        elif v is False:
            s = 'false'
        else:
            s = str(v).replace(' ', '%20').replace('&', '%26')
        parts.append(f"{k}={s}")
    return '&'.join(parts)


def _to_body(data):
    """将 data 转为 bytes：支持 dict(JSON)、str、bytes"""
    if isinstance(data, bytes):
        return data
    if isinstance(data, dict):
        try:
            import json
        except ImportError:
            try:
                import ujson as json
            except ImportError:
                raise ValueError('dict 需 json/ujson 模块序列化，当前平台可能不支持')
        return json.dumps(data).encode('utf-8')
    return data.encode('utf-8')


def _build_multipart(fields, files):
    """
    构造 multipart/form-data 请求体。

    fields: dict，普通表单字段，例如 {"model": "xyz"}
    files:  dict，文件字段，例如 {"file": ("speech.wav", b"...", "audio/wav")}

    返回: (body_bytes, content_type_header)
    """
    boundary = '----K230Boundary9876543210'
    parts = []

    # 普通字段
    if fields:
        for key, value in fields.items():
            parts.append(('--%s\r\n' % boundary).encode())
            parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % key).encode())
            parts.append(('%s\r\n' % value).encode())

    # 文件字段
    if files:
        for field_name, file_info in files.items():
            filename, file_data, mime = file_info
            parts.append(('--%s\r\n' % boundary).encode())
            parts.append(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (field_name, filename)).encode())
            parts.append(('Content-Type: %s\r\n\r\n' % mime).encode())
            parts.append(file_data)
            parts.append(b'\r\n')

    parts.append(('--%s--\r\n' % boundary).encode())

    body = b''.join(parts)
    content_type = 'multipart/form-data; boundary=%s' % boundary
    return body, content_type


def post(url, headers, data):
    return request(url, 'POST', headers, data)


def get(url, headers=None, params=None):
    if headers is None:
        headers = {}
    data = ''
    if params:
        qs = _urlencode(params)
        url = url + ('&' if '?' in url else '?') + qs
    return request(url, 'GET', headers, data)


def request(url, method, headers, data):
    if headers is None:
        headers = {}

    scheme, host, port, path = _parse_url(url)
    use_ssl = (scheme == 'https')

    # 解析地址
    ai = socket.getaddrinfo(host, port)
    addr = ai[0][-1]

    sock = socket.socket()
    sock.settimeout(30)
    sock.connect(addr)

    if use_ssl:
        try: 
            import ssl
        except ImportError:
            try:
                import ussl as ssl
            except ImportError:
                sock.close()
                raise OSError('HTTPS 需要 ssl/ussl 模块，当前平台可能不支持')
        # K230 证书解析有问题，禁用验证；生产环境建议启用
        try:
            sock = ssl.wrap_socket(sock, cert_reqs=ssl.CERT_NONE, server_hostname=host)
        except TypeError:
            sock = ssl.wrap_socket(sock, cert_reqs=ssl.CERT_NONE)

    body = _to_body(data) if data is not None else b''

    # 构建请求
    req = f"{method} {path} HTTP/1.1\r\n"
    req += f"Host: {host}\r\n"
    headers_lower = {k.lower() for k in headers}
    for key, value in headers.items():
        req += f"{key}: {value}\r\n"
    if body and 'content-length' not in headers_lower:
        req += f"Content-Length: {len(body)}\r\n"
    if 'connection' not in headers_lower:
        req += "Connection: close\r\n"
    req += "\r\n"
    print("start send request...")
    start_time = time.ticks_ms()
    _sock_send(sock, req.encode('utf-8'))
    if body:
        _sock_send(sock, body)
    print("send request time: ", time.ticks_diff(time.ticks_ms(), start_time))
    print("start read response...")
    response = _read_response(sock)
    sock.close()

    return response



import network
import time
import json
import audio
from machine import Pin

# 连接 WiFi
sta = network.WLAN(0)
sta.active(True)
if not sta.isconnected():
    sta.connect("706", "12345678")
    # 等待连接
    for _ in range(20):
        if sta.isconnected():
            break
        time.sleep(0.5)
    else:
        print("WiFi 连接超时")
        raise SystemExit(1)

print("WiFi 已连接")

# Coze 相关配置
authorization = 'Bearer pat_JrYrSPfHItMZUfpFEuwp3GEqqPM5OQXI5ftAqbYGd3XNSCVkBnuMTTpxBw79DfDc'
bot_id = '7618103224301944847'
user_id = '123456789'

asr_url = 'https://api.coze.cn/v1/audio/transcriptions'
chat_url = 'https://api.coze.cn/v3/chat'
tts_url = 'https://api.coze.cn/v1/audio/speech'
voice_id = '7426720361753968677'  # 你喜欢的声音 ID，可按需调整

def coze_chat(message_history):
    """调用 Coze /v3/chat，流式 SSE，解析完整回复"""
    payload = {
        'bot_id': bot_id,
        'user_id': user_id,
        'stream': True,
        'additional_messages': message_history,
        'parameters': {}
    }
    headers = {
        'Authorization': authorization,
        'Content-Type': 'application/json'
    }
    resp = post(chat_url, headers, payload)
    text = resp.decode('utf-8')

    # 解析 SSE 事件，提取最终 answer
    answer = ''
    event_type = ''
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        if line.startswith('event:'):
            event_type = line[6:].strip()
        elif line.startswith('data:'):
            if event_type == 'conversation.message.delta':
                try:
                    data = json.loads(line[5:].strip())
                    print(data.get('content', ''), end='')
                except Exception as e:
                    print('解析 SSE data 失败:', e)
            elif event_type == 'conversation.message.completed':
                try:
                    data = json.loads(line[5:].strip())
                    if data.get('type') == 'answer':
                        answer = data.get('content', '')
                except Exception as e:
                    print('解析 SSE data 失败:', e)
    print('\n')
    return answer

def tts_to_wav(text, filename):
    """文本转语音，保存为 WAV 文件"""
    headers = {
        'Authorization': authorization,
        'Content-Type': 'application/json'
    }
    payload = {
        'input': text,
        'voice_id': voice_id,
        'response_format': 'wav',
        'sample_rate': 8000,
        'loudness_rate': -50
    }
    resp = post(tts_url, headers, payload)

    with open(filename, 'wb') as f:
        for i in range(0, len(resp), 1024):
            f.write(resp[i:i+1024])

def asr_from_wav(filename):
    """上传 WAV 到 Coze 做语音识别，返回文本"""
    with open(filename, 'rb') as f:
        wav_data = f.read()

    body, content_type = _build_multipart(
        fields=None,
        files={'file': (filename, wav_data, 'audio/wav')},
    )

    headers = {
        'Authorization': authorization,
        'Content-Type': content_type,
    }

    resp = post(asr_url, headers, body)
    try:
        data = json.loads(resp.decode('utf-8'))
    except Exception as e:
        print('解析 ASR 响应失败:', e, resp)
        return ''

    try:
        return data['data']['text']
    except Exception as e:
        print('解析 ASR 响应失败:', e, data)
        return ''

def main_loop():
    """asr-chatbot-tts 主循环：按键说话 -> 识别 -> 对话 -> 合成语音并播放"""
    btn = Pin(21, Pin.IN, Pin.PULL_UP)
    message_history = []

    print('asr-chatbot-tts 已启动，按下按键开始说话...')

    while True:
        print('\n等待按键开始新一轮对话...')
        # 不在这里等按键，直接调用已经支持按键控制的录音函数
        audio.record_audio('/data/asr.wav', duration=None, btn=btn)

        print('开始语音识别...')
        user_text = asr_from_wav('/data/asr.wav')
        if not user_text:
            print('识别结果为空，跳过本轮。')
            continue

        print('识别结果:', user_text)

        # 加入到对话历史
        message_history.append({
            'content': user_text,
            'content_type': 'text',
            'role': 'user',
            'type': 'question'
        })

        print('发送到聊天机器人...')
        answer = coze_chat(message_history)
        if not answer:
            print('机器人没有返回内容。')
            continue

        # 将机器人回答也加入历史，便于多轮对话
        message_history.append({
            'content': answer,
            'content_type': 'text',
            'role': 'assistant',
            'type': 'answer'
        })

        print('开始语音合成并播放...')
        tts_to_wav(answer, '/data/reply.wav')
        audio.play_audio('/data/reply.wav')

if __name__ == '__main__':
    main_loop()