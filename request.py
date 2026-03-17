import socket

s = None

def _get_socket():
    global s
    if s is None:
        s = socket.socket()
    return s

def post(url, headers, data):
    return request(url, 'POST', headers, data)

def get(url, headers):
    return request(url, 'GET', headers, '')

def request(url, method, headers, data):
    s = _get_socket()
    ai = s.getaddrinfo(url, 80)
    print("Address info:", ai)
    addr = ai[0][-1]
    print("connect address:", addr)
    s.connect(addr)
    s = s.makefile('rwb', 0)
    s.write(f"{method} {url} HTTP/1.1\r\n")
    for key, value in headers.items():
        s.write(f"{key}: {value}\r\n")
    s.write("\r\n")
    s.write(data.encode('utf-8'))
    response = s.read()
    s.close()
    return response
