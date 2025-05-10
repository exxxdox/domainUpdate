import os

import requests


def send_gotify_message(title, message, **kw):
    url = f"http://{os.environ.get('GOTIFY_ADDRESS')}/message" #
    params = {
        "token": os.environ.get('GOTIFY_TOKEN')
    }
    data = {
        "title": title,
        "message": message,
        "priority": 5
    }
    try:
        response = requests.post(url, params=params, data=data)
        if response.status_code == 200:
            pass
            # print("响应数据:", response.json())
        else:
            print("gotify api 请求失败，状态码:", response.status_code)
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
