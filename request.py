import socket
import time
import struct
import _thread
import json
import os
import random
import network
from media.pyaudio import *
from media.media import *
from media.display import *
import media.wave as wave
from machine import Pin

# ─── 硬件 & 显示常量 ─────────────────────────────────────────────

btn = Pin(21, Pin.IN, Pin.PULL_UP)

DISPLAY_WIDTH = ALIGN_UP(800, 16)
DISPLAY_HEIGHT = 480

LCD_FONT_SIZE = 20
LCD_LINE_HEIGHT = 24
LCD_TEXT_COLOR = (255, 255, 255)
LCD_MAX_LINES = DISPLAY_HEIGHT // LCD_LINE_HEIGHT
LCD_MAX_CHARS_PER_LINE = 40
LCD_CELL_WIDTH = DISPLAY_WIDTH // LCD_MAX_CHARS_PER_LINE

LCD_REPLY_START_LINE = 0

MULTIPART_BOUNDARY = '----K230Boundary9876543210'

img = image.Image(DISPLAY_WIDTH, DISPLAY_HEIGHT, image.ARGB8888)
Display.init(Display.ST7701, width=DISPLAY_WIDTH, height=DISPLAY_HEIGHT, to_ide=True)

# ─── Coze API 配置 ────────────────────────────────────────────────

authorization = 'Bearer pat_JrYrSPfHItMZUfpFEuwp3GEqqPM5OQXI5ftAqbYGd3XNSCVkBnuMTTpxBw79DfDc'
bot_id = '7618103224301944847'
user_id = 'k230_zy'

asr_url = 'https://api.coze.cn/v1/audio/transcriptions'
chat_url = 'https://api.coze.cn/v3/chat'
tts_url = 'https://api.coze.cn/v1/audio/speech'
voice_id = '7426720361753968677'

# 已知 WiFi：扫描到列表中的 SSID 才连接（按写入顺序优先，例如两个都在时先连 706）
WIFI_CREDENTIALS = {
    '706': '12345678',
    'Kittenbot': 'kittenbot428',
}


# ─── 音频工具 ─────────────────────────────────────────────────────

def play_audio(filename):
    try:
        wf = wave.open(filename, 'rb')
        CHUNK = int(wf.get_framerate() / 25)
        p = PyAudio()
        stream = p.open(
            format=p.get_format_from_width(wf.get_sampwidth()),
            channels=wf.get_channels(),
            rate=wf.get_framerate(),
            output=True, frames_per_buffer=CHUNK,
        )
        stream.volume(vol=85)
        data = wf.read_frames(CHUNK)
        while data:
            stream.write(data)
            data = wf.read_frames(CHUNK)
            if btn.value() == 0:
                print("按键退出")
                time.sleep(0.3)
                break
    except BaseException as e:
        import sys
        sys.print_exception(e)
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()
        wf.close()


def _audio_playback_worker(_lock, _state, _stream, _p, _framesize, _buf_size, _compact_th):
    """音频播放线程：从共享缓冲区消费数据并写入音频流"""
    try:
        while True:
            block = None
            with _lock:
                available = len(_state['buf']) - _state['pos']
                if available >= _buf_size:
                    block = bytes(_state['buf'][_state['pos']:_state['pos'] + _buf_size])
                    _state['pos'] += _buf_size
                elif _state['done']:
                    if available <= 0:
                        break
                    aligned = available - (available % _framesize)
                    if aligned > 0:
                        block = bytes(_state['buf'][_state['pos']:_state['pos'] + aligned])
                        _state['pos'] += aligned
                    else:
                        break
                if _state['pos'] >= _compact_th:
                    _state['buf'] = bytearray(_state['buf'][_state['pos']:])
                    _state['pos'] = 0

            if block:
                _stream.write(block)
            else:
                time.sleep_ms(5)
    except Exception as e:
        print("音频播放线程异常:", e)
    finally:
        try: _stream.stop_stream()
        except Exception: pass
        try: _stream.close()
        except Exception: pass
        try: _p.terminate()
        except Exception: pass


