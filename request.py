import socket
import struct
import _thread
from media.pyaudio import *
from media.media import *
from media.display import *
import json

DISPLAY_WIDTH = ALIGN_UP(800, 16)
DISPLAY_HEIGHT = 480

LCD_FONT_SIZE = 20
LCD_LINE_HEIGHT = 24
LCD_TEXT_COLOR = (255, 255, 255)
LCD_MAX_LINES = DISPLAY_HEIGHT // LCD_LINE_HEIGHT

LCD_MAX_CHARS_PER_LINE = 40

# chat（SSE 增量回复）从 LCD 的第几行开始绘制。
# main_loop 在显示识别结果后会设置这个值，这样“识别结果”和“回复内容”可以同屏。
LCD_REPLY_START_LINE = 0

def _wrap_text_by_chars(text, max_chars_per_line=LCD_MAX_CHARS_PER_LINE):
    """把长文本按固定字符数切成多行，供 LCD 多行绘制使用。"""
    if text is None:
        return []
    # 替换换行，避免出现意外的行空白
    text = str(text).replace('\r', '').replace('\n', ' ')
    if not text:
        return []
    return [text[i:i + max_chars_per_line] for i in range(0, len(text), max_chars_per_line)]

def _lcd_show_lines(lines):
    """在 LCD 上显示 1~n 行提示信息（用于状态引导）"""
    img.clear()
    y = 0
    for i in range(min(len(lines), LCD_MAX_LINES)):
        s = lines[i]
        if not s:
            continue
        img.draw_string_advanced(0, y, LCD_FONT_SIZE, s, color=LCD_TEXT_COLOR)
        y += LCD_LINE_HEIGHT
    Display.show_image(img)


img = image.Image(DISPLAY_WIDTH, DISPLAY_HEIGHT, image.ARGB8888)
# use lcd as display output
Display.init(Display.ST7701, width = DISPLAY_WIDTH, height = DISPLAY_HEIGHT, to_ide = True)

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


def _read_chunked_sse_chat(sock, initial=b''):
    """解析 chat 的 SSE（Transfer-Encoding: chunked）增量消息"""
    buf = initial
    # SSE 一次事件由多行组成（event:/data: 等），需要边收边按行处理
    event_type = None
    full_answer = ''
    # LCD 增量绘制参数：每次只画“新收到的 content”
    max_chars_per_line = 40
    font_size = LCD_FONT_SIZE
    line_height = LCD_LINE_HEIGHT  # 与 font_size 对齐的经验值，避免重叠
    max_lines = DISPLAY_HEIGHT // line_height
    cell_width = DISPLAY_WIDTH // max_chars_per_line

    # 预留标题行：根据 main_loop 传入的起始行，在“识别结果”后开始展示回复
    reply_start_line = LCD_REPLY_START_LINE
    if reply_start_line <= 0:
        # 未设置时，兼容旧行为：清屏并从第 0 行开始显示回复
        img.clear()
        reply_start_line = 0

    # 标题行
    title_line = reply_start_line
    img.draw_string_advanced(
        0,
        title_line * line_height,
        font_size,
        "回复中...",
        color=LCD_TEXT_COLOR,
    )

    # 光标从标题下一行开始
    cursor_line = title_line + 1
    cursor_col = 0
    cursor_x = 0
    cursor_y = cursor_line * line_height
    Display.show_image(img)
    while True:
        data_lines = []
        c = _sock_recv(sock, 512)
        if not c:
            break
        buf += c

        while True:
            index = buf.find(b'\n')
            if index == -1:
                break
            data_lines.append(buf[:index])
            buf = buf[index+1:]

        for data_line in data_lines:
            if not data_line:
                continue
            # print("sse line:", data_line)
            if data_line.startswith(b'event:'):
                event_type = data_line[6:].strip()
            elif data_line.startswith(b'data:'):
                # data: 后面是一个 json 行，可能带 \r\n，先 strip 掉
                payload = data_line[5:].strip()
                try:
                    data_content = json.loads(payload)
                except Exception:
                    # 非预期的 data 行，跳过继续等下一条事件
                    continue
                if event_type == b'conversation.message.delta':
                    content = data_content.get('content', '')
                    if content:
                        # 边收边显示：只打印增量，避免 \r 在部分终端/日志里变成换行
                        full_answer += content
                        # 按当前 cursor(x,y) 增量绘制，超出当行字数则换行
                        left = content
                        while left:
                            if cursor_line >= max_lines:
                                # 屏幕满了：不再绘制新内容，仍继续接收网络数据
                                break
                            remaining_in_line = max_chars_per_line - cursor_col
                            if remaining_in_line <= 0:
                                cursor_line += 1
                                cursor_col = 0
                                cursor_x = 0
                                cursor_y = cursor_line * line_height
                                continue

                            take = min(len(left), remaining_in_line)
                            part = left[:take]
                            if part:
                                img.draw_string_advanced(cursor_x, cursor_y, font_size, part, color=(255, 255, 255))
                            cursor_col += take
                            cursor_x = cursor_col * cell_width
                            left = left[take:]

                        Display.show_image(img)
                        print(content, end='')
                elif event_type == b'conversation.message.completed':
                    # completed 事件里可能还带 type 字段，统一返回 answer 的 content
                    # completed 后补一个换行，让控制台/日志更整洁
                    print()
                    return data_content.get('content', '') or full_answer


