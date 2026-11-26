# 太陽能監察平台

版本：3.6.5。GE100B 太陽能控制器的多站點監察平台，使用 Python HTTP 橋接及單一 HTML 前端。

[English documentation](README.en.md)

## 功能

- 多站點即時數據、地圖、NVR 在線狀態及電池告警。
- CSV 長期記錄、歷史選檔、每日發電／用電統計及供電預測。
- 系統設定：容量計算、站點設備、品牌、資料採集及郵件通知。
- 收件人可個別啟用，並分別選擇警報及報表。
- 新項目容量計算：單顆電池只可選 60、120、200、400 Ah，容量不足時提示。

## 全新安裝

需要 Python 3.10 或以上，後端只用標準函式庫；瀏覽器需連線載入 Chart.js、Leaflet、字體及地圖。天氣預測使用 Open-Meteo。

以下指令適用於 Linux，請在本專案目錄執行：

```sh
mkdir -p data
cp config.example.json config.json
cp email_config.example.json email_config.json
cp .env.example .env
# 編輯 .env，填入自訂 TY_AUTH_USER 及強密碼 TY_AUTH_PASS。
python3 -B bridge.py --log-dir ./data
```

開啟 http://localhost:8080/，在「系統設定 → 站點設備」加入自己的設備。透過公網使用時，須先設定認證，並使用 HTTPS 反向代理；未設定 TY_AUTH_PASS 時不會啟用認證。

Windows 可用 PowerShell 的 New-Item / Copy-Item 建立目錄及複製範本，再執行 `python -B bridge.py --log-dir ./data`。後端從 bridge.py 所在目錄讀取 .env。

## 保留現有數據

現有安裝只需更新 bridge.py 及 solar_dashboard.html；請先備份，不要用範本覆蓋現有設定或數據。兩個程式檔應使用同一版本。

--log-dir 指定 CSV 目錄。config.json、email_config.json 及 pv_accum.json 位於該目錄的上一層。例如 --log-dir /var/www/solar/data 對應 /var/www/solar/config.json。請保留原有資料路徑及啟動參數。

公開程式的備用控制器位址預設為 127.0.0.1；請在站點設定填入自己的設備位址，或使用 --ge-host 指定備用位址。

本儲存庫不包含實際站點設定、收件人、密碼、CSV 或累積發電數據；這些檔案已加入 .gitignore。請另行備份。

若現有 Linux 服務名稱為 ty-solar.service，替換程式檔後可執行：

```sh
sudo systemctl restart ty-solar.service
sudo systemctl status ty-solar.service --no-pager
```

## 容量計算

輸入設備總平均功率 P（W）、每日運行時間 H（小時）、無發電備用天數 D、電池標稱電壓 V，並選擇單顆電池容量。

- 每日用電量：E = P × H（Wh）。
- 太陽能板總功率：向上取整 E × 1.2 ÷ (4 × 0.70)（Wp）。
- 理論電池需求：向上取整 E × 1.2 × D ÷ (V × 0.95 × 0.95)（Ah）。
- 所選電池可備用天數：容量 × V × 0.95 × 0.95 ÷ (E × 1.2)。

固定假設：負載預留 20%、有效日照 4 小時／日、太陽能系統效率 70%、可放電比例 95%、電池至負載效率 95%。平台比較理論需求及所選單顆規格，不會組合多顆電池。95% 可放電比例須由電池規格及 BMS 允許。太陽能板結果估算日常供電，不包括指定時間內補回多日虧電的額外容量，實際選型仍須考慮遮擋、季節及設備限制。

## 驗證

Python 測試使用隔離資料及模擬 SMTP；JavaScript 測試使用 Node.js 內建測試工具。無需安裝套件，不會連接實際設備或寄出郵件。

```sh
PYTHONUTF8=1 python3 -B -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_settings.cjs tests/test_prediction.cjs tests/test_daily_energy.cjs tests/test_recipients.cjs
```

PowerShell 執行 Python 測試前可設定 `$env:PYTHONUTF8='1'`，並使用 python 代替 python3。