def _record_worker(_lock, _state, _stream, _p, _btn):
    """录音线程：按住按键持续采集音频数据"""
    try:
        while _btn.value() == 0:
            frame = _stream.read()
            if frame:
                with _lock:
                    _state['buf'].extend(frame)
    except Exception as e:
        print('录音线程异常:', e)
    finally:
        with _lock:
            _state['done'] = True
        try: _stream.stop_stream()
        except Exception: pass
        try: _stream.close()
        except Exception: pass
        try: _p.terminate()
        except Exception: pass


# ─── LCD 显示工具 ─────────────────────────────────────────────────

def _wrap_text_by_chars(text, max_chars=LCD_MAX_CHARS_PER_LINE):
    """把长文本按固定字符数切成多行"""
    if text is None:
        return []
    text = str(text).replace('\r', '').replace('\n', ' ')
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)] if text else []


def _lcd_show_lines(lines):
    """在 LCD 上显示多行提示信息"""
    img.clear()
    y = 0
    for i in range(min(len(lines), LCD_MAX_LINES)):
        if lines[i]:
            img.draw_string_advanced(0, y, LCD_FONT_SIZE, lines[i], color=LCD_TEXT_COLOR)
            y += LCD_LINE_HEIGHT
    Display.show_image(img)


# ─── 网络底层工具 ─────────────────────────────────────────────────

def _parse_url(url):
    """解析 URL，返回 (scheme, host, port, path)"""
    url = url.strip()
    if url.startswith('https://'):
        scheme, rest, default_port = 'https', url[8:], 443
    elif url.startswith('http://'):
        scheme, rest, default_port = 'http', url[7:], 80
    else:
        scheme, rest, default_port = 'http', url, 80

    if '/' in rest:
        host, path = rest.split('/', 1)
        path = '/' + path
    else:
        host, path = rest, '/'

    if ':' in host and host.rfind(':') > 0:
        host, port_str = host.rsplit(':', 1)
        try:
            port = int(port_str)
        except ValueError:
            port = default_port
    else:
        port = default_port

    return scheme, host, port, path


def _create_socket(host, port, timeout=5, use_ssl=False):
    """创建并连接 socket，支持 SSL"""
    ai = socket.getaddrinfo(host, port)
    sock = socket.socket()
    sock.settimeout(timeout)
    sock.connect(ai[0][-1])
    if use_ssl:
        try:
            import ssl
        except ImportError:
            try:
                import ussl as ssl
            except ImportError:
                sock.close()
                raise OSError('HTTPS 需要 ssl/ussl 模块')
        try:
            sock = ssl.wrap_socket(sock, cert_reqs=ssl.CERT_NONE, server_hostname=host)
        except TypeError:
            sock = ssl.wrap_socket(sock, cert_reqs=ssl.CERT_NONE)
    return sock


def _sock_recv(sock, n, _retries=50):
    """兼容普通 socket 与 MicroPython SSLSocket"""
    if hasattr(sock, 'read'):
        for _ in range(_retries):
            d = sock.read(n)
            if d is not None and len(d) > 0:
                return d
            try:
                time.sleep_ms(100)
            except Exception:
                time.sleep(0.1)
        return b''
    return sock.recv(n)


def _sock_send(sock, data):
    """兼容普通 socket 与 MicroPython SSLSocket"""
    if hasattr(sock, 'write'):
        sock.write(data)
    else:
        sock.send(data)


def _send_chunk(sock, data):
    """发送一个 HTTP chunked 编码块"""
    _sock_send(sock, ('%x\r\n' % len(data)).encode())
    _sock_send(sock, data)
    _sock_send(sock, b'\r\n')


# ─── HTTP 辅助 ────────────────────────────────────────────────────

def _coze_headers(content_type='application/json'):
    """返回 Coze API 通用请求头"""
    return {'Authorization': authorization, 'Content-Type': content_type}


def _parse_asr_response(response):
    """解析 ASR JSON 响应，返回识别文本"""
    try:
        data = json.loads(response.decode('utf-8'))
    except Exception as e:
        print('解析 ASR 响应失败:', e, response)
        return ''
    try:
        return data['data']['text']
    except Exception as e:
        print('解析 ASR 响应失败:', e, data)
        return ''


