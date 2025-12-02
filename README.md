## 环境变量

| 环境变量名                      | 释义            |
| ------------------------------- | --------------- |
| ALIBABA_CLOUD_ACCESS_KEY_ID     | 阿里云ak        |
| ALIBABA_CLOUD_ACCESS_KEY_SECRET | 阿里云sk        |
| ALIBABA_CLOUD_RECORDID          | 域名解析记录id  |
| ALIBABA_CLOUD_RR                | 域名前缀        |
| ALIBABA_CLOUD_IPTYPE            | 域名类型        |
| GOTIFY_ADDRESS                  | gotify地址:端口 |
| GOTIFY_TOKEN                    | gotify token    |
| CLOUDFARE_TOKEN                 |                 |
| CLOUDFARE_ZONE_ID               |                 |
| CLOUDFARE_RECORD_NAME           |                 |



## 使用方式

1. 配置环境变量

   例如`~/.profile`中增加

    ``` bash
    [ -f ~/.secrets ] && source ~/.secrets
    ```
   在 `~/.secrets`中设置
      ``` bash
      export A="B"
      ```
2. 初始化

    ```bash
    sh script.sh init
    ```

3. 运行

    ```bash
    sh script.sh run
    ```

4. 周期运行

   例如可以托管在1panel定时任务中