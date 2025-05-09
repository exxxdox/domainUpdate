import os
import sys

from typing import List

from alibabacloud_alidns20150109.client import Client as Alidns20150109Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_alidns20150109 import models as alidns_20150109_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_tea_console.client import Client as ConsoleClient
from alibabacloud_tea_util.client import Client as UtilClient


def create_client() -> Alidns20150109Client:
    """
    使用AK&SK初始化账号Client
    @return: Client
    @throws Exception
    """
    # 工程代码泄露可能会导致 AccessKey 泄露，并威胁账号下所有资源的安全性。以下代码示例仅供参考。
    # 建议使用更安全的 STS 方式，更多鉴权访问方式请参见：https://help.aliyun.com/document_detail/378659.html。
    config = open_api_models.Config(
        # 必填，请确保代码运行环境设置了环境变量 ALIBABA_CLOUD_ACCESS_KEY_ID。,
        access_key_id=os.environ.get('ALIBABA_CLOUD_ACCESS_KEY_ID'),
        # 必填，请确保代码运行环境设置了环境变量 ALIBABA_CLOUD_ACCESS_KEY_SECRET。,
        access_key_secret=os.environ.get('ALIBABA_CLOUD_ACCESS_KEY_SECRET'),
    )
    # Endpoint 请参考 https://api.aliyun.com/product/Alidns
    config.endpoint = f'alidns.cn-hangzhou.aliyuncs.com'
    return Alidns20150109Client(config)

if __name__ == '__main__':
    print(os.environ.get('ALIBABA_CLOUD_ACCESS_KEY_ID'))
    print(os.environ.get('ALIBABA_CLOUD_ACCESS_KEY_SECRET'))