def _urlencode(params):
    """dict -> URL 查询字符串"""
    parts = []
    for k, v in params.items():
        if v is True:
            s = 'true'
        elif v is False:
            s = 'false'
        else:
            s = str(v).replace(' ', '%20').replace('&', '%26')
        parts.append('%s=%s' % (k, s))
    return '&'.join(parts)


def _to_body(data):
    """将 data 转为 bytes：支持 dict(JSON)、str、bytes"""
    if isinstance(data, bytes):
        return data
    if isinstance(data, dict):
        return json.dumps(data).encode('utf-8')
    return data.encode('utf-8')


def _build_multipart(fields, files):
    """构造 multipart/form-data 请求体，返回 (body_bytes, content_type_header)"""
    parts = []
    if fields:
        for key, value in fields.items():
            parts.append(('--%s\r\n' % MULTIPART_BOUNDARY).encode())
            parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % key).encode())
            parts.append(('%s\r\n' % value).encode())
    if files:
        for field_name, (filename, file_data, mime) in files.items():
            parts.append(('--%s\r\n' % MULTIPART_BOUNDARY).encode())
            parts.append(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (field_name, filename)).encode())
            parts.append(('Content-Type: %s\r\n\r\n' % mime).encode())
            parts.append(file_data)
            parts.append(b'\r\n')
    parts.append(('--%s--\r\n' % MULTIPART_BOUNDARY).encode())
    return b''.join(parts), 'multipart/form-data; boundary=%s' % MULTIPART_BOUNDARY


# ─── HTTP 响应解析 ────────────────────────────────────────────────

def _get_header(headers_str, name):
    """从 HTTP 头字符串中获取指定头的值"""
    prefix = name.lower() + ':'
    for line in headers_str.split('\r\n'):
        if line.lower().startswith(prefix):
            return line.split(':', 1)[1].strip()
    return ''


def _read_response(sock):
    """读取 HTTP 响应，支持 Content-Length / chunked / WAV 流式播放"""
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

    chunked = 'chunked' in _get_header(headers_str, 'transfer-encoding').lower()
    content_type = _get_header(headers_str, 'content-type').lower()

    if chunked:
        is_sse = ('event-stream' in content_type
                  or b'event:' in body[:256] or b'data:' in body[:256])
        return _read_chunked_sse_chat(sock, body) if is_sse else _read_chunked_raw(sock, body)

    cl_str = _get_header(headers_str, 'content-length')
    content_length = int(cl_str) if cl_str else None

    if body[:4] == b'RIFF':
        return _read_wav_streaming(sock, body, content_length)

    while content_length is not None and len(body) < content_length:
        chunk = _sock_recv(sock, min(512, content_length - len(body)))
        if not chunk:
            break
        body += chunk

    return body


def _read_chunked_sse_chat(sock, initial=b''):
    """解析 chat 的 SSE（chunked）增量消息，并在 LCD 上实时显示"""
    buf = initial
    event_type = None
    full_answer = ''

    reply_start_line = LCD_REPLY_START_LINE
    if reply_start_line <= 0:
        img.clear()
        reply_start_line = 0

    img.draw_string_advanced(
        0, reply_start_line * LCD_LINE_HEIGHT,
        LCD_FONT_SIZE, "回复中...", color=LCD_TEXT_COLOR,
    )
    cursor_line = reply_start_line + 1
    cursor_col = 0
    cursor_x = 0
    cursor_y = cursor_line * LCD_LINE_HEIGHT
    Display.show_image(img)

    while True:
        c = _sock_recv(sock, 512)
        if not c:
            break
        buf += c

        data_lines = []
        while True:
            idx = buf.find(b'\n')
            if idx == -1:
                break
            data_lines.append(buf[:idx])
            buf = buf[idx + 1:]

        for line in data_lines:
            if not line:
                continue
            if line.startswith(b'event:'):
                event_type = line[6:].strip()
            elif line.startswith(b'data:'):
                try:
                    data_content = json.loads(line[5:].strip())
                except Exception:
                    continue

                if event_type == b'conversation.message.delta':
                    if data_content.get('type') != 'answer':
                        continue
                    content = data_content.get('content', '')
                    if not content:
                        continue
                    full_answer += content
                    left = content
                    while left and cursor_line < LCD_MAX_LINES:
                        remaining = LCD_MAX_CHARS_PER_LINE - cursor_col
                        if remaining <= 0:
                            cursor_line += 1
                            cursor_col = 0
                            cursor_x = 0
                            cursor_y = cursor_line * LCD_LINE_HEIGHT
                            continue
                        take = min(len(left), remaining)
                        img.draw_string_advanced(
                            cursor_x, cursor_y, LCD_FONT_SIZE,
                            left[:take], color=LCD_TEXT_COLOR,
                        )
                        cursor_col += take
                        cursor_x = cursor_col * LCD_CELL_WIDTH
                        left = left[take:]
                    Display.show_image(img)
                    print(content, end='')

                elif event_type == b'conversation.message.completed':
                    if data_content.get('type') == 'answer':
                        print()
                        return data_content.get('content', '') or full_answer

                elif event_type == b'conversation.chat.requires_action':
                    result = _handle_tool_action(data_content)
                    if result:
                        return result