def _read_chunked_raw(sock, initial=b''):
    """按 HTTP/1.1 标准解码 Transfer-Encoding: chunked，并返回原始 body bytes"""
    body = b''
    buf = initial
    while True:
        # 读取 chunk size 行：直到出现 \r\n
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
            # 0\r\n\r\n 后面可能还有 trailer，这里至少把后续的 \r\n 吃掉
            while len(buf) < 2:
                c = _sock_recv(sock, 2 - len(buf))
                if not c:
                    return body
                buf += c
            if buf[:2] == b'\r\n':
                buf = buf[2:]
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

        # 吃掉 chunk 末尾的 \r\n
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
    content_type = ''
    for line in headers_str.split('\r\n'):
        if line.lower().startswith('transfer-encoding:'):
            if 'chunked' in line.lower():
                chunked = True
            break

    for line in headers_str.split('\r\n'):
        if line.lower().startswith('content-type:'):
            content_type = line.split(':', 1)[1].strip().lower()
            break

    is_event_stream = ('event-stream' in content_type)

    if chunked:
        # 只在 SSE/chat 场景下按事件解析；其它 chunked 返回原始 bytes，避免误影响其它流式输出
        looks_like_sse = (b'event:' in body[:256] or b'data:' in body[:256])
        if is_event_stream or looks_like_sse:
            return _read_chunked_sse_chat(sock, body)
        return _read_chunked_raw(sock, body)

    # 解析 Content-Length
    content_length = None
    for line in headers_str.split('\r\n'):
        if line.lower().startswith('content-length:'):
            content_length = int(line.split(':', 1)[1].strip())
            break
    # print("content_length:",content_length)
    # print("body:",body)
    # print("body length:",len(body))


    audio_play_module = False
    BUFFER_SIZE = 0
    CHUNK = 0
    audio_data = None
    p = None
    stream = None
    audio_lock = None
    audio_state = None
    if body[0:4] == b'RIFF':
        audio_play_module = True
        # 兼容首包不完整：WAV PCM 头固定至少 44 字节（RIFF/WAVE/fmt/data）
        WAV_HEADER_MIN = 0x2c  # 44
        if len(body) < WAV_HEADER_MIN:
            #print("首次接收到的WAV头不完整，需要继续接收")
            # content_length 可能存在也可能不存在；有则不超过，避免阻塞到多余数据
            remain = None
            if content_length is not None:
                remain = max(0, content_length - len(body))
            need = WAV_HEADER_MIN - len(body)
            if remain is not None:
                need = min(need, remain)
            while need > 0:
                chunk = _sock_recv(sock, min(512, need))
                if not chunk:
                    break
                body += chunk
                need = WAV_HEADER_MIN - len(body)

        if len(body) < WAV_HEADER_MIN:
            print(f"wav header not enough, len:{len(body)} body:{body[:64]}")
            print("语音合成失败已跳过")
            return None
        try:
            wFormatTag, nchannels, framerate, dwAvgBytesPerSec, wBlockAlign = struct.unpack('<HHLLH', body[0x14:0x14+14])
        except ValueError as e:
            print(f"wav file error:{e},len:{len(body)},body:{body}")
            print("语音合成失败已跳过")
            return None
        sampwidth = struct.unpack('<H', body[0x22:0x24])[0]
        sampwidth = (sampwidth + 7) // 8

        # print("framerate:",framerate)
        # print("sampwidth:",sampwidth)
        # print("nchannels:",nchannels)
        framesize = sampwidth * nchannels
        CHUNK = int(framerate / 25)#960
        BUFFER_SIZE = CHUNK * framesize#1920
        # WAV PCM 数据区一般从 0x2c(44字节)开始；这里沿用原有偏移。
        audio_data = bytearray(body[0x2c:])
        p = PyAudio()
        stream = p.open(format=p.get_format_from_width(sampwidth),
                    channels=nchannels,
                    rate=framerate,
                    output=True,frames_per_buffer=CHUNK)
        stream.volume(vol=85)

        audio_lock = _thread.allocate_lock()
        # 消费端用 pos 做“挪移”，避免频繁 audio_data=b'' 导致丢余量/重复拼接
        audio_state = {'buf': audio_data, 'pos': 0, 'done': False}

        max_bytes = BUFFER_SIZE * 8
        compact_threshold = BUFFER_SIZE * 4

        def _audio_playback_worker(_lock, _state, _stream, _p, _framesize, _buffer_size, _compact_threshold):
            import time as _time
            try:
                while True:
                    block = None
                    with _lock:
                        available = len(_state['buf']) - _state['pos']
                        if available >= _buffer_size:
                            block = bytes(_state['buf'][_state['pos']:_state['pos'] + _buffer_size])
                            _state['pos'] += _buffer_size
                            if _state['pos'] >= _compact_threshold:
                                # Micropython 中 `bytearray` 切片删除不一定支持；
                                # 用重切片替代“删除前缀”，避免抛异常。
                                _state['buf'] = bytearray(_state['buf'][_state['pos']:])
                                _state['pos'] = 0
                        elif _state['done']:
                            rem = len(_state['buf']) - _state['pos']
                            if rem <= 0:
                                break
                            # 末尾数据按“整帧”刷新，避免写入半帧导致的噪声/卡顿
                            rem_len = rem - (rem % _framesize)
                            if rem_len > 0:
                                block = bytes(_state['buf'][_state['pos']:_state['pos'] + rem_len])
                                _state['pos'] += rem_len
                                if _state['pos'] >= _compact_threshold:
                                    _state['buf'] = bytearray(_state['buf'][_state['pos']:])
                                    _state['pos'] = 0
                            else:
                                break

                    if block:
                        _stream.write(block)
                    else:
                        _time.sleep_ms(5)
            except Exception as e:
                print("audio playback thread error:", e)
            finally:
                # 在播放线程里做清理，避免主线程没法 join 导致提前 close
                try:
                    _stream.stop_stream()
                except Exception:
                    pass
                try:
                    _stream.close()
                except Exception:
                    pass
                try:
                    _p.terminate()
                except Exception:
                    pass

        _thread.start_new_thread(
            _audio_playback_worker,
            (audio_lock, audio_state, stream, p, framesize, BUFFER_SIZE, compact_threshold),
        )
        import time as _time_main

    while content_length is not None and len(body) < content_length:
        need = content_length - len(body)
        chunk = _sock_recv(sock, min(512, need))
        if audio_play_module:
            if not chunk:
                break
            while True:
                with audio_lock:
                    available = len(audio_state['buf']) - audio_state['pos']
                    if available + len(chunk) <= max_bytes:
                        audio_state['buf'].extend(chunk)
                        break
                # 缓冲太多，等待播放线程消费
                _time_main.sleep_ms(5)
        else:
            if not chunk:
                break
            body += chunk

    if audio_play_module and audio_state is not None:
        # 通知播放线程：网络收包结束，可以把剩余缓冲刷完后退出
        with audio_lock:
            audio_state['done'] = True

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
    sock.settimeout(5)
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
    # print("start send request...")
    # start_time = time.ticks_ms()
    _sock_send(sock, req.encode('utf-8'))
    if body:
        _sock_send(sock, body)
    # print("send request time: ", time.ticks_diff(time.ticks_ms(), start_time))
    # print("start read response...")
    response = _read_response(sock)
    sock.close()

    return response



