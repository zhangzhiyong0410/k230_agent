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


def _sock_recv(sock, n):
    """兼容普通 socket(recv) 与 MicroPython SSLSocket(read/流接口)"""
    if hasattr(sock, 'read'):
        return sock.read(n)
    return sock.recv(n)


def _sock_send(sock, data):
    """兼容普通 socket(send) 与 MicroPython SSLSocket(write/流接口)"""
    if hasattr(sock, 'write'):
        sock.write(data)
    else:
        sock.send(data)


def _read_response(sock):
    """读取 HTTP 响应，根据 Content-Length 解析"""
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

    # 解析 Content-Length
    content_length = None
    for line in headers_str.split('\r\n'):
        if line.lower().startswith('content-length:'):
            content_length = int(line.split(':', 1)[1].strip())
            break

    while content_length is not None and len(body) < content_length:
        need = content_length - len(body)
        chunk = _sock_recv(sock, min(256, need))
        if not chunk:
            break
        body += chunk

    return body


def _urlencode(params):
    """将 dict 转为 URL 查询字符串，K230 无 urllib"""
    parts = []
    for k, v in params.items():
        s = str(v).replace(' ', '%20').replace('&', '%26')
        parts.append(f"{k}={s}")
    return '&'.join(parts)


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
    sock.settimeout(10)
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

    # 构建请求
    req = f"{method} {path} HTTP/1.1\r\n"
    req += f"Host: {host}\r\n"
    for key, value in headers.items():
        req += f"{key}: {value}\r\n"
    req += "\r\n"

    body = data if isinstance(data, bytes) else data.encode('utf-8')
    _sock_send(sock, req.encode('utf-8'))
    if body:
        _sock_send(sock, body)

    response = _read_response(sock)
    sock.close()

    return response


if __name__ == "__main__":
    import network
    import time

    # 连接 WiFi
    sta = network.WLAN(0)
    sta.active(True)
    sta.connect("Kittenbot", "kittenbot428")

    # 等待连接
    for _ in range(20):
        if sta.isconnected():
            break
        time.sleep(0.5)
    else:
        print("WiFi 连接超时")
        raise SystemExit(1)

    print("WiFi 已连接")

    # 调用英语单词 API
    url = "https://v2.xxapi.cn/api/englishwords"
    headers = {
        "User-Agent": "K230-CanMV/1.0",
        "Accept": "application/json",
    }
    data = {"word": "cancel"}

    
    resp = get(url, headers, params=data)
    print("响应:", resp.decode('utf-8'))
    # 若 HTTPS 不支持，可尝试 HTTP（若 API 支持）
    # url_http = "http://v2.xxapi.cn/api/englishwords?word=cancel"
    # resp = get(url_http, headers)
    # print("响应:", resp.decode('utf-8'))