def _handle_tool_action(data_content):
    """处理 SSE 中的工具调用请求，提交结果后返回后续回复"""
    required = data_content.get('required_action', {})
    tool_calls = required.get('submit_tool_outputs', {}).get('tool_calls', []) or []
    conversation_id = data_content.get('conversation_id', '')
    chat_id = data_content.get('id', '')
    if not conversation_id or not chat_id or not tool_calls:
        return None

    submit_outputs = []
    for tc in tool_calls:
        tc_id = tc.get('id', '')
        if not tc_id:
            continue
        func = tc.get('function', {}) or {}
        tool_name = func.get('name', '')
        args = func.get('arguments', '{}')
        try:
            args_json = json.loads(args) if isinstance(args, str) else args
        except Exception:
            args_json = {}
        submit_outputs.append({
            'tool_call_id': tc_id,
            'output': json.dumps({'response': _execute_tool(tool_name, args_json)}),
        })

    if not submit_outputs:
        return None

    submit_url = (
        'https://api.coze.cn/v3/chat/submit_tool_outputs'
        '?conversation_id=' + str(conversation_id)
        + '&chat_id=' + str(chat_id)
    )
    return post(submit_url, _coze_headers(), {
        'stream': True,
        'auto_save_history': True,
        'tool_outputs': submit_outputs,
    })


def _execute_tool(tool_name, args_json):
    """执行具体的工具调用，返回结果字符串"""
    if tool_name == 'print':
        text = args_json.get('text', '')
        if text:
            print("收到工具调用：" + text)
        return '成功打印了文本：' + (text or '')

    if tool_name == 'play_music':
        try:
            print("收到工具调用：播放音乐")
            file_list = os.listdir('/data/yinyue')
            if not file_list:
                return '音乐目录为空，未播放'
            picked = random.choice(file_list)
            play_audio('/data/yinyue/' + picked)
            return '音乐播放完毕：' + picked
        except Exception as e:
            return '播放失败：' + str(e)

    return '未实现工具：' + str(tool_name)


def _read_chunked_raw(sock, initial=b''):
    """按 HTTP/1.1 标准解码 Transfer-Encoding: chunked"""
    body = b''
    buf = initial
    while True:
        while b'\r\n' not in buf:
            c = _sock_recv(sock, 64)
            if not c:
                return body
            buf += c

        line, buf = buf.split(b'\r\n', 1)
        try:
            chunk_size = int(line.decode('ascii', 'ignore').split(';')[0].strip(), 16)
        except ValueError:
            return body

        if chunk_size == 0:
            break

        got = 0
        while got < chunk_size:
            if buf:
                take = min(len(buf), chunk_size - got)
                body += buf[:take]
                buf = buf[take:]
                got += take
            else:
                c = _sock_recv(sock, min(512, chunk_size - got))
                if not c:
                    return body
                buf = c

        while len(buf) < 2:
            c = _sock_recv(sock, 2 - len(buf))
            if not c:
                return body
            buf += c
        buf = buf[2:]

    return body


