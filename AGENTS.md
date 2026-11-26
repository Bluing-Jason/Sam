# 太陽能監察平台開發規則 / Solar platform development rules

使用者於 2026-11-26 確認，適用於本專案後續更新。

## GitHub 同步

- 每次完成程式更新及適當驗證後，將相關程式、測試及文件同步至 https://github.com/Bluing-Jason/Sam 的 `solar` 分支。這是持續授權，例行同步無需重複確認。
- 保留分支既有提交及其他人的變更；使用一般提交及推送，不強制覆寫遠端歷史。
- 同步前檢查差異及敏感資料。儲存庫目前為公開，只提交程式及不含真實資料的設定範本，不提交帳密、真實設備位址、收件人或歷史數據。
- 公開版本的備用控制器位址使用本機或文件示例位址，不從現有站點設定帶入內網位址。

## 伺服器設定及歷史數據

- 部署更新前先備份，使用指定程式檔案更新；不得用整個專案目錄覆蓋伺服器資料。
- 保留伺服器上的 `config.json`、`email_config.json`、`.env`、`pv_accum.json`、`data/`、所有 CSV、其他現有設定及歷史數據。不得以本機檔案或範本取代，也不得刪除、清空或重建歷史資料。
- 保留既有資料路徑及服務啟動設定。此程式的設定及 PV 累積檔案位於 CSV 目錄的上一層，更新時須核對實際路徑。
- 如程式更新需要資料格式遷移，先備份並以保留既有內容及歷史數據的方式遷移。
- 更新後驗證服務、程式版本、既有設定及歷史數據仍可讀取。持續採集期間允許新增記錄，既有記錄必須保留。

## English

- After each completed code update and appropriate validation, sync the relevant code, tests and documentation to the `solar` branch of `Bluing-Jason/Sam`. Routine sync is already authorized; do not ask for repeated confirmation.
- Preserve existing commits and others' changes. Use normal commits and pushes; do not force-push over remote history.
- Review diffs and sensitive information before publishing. The repository is currently public. Publish source and safe configuration examples only; exclude credentials, real device addresses, recipients and historical data. Keep the public fallback controller address local or use a documentation example.
- Back up before deployment and replace only the intended program files. Preserve server configuration, `config.json`, `email_config.json`, `.env`, `pv_accum.json`, `data/`, all CSV files and historical data. Never replace these with local files or templates, or delete, clear or recreate historical records.
- Preserve existing data paths and service startup settings. Configuration and PV accumulation files are stored in the parent of the CSV directory.
- If a format migration is required, back up and migrate without losing existing settings or history. After deployment, verify service health, program version and access to existing configuration/history. New collection records may be appended; existing records must remain.
