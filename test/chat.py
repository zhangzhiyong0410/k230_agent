import requests
import json
import socket
import threading

chat_message = ""
shutdown_event = threading.Event()

bot_id = '7618103224301944847'
user_id = '123456789'
authorization = 'Bearer pat_JrYrSPfHItMZUfpFEuwp3GEqqPM5OQXI5ftAqbYGd3XNSCVkBnuMTTpxBw79DfDc'

Paragraph_marks = ['，','。','？','！','～']

url = 'https://api.coze.cn/v3/chat?'

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
    'additional_messages':
        message_history
    ,
    'parameters': {}
}

# 连接超时、单次读超时（流式接口长时间无包会触发，可按需调大）
HTTP_TIMEOUT = (30, 120)

def chat():
    global chat_message
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
                url, headers=headers, json=payload, stream=True, timeout=HTTP_TIMEOUT
            )
        except requests.RequestException as e:
            if not shutdown_event.is_set():
                print(f"[chat] 请求失败: {e}")
            continue
        data_type = ""
        try:
            for line in response.iter_lines():
                if shutdown_event.is_set():
                    break
                if line:
                    data = line.decode('utf-8')
                    # print(data)
                    if data[0:5] == "event":
                        data_type = data[6:]
                    elif data[0:4] == "data":
                        if data_type == "conversation.message.delta":
                            data = json.loads(data[5:])
                            print(data['content'], end='', flush=True)
                        elif data_type == "conversation.message.completed":
                            data = json.loads(data[5:])
                            if data['type'] == "answer":
                                message_history.append({
                                    'content': data['content'],
                                    'content_type': data['content_type'],
                                    'role': data['role'],
                                    'type': data['type']
                                    })
                                print('\n')
                        elif data_type == "conversation.chat.requires_action":
                            data = json.loads(data[5:])
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

# 处理单个客户端的函数（独立线程）
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

            # 回复消息
            conn.send(f"服务端回复：{chat_message}".encode('utf-8'))
    finally:
        conn.close()
        print(f"[断开连接] {addr} 已退出")

# 主服务程序
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
                target=handle_client, args=(conn, addr), daemon=True
            )
            client_thread.start()
            print(f"[活跃连接数] {threading.active_count() - 1}")
    except KeyboardInterrupt:
        print("\n[退出] 收到 Ctrl+C", flush=True)
    finally:
        shutdown_event.set()
        server_socket.close()


if __name__ == "__main__":
    start_server()