def _read_wav_streaming(sock, body, content_length):
    """边接收边播放 WAV 音频"""
    WAV_HEADER_MIN = 0x2c
    while len(body) < WAV_HEADER_MIN:
        remain = max(0, content_length - len(body)) if content_length else 512
        need = min(WAV_HEADER_MIN - len(body), remain)
        if need <= 0:
            break
        chunk = _sock_recv(sock, min(512, need))
        if not chunk:
            break
        body += chunk

    if len(body) < WAV_HEADER_MIN:
        print("WAV 头不完整，语音合成失败已跳过")
        return None

    try:
        _, nchannels, framerate, _, _ = struct.unpack('<HHLLH', body[0x14:0x14 + 14])
    except ValueError as e:
        print("WAV 解析错误:", e)
        return None

    sampwidth = (struct.unpack('<H', body[0x22:0x24])[0] + 7) // 8
    framesize = sampwidth * nchannels
    CHUNK = int(framerate / 25)
    BUFFER_SIZE = CHUNK * framesize
    compact_threshold = BUFFER_SIZE * 4
    max_bytes = BUFFER_SIZE * 8

    audio_lock = _thread.allocate_lock()
    audio_state = {'buf': bytearray(body[0x2c:]), 'pos': 0, 'done': False}

    p = PyAudio()
    stream = p.open(
        format=p.get_format_from_width(sampwidth),
        channels=nchannels, rate=framerate,
        output=True, frames_per_buffer=CHUNK,
    )
    stream.volume(vol=85)

    _thread.start_new_thread(
        _audio_playback_worker,
        (audio_lock, audio_state, stream, p, framesize, BUFFER_SIZE, compact_threshold),
    )

    total_received = len(body)
    while content_length is not None and total_received < content_length:
        chunk = _sock_recv(sock, min(512, content_length - total_received))
        if not chunk:
            break
        total_received += len(chunk)
        while True:
            with audio_lock:
                if len(audio_state['buf']) - audio_state['pos'] + len(chunk) <= max_bytes:
                    audio_state['buf'].extend(chunk)
                    break
            time.sleep_ms(5)

    with audio_lock:
        audio_state['done'] = True

    return body


# ─── HTTP 请求方法 ────────────────────────────────────────────────

def post(url, headers, data, timeout=5):
    return request(url, 'POST', headers, data, timeout)


def get(url, headers=None, params=None, timeout=5):
    if params:
        url += ('&' if '?' in url else '?') + _urlencode(params)
    return request(url, 'GET', headers or {}, '', timeout)


def request(url, method, headers, data, timeout=5):
    if headers is None:
        headers = {}

    scheme, host, port, path = _parse_url(url)
    sock = _create_socket(host, port, timeout, use_ssl=(scheme == 'https'))

    body = _to_body(data) if data is not None else b''

    headers_lower = {k.lower() for k in headers}
    req = '%s %s HTTP/1.1\r\n' % (method, path)
    req += 'Host: %s\r\n' % host
    for key, value in headers.items():
        req += '%s: %s\r\n' % (key, value)
    if body and 'content-length' not in headers_lower:
        req += 'Content-Length: %d\r\n' % len(body)
    if 'connection' not in headers_lower:
        req += 'Connection: close\r\n'
    req += '\r\n'

    _sock_send(sock, req.encode('utf-8'))
    if body:
        _sock_send(sock, body)

    response = _read_response(sock)
    sock.close()
    return response


# ─── Coze API 接口 ────────────────────────────────────────────────

def coze_chat(message_history):
    """调用 Coze /v3/chat，流式 SSE，返回完整回复"""
    payload = {
        'bot_id': bot_id,
        'user_id': user_id,
        'stream': True,
        'additional_messages': message_history,
        'parameters': {},
    }
    return post(chat_url, _coze_headers(), payload)


def tts_play(text):
    """文本转语音并播放"""
    payload = {
        'input': text,
        'voice_id': voice_id,
        'response_format': 'wav',
        'sample_rate': 8000,
        'loudness_rate': -50,
    }
    post(tts_url, _coze_headers(), payload, timeout=3)


def asr_from_wav(filename):
    """上传 WAV 到 Coze 做语音识别，返回文本"""
    with open(filename, 'rb') as f:
        wav_data = f.read()
    body, content_type = _build_multipart(
        fields=None,
        files={'file': (filename, wav_data, 'audio/wav')},
    )
    resp = post(asr_url, _coze_headers(content_type), body)
    return _parse_asr_response(resp)


