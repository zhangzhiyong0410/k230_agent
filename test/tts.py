import requests

authorization = 'Bearer pat_JrYrSPfHItMZUfpFEuwp3GEqqPM5OQXI5ftAqbYGd3XNSCVkBnuMTTpxBw79DfDc'

# voices_url = 'https://api.coze.cn/v1/audio/voices'
# voices_headers = {
#     'Authorization': authorization,
#     'Content-Type': 'application/json'
# }
# voices_payload = {
#     'filter_system_voice': False,
#     'model_type': 'big',
#     'voice_state': '', 
#     'page_num': 1,
#     'page_size': 100
# }
# response = requests.get(voices_url, headers=voices_headers, json=voices_payload)
# #print(response.json())
# voices_list = {
# }
# for voice in response.json()['data']['voice_list']:
#     voices_list[voice['name']] = voice['voice_id']
#     print(voice['name'], voice['voice_id'])

# curl --location --request POST 'https://api.coze.cn/v1/audio/speech' \
# --header 'Authorization: Bearer pat_OYDacMzM3WyOWV3Dtj2bHRMymzxP****' \
# --header 'Content-Type: application/json' \
# -d '{
#   "input": "你好呀",
#   "voice_id": "742894*********",
#   "response_format": "wav"
# }' 
# --output speech.wav

speech_url = 'https://api.coze.cn/v1/audio/speech'
speech_headers = {
    'Authorization': authorization,
    'Content-Type': 'application/json'
}
speech_payload = {
    'input': '你好呀',
    #'voice_id': voices_list['邻家女孩'],
    'voice_id': '7426720361733046281',
    'response_format': 'wav'
}
response = requests.post(speech_url, headers=speech_headers, json=speech_payload)

with open('speech.wav', 'wb') as f:
    f.write(response.content)