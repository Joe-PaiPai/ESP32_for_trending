# ESP32-S3-RLCD-4.2 资料笔记

资料来源：
- Spotpear 用户指南：https://spotpear.com/wiki/ESP32-S3-RLCD-4.2-inch-Deepseek-Xiaozhi-AI.html
- Waveshare GitHub 示例仓库：https://github.com/waveshareteam/ESP32-S3-RLCD-4.2

## 硬件能力

- 主控：ESP32-S3-WROOM-1-N16R8，Xtensa LX7 双核，最高 240MHz。
- 存储：16MB Flash，8MB PSRAM。
- 通信：2.4GHz Wi-Fi，Bluetooth 5 LE。
- 屏幕：4.2 寸全反射 RLCD，分辨率 300 x 400，无背光，刷新比电子墨水屏快。
- 供电：板载 18650 电池座；有 RTC 独立电池接口。
- 按键：BOOT、PWR、KEY，其中 KEY 可做自定义功能键。
- 存储扩展：TF 卡槽，支持 FAT32。
- 时钟：PCF85063 RTC，可做离线时间保持。
- 传感器：SHTC3 温湿度。
- 音频：ES7210 ADC、ES8311 音频 codec、双麦克风、扬声器接口。
- 扩展：2 x 8Pin 2.54mm 排母，预留 USB、UART、I2C、多路 GPIO。

## 对交易日历项目的意义

- Wi-Fi STA：开发板可以连接办公室/手机热点，从电脑网页服务读取 `schedule.csv`。
- Wi-Fi AP：开发板也可以自己开热点，保留备用上传/调试入口。
- RTC：后续可加入离线时间，不依赖每次联网校时。
- TF 卡：后续可把 CSV、历史 PDF 解析结果、日志保存到卡里，电脑网页关闭后仍能显示。
- KEY/BOOT：当前已用于前后日期/月切换，后续可以加入翻页、隐藏/显示过滤项。
- 反射屏：适合日历、清单、看板类静态显示；不适合复杂动画或高刷新彩色 UI。
- 温湿度：可在日历空白区加环境温湿度。
- 电池：可做桌面独立显示器，但需要后续加低功耗策略和电量显示。

## 官方 Demo 重点

Spotpear/Waveshare 文档列出的 Arduino 示例：

- `01_WIFI_AP`：开发板开热点。
- `02_WIFI_STA`：连接路由器并获取 IP。
- `03_ADC_Test`：读取锂电池电压/电量。
- `04_I2C_PCF85063`：初始化和读取 RTC 时间。
- `05_I2C_SHTC3`：读取温湿度。
- `06_SD_Card`：挂载 TF 卡并读写文件。
- `07_Audio_Test`：麦克风录音和扬声器播放。
- `08_LVGL_V8_Test`：LVGL v8 图形显示。
- `09_LVGL_V9_Test`：LVGL v9 图形显示。
- `10_FactoryProgram`：综合示例，包含 SD、ADC、按键、RTC、温湿度、Wi-Fi、音频。

## 后续可参考的实现方向

1. RTC 离线时间
   - 参考 `04_I2C_PCF85063`。
   - I2C 初始化示例中使用 SDA/SCL 相关总线配置。
   - 目标：联网时同步 NTP 并写入 RTC；断网时从 RTC 读时间。

2. TF 卡保存交易日历
   - 参考 `06_SD_Card`。
   - 目标：网页生成 CSV 后，开发板下载并保存到 TF 卡或 SPIFFS；断网也能显示最后一次数据。

3. 电池电量显示
   - 参考 `03_ADC_Test`。
   - 目标：右下角或顶部显示电池百分比。

4. 更复杂 UI
   - 当前项目使用 U8g2 直接绘图，稳定轻量。
   - 如果要做更复杂页面、图标、动画或多页面控件，可参考 `08_LVGL_V8_Test` 或 `09_LVGL_V9_Test`。

5. 语音功能
   - 硬件支持双麦克风和扬声器，但交易日历项目暂时不需要。
   - 后续可做“语音播报今日交易”或“问今天有哪些交易”。

## 资料链接

- 原理图：Spotpear 资源页链接到 `ESP32-S3-RLCD-4.2-Schematic.pdf`。
- 结构尺寸：Spotpear 资源页链接到 `ESP32-S3-RLCD-4.2-3dFile.rar`。
- ST7305 屏幕资料：Spotpear 资源页链接到 `ST7305 Datasheet`。
- ES8311、PCF85063、SHTC3 数据手册：Spotpear 资源页提供链接。
- 示例仓库目录：
  - `01_Arduino_Libraries`
  - `02_Example`
  - `03_Firmware`
  - `Tools-Configuration.png`

