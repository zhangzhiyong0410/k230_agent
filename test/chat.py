import requests
import json

bot_id = '7618103224301944847'
user_id = '123456789'
authorization = 'Bearer pat_JrYrSPfHItMZUfpFEuwp3GEqqPM5OQXI5ftAqbYGd3XNSCVkBnuMTTpxBw79DfDc'

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
while True:
    message = input('请输入消息: ')
    message_history.append({
        'content': message,
        'content_type': 'text',
        'role': 'user',
        'type': 'question'
    })
    print('--------------------------------')
    response = requests.post(url, headers=headers, json=payload, stream=True)
    data_type = ""
    for line in response.iter_lines():
        if line:
            data = line.decode('utf-8')
            #print(data)
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
                        