import network
import time
import json
from machine import Pin

# 连接 WiFi
sta = network.WLAN(0)
sta.active(True)
if not sta.isconnected():
    _lcd_show_lines(["WiFi连接中", "正在尝试"])
    # 等待连接
    for i in range(20):
        _lcd_show_lines(["WiFi连接中", "尝试 %d/20" % (i + 1)])
        # 有些固件/网络状态下，单次 connect 后 isconnected 可能不会及时刷新；
        # 这里按你的描述：每次尝试都 connect 一次，然后立刻检查状态。
        try:
            sta.connect("706", "12345678")
        except Exception:
            pass
        if sta.isconnected():
            break
        time.sleep(0.5)
    else:
        _lcd_show_lines(["WiFi连接超时", "请检查网络"])
        print("WiFi 连接超时")
        raise SystemExit(1)

try:
    cfg = sta.ifconfig()
    ip = cfg[0] if cfg else ''
except Exception:
    ip = ''

_lcd_show_lines(["WiFi已连接", ip[:40]])
print("WiFi 已连接", ip)

# Coze 相关配置
authorization = 'Bearer pat_JrYrSPfHItMZUfpFEuwp3GEqqPM5OQXI5ftAqbYGd3XNSCVkBnuMTTpxBw79DfDc'
bot_id = '7618103224301944847'
user_id = 'k230_zy'

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
    return resp