def asr_realtime(btn):
    """实时语音识别：录音线程采集，主线程 chunked 编码流式上传"""
    FRAMERATE = 16000
    SAMPWIDTH = 2
    NCHANNELS = 1
    CHUNK = FRAMERATE // 25
    BUFFER_SIZE = CHUNK * SAMPWIDTH * NCHANNELS
    compact_threshold = BUFFER_SIZE * 4

    print('按住按键说话...')
    _lcd_show_lines(["按住按键说话...", "松开结束录音"])
    while btn.value() != 0:
        time.sleep_ms(10)

    _lcd_show_lines(["录音中...", "正在采集语音"])
    p = PyAudio()
    stream = p.open(
        format=paInt16, channels=NCHANNELS, rate=FRAMERATE,
        input=True, frames_per_buffer=CHUNK,
    )
    stream.volume(70, LEFT)
    stream.volume(85, RIGHT)
    stream.enable_audio3a(AUDIO_3A_ENABLE_ANS)

    audio_lock = _thread.allocate_lock()
    state = {'buf': bytearray(), 'pos': 0, 'done': False}

    _thread.start_new_thread(_record_worker, (audio_lock, state, stream, p, btn))

    print('正在连接 ASR 服务器...')
    _lcd_show_lines(["识别中...", "连接并上传音频"])
    start_time = time.ticks_ms()

    scheme, host, port, path = _parse_url(asr_url)
    sock = _create_socket(host, port, timeout=5, use_ssl=(scheme == 'https'))

    print('连接建立耗时: %d ms' % time.ticks_diff(time.ticks_ms(), start_time))

    content_type = 'multipart/form-data; boundary=%s' % MULTIPART_BOUNDARY
    req = 'POST %s HTTP/1.1\r\n' % path
    req += 'Host: %s\r\n' % host
    req += 'Authorization: %s\r\n' % authorization
    req += 'Content-Type: %s\r\n' % content_type
    req += 'Transfer-Encoding: chunked\r\n'
    req += 'Connection: close\r\n\r\n'
    _sock_send(sock, req.encode('utf-8'))

    preamble = ('--%s\r\n' % MULTIPART_BOUNDARY).encode()
    preamble += b'Content-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
    preamble += b'Content-Type: audio/wav\r\n\r\n'

    wav_hdr = bytearray(44)
    wav_hdr[0:4] = b'RIFF'
    struct.pack_into('<I', wav_hdr, 4, 0x7FFFFFFF)
    wav_hdr[8:12] = b'WAVE'
    wav_hdr[12:16] = b'fmt '
    struct.pack_into('<I', wav_hdr, 16, 16)
    struct.pack_into('<H', wav_hdr, 20, 1)
    struct.pack_into('<H', wav_hdr, 22, NCHANNELS)
    struct.pack_into('<I', wav_hdr, 24, FRAMERATE)
    struct.pack_into('<I', wav_hdr, 28, FRAMERATE * NCHANNELS * SAMPWIDTH)
    struct.pack_into('<H', wav_hdr, 32, NCHANNELS * SAMPWIDTH)
    struct.pack_into('<H', wav_hdr, 34, SAMPWIDTH * 8)
    wav_hdr[36:40] = b'data'
    struct.pack_into('<I', wav_hdr, 40, 0x7FFFFFFF)

    _send_chunk(sock, preamble + bytes(wav_hdr))

    total_sent = 0
    while True:
        chunk_data = None
        done = False
        with audio_lock:
            available = len(state['buf']) - state['pos']
            if available > 0:
                chunk_data = bytes(state['buf'][state['pos']:])
                state['pos'] += available
                if state['pos'] >= compact_threshold:
                    state['buf'] = bytearray(state['buf'][state['pos']:])
                    state['pos'] = 0
            done = state['done']

        if chunk_data:
            _send_chunk(sock, chunk_data)
            total_sent += len(chunk_data)
        elif done:
            with audio_lock:
                remaining = len(state['buf']) - state['pos']
                if remaining > 0:
                    chunk_data = bytes(state['buf'][state['pos']:])
            if chunk_data:
                _send_chunk(sock, chunk_data)
                total_sent += len(chunk_data)
            break
        else:
            time.sleep_ms(10)

    print('录音结束，共发送 %d 字节音频数据' % total_sent)

    _send_chunk(sock, ('\r\n--%s--\r\n' % MULTIPART_BOUNDARY).encode())
    _sock_send(sock, b'0\r\n\r\n')

    response = _read_response(sock)
    sock.close()
    return _parse_asr_response(response)


