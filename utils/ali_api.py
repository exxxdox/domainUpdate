# -*- coding: utf-8 -*-
# This file is auto-generated, don't edit it. Thanks.
import os
from alibabacloud_alidns20150109.client import Client as Alidns20150109Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_alidns20150109 import models as alidns_20150109_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_tea_console.client import Client as ConsoleClient
from alibabacloud_tea_util.client import Client as UtilClient

recordId = os.environ.get('ALIBABA_CLOUD_RECORDID')
rr = os.environ.get('ALIBABA_CLOUD_RR')
ip_type = os.environ.get('ALIBABA_CLOUD_IPTYPE')


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


class DomainUpdate:
    def __init__(self):
        pass

    @staticmethod
    def update_domain_record(
            ip
    ) -> None:
        client = create_client()
        update_domain_record_request = alidns_20150109_models.UpdateDomainRecordRequest(
            record_id=recordId,
            rr=rr,
            type=ip_type,
            value=ip
        )
        runtime = util_models.RuntimeOptions()
        try:
            resp = client.update_domain_record_with_options(update_domain_record_request, runtime)
            ConsoleClient.log(UtilClient.to_jsonstring(resp))
            print("update domain record success")
        except Exception as error:
            # 此处仅做打印展示，请谨慎对待异常处理，在工程项目中切勿直接忽略异常。
            # 错误 message
            print(error.message)
            # 诊断地址
            print(error.data.get("Recommend"))
            UtilClient.assert_as_string(error.message)

    @staticmethod
    def get_domain_record_value(
    ) -> str | None:
        client = create_client()
        describe_domain_record_info_request = alidns_20150109_models.DescribeDomainRecordInfoRequest(
            record_id=recordId
        )
        runtime = util_models.RuntimeOptions()
        try:
            # 复制代码运行请自行打印 API 的返回值
            result = client.describe_domain_record_info_with_options(describe_domain_record_info_request, runtime)
            return result.body.value
        except Exception as error:
            # 此处仅做打印展示，请谨慎对待异常处理，在工程项目中切勿直接忽略异常。
            # 错误 message
            print(error.message)
            # 诊断地址
            print(error.data.get("Recommend"))
            UtilClient.assert_as_string(error.message)


if __name__ == '__main__':
    print(DomainUpdate.get_domain_record_value())
