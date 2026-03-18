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
    _sock_send(sock, req.encode('utf-8'))
    if body:
        _sock_send(sock, body)

    response = _read_response(sock)
    sock.close()

    return response



import network
import time
import json
import audio

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



# # 调用英语单词 API
# url = "https://v2.xxapi.cn/api/englishwords"
# headers = {
#     "User-Agent": "K230-CanMV/1.0",
#     "Accept": "application/json",
# }
# data = {"word": "cancel"}

authorization = 'Bearer pat_JrYrSPfHItMZUfpFEuwp3GEqqPM5OQXI5ftAqbYGd3XNSCVkBnuMTTpxBw79DfDc'

# voices_url = 'https://api.coze.cn/v1/audio/voices'
# voices_headers = {
#     'Authorization': authorization,
#     'Content-Type': 'application/json'
# }
# voices_payload = {
#     'filter_system_voice': False,
#     'model_type': 'big',
#     'page_num': 1,
#     'page_size': 100
# }

# resp = get(voices_url, voices_headers, voices_payload)
# voices_list = json.loads(resp.decode('utf-8'))
# voices_dict = {}
# for voice in voices_list['data']['voice_list']:
#     voices_dict[voice['name']] = voice['voice_id']
#     print(voice['name'], voice['voice_id'])

# voice_id = voices_dict["魅力女友"]

speech_url = 'https://api.coze.cn/v1/audio/speech'
speech_headers = {
    'Authorization': authorization,
    'Content-Type': 'application/json'
}
speech_payload = {
    'input': '你好',
    'voice_id': '7426720361733013513',
    'response_format': 'wav'
}

resp = post(speech_url, speech_headers, speech_payload)
print(len(resp))

with open('speech.wav', 'wb') as f:
    for i in range(0, len(resp), 1024):
        f.write(resp[i:i+1024])

audio.play_audio('speech.wav')

# 若 HTTPS 不支持，可尝试 HTTP（若 API 支持）
# url_http = "http://v2.xxapi.cn/api/englishwords?word=cancel"
# resp = get(url_http, headers)
# print("响应:", resp.decode('utf-8'))
