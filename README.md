# ESP32 Trade Calendar

广西电力交易日历工具，包含两个部分：

- `trade_calendar_server/`：本地网页服务，上传交易时间安排 PDF，转换为 `schedule.csv` 和 `schedule.json`，并提供网页日历预览。
- `trade_calendar_display/`：ESP32-S3-RLCD-4.2 Arduino 固件，从网页服务读取 `schedule.csv`，在开发板上显示封面和交易日历。

## 网页服务

```powershell
python .\trade_calendar_server\server.py
```

浏览器打开：

```text
http://127.0.0.1:8080/
```

开发板读取 CSV 的地址会显示在网页右上角，例如：

```text
http://你的电脑局域网IP:8080/schedule.csv
```

## ESP32 固件

烧录前在 `trade_calendar_display/trade_calendar_display.ino` 中修改：

```cpp
const char *WIFI_SSID = "YOUR_WIFI_NAME";
const char *WIFI_PASS = "YOUR_WIFI_PASSWORD";
const char *SCHEDULE_URL = "http://YOUR_SERVER_IP:8080/schedule.csv";
```

开发板型号建议使用 ESP32-S3，参数参考：

- Flash Size: `16MB`
- PSRAM: `OPI PSRAM`
- USB CDC On Boot: `Enabled`

## 说明

仓库不提交本地依赖、构建产物、上传过的 PDF 原件和 Wi-Fi 密码。当前转换后的 `schedule.csv` / `schedule.json` 会随源码保存，方便开发板读取和后续调试。