# ─── 主循环 ───────────────────────────────────────────────────────

def _scan_ssid_set(sta):
    """把 sta.scan() 结果里的 SSID 归一化成 str 集合（兼容 bytes / 带尾零）"""
    found = set()
    try:
        ap_list = sta.scan()
    except Exception as e:
        print('WiFi 扫描失败:', e)
        return found
    for ap in ap_list:
        if not ap:
            continue
        raw = ap[0] if isinstance(ap, (tuple, list)) else getattr(ap, 'ssid', None)
        if raw is None:
            continue
        if isinstance(raw, bytes):
            try:
                s = raw.decode('utf-8').rstrip('\x00')
            except Exception:
                s = ''
        else:
            s = str(raw).rstrip('\x00')
        if s:
            found.add(s)
    return found


def connect_wifi():
    """扫描 WiFi，仅当扫到配置里的 SSID 才连接；都未扫到则不连网并退出。"""
    sta = network.WLAN(0)
    sta.active(True)
    time.sleep_ms(100)

    _lcd_show_lines(["扫描WiFi...", "请稍候"])
    scanned = _scan_ssid_set(sta)

    chosen = None
    for ssid, password in WIFI_CREDENTIALS.items():
        if ssid in scanned:
            chosen = (ssid, password)
            break

    if chosen is None:
        _lcd_show_lines(["未扫描到", "706/Kittenbot"])
        print('未扫描到已知 WiFi（706 / Kittenbot），不连接')
        raise SystemExit(1)

    ssid, password = chosen
    print('将连接:', ssid)

    if not sta.isconnected():
        _lcd_show_lines(["WiFi连接中", ssid[:20]])
        for i in range(20):
            _lcd_show_lines(["连接中 %s" % ssid[:12], "尝试 %d/20" % (i + 1)])
            try:
                sta.connect(ssid, password)
            except Exception:
                pass
            if sta.isconnected():
                break
            time.sleep(0.5)
        else:
            _lcd_show_lines(["WiFi连接超时", ssid[:20]])
            print('WiFi 连接超时:', ssid)
            raise SystemExit(1)

    try:
        cfg = sta.ifconfig()
        ip = cfg[0] if cfg else ''
    except Exception:
        ip = ''

    _lcd_show_lines(["WiFi已连接", ip[:40]])
    print("WiFi 已连接", ip)


def main_loop():
    """asr-chatbot-tts 主循环：按键说话 -> 识别 -> 对话 -> 合成语音并播放"""
    message_history = []
    global LCD_REPLY_START_LINE

    print('asr-chatbot-tts 已启动，按下按键开始说话...')
    _lcd_show_lines(["已启动", "等待按键开始"])

    while True:
        print('\n等待按键开始新一轮对话...')
        _lcd_show_lines(["等待按键...", "按下开始说话"])

        print('开始实时语音识别...')
        user_text = asr_realtime(btn)
        if not user_text:
            print('识别结果为空，跳过本轮。')
            continue

        print('识别结果:', user_text)
        user_lines = _wrap_text_by_chars(user_text, LCD_MAX_CHARS_PER_LINE)
        max_header_lines = max(0, LCD_MAX_LINES - 2)
        header_lines = ["识别结果"] + user_lines
        if len(header_lines) > max_header_lines:
            header_lines = header_lines[:max_header_lines]
        LCD_REPLY_START_LINE = len(header_lines)
        _lcd_show_lines(header_lines)

        message_history.append({
            'content': user_text,
            'content_type': 'text',
            'role': 'user',
            'type': 'question',
        })

        print('发送到聊天机器人...')
        answer = coze_chat(message_history)
        if not answer:
            print('机器人没有返回内容。')
            continue

        message_history.append({
            'content': answer,
            'content_type': 'text',
            'role': 'assistant',
            'type': 'answer',
        })

        tts_play(answer)


if __name__ == '__main__':
    connect_wifi()
    main_loop()