def tts_play(text):
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
    post(tts_url, headers, payload)

    # with open(filename, 'wb') as f:
    #     for i in range(0, len(resp), 1024):
    #         f.write(resp[i:i+1024])

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

def asr_realtime(btn):
    """实时语音识别：录音线程采集音频，主线程同步建立连接并以 chunked 编码流式上传"""
    FRAMERATE = 16000
    SAMPWIDTH = 2
    NCHANNELS = 1
    CHUNK = FRAMERATE // 25
    FRAMESIZE = SAMPWIDTH * NCHANNELS
    BUFFER_SIZE = CHUNK * FRAMESIZE

    boundary = '----K230Boundary9876543210'

    print('按住按键说话...')
    _lcd_show_lines(["按住按键说话...", "松开结束录音"])
    while btn.value() != 0:
        time.sleep_ms(10)

    # ---- 在主线程初始化录音硬件 ----
    _lcd_show_lines(["录音中...", "正在采集语音"])
    p = PyAudio()
    stream = p.open(
        format=paInt16,
        channels=NCHANNELS,
        rate=FRAMERATE,
        input=True,
        frames_per_buffer=CHUNK,
    )
    stream.volume(70, LEFT)
    stream.volume(85, RIGHT)
    stream.enable_audio3a(AUDIO_3A_ENABLE_ANS)

    # 共享缓冲区
    audio_lock = _thread.allocate_lock()
    state = {'buf': bytearray(), 'pos': 0, 'done': False}
    compact_threshold = BUFFER_SIZE * 4

    def _record_worker(_lock, _state, _stream, _p, _btn):
        import time as _t
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
            try:
                _stream.stop_stream()
            except Exception:
                pass
            try:
                _stream.close()
            except Exception:
                pass
            try:
                _p.terminate()
            except Exception:
                pass

    _thread.start_new_thread(
        _record_worker, (audio_lock, state, stream, p, btn)
    )

    # ---- 主线程：建立连接（与录音并行） ----
    print('正在连接 ASR 服务器...')
    _lcd_show_lines(["识别中...", "连接并上传音频"])
    start_time = time.ticks_ms()
    scheme, host, port, path = _parse_url(asr_url)
    use_ssl = (scheme == 'https')
    ai = socket.getaddrinfo(host, port)
    addr = ai[0][-1]

    sock = socket.socket()
    sock.settimeout(5)
    sock.connect(addr)

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

    print('连接建立耗时: %d ms' % time.ticks_diff(time.ticks_ms(), start_time))

    # ---- 发送 HTTP 请求头（chunked 编码） ----
    content_type = 'multipart/form-data; boundary=%s' % boundary
    req = 'POST %s HTTP/1.1\r\n' % path
    req += 'Host: %s\r\n' % host
    req += 'Authorization: %s\r\n' % authorization
    req += 'Content-Type: %s\r\n' % content_type
    req += 'Transfer-Encoding: chunked\r\n'
    req += 'Connection: close\r\n'
    req += '\r\n'
    _sock_send(sock, req.encode('utf-8'))

    def _send_chunk(data):
        _sock_send(sock, ('%x\r\n' % len(data)).encode())
        _sock_send(sock, data)
        _sock_send(sock, b'\r\n')

    # multipart 前导
    preamble = ('--%s\r\n' % boundary).encode()
    preamble += b'Content-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
    preamble += b'Content-Type: audio/wav\r\n\r\n'

    # WAV 头（size 用占位值，服务端按实际数据长度解析）
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

    _send_chunk(preamble + bytes(wav_hdr))

    # ---- 边录边发 ----
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
            _send_chunk(chunk_data)
            total_sent += len(chunk_data)
        elif done:
            with audio_lock:
                remaining = len(state['buf']) - state['pos']
                if remaining > 0:
                    chunk_data = bytes(state['buf'][state['pos']:])
            if chunk_data:
                _send_chunk(chunk_data)
                total_sent += len(chunk_data)
            break
        else:
            time.sleep_ms(10)

    print('录音结束，共发送 %d 字节音频数据' % total_sent)

    # multipart 结束标记
    epilogue = ('\r\n--%s--\r\n' % boundary).encode()
    _send_chunk(epilogue)

    # chunked 编码终止符
    _sock_send(sock, b'0\r\n\r\n')

    # ---- 读取并解析响应 ----
    response = _read_response(sock)
    sock.close()

    try:
        result = json.loads(response.decode('utf-8'))
    except Exception as e:
        print('解析 ASR 响应失败:', e, response)
        return ''

    try:
        return result['data']['text']
    except Exception as e:
        print('解析 ASR 响应失败:', e, result)
        return ''

def main_loop():
    """asr-chatbot-tts 主循环：按键说话 -> 识别 -> 对话 -> 合成语音并播放"""
    btn = Pin(21, Pin.IN, Pin.PULL_UP)
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
        # 让“识别结果”和“回复内容”同屏：回复标题/正文从识别结果下方开始绘制
        max_header_lines = max(0, LCD_MAX_LINES - 2)  # 预留 1 行“回复中...” + 至少 1 行正文
        header_lines = ["识别结果"] + user_lines
        if len(header_lines) > max_header_lines:
            header_lines = header_lines[:max_header_lines]
        LCD_REPLY_START_LINE = len(header_lines)
        _lcd_show_lines(header_lines)

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

        tts_play(answer)

if __name__ == '__main__':
    main_loop()
    # message_history = []
    # message_history.append({
    #     'content': "你好",
    #     'content_type': 'text',
    #     'role': 'user',
    #     'type': 'question'
    # })    
    # coze_chat(message_history)