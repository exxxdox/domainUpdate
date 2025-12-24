import os

import requests

gotify_address = os.environ.get('GOTIFY_ADDRESS')
gotify_token = os.environ.get('GOTIFY_TOKEN')


def send_gotify_message(title, message, **kw):
    if gotify_address is None or gotify_token is None:
        return
    url = f"https://{gotify_address}/message"  #
    params = {
        "token": gotify_token
    }
    data = {
        "title": title,
        "message": message,
        "priority": 0
    }
    try:
        response = requests.post(url, params=params, data=data)
        if response.status_code == 200:
            pass
            print("Gotify 响应数据:", response.json())
        else:
            print("Gotify api 请求失败，状态码:", response.status_code)
    except requests.exceptions.Timeout as e:
        if isinstance(e, requests.exceptions.ConnectTimeout):
            print("连接超时!")
        elif isinstance(e, requests.exceptions.ReadTimeout):
            print("读取超时!")
        print(f"请求超时: {e}")
    except requests.exceptions.RequestException as e:
        print(f"发生了: requests.exceptions.RequestException")


if __name__ == '__main__':
    send_gotify_message(message="test", title="test")
