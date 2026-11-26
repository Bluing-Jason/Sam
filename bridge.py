#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GE100B Solar Controller HTTP Bridge V3
=======================================
原版功能：将瀏覽器 HTTP 請求轉發到 GE100B 的 TCP Socket
增强功能：
  - CSV 长期数据记录 (~/ty-solar-history/<device>.csv)
  - HTTP Basic Auth (公网部署必需)
  - 通过 .env 文件读配置

API:
    GET  /                — 静态 HTML
    GET  /api/ping        — 健康检查 (无需 auth)
    POST /api/test        — 测试 TCP 连通性
    POST /api/modbus      — 转发 Modbus 帧
    POST /api/log_save    — 写一行 CSV 数据
    GET  /api/log_list    — 列出所有 CSV 文件
    GET  /api/log_get      — 读取指定设备的 CSV 数据 (JSON)
    GET  /api/log_download — 下載指定设备的 CSV 檔案 (attachment)
    GET  /api/pv_accum     — 讀取 PV 累積發電量 (後台 24h 線程累積)
    GET  /api/status_all   — 一次拎所有設備實時狀態 + 全局統計 (公眾 view 用)
    GET  /api/email_config — 讀郵件告警配置
    POST /api/email_config — 儲存郵件告警配置
    POST /api/email_test   — 發送測試郵件

环境变量(也可写到 .env 文件):
    TY_AUTH_USER     用户名 (默认: admin)
    TY_AUTH_PASS     密码   (无默认 — 不设则不启用认证, 仅适合本地)
    TY_HTTP_PORT     端口   (默认: 8080)
    TY_LOG_DIR       CSV 目录 (默认: ~/ty-solar-history)

僅使用標準函式庫，無需 pip install。
"""

import argparse
import base64
import csv
import http.server
import hmac
import io
import json
import math
import os
import re
import socket
import socketserver
import sys
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs


CONFIG = {
    'http_port': 8080,
    'ge_host': '127.0.0.1',
    'ge_port': 1030,
    'auth_user': None,
    'auth_pass': None,
}

VERSION = '3.6.5'
HTML_FILE = 'solar_dashboard.html'

# CSV 数据目录(在用户主目录下)
LOG_DIR = os.path.join(os.path.expanduser('~'), 'ty-solar-history')

# ============================================================
#  PV 累積後台輪詢
#  - 每 5 秒讀一次所有設備 PV 功率
#  - 用梯形積分累積成當日 PV 總發電量 (Wh)
#  - 數據存喺 LOG_DIR/../pv_accum.json
#  - 每日 00:00 自動換新 key (按日期分桶)
# ============================================================
PV_ACCUM_FILE = None  # 由 main() 設置, 在 LOG_DIR 旁邊
PV_ACCUM_LOCK = threading.Lock()
PV_ACCUM_STATE = {
    # device_id: {
    #   'last_ts': <epoch>, 'last_pv_w': <float>,
    #   'daily': { 'YYYY-MM-DD': <accumulated Wh>, ... },
    # }
}
PV_POLL_INTERVAL = 5  # 秒
PV_MAX_GAP = 600       # 秒, 超過呢個間隔當作斷線唔累積
PV_KEEP_DAYS = 90      # 保留 90 日歷史


def _pv_accum_path():
    return PV_ACCUM_FILE or os.path.join(LOG_DIR, '..', 'pv_accum.json')


def _today_key():
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d')


def _load_pv_accum():
    """從 disk 加載 PV 累積數據, 自動清理明顯錯誤嘅數據"""
    global PV_ACCUM_STATE
    p = _pv_accum_path()
    if not os.path.exists(p):
        PV_ACCUM_STATE = {}
        return
    try:
        with open(p, encoding='utf-8') as f:
            PV_ACCUM_STATE = json.load(f) or {}
    except Exception as e:
        print(f'⚠ PV accum 加載失敗: {e}, 重新開始')
        PV_ACCUM_STATE = {}
        return

    # 自動清理: 單日累積 > 1 MWh (1,000,000 Wh) 嘅站點數據明顯錯誤
    # (單個 12V 100Ah 系統理論最大發電量約 5-10 kWh/日, 1 MWh 完全不可能)
    n_purged = 0
    for dev_id in list(PV_ACCUM_STATE.keys()):
        daily = PV_ACCUM_STATE[dev_id].get('daily', {})
        for date_key in list(daily.keys()):
            if daily[date_key] > 1_000_000:
                del daily[date_key]
                n_purged += 1
        # 重置 last_pv_w 同 last_ts (避免錯誤值繼續造成跳變)
        PV_ACCUM_STATE[dev_id]['last_pv_w'] = None
        PV_ACCUM_STATE[dev_id]['last_ts'] = None
    if n_purged > 0:
        print(f'⚠ 已清理 {n_purged} 筆異常 PV 累積數據 (>1 MWh/日)')
        _save_pv_accum()


def _save_pv_accum():
    """寫回 disk (持有 LOCK 嘅情況下調用)"""
    p = _pv_accum_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # 寫到臨時檔再 rename, 確保原子性
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(PV_ACCUM_STATE, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as e:
        print(f'⚠ PV accum 寫入失敗: {e}')


def _cleanup_old_pv_data():
    """清理 PV_KEEP_DAYS 天之前的數據 (持有 LOCK 嘅情況下調用)"""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=PV_KEEP_DAYS)).strftime('%Y-%m-%d')
    for dev_id in list(PV_ACCUM_STATE.keys()):
        daily = PV_ACCUM_STATE[dev_id].get('daily', {})
        for date_key in list(daily.keys()):
            if date_key < cutoff:
                del daily[date_key]


def _crc16(data):
    """計算 Modbus RTU CRC16 (返回 2 bytes, low first)"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


# ============================================================
#  郵件告警系統
#  - 配置存喺 LOG_DIR/../email_config.json (獨立於 config.json, 有密碼)
#  - 檢查電池電壓 < 閾值 → 發郵件
#  - 每個設備獨立 cooldown, 避免刷屏
# ============================================================
EMAIL_CONFIG_FILE = None
EMAIL_ALERT_STATE = {
    # device_id: last_alert_ts (epoch)
}
EMAIL_LOCK = threading.Lock()

DEFAULT_EMAIL_CONFIG = {
    'enabled': False,
    'smtp_host': 'smtp.gmail.com',
    'smtp_port': 587,
    'smtp_user': '',
    'smtp_pass': '',
    'from_email': '',
    'from_name': 'TY 太陽能監控',
    'company_name': 'TY 太陽能監控系統',   # 郵件底部落款 (可自定義)
    'recipients': [],            # [{email, enabled, alerts, reports}]；兼容舊字串名單
    'cooldown_minutes': 60,      # 每個設備多久最多一封郵件
    'use_tls': True,             # 587 用 STARTTLS; 465 用 SSL
    # === 即時電壓告警 (子開關) ===
    'alert_email_enabled': True,          # 是否啟用即時電壓告警(預設 True 保留舊行為)
    # === 每日健康檢查 ===
    'daily_report_enabled': False,        # 是否啟用每日檢查
    'daily_report_times': ['09:00'],      # 檢查時間陣列 HH:MM (24小時制)
    'daily_report_send_ok': False,        # 若冇異常, 是否都發個「一切正常」郵件
}

# 每日報告執行記錄 (防止同一 slot 一日 run 多次)
DAILY_REPORT_STATE = {'last_ran_slots': {}}   # {"2026-07-04_09:00": True, ...}


def _email_config_path():
    return EMAIL_CONFIG_FILE or os.path.join(LOG_DIR, '..', 'email_config.json')


EMAIL_CONFIG_LOCK = threading.RLock()


def _normalise_recipients(recipients):
    """舊電郵字串沿用接收兩類郵件；新名單保留獨立開關。"""
    if not isinstance(recipients, list):
        raise ValueError('收件人列表格式不正確')
    result, seen = [], set()
    for item in recipients:
        if isinstance(item, str):
            item = {'email': item}
        if not isinstance(item, dict) or not isinstance(item.get('email'), str):
            raise ValueError('每位收件人須有電郵地址')
        email = item['email'].strip()
        pattern = r"[A-Za-z0-9.!#$%&'*+/=?^_{}|~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
        if len(email) > 254 or not re.fullmatch(pattern, email):
            raise ValueError(f'電郵地址格式不正確: {email}')
        entry = {'email': email}
        for key in ('enabled', 'alerts', 'reports'):
            value = item.get(key, True)
            if not isinstance(value, bool):
                raise ValueError(f'收件人 {key} 須為開啟或關閉')
            entry[key] = value
        if email.casefold() in seen:
            raise ValueError(f'收件人電郵重複: {email}')
        seen.add(email.casefold())
        result.append(entry)
    return result


def _recipient_addresses(cfg, message_type='test'):
    if message_type not in ('alert', 'report', 'test'):
        raise ValueError('郵件類別不正確')
    entries = _normalise_recipients(cfg.get('recipients', []))
    return [entry['email'] for entry in entries if entry['enabled'] and (
        entry['alerts'] if message_type == 'alert' else
        entry['reports'] if message_type == 'report' else
        entry['alerts'] or entry['reports'])]


def _load_email_config():
    p = _email_config_path()
    if not os.path.exists(p):
        return DEFAULT_EMAIL_CONFIG.copy()
    try:
        with open(p, encoding='utf-8') as f:
            cfg = json.load(f) or {}
        # 合併預設值 (支持新加字段)
        merged = DEFAULT_EMAIL_CONFIG.copy()
        merged.update(cfg)
        # 兼容舊字段 daily_report_time (單一) → daily_report_times (陣列)
        if 'daily_report_time' in cfg and cfg.get('daily_report_time'):
            if not merged.get('daily_report_times'):
                merged['daily_report_times'] = [cfg['daily_report_time']]
        return merged
    except Exception as e:
        print(f'⚠ email_config.json 加載失敗: {e}')
        return DEFAULT_EMAIL_CONFIG.copy()


def _save_email_config(cfg):
    p = _email_config_path()
    try:
        clean = dict(cfg)
        clean['recipients'] = _normalise_recipients(clean.get('recipients', []))
        with EMAIL_CONFIG_LOCK:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
            try: os.chmod(p, 0o600)
            except Exception: pass
        return True
    except Exception as e:
        print(f'⚠ email_config.json 寫入失敗: {e}')
        return False


def _send_email(cfg, subject, body_text, body_html=None, message_type='test'):
    """發送郵件 (阻塞, 應該喺線程入面 call)
    Returns (success:bool, error:str|None)"""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.utils import formataddr

    try:
        recipients = _recipient_addresses(cfg, message_type)
    except ValueError as e:
        return (False, str(e))
    if not recipients:
        return (False, '沒有已啟用且接收這類郵件的收件人')
    if not cfg.get('from_email') or not cfg.get('smtp_host'):
        return (False, 'SMTP 配置不完整')

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = formataddr((cfg.get('from_name', 'TY 太陽能監控'), cfg['from_email']))
        msg['To'] = ', '.join(recipients)
        msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
        if body_html:
            msg.attach(MIMEText(body_html, 'html', 'utf-8'))

        port = int(cfg.get('smtp_port', 587))
        host = cfg['smtp_host']
        user = cfg.get('smtp_user', '')
        pw = cfg.get('smtp_pass', '')

        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if cfg.get('use_tls', True):
                server.starttls()
        try:
            if user and pw:
                server.login(user, pw)
            # sendmail 返回 dict {recipient: (code, err_msg)} 為被拒絕嘅收件人
            refused = server.sendmail(cfg['from_email'], recipients, msg.as_string())
            if refused:
                # 部分收件人被拒 — 仍然算成功但要 log
                refused_list = ', '.join(f"{r} ({info[0]}: {info[1].decode() if isinstance(info[1], bytes) else info[1]})" for r, info in refused.items())
                print(f'⚠ 部分收件人被拒: {refused_list}')
                return (True, f'部分收件人被拒: {refused_list}')
            return (True, None)
        finally:
            try: server.quit()
            except Exception: pass
    except smtplib.SMTPRecipientsRefused as e:
        # 所有收件人被拒
        refused_list = ', '.join(f"{r}" for r in e.recipients.keys())
        return (False, f'所有收件人被拒: {refused_list}')
    except Exception as e:
        return (False, str(e))


def _try_alert_email(dev, bat_v, threshold_v, now_ts, status=None):
    """檢查是否需要發送低電壓告警郵件
    status: {pv_w, bat_soc} 從 modbus 讀取的實時狀態"""
    cfg = _load_email_config()
    if not cfg.get('enabled'):
        return
    # sub switch: 即時電壓告警可以獨立關閉
    if not cfg.get('alert_email_enabled', True):
        return
    if not _recipient_addresses(cfg, 'alert'):
        return
    dev_id = dev.get('id')
    if not dev_id:
        return

    cooldown_sec = int(cfg.get('cooldown_minutes', 60)) * 60
    with EMAIL_LOCK:
        last_ts = EMAIL_ALERT_STATE.get(dev_id, 0)
        if now_ts - last_ts < cooldown_sec:
            return  # 未夠 cooldown
        EMAIL_ALERT_STATE[dev_id] = now_ts   # 樂觀鎖, 先標記再發

    from datetime import datetime
    dev_name = dev.get('name', dev_id)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # 站點資料
    cap_ah = dev.get('batCapacityAh', 100)
    bat_type_raw = dev.get('batType', 'lifepo4')
    bat_type_label = {
        'lifepo4': '鋰電 LiFePO4',
        'lead-acid': '鉛酸',
        'other': '其他',
    }.get(bat_type_raw, bat_type_raw)

    # 實時狀態
    pv_w = status.get('pv_w') if status else None
    bat_soc = status.get('bat_soc') if status else None

    # 電壓百分比 (自己計)
    v_min = float(dev.get('batVMin', 9.3))
    v_max = float(dev.get('batVMax', 12.6))
    if v_max > v_min:
        v_pct = max(0, min(100, (bat_v - v_min) / (v_max - v_min) * 100))
    else:
        v_pct = None

    # 拎當日發電 (從 PV_ACCUM_STATE)
    today = _today_key()
    with PV_ACCUM_LOCK:
        today_wh = PV_ACCUM_STATE.get(dev_id, {}).get('daily', {}).get(today, 0.0)

    subject = f'⚠ 太陽能告警: {dev_name} 電池電壓過低 ({bat_v:.2f}V)'
    body_text = (
        f'{cfg.get("company_name", "TY 太陽能監控系統")}告警\n\n'
        f'站點名稱: {dev_name}\n'
        f'發生時間: {now_str}\n'
        f'\n'
        f'━━ 電池狀態 ━━\n'
        f'⚠ 當前電池電壓: {bat_v:.2f} V\n'
        f'告警閾值: {threshold_v:.2f} V (低於此值觸發警報)\n'
        f'電壓百分比: {v_pct:.0f} %\n' if v_pct is not None else ''
        f'控制器 SOC: {bat_soc} %\n' if bat_soc is not None else ''
    )
    # Python string concatenation 有 bug, 重新拼接
    lines = [
        f'{cfg.get("company_name", "TY 太陽能監控系統")}告警',
        f'',
        f'站點名稱: {dev_name}',
        f'電池類型: {bat_type_label}',
        f'電池容量: {cap_ah} Ah',
        f'發生時間: {now_str}',
        f'',
        f'━━ 電池狀態 ━━',
        f'⚠ 當前電池電壓: {bat_v:.2f} V (低於閾值 {threshold_v:.2f} V)',
    ]
    if v_pct is not None:
        lines.append(f'電壓百分比: {v_pct:.0f} %')
    if bat_soc is not None:
        lines.append(f'控制器 SOC: {bat_soc} %')
    lines.extend([
        f'',
        f'━━ 實時運行 ━━',
    ])
    if pv_w is not None:
        lines.append(f'當前 PV 發電功率: {pv_w:.0f} W')
    lines.append(f'今日 PV 累積發電: {int(today_wh)} Wh')
    lines.extend([
        f'',
        f'請盡快檢查該站點, 建議措施:',
        f'  1. 遠程檢查用電負載是否過大',
        f'  2. 檢查太陽能板是否被遮擋 / 有故障',
        f'  3. 必要時派人到現場',
        f'',
        f'---',
        f'此郵件由 {cfg.get("company_name", "TY 太陽能監控系統")} 自動發送',
        f'每 {cfg.get("cooldown_minutes", 60)} 分鐘最多一封同類告警',
    ])
    body_text = '\n'.join(lines)

    # HTML 版本
    pv_row = f'<tr><td style="padding:6px 0;color:#6b7280">當前發電功率:</td><td style="padding:6px 0;font-family:monospace;font-weight:bold">{pv_w:.0f} W</td></tr>' if pv_w is not None else ''
    soc_row = f'<tr><td style="padding:6px 0;color:#6b7280">控制器 SOC:</td><td style="padding:6px 0;font-family:monospace;font-weight:bold">{bat_soc} %</td></tr>' if bat_soc is not None else ''
    pct_row = f'<tr><td style="padding:6px 0;color:#6b7280">電壓百分比:</td><td style="padding:6px 0;font-family:monospace;font-weight:bold">{v_pct:.0f} %</td></tr>' if v_pct is not None else ''

    body_html = f'''<div style="font-family:Arial,sans-serif;max-width:600px">
      <div style="background:#dc2626;color:white;padding:16px 20px;font-size:18px;font-weight:bold">
        ⚠ {cfg.get("company_name", "TY 太陽能監控系統")}告警
      </div>
      <div style="background:#f9fafb;padding:20px;border:1px solid #e5e7eb">
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <tr><td style="padding:6px 0;color:#6b7280;width:130px">站點名稱:</td><td style="padding:6px 0;font-weight:bold;font-size:16px">{dev_name}</td></tr>
          <tr><td style="padding:6px 0;color:#6b7280">電池類型:</td><td style="padding:6px 0">{bat_type_label}</td></tr>
          <tr><td style="padding:6px 0;color:#6b7280">電池容量:</td><td style="padding:6px 0;font-family:monospace">{cap_ah} Ah</td></tr>
          <tr><td style="padding:6px 0;color:#6b7280">發生時間:</td><td style="padding:6px 0;font-family:monospace">{now_str}</td></tr>
        </table>

        <div style="margin-top:16px;padding:14px;background:#fef2f2;border-left:3px solid #dc2626;border-radius:2px">
          <div style="font-size:12px;color:#991b1b;margin-bottom:6px;font-weight:bold">電池狀態</div>
          <div style="font-size:28px;color:#dc2626;font-weight:bold;line-height:1">{bat_v:.2f} V</div>
          <div style="font-size:12px;color:#6b7280;margin-top:4px">告警閾值: {threshold_v:.2f} V</div>
          <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:10px">
            {pct_row}
            {soc_row}
          </table>
        </div>

        <div style="margin-top:14px;padding:14px;background:#fffbeb;border-left:3px solid #f59e0b;border-radius:2px">
          <div style="font-size:12px;color:#92400e;margin-bottom:6px;font-weight:bold">實時運行</div>
          <table style="width:100%;border-collapse:collapse;font-size:13px">
            {pv_row}
            <tr><td style="padding:6px 0;color:#6b7280">今日 PV 累積:</td><td style="padding:6px 0;font-family:monospace;font-weight:bold">{int(today_wh):,} Wh</td></tr>
          </table>
        </div>

        <div style="margin-top:14px;padding:14px;background:#f0fdf4;border-left:3px solid #22c55e;border-radius:2px">
          <div style="font-size:12px;color:#166534;margin-bottom:6px;font-weight:bold">建議措施</div>
          <ol style="margin:0;padding-left:20px;font-size:13px;line-height:1.6">
            <li>遠程檢查用電負載是否過大</li>
            <li>檢查太陽能板是否被遮擋 / 有故障</li>
            <li>必要時派人到現場</li>
          </ol>
        </div>

        <div style="margin-top:16px;font-size:11px;color:#9ca3af">
          此郵件由 {cfg.get("company_name", "TY 太陽能監控系統")} 自動發送 · 每 {cfg.get("cooldown_minutes", 60)} 分鐘最多一封同類告警
        </div>
      </div>
    </div>'''

    # 喺獨立線程發, 避免 blocking 主輪詢
    def _do_send():
        ok, err = _send_email(cfg, subject, body_text, body_html, message_type='alert')
        if ok:
            print(f'✓ 告警郵件已發送: {dev_name} ({bat_v:.2f}V)')
        else:
            print(f'✗ 告警郵件發送失敗 ({dev_name}): {err}')
            # 發送失敗, 撤銷 cooldown, 允許下次重試
            with EMAIL_LOCK:
                if EMAIL_ALERT_STATE.get(dev_id) == now_ts:
                    del EMAIL_ALERT_STATE[dev_id]
    threading.Thread(target=_do_send, daemon=True).start()



def _read_device_status(host, port, slave_id, timeout=2.5):
    """通過 TCP 讀指定設備狀態
    Returns dict {pv_w, bat_v, bat_soc, load_w, bat_current, env_temp, dev_temp,
                  today_chg_kwh, today_load_kwh} 或 None
    依協議 V3.9:
      - 環境溫度  I16 reg[6] / 100
      - 設備溫度  I16 reg[7] / 100 (機內/控制器溫度)
      - 電池 SOC  U16 reg[21]
      - 電池電壓  U16 reg[22] / 100
      - 電池電流  I16 reg[23] / 100 (充電正, 放電負)
      - 負載功率  U32 reg[28]=LOW, reg[29]=HIGH / 100
      - PV 功率   U32 reg[32]=LOW, reg[33]=HIGH / 100
      - 今日充電  U16 reg[34] / 100 (kWh)
      - 今日用電  U16 reg[37] / 100 (kWh) """
    tx = bytearray([slave_id, 0x04, 0x30, 0x30, 0x00, 0x28])
    tx += _crc16(bytes(tx))
    expected_len = 85

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.sendall(bytes(tx))
        response = bytearray()
        while len(response) < expected_len:
            try:
                chunk = sock.recv(256)
                if not chunk: break
                response.extend(chunk)
            except socket.timeout:
                break

        if len(response) < expected_len:
            return None
        body = bytes(response[:-2])
        crc_recv = bytes(response[-2:])
        if _crc16(body) != crc_recv:
            return None
        if response[0] != slave_id or response[1] != 0x04:
            return None
        i = 3
        def get_reg(idx):
            offset = i + idx * 2
            return (response[offset] << 8) | response[offset + 1]
        def get_i16(idx):
            v = get_reg(idx)
            return v - 0x10000 if v >= 0x8000 else v

        return {
            'env_temp':       get_i16(6) / 100.0,
            'dev_temp':       get_i16(7) / 100.0,
            'bat_soc':        get_reg(21),
            'bat_v':          get_reg(22) / 100.0,
            'bat_current':    get_i16(23) / 100.0,
            'load_w':         ((get_reg(29) << 16) | get_reg(28)) / 100.0,
            'pv_w':           ((get_reg(33) << 16) | get_reg(32)) / 100.0,
            'today_chg_kwh':  get_reg(34) / 100.0,
            'today_load_kwh': get_reg(37) / 100.0,
        }
    except (socket.timeout, ConnectionRefusedError, OSError):
        return None
    finally:
        try: sock.shutdown(socket.SHUT_RDWR)
        except Exception: pass
        sock.close()

# 舊 API 保留 (返回 pv_w 兼容原代碼)
def _read_pv_power_w(host, port, slave_id, timeout=2.5):
    status = _read_device_status(host, port, slave_id, timeout)
    return status['pv_w'] if status else None


# ============================================================
#  每日健康檢查
# ============================================================
def _do_daily_health_check(cfg):
    """執行每日健康檢查，如果有異常先發郵件"""
    from datetime import datetime, timedelta

    config_path = os.path.join(LOG_DIR, '..', 'config.json')
    if not os.path.exists(config_path):
        return
    try:
        with open(config_path, encoding='utf-8') as f:
            config = json.load(f)
    except Exception as e:
        print(f'⚠ 每日檢查: 讀取 config 失敗: {e}')
        return
    devices = [d for d in config.get('devices', []) if d.get('enabled', True)]
    if not devices:
        return

    yesterday_key = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    today_key = datetime.now().strftime('%Y-%m-%d')

    issues = []      # {device, category, detail}
    all_status = []  # 用於「全部正常」報告

    # 檢測重名: 有重複名嘅設備會加上 host:port 尾綴以區分
    name_count = {}
    for d in devices:
        n = d.get('name', d.get('id'))
        name_count[n] = name_count.get(n, 0) + 1

    for dev in devices:
        dev_id = dev.get('id')
        dev_name = dev.get('name', dev_id)
        if not dev_id:
            continue
        # 重名: 加 host:port 尾綴等收件人分辨
        if name_count.get(dev_name, 0) > 1:
            dev_name = f"{dev_name} ({dev.get('host', '?')}:{dev.get('port', '?')})"

        # 拎 PV 累積狀態
        with PV_ACCUM_LOCK:
            state = PV_ACCUM_STATE.get(dev_id, {})
            last_ts = state.get('last_ts')
            daily = state.get('daily', {}).copy()

        # 1. 檢查在線 (30 分鐘無數據 = 離線)
        now_ts = time.time()
        if not last_ts or (now_ts - last_ts) > 1800:
            issues.append({
                'device': dev_name,
                'category': 'offline',
                'detail': '站點離線 (超過 30 分鐘無數據)',
            })
            continue

        # 2. 讀當前狀態
        host = dev.get('host')
        port = dev.get('port')
        slave = dev.get('slaveId', 1)
        status = _read_device_status(host, int(port), int(slave), timeout=3.0) if host else None
        if not status:
            issues.append({
                'device': dev_name,
                'category': 'offline',
                'detail': '無法讀取當前數據',
            })
            continue

        bat_v = status['bat_v']
        threshold_v = float(dev.get('batVAlarmLow', 11.0))

        dev_ok = True
        # 3. 電壓過低
        if bat_v < threshold_v:
            issues.append({
                'device': dev_name,
                'category': 'low_voltage',
                'detail': f'電池電壓 {bat_v:.2f}V < 閾值 {threshold_v:.2f}V',
            })
            dev_ok = False

        # 4. 昨日發電偏低 (需要至少 3 天歷史數據)
        yest_wh = daily.get(yesterday_key, 0)
        past_keys = sorted(daily.keys())
        # 過去 7 天(唔算今日), 至少 3 天先計平均
        past_keys = [k for k in past_keys if k != today_key][-7:]
        if len(past_keys) >= 3:
            values = [daily[k] for k in past_keys]
            avg_wh = sum(values) / len(values)
            # 平均值超過 300 Wh 才有意義(過低就唔比較)
            # 昨日 < 40% 平均先算異常
            if avg_wh > 300 and yest_wh < avg_wh * 0.4:
                issues.append({
                    'device': dev_name,
                    'category': 'low_generation',
                    'detail': f'昨日發電偏低: {int(yest_wh):,} Wh (近 {len(past_keys)} 日平均 {int(avg_wh):,} Wh)',
                })
                dev_ok = False

        # 記錄本站狀態供「一切正常」郵件用
        all_status.append({
            'device': dev_name,
            'bat_v': bat_v,
            'yest_wh': yest_wh,
            'ok': dev_ok,
        })

    _send_daily_report(cfg, issues, all_status, devices)


def _send_daily_report(cfg, issues, all_status, devices):
    """發送每日健康報告"""
    from datetime import datetime, timedelta

    if not _recipient_addresses(cfg, 'report'):
        return
    if not issues and not cfg.get('daily_report_send_ok'):
        print(f'✓ 每日健康檢查完成: 冇異常, 不發郵件')
        return

    total = len(devices)
    ok_count = total - len(set(i['device'] for i in issues))
    date_str = datetime.now().strftime('%Y-%m-%d')
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime('%m-%d')

    if issues:
        subject = f'⚠ 太陽能日報 {date_str} · {len(issues)} 個問題 ({ok_count}/{total} 正常)'
    else:
        subject = f'✓ 太陽能日報 {date_str} · 全部正常 ({total} 站點)'

    # === 純文字版本 ===
    lines = [
        f'{cfg.get("company_name", "TY 太陽能監控系統")}每日健康報告',
        f'',
        f'報告日期: {date_str}',
        f'檢查時間: {datetime.now().strftime("%H:%M")}',
        f'站點總數: {total}',
        f'正常數: {ok_count}',
        f'異常數: {total - ok_count}',
        f'',
    ]
    if issues:
        lines.append('━━ 發現嘅問題 ━━')
        lines.append('')
        by_dev = {}
        for i in issues:
            by_dev.setdefault(i['device'], []).append(i)
        for dev_name, dev_issues in by_dev.items():
            lines.append(f'▸ {dev_name}')
            for i in dev_issues:
                lines.append(f'  · {i["detail"]}')
            lines.append('')
    else:
        lines.append('✓ 所有站點運行正常')
        lines.append('')

    if all_status:
        lines.append('━━ 站點狀態總覽 ━━')
        lines.append('')
        for s in all_status:
            status_icon = '✓' if s['ok'] else '⚠'
            lines.append(f'{status_icon} {s["device"]}: 電壓 {s["bat_v"]:.2f}V · 昨日發電 {int(s["yest_wh"]):,} Wh')
        lines.append('')

    lines.extend([
        f'---',
        f'此郵件由 {cfg.get("company_name", "TY 太陽能監控系統")} 每日定時發送 (時段: {", ".join(cfg.get("daily_report_times") or [cfg.get("daily_report_time", "09:00")])})',
    ])
    body_text = '\n'.join(lines)

    # === HTML 版本 ===
    header_color = '#dc2626' if issues else '#22c55e'
    header_icon = '⚠' if issues else '✓'

    # 問題塊
    issues_html = ''
    if issues:
        by_dev = {}
        for i in issues:
            by_dev.setdefault(i['device'], []).append(i)
        cat_labels = {
            'offline': ('離線', '#6c7689'),
            'low_voltage': ('電壓過低', '#dc2626'),
            'low_generation': ('發電偏低', '#f59e0b'),
        }
        rows = []
        for dev_name, dev_issues in by_dev.items():
            for i in dev_issues:
                label, color = cat_labels.get(i['category'], ('異常', '#dc2626'))
                rows.append(f'''
                  <tr>
                    <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:600">{dev_name}</td>
                    <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb"><span style="background:{color};color:white;padding:2px 8px;border-radius:2px;font-size:11px">{label}</span></td>
                    <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#4b5563">{i["detail"]}</td>
                  </tr>''')
        issues_html = f'''
        <div style="margin-top:16px">
          <div style="font-size:12px;color:#991b1b;margin-bottom:8px;font-weight:bold">發現嘅問題 ({len(issues)})</div>
          <table style="width:100%;border-collapse:collapse;font-size:13px;background:white">
            <thead>
              <tr style="background:#fef2f2">
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#991b1b">站點</th>
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#991b1b">類型</th>
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#991b1b">詳情</th>
              </tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
          </table>
        </div>'''

    # 總覽表
    overview_html = ''
    if all_status:
        rows = []
        for s in all_status:
            row_bg = '#fef2f2' if not s['ok'] else 'white'
            icon = '⚠' if not s['ok'] else '✓'
            icon_color = '#dc2626' if not s['ok'] else '#22c55e'
            rows.append(f'''
              <tr style="background:{row_bg}">
                <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-size:13px"><span style="color:{icon_color};font-weight:bold">{icon}</span> {s["device"]}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-family:monospace;font-size:13px">{s["bat_v"]:.2f} V</td>
                <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;font-family:monospace;font-size:13px">{int(s["yest_wh"]):,} Wh</td>
              </tr>''')
        overview_html = f'''
        <div style="margin-top:16px">
          <div style="font-size:12px;color:#6b7280;margin-bottom:8px;font-weight:bold">站點狀態總覽</div>
          <table style="width:100%;border-collapse:collapse;font-size:13px;background:white">
            <thead>
              <tr style="background:#f9fafb">
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280">站點</th>
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280">當前電壓</th>
                <th style="padding:8px 12px;text-align:left;font-size:12px;color:#6b7280">昨日發電</th>
              </tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
          </table>
        </div>'''

    body_html = f'''<div style="font-family:Arial,sans-serif;max-width:680px">
      <div style="background:{header_color};color:white;padding:16px 20px;font-size:18px;font-weight:bold">
        {header_icon} {cfg.get("company_name", "TY 太陽能監控系統")}每日健康報告
      </div>
      <div style="background:#f9fafb;padding:20px;border:1px solid #e5e7eb">
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <tr>
            <td style="padding:4px 0;color:#6b7280;width:130px">報告日期:</td>
            <td style="padding:4px 0;font-family:monospace">{date_str}</td>
            <td style="padding:4px 0;color:#6b7280">站點總數:</td>
            <td style="padding:4px 0;font-weight:bold">{total}</td>
          </tr>
          <tr>
            <td style="padding:4px 0;color:#6b7280">正常:</td>
            <td style="padding:4px 0;color:#22c55e;font-weight:bold">{ok_count}</td>
            <td style="padding:4px 0;color:#6b7280">異常:</td>
            <td style="padding:4px 0;color:{'#dc2626' if (total - ok_count) else '#6b7280'};font-weight:bold">{total - ok_count}</td>
          </tr>
        </table>
        {issues_html}
        {overview_html}
        <div style="margin-top:20px;font-size:11px;color:#9ca3af">
          此郵件由 {cfg.get("company_name", "TY 太陽能監控系統")} 每日定時發送 (時段: {", ".join(cfg.get("daily_report_times") or [cfg.get("daily_report_time", "09:00")])})
        </div>
      </div>
    </div>'''

    ok, err = _send_email(cfg, subject, body_text, body_html, message_type='report')
    if ok:
        print(f'✓ 每日健康報告已發送 ({len(issues)} 個問題)')
    else:
        print(f'✗ 每日健康報告發送失敗: {err}')


# CSV 自動記錄間隔 (秒)
CSV_RECORD_INTERVAL = 1800   # 30 分鐘

# ============================================================
#  NVR 在線檢測 (TCP port 探測)
# ============================================================
NVR_CHECK_INTERVAL = 60      # 每 60 秒檢測一次
NVR_CHECK_TIMEOUT = 3.0      # TCP connect timeout (秒)
NVR_STATUS_LOCK = threading.Lock()
NVR_STATUS = {}              # {dev_id: {online: bool, last_ts: float, host: str, port: int, err: str|None}}


def _check_nvr_online(host, port, timeout=NVR_CHECK_TIMEOUT):
    """TCP connect 試連, 通 = 在線"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((str(host), int(port)))
        return True, None
    except socket.timeout:
        return False, 'timeout'
    except ConnectionRefusedError:
        return False, 'refused'
    except OSError as e:
        return False, f'net err: {e.errno}'
    except Exception as e:
        return False, f'{type(e).__name__}'
    finally:
        try: sock.close()
        except Exception: pass


def _nvr_check_worker():
    """後台 NVR 檢測線程
    每 NVR_CHECK_INTERVAL 秒對每個有 nvrHost 嘅 device 做 TCP 探測"""
    print(f'  NVR 檢測線程已啟動 (每 {NVR_CHECK_INTERVAL} 秒 TCP 探測)')
    # 頭一次延遲 10 秒讓其他線程先起
    time.sleep(10)
    while True:
        try:
            config_path = os.path.join(LOG_DIR, '..', 'config.json')
            if not os.path.exists(config_path):
                time.sleep(NVR_CHECK_INTERVAL); continue
            try:
                with open(config_path, encoding='utf-8') as f:
                    cfg = json.load(f)
            except Exception:
                time.sleep(NVR_CHECK_INTERVAL); continue

            devices = cfg.get('devices', [])
            now_ts = time.time()

            for dev in devices:
                dev_id = dev.get('id')
                if not dev_id: continue
                nvr_host = (dev.get('nvrHost') or '').strip()
                nvr_port = dev.get('nvrPort')
                if not nvr_host or not nvr_port:
                    # 冇配置 NVR IP → 標記做 unconfigured
                    with NVR_STATUS_LOCK:
                        NVR_STATUS[dev_id] = {
                            'online': None,
                            'last_ts': now_ts,
                            'host': nvr_host or None,
                            'port': nvr_port or None,
                            'err': 'unconfigured',
                        }
                    continue
                try:
                    online, err = _check_nvr_online(nvr_host, nvr_port)
                except Exception as e:
                    online, err = False, str(e)
                with NVR_STATUS_LOCK:
                    NVR_STATUS[dev_id] = {
                        'online': online,
                        'last_ts': now_ts,
                        'host': nvr_host,
                        'port': int(nvr_port) if nvr_port else None,
                        'err': err,
                    }
        except Exception as e:
            print(f'⚠ NVR check worker error: {e}')
        time.sleep(NVR_CHECK_INTERVAL)


def _csv_recorder_worker():
    """後台 CSV 自動記錄線程
    每 CSV_RECORD_INTERVAL 秒讀所有 device 嘅 snapshot, 寫入 CSV
    唔依賴前端 tab, 保證數據連續性 (解決週末冇開瀏覽器就冇 CSV 嘅問題)"""
    from datetime import datetime
    print(f'  CSV 自動記錄線程已啟動 (每 {CSV_RECORD_INTERVAL//60} 分鐘)')
    # 頭一次延遲 2 分鐘, 等 PV_ACCUM_STATE 有數據
    time.sleep(120)
    while True:
        try:
            config_path = os.path.join(LOG_DIR, '..', 'config.json')
            if not os.path.exists(config_path):
                time.sleep(CSV_RECORD_INTERVAL); continue
            try:
                with open(config_path, encoding='utf-8') as f:
                    cfg = json.load(f)
            except Exception as e:
                print(f'⚠ CSV recorder: 讀 config 失敗: {e}')
                time.sleep(CSV_RECORD_INTERVAL); continue

            devices = [d for d in cfg.get('devices', []) if d.get('enabled', True)]
            ts_ms = int(time.time() * 1000)
            iso = datetime.now().astimezone().isoformat(timespec='seconds')
            recorded = 0

            for dev in devices:
                dev_id = dev.get('id')
                dev_name = dev.get('name', dev_id)
                if not dev_id: continue

                with PV_ACCUM_LOCK:
                    state = PV_ACCUM_STATE.get(dev_id, {})
                    snap = (state.get('snapshot') or {}).copy()
                    last_ts = state.get('last_ts')

                # 冇 snapshot 或者 > 5 分鐘冇更新 → 認為離線, skip
                if not snap or not last_ts or (time.time() - last_ts) > 300:
                    continue

                row = {
                    'timestamp_ms':   ts_ms,
                    'iso_time':       iso,
                    'device_id':      dev_id,
                    'device_name':    dev_name,
                    'bat_voltage':    snap.get('bat_v'),
                    'bat_soc_pct':    snap.get('bat_soc'),
                    'bat_current_a':  snap.get('bat_current'),
                    'bat_power_w':    None,
                    'pv_voltage':     None,
                    'pv_current':     None,
                    'pv_power_w':     snap.get('pv_w'),
                    'load_voltage':   None,
                    'load_current':   None,
                    'load_power_w':   snap.get('load_w'),
                    'env_temp_c':     snap.get('env_temp'),
                    'dev_temp_c':     snap.get('dev_temp'),
                    'today_chg_kwh':  snap.get('today_chg_kwh'),
                    'total_chg_kwh':  None,
                    'today_load_kwh': snap.get('today_load_kwh'),
                    'total_load_kwh': None,
                    'run_days':       None,
                    'bat_level':      None,
                    'bat_status_raw': None,
                    'chg_status_raw': None,
                    'dischg_status_raw': None,
                    'charge_stage':   '',
                    'faults':         '',
                }

                try:
                    csv_path = _csv_path_for(dev_id, dev_name)
                    is_new = not os.path.exists(csv_path)
                    lock = _get_lock(csv_path)
                    with lock:
                        with open(csv_path, 'a', encoding='utf-8', newline='') as f:
                            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction='ignore')
                            if is_new:
                                writer.writeheader()
                            writer.writerow(row)
                    recorded += 1
                except Exception as e:
                    print(f'⚠ CSV recorder: 寫 {dev_name} 失敗: {e}')

            if recorded:
                print(f'📝 CSV 自動記錄: 寫入 {recorded}/{len(devices)} 個站點')
        except Exception as e:
            print(f'⚠ CSV recorder error: {e}')

        time.sleep(CSV_RECORD_INTERVAL)


def _daily_report_worker():
    """每日報告後台線程 - 每分鐘檢查時間, 到咗任一設定時間就 run"""
    from datetime import datetime, timedelta
    print(f'  每日健康檢查線程已啟動')
    while True:
        try:
            time.sleep(60)  # 每分鐘檢查一次
            cfg = _load_email_config()
            if not cfg.get('enabled') or not cfg.get('daily_report_enabled'):
                continue
            # 拎所有設定時間 (兼容舊字段)
            times = cfg.get('daily_report_times') or []
            if not times and cfg.get('daily_report_time'):
                times = [cfg['daily_report_time']]
            if not times:
                continue

            now = datetime.now()
            today_str = now.strftime('%Y-%m-%d')
            now_min = now.hour * 60 + now.minute

            # 清理 3 天前嘅記錄 (避免無限增長)
            for slot_key in list(DAILY_REPORT_STATE['last_ran_slots'].keys()):
                date_part = slot_key.split('_')[0]
                try:
                    d = datetime.strptime(date_part, '%Y-%m-%d')
                    if (now - d).days > 3:
                        del DAILY_REPORT_STATE['last_ran_slots'][slot_key]
                except Exception:
                    pass

            # 檢查每一個設定時間
            for t in times:
                try:
                    hh, mm = [int(x) for x in t.strip().split(':')]
                except Exception:
                    continue
                target_min = hh * 60 + mm
                # 條件: 已經過咗設定時間, 但唔超過 30 分鐘 (tolerance)
                if now_min < target_min:
                    continue
                if now_min - target_min > 30:
                    continue
                # 今日呢個 slot 已 run 過就 skip
                slot_key = f'{today_str}_{t}'
                if DAILY_REPORT_STATE['last_ran_slots'].get(slot_key):
                    continue
                DAILY_REPORT_STATE['last_ran_slots'][slot_key] = True
                print(f'→ 觸發每日健康檢查 ({t}, 當前 {now.strftime("%H:%M")})')
                try:
                    _do_daily_health_check(cfg)
                except Exception as e:
                    print(f'⚠ 每日檢查執行出錯: {e}')
        except Exception as e:
            print(f'⚠ Daily report worker error: {e}')


def _pv_poll_worker():
    """後台 PV 輪詢線程
    每 PV_POLL_INTERVAL 秒讀一次所有設備, 累積能量"""
    print(f'  PV 累積線程已啟動 (每 {PV_POLL_INTERVAL} 秒輪詢)')
    while True:
        try:
            time.sleep(PV_POLL_INTERVAL)
            # 從 config.json 讀設備列表
            config_path = os.path.join(LOG_DIR, '..', 'config.json')
            if not os.path.exists(config_path):
                continue
            try:
                with open(config_path, encoding='utf-8') as f:
                    config = json.load(f)
            except Exception:
                continue
            devices = config.get('devices', [])
            now_ts = time.time()
            today = _today_key()

            for dev in devices:
                if not dev.get('enabled', True):
                    continue
                dev_id = dev.get('id')
                host = dev.get('host')
                port = dev.get('port')
                slave = dev.get('slaveId', 1)
                if not (dev_id and host and port):
                    continue
                # 讀設備狀態 (PV + 電池)
                status = _read_device_status(host, int(port), int(slave), timeout=2.5)
                if status is None:
                    continue  # 讀失敗就跳過
                pv_w = status['pv_w']
                bat_v = status['bat_v']
                # 安全網: 超過 100 kW 嘅讀值明顯有問題 (協議解析錯 / 干擾)
                if pv_w < 0 or pv_w > 100000:
                    print(f'⚠ 設備 {dev_id} 讀到異常 PV 功率: {pv_w}W, 跳過')
                    continue

                # 檢查是否需要發低電壓告警
                threshold_v = dev.get('batVAlarmLow')
                if threshold_v and bat_v > 0 and bat_v < float(threshold_v):
                    _try_alert_email(dev, bat_v, float(threshold_v), now_ts, status=status)

                with PV_ACCUM_LOCK:
                    state = PV_ACCUM_STATE.setdefault(dev_id, {
                        'last_ts': None, 'last_pv_w': None, 'daily': {}
                    })
                    last_ts = state.get('last_ts')
                    last_pv = state.get('last_pv_w')
                    state['last_ts'] = now_ts
                    state['last_pv_w'] = pv_w
                    # 儲存完整 snapshot 供 /api/status_all 使用
                    state['snapshot'] = {
                        'pv_w':           status.get('pv_w'),
                        'load_w':         status.get('load_w'),
                        'bat_v':          status.get('bat_v'),
                        'bat_soc':        status.get('bat_soc'),
                        'bat_current':    status.get('bat_current'),
                        'env_temp':       status.get('env_temp'),
                        'dev_temp':       status.get('dev_temp'),
                        'today_chg_kwh':  status.get('today_chg_kwh'),
                        'today_load_kwh': status.get('today_load_kwh'),
                        'ts':             now_ts,
                    }

                    if last_ts is not None and last_pv is not None:
                        dt_sec = now_ts - last_ts
                        if 0 < dt_sec <= PV_MAX_GAP:
                            # 梯形積分
                            avg_w = (last_pv + pv_w) / 2.0
                            inc_wh = avg_w * dt_sec / 3600.0
                            if inc_wh > 0:
                                state['daily'][today] = state['daily'].get(today, 0.0) + inc_wh

            # 每分鐘存檔一次 (避免每 5 秒就寫 disk)
            if int(now_ts) % 60 < PV_POLL_INTERVAL:
                with PV_ACCUM_LOCK:
                    _cleanup_old_pv_data()
                    _save_pv_accum()
        except Exception as e:
            print(f'⚠ PV 輪詢出錯: {e}')

# CSV 列定义(顺序固定,新加列时只追加在末尾)
CSV_COLUMNS = [
    'timestamp_ms', 'iso_time',
    'device_id', 'device_name',
    'bat_voltage', 'bat_soc_pct', 'bat_current_a', 'bat_power_w',
    'pv_voltage', 'pv_current', 'pv_power_w',
    'load_voltage', 'load_current', 'load_power_w',
    'env_temp_c', 'dev_temp_c',
    'today_chg_kwh', 'total_chg_kwh',
    'today_load_kwh', 'total_load_kwh',
    'run_days', 'bat_level',
    'bat_status_raw', 'chg_status_raw', 'dischg_status_raw',
    'charge_stage', 'faults',
]

# 文件锁保护并发写入(多个设备同时到 30 分钟点的概率虽小,加锁更稳)
_file_locks = {}
_file_locks_main = threading.Lock()

def _get_lock(path):
    with _file_locks_main:
        if path not in _file_locks:
            _file_locks[path] = threading.Lock()
        return _file_locks[path]


def _safe_filename(name):
    """把任意字符串变成安全的文件名"""
    if not name:
        return 'unknown'
    name = re.sub(r'[\s/\\:*?"<>|]+', '_', name).strip('._')
    return name[:64] or 'unknown'


def _csv_path_for(device_id, device_name):
    """每个设备一个 CSV，文件名优先用 device_name + 短 id 后缀"""
    safe = _safe_filename(device_name or device_id)
    short_id = (device_id or '')[-6:] if device_id else ''
    if short_id:
        fname = f'{safe}_{short_id}.csv'
    else:
        fname = f'{safe}.csv'
    return os.path.join(LOG_DIR, fname)


def _ensure_log_dir():
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)


def _history_timestamp(row):
    """用數值時間戳排序，無效時間保留在有效記錄之前。"""
    try:
        value = float(row.get('timestamp_ms', ''))
        if math.isfinite(value):
            return value
    except (ValueError, TypeError):
        pass
    return float('-inf')


def _read_device_history(device_id='', device_name=''):
    """按完整設備 ID 合併歷史；只讀取原檔，不遷移或重寫數據。"""
    if not device_id and not device_name:
        return [], [], list(CSV_COLUMNS)
    _ensure_log_dir()
    short_id = device_id[-6:] if device_id else ''
    safe_name = _safe_filename(device_name) if device_name else ''
    rows, sources, seen = [], [], set()
    columns = list(CSV_COLUMNS)
    for filename in sorted(os.listdir(LOG_DIR)):
        full = os.path.join(LOG_DIR, filename)
        if not filename.endswith('.csv') or not os.path.isfile(full):
            continue
        # 舊檔若沒有 ID 欄，只接受完整檔名後綴；有 ID 時以行內 ID 為準。
        legacy_id_match = bool(short_id and filename.endswith(f'_{short_id}.csv'))
        legacy_name_match = bool(safe_name and (
            filename == f'{safe_name}.csv' or
            re.fullmatch(re.escape(safe_name) + r'_[A-Za-z0-9]{6}\.csv', filename)))
        matched = False
        with _get_lock(full):
            with open(full, encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row_id = (row.get('device_id') or '').strip()
                    row_name = row.get('device_name') or ''
                    if device_id:
                        matches = row_id == device_id if row_id else legacy_id_match
                    else:
                        matches = row_name == device_name if row_name else legacy_name_match
                    if not matches:
                        continue
                    if None in row:
                        raise ValueError(f'CSV 欄數不正確: {filename}')
                    matched = True
                    for column in row:
                        if column not in columns:
                            columns.append(column)
                    # 只去除整行相同的副本；同一時間但讀值不同的記錄仍完整保留。
                    key = tuple(sorted((k, v) for k, v in row.items() if v not in (None, '')))
                    if key not in seen:
                        seen.add(key)
                        rows.append(row)
        if matched:
            sources.append(full)
    rows.sort(key=_history_timestamp)
    return rows, sources, columns


class ModbusBridgeHandler(http.server.BaseHTTPRequestHandler):
    """HTTP 处理器：服务静态 HTML + Modbus API + CSV 日志"""

    # 抑制默认访问日志
    def log_message(self, format, *args):
        if '--verbose' in sys.argv:
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))

    # ============ CORS ============
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 前端已斷開 (刷新/切 tab), 忽略 - 唔算 error
            pass

    def log_message(self, format, *args):
        # 抑制 BrokenPipe 等連接中斷嘅噪音 log
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # 客戶端斷開連接, 唔係錯
            self.close_connection = True

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    # ============ Authentication ============
    # 不需要鉴权的路径(/api/ping 用于探测 bridge 是否启动, 不暴露任何敏感信息)
    PUBLIC_PATHS = {'/api/ping'}

    def _check_auth(self):
        """Returns True if request is authorized (or auth disabled)"""
        if not CONFIG['auth_pass']:
            return True   # 未配密码 = 本地模式,不验证
        path = urlparse(self.path).path
        if path in self.PUBLIC_PATHS:
            return True
        header = self.headers.get('Authorization', '')
        if not header.startswith('Basic '):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode('utf-8', errors='replace')
            user, _, pw = raw.partition(':')
        except Exception:
            return False
        # 用 hmac.compare_digest 防时序攻击
        u_ok = hmac.compare_digest(user, CONFIG['auth_user'] or '')
        p_ok = hmac.compare_digest(pw,   CONFIG['auth_pass'] or '')
        return u_ok and p_ok

    def _send_auth_required(self):
        body = b'401 Unauthorized\n'
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="TY Solar Dashboard"')
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    # ============ Routing ============
    def do_GET(self):
        if not self._check_auth():
            self._send_auth_required()
            return
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)

        if path == '/' or path == '/index.html':
            self._serve_html('solar_dashboard.html')
        elif path == '/detail' or path == '/detail.html':
            self._serve_html('solar_dashboard_detail.html')
        elif path == '/api/ping':
            self._send_json({
                'success': True,
                'version': VERSION,
                'ge_host': CONFIG['ge_host'],
                'ge_port': CONFIG['ge_port'],
                'log_dir': LOG_DIR,
                'log_features': True,
                'auth_required': bool(CONFIG['auth_pass']),
            })
        elif path == '/api/nvr_status':
            self._handle_nvr_status()
        elif path == '/api/status_all':
            self._handle_status_all()
        elif path == '/api/log_list':
            self._handle_log_list()
        elif path == '/api/log_get':
            self._handle_log_get(query)
        elif path == '/api/log_download':
            self._handle_log_download(query)
        elif path == '/api/pv_accum':
            self._handle_pv_accum(query)
        elif path == '/api/email_config':
            self._handle_email_config_get()
        elif path == '/api/config_get':
            self._handle_config_get()
        elif path == '/api/weather':
            self._handle_weather(query)
        else:
            self.send_error(404, 'Not Found')

    def do_POST(self):
        if not self._check_auth():
            self._send_auth_required()
            return
        path = urlparse(self.path).path
        if path == '/api/modbus':
            self._handle_modbus()
        elif path == '/api/test':
            self._handle_test()
        elif path == '/api/log_save':
            self._handle_log_save()
        elif path == '/api/config_set':
            self._handle_config_set()
        elif path == '/api/email_config':
            self._handle_email_config_set()
        elif path == '/api/email_test':
            self._handle_email_test()
        elif path == '/api/email_recipients':
            self._handle_email_recipients_set()
        elif path == '/api/daily_report_test':
            self._handle_daily_report_test()
        else:
            self.send_error(404, 'Not Found')

    # ============ Static HTML ============
    def _serve_html(self, filename=None):
        filename = filename or HTML_FILE
        # 搜索路徑順序: (1) bridge.py 所在目錄 (2) LOG_DIR 上層 (兼容部分部署)
        base = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(base, filename)
        if not os.path.exists(html_path):
            alt_base = os.path.dirname(LOG_DIR.rstrip('/'))
            if alt_base:
                alt = os.path.join(alt_base, filename)
                if os.path.exists(alt): html_path = alt
        # Fallback: 如果係公眾 view (solar_dashboard.html) 但檔案唔存在,
        # 試下 solar_dashboard_v3.html (舊命名, 兼容過渡期)
        if not os.path.exists(html_path) and filename == 'solar_dashboard.html':
            for dir_ in [base, os.path.dirname(LOG_DIR.rstrip('/'))]:
                if dir_:
                    alt = os.path.join(dir_, 'solar_dashboard_v3.html')
                    if os.path.exists(alt):
                        html_path = alt
                        break
        if not os.path.exists(html_path):
            print(f'⚠ HTML 不存在: {html_path}')
            self.send_error(404, f'HTML file not found: {filename}')
            return
        try:
            with open(html_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, str(e))

    def _handle_nvr_status(self):
        """GET /api/nvr_status
        返回所有 device 嘅 NVR 在線狀態 (由後台線程每 60 秒 TCP 探測 cache)"""
        try:
            # 讀 config.json 拎 device 清單以取 name
            config_path = os.path.join(LOG_DIR, '..', 'config.json')
            devs_info = {}
            if os.path.exists(config_path):
                try:
                    with open(config_path, encoding='utf-8') as f:
                        c = json.load(f)
                    for d in c.get('devices', []):
                        if d.get('id'):
                            devs_info[d['id']] = {
                                'name': d.get('name', d['id']),
                                'nvr_host': (d.get('nvrHost') or '').strip() or None,
                                'nvr_port': d.get('nvrPort'),
                            }
                except Exception:
                    pass

            result = []
            now_ts = time.time()
            online_count = 0
            offline_count = 0
            unconfigured_count = 0

            with NVR_STATUS_LOCK:
                snapshot = dict(NVR_STATUS)

            for dev_id, info in devs_info.items():
                st = snapshot.get(dev_id, {})
                online = st.get('online')   # True / False / None
                if online is True:
                    online_count += 1
                elif online is False:
                    offline_count += 1
                else:
                    unconfigured_count += 1
                result.append({
                    'id':         dev_id,
                    'name':       info['name'],
                    'nvr_host':   info['nvr_host'],
                    'nvr_port':   info['nvr_port'],
                    'online':     online,
                    'last_ts':    st.get('last_ts'),
                    'err':        st.get('err'),
                })
            self._send_json({
                'success':            True,
                'ts':                 now_ts,
                'check_interval_s':   NVR_CHECK_INTERVAL,
                'devices':            result,
                'online_count':       online_count,
                'offline_count':      offline_count,
                'unconfigured_count': unconfigured_count,
                'total':              len(result),
            })
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    def _handle_status_all(self):
        """GET /api/status_all
        一次過返回所有設備嘅實時狀態 + 全局統計
        數據源: bridge 後台 PV 累積線程每 5 秒儲存嘅 snapshot"""
        try:
            # 讀 config.json 拎設備清單
            config_path = os.path.join(LOG_DIR, '..', 'config.json')
            devices_config = []
            if os.path.exists(config_path):
                try:
                    with open(config_path, encoding='utf-8') as f:
                        c = json.load(f)
                    devices_config = c.get('devices', [])
                except Exception:
                    pass

            now_ts = time.time()
            today_key_str = _today_key()
            OFFLINE_THRESHOLD = 90   # 90 秒無 update = 離線 (輪詢週期 5s 嘅 18x)

            devs = []
            total_pv_w = 0.0
            total_load_w = 0.0
            total_today_pv_wh = 0.0
            total_today_chg_wh = 0.0
            total_today_load_wh = 0.0
            total_cum_pv_wh = 0.0
            online_count = 0
            alarm_count = 0

            with PV_ACCUM_LOCK:
                for dev in devices_config:
                    dev_id = dev.get('id')
                    if not dev_id: continue
                    state = PV_ACCUM_STATE.get(dev_id, {})
                    snap = state.get('snapshot', {}) or {}
                    last_ts = state.get('last_ts')
                    daily = state.get('daily', {})
                    today_pv_wh = daily.get(today_key_str, 0.0)
                    # 累計 = 所有 daily 之和
                    cum_pv_wh = sum(daily.values())

                    online = bool(last_ts and (now_ts - last_ts) < OFFLINE_THRESHOLD) and bool(dev.get('enabled', True))
                    # 電壓百分比 (用 batVMin / batVMax)
                    v_min = float(dev.get('batVMin', 9.3))
                    v_max = float(dev.get('batVMax', 12.6))
                    bat_v = snap.get('bat_v')
                    bat_pct = None
                    if bat_v is not None and v_max > v_min:
                        bat_pct = max(0, min(100, (bat_v - v_min) / (v_max - v_min) * 100))
                    # 告警閾值檢查
                    threshold_v = dev.get('batVAlarmLow')
                    is_alarm = False
                    if threshold_v and bat_v is not None and bat_v > 0 and bat_v < float(threshold_v):
                        is_alarm = True
                    # 累計係實時 + snapshot
                    today_chg_kwh = snap.get('today_chg_kwh') or 0
                    today_load_kwh = snap.get('today_load_kwh') or 0
                    pv_w = snap.get('pv_w') or 0
                    load_w = snap.get('load_w') or 0

                    if online:
                        online_count += 1
                        total_pv_w += pv_w
                        total_load_w += load_w
                        total_today_chg_wh += today_chg_kwh * 1000
                        total_today_load_wh += today_load_kwh * 1000
                    if is_alarm:
                        alarm_count += 1
                    total_today_pv_wh += today_pv_wh
                    total_cum_pv_wh += cum_pv_wh

                    devs.append({
                        'id':             dev_id,
                        'name':           dev.get('name', dev_id),
                        'lat':            dev.get('lat'),
                        'lon':            dev.get('lon'),
                        'online':         online,
                        'alarm':          is_alarm,
                        'pv_w':           round(pv_w, 1),
                        'load_w':         round(load_w, 1),
                        'bat_v':          round(bat_v, 2) if bat_v is not None else None,
                        'bat_soc':        snap.get('bat_soc'),
                        'bat_pct':        round(bat_pct, 0) if bat_pct is not None else None,
                        'bat_current':    round(snap.get('bat_current') or 0, 2),
                        'env_temp':       round(snap.get('env_temp') or 0, 1),
                        'dev_temp':       round(snap.get('dev_temp') or 0, 1),
                        'today_pv_wh':    round(today_pv_wh, 0),
                        'today_chg_kwh':  round(today_chg_kwh, 3),
                        'today_load_kwh': round(today_load_kwh, 3),
                        'v_min':          v_min,
                        'v_max':          v_max,
                        'v_alarm_low':    float(threshold_v) if threshold_v else None,
                        'cap_ah':         dev.get('batCapacityAh'),
                        'bat_type':       dev.get('batType'),
                        'last_ts':        last_ts,
                    })

            self._send_json({
                'success': True,
                'ts': now_ts,
                'devices': devs,
                'totals': {
                    'pv_w':          round(total_pv_w, 0),
                    'load_w':        round(total_load_w, 0),
                    'balance_w':     round(total_pv_w - total_load_w, 0),
                    'today_pv_wh':   round(total_today_pv_wh, 0),
                    'today_chg_wh':  round(total_today_chg_wh, 0),
                    'today_load_wh': round(total_today_load_wh, 0),
                    'cum_pv_wh':     round(total_cum_pv_wh, 0),
                    'online_count':  online_count,
                    'alarm_count':   alarm_count,
                    'offline_count': len(devices_config) - online_count,
                    'total_count':   len(devices_config),
                },
            })
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    # ============ Modbus / TCP test (原功能,不变) ============
    def _handle_test(self):
        try:
            body = self._read_json_body()
            host = body.get('host', CONFIG['ge_host'])
            port = int(body.get('port', CONFIG['ge_port']))

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect((host, port))
            sock.close()

            self._send_json({'success': True, 'host': host, 'port': port})
        except socket.timeout:
            self._send_json({'success': False, 'error': '連線超時，請確認 GE100B 的 IP 與端口'})
        except ConnectionRefusedError:
            self._send_json({'success': False, 'error': '連線被拒，請確認 GE100B 設定為 TCP SERVER 模式'})
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    def _handle_modbus(self):
        try:
            body = self._read_json_body()
            host = body.get('host', CONFIG['ge_host'])
            port = int(body.get('port', CONFIG['ge_port']))
            tx_data = bytes(body['tx'])
            expected_len = int(body.get('expectedLen', 0))
            timeout = float(body.get('timeout', 2.0))

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect((host, port))
                sock.sendall(tx_data)

                response = bytearray()
                while len(response) < expected_len:
                    try:
                        chunk = sock.recv(256)
                        if not chunk:
                            break
                        response.extend(chunk)
                    except socket.timeout:
                        break

                self._send_json({
                    'success': True,
                    'rx': list(response),
                    'rx_len': len(response),
                })
            finally:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                sock.close()

        except socket.timeout:
            self._send_json({'success': False, 'error': '網路超時'})
        except ConnectionRefusedError:
            self._send_json({'success': False, 'error': 'GE100B 拒絕連線'})
        except OSError as e:
            self._send_json({'success': False, 'error': f'網路錯誤: {e}'})
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    # ============ CSV log save / list / get (新增) ============
    def _handle_log_save(self):
        """POST /api/log_save  body: {deviceId, deviceName, data: {...}}"""
        try:
            body = self._read_json_body()
            device_id   = body.get('deviceId', '')
            device_name = body.get('deviceName', '')
            data        = body.get('data', {}) or {}

            _ensure_log_dir()
            csv_path = _csv_path_for(device_id, device_name)
            is_new = not os.path.exists(csv_path)

            # 时间戳(服务器端权威,避免客户端时钟漂移)
            import time
            ts_ms = int(time.time() * 1000)
            from datetime import datetime
            iso = datetime.now().astimezone().isoformat(timespec='seconds')

            row = {
                'timestamp_ms':   ts_ms,
                'iso_time':       iso,
                'device_id':      device_id,
                'device_name':    device_name,
                'bat_voltage':    data.get('batVoltage'),
                'bat_soc_pct':    data.get('batSoc'),
                'bat_current_a':  data.get('batCurrent'),
                'bat_power_w':    data.get('batPower'),
                'pv_voltage':     data.get('pvVoltage'),
                'pv_current':     data.get('pvCurrent'),
                'pv_power_w':     data.get('pvPower'),
                'load_voltage':   data.get('loadVoltage'),
                'load_current':   data.get('loadCurrent'),
                'load_power_w':   data.get('loadPower'),
                'env_temp_c':     data.get('envTemp'),
                'dev_temp_c':     data.get('devTemp'),
                'today_chg_kwh':  data.get('todayChgKwh'),
                'total_chg_kwh':  data.get('totalChgKwh'),
                'today_load_kwh': data.get('todayLoadKwh'),
                'total_load_kwh': data.get('totalLoadKwh'),
                'run_days':       data.get('runDays'),
                'bat_level':      data.get('batLevel'),
                'bat_status_raw': data.get('batStatus'),
                'chg_status_raw': data.get('chgStatus'),
                'dischg_status_raw': data.get('dischgStatus'),
                'charge_stage':   data.get('stage'),
                'faults':         ';'.join(data.get('faults', [])) if isinstance(data.get('faults'), list) else (data.get('faults') or ''),
            }

            lock = _get_lock(csv_path)
            with lock:
                with open(csv_path, 'a', encoding='utf-8', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction='ignore')
                    if is_new:
                        writer.writeheader()
                    writer.writerow(row)

            self._send_json({
                'success': True,
                'path':    csv_path,
                'is_new':  is_new,
                'timestamp_ms': ts_ms,
            })

        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    def _handle_log_list(self):
        """GET /api/log_list  → 返回所有 CSV 文件列表"""
        try:
            _ensure_log_dir()
            files = []
            for fname in sorted(os.listdir(LOG_DIR)):
                if not fname.endswith('.csv'):
                    continue
                full = os.path.join(LOG_DIR, fname)
                try:
                    stat = os.stat(full)
                    # 数行
                    with open(full, encoding='utf-8') as f:
                        line_count = sum(1 for _ in f) - 1  # 减 header
                    files.append({
                        'filename': fname,
                        'path':     full,
                        'size':     stat.st_size,
                        'mtime':    int(stat.st_mtime * 1000),
                        'rows':     max(0, line_count),
                    })
                except Exception:
                    continue
            self._send_json({
                'success': True,
                'log_dir': LOG_DIR,
                'files':   files,
            })
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    def _handle_log_get(self, query):
        """讀取合併後的設備歷史，排序後才套用時間範圍及筆數上限。"""
        try:
            query = query or {}
            device_id = query.get('deviceId', [''])[0]
            device_name = query.get('deviceName', [''])[0]
            limit = int(query.get('limit', ['5000'])[0])
            since = float(query['since'][0]) if 'since' in query else None
            if limit < 1 or (since is not None and not math.isfinite(since)):
                raise ValueError('limit 須為正整數，since 須為有效毫秒時間戳')

            all_rows, sources, _ = _read_device_history(device_id, device_name)
            total_rows = len(all_rows)
            selected = all_rows if since is None else [
                row for row in all_rows if _history_timestamp(row) >= since]
            rows = [dict(row) for row in selected[-limit:]]
            number_fields = {
                'timestamp_ms', 'bat_voltage', 'bat_soc_pct', 'bat_current_a', 'bat_power_w',
                'pv_voltage', 'pv_current', 'pv_power_w',
                'load_voltage', 'load_current', 'load_power_w',
                'env_temp_c', 'dev_temp_c',
                'today_chg_kwh', 'total_chg_kwh', 'today_load_kwh', 'total_load_kwh',
                'run_days', 'bat_level',
                'bat_status_raw', 'chg_status_raw', 'dischg_status_raw',
            }
            for row in rows:
                for key in number_fields.intersection(row):
                    if row[key] not in (None, ''):
                        try:
                            row[key] = float(row[key]) if '.' in row[key] else int(row[key])
                        except (ValueError, TypeError):
                            pass
            self._send_json({
                'success': True,
                'path': sources[0] if sources else None,
                'paths': sources,
                'total_rows': total_rows,
                'count': total_rows,
                'returned_rows': len(rows),
                'rows': rows,
            })
        except (ValueError, TypeError) as e:
            self._send_json({'success': False, 'error': str(e)}, status=400)
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    def _handle_log_download(self, query):
        """下載與 log_get 相同的完整合併歷史，原始 CSV 保持不變。"""
        try:
            query = query or {}
            device_id = query.get('deviceId', [''])[0]
            device_name = query.get('deviceName', [''])[0]
            rows, sources, columns = _read_device_history(device_id, device_name)
            if not sources:
                self.send_error(404, 'CSV not found for this device')
                return

            output = io.StringIO(newline='')
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
            data = output.getvalue().encode('utf-8-sig')
            short_id = device_id[-6:] if device_id else ''
            name = _safe_filename(device_name or device_id)
            target_name = f'{name}_{short_id}_history.csv' if short_id else f'{name}_history.csv'
            from urllib.parse import quote
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Disposition', f"attachment; filename*=UTF-8''{quote(target_name)}")
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            try:
                self.send_error(500, str(e))
            except Exception:
                pass

    def _handle_pv_accum(self, query):
        """GET /api/pv_accum  → 返回所有設備的 PV 累積數據
        Optional: ?deviceId=xxx 只返回一個, ?date=YYYY-MM-DD 指定日期"""
        try:
            device_id = (query.get('deviceId', [''])[0]) if query else ''
            date = (query.get('date', [''])[0]) if query else ''
            target_date = date or _today_key()

            with PV_ACCUM_LOCK:
                if device_id:
                    state = PV_ACCUM_STATE.get(device_id, {})
                    daily = state.get('daily', {})
                    self._send_json({
                        'success': True,
                        'deviceId': device_id,
                        'date': target_date,
                        'wh': round(daily.get(target_date, 0.0), 3),
                        'last_pv_w': state.get('last_pv_w'),
                        'last_ts': state.get('last_ts'),
                    })
                else:
                    # 返回所有設備今日累積
                    result = {}
                    for dev_id, state in PV_ACCUM_STATE.items():
                        daily = state.get('daily', {})
                        result[dev_id] = {
                            'today_wh': round(daily.get(target_date, 0.0), 3),
                            'last_pv_w': state.get('last_pv_w'),
                            'last_ts': state.get('last_ts'),
                            'history': {k: round(v, 3) for k, v in daily.items()},
                        }
                    self._send_json({
                        'success': True,
                        'date': target_date,
                        'devices': result,
                    })
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    # ============ Server-side config (shared across all clients) ============
    def _config_path(self):
        return os.path.join(LOG_DIR, '..', 'config.json')

    def _handle_config_get(self):
        """GET /api/config_get  → 读取服务端配置"""
        try:
            path = self._config_path()
            if not os.path.exists(path):
                self._send_json({'success': True, 'config': None, 'exists': False})
                return
            with open(path, encoding='utf-8') as f:
                config = json.load(f)
            stat = os.stat(path)
            self._send_json({
                'success':  True,
                'config':   config,
                'exists':   True,
                'mtime_ms': int(stat.st_mtime * 1000),
            })
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    def _handle_config_set(self):
        """POST /api/config_set  body: {config: {...}}"""
        try:
            body = self._read_json_body()
            config = body.get('config')
            if config is None:
                self._send_json({'success': False, 'error': '缺少 config 字段'})
                return
            path = self._config_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 原子写入: 先写到临时文件,再 rename(避免半途断电导致配置损坏)
            tmp_path = path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            self._send_json({
                'success':  True,
                'path':     path,
                'mtime_ms': int(os.stat(path).st_mtime * 1000),
            })
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    # ============ Email alert config ============
    def _handle_email_config_get(self):
        """GET /api/email_config  → 返回配置 (密碼 mask 為 '***')"""
        try:
            cfg = _load_email_config()
            safe = cfg.copy()
            safe['recipients'] = _normalise_recipients(safe.get('recipients', []))
            if safe.get('smtp_pass'):
                safe['smtp_pass'] = '***'   # 唔畀前端讀密碼
                safe['_pass_saved'] = True
            else:
                safe['_pass_saved'] = False
            self._send_json({'success': True, 'config': safe})
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    def _handle_email_config_set(self):
        """POST /api/email_config  body: {config: {...}}"""
        try:
            body = self._read_json_body()
            new_cfg = body.get('config', {}) or {}
            # 保留已存的密碼 (前端傳 '***' 即係唔改)
            if new_cfg.get('smtp_pass') == '***':
                existing = _load_email_config()
                new_cfg['smtp_pass'] = existing.get('smtp_pass', '')
            # 合併預設值
            merged = DEFAULT_EMAIL_CONFIG.copy()
            merged.update(new_cfg)
            merged.pop('_pass_saved', None)
            merged['recipients'] = _normalise_recipients(merged.get('recipients', []))
            ok = _save_email_config(merged)
            self._send_json({'success': ok})
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    def _handle_email_recipients_set(self):
        """只更新收件人，不影響 SMTP 密碼及其他未儲存表單設定。"""
        try:
            body = self._read_json_body()
            if 'recipients' not in body:
                raise ValueError('缺少收件人列表')
            recipients = _normalise_recipients(body['recipients'])
            with EMAIL_CONFIG_LOCK:
                cfg = _load_email_config()
                cfg['recipients'] = recipients
                ok = _save_email_config(cfg)
            self._send_json({'success': ok, 'recipients': recipients}, status=200 if ok else 500)
        except (ValueError, TypeError) as e:
            self._send_json({'success': False, 'error': str(e)}, status=400)
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)}, status=500)

    def _handle_email_test(self):
        """POST /api/email_test  body: {config: {...}} 發測試郵件, 不儲存"""
        try:
            body = self._read_json_body()
            test_cfg = body.get('config', {}) or {}
            # 密碼處理
            if test_cfg.get('smtp_pass') == '***':
                existing = _load_email_config()
                test_cfg['smtp_pass'] = existing.get('smtp_pass', '')
            merged = DEFAULT_EMAIL_CONFIG.copy()
            merged.update(test_cfg)

            from datetime import datetime
            subject = f'✓ {merged.get("company_name", "TY 太陽能監控系統")} · 測試郵件'
            body_text = (
                f'呢封係 {merged.get("company_name", "TY 太陽能監控系統")}的測試郵件。\n\n'
                f'如你收到, 表示 SMTP 配置正確, 之後低電壓告警會發到呢個郵箱。\n\n'
                f'發送時間: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}\n'
            )
            body_html = f'''<div style="font-family:Arial,sans-serif;max-width:600px">
              <div style="background:#22c55e;color:white;padding:16px 20px;font-size:18px;font-weight:bold">
                ✓ {merged.get("company_name", "TY 太陽能監控系統")} · 測試郵件
              </div>
              <div style="background:#f9fafb;padding:20px;border:1px solid #e5e7eb">
                <p>如你收到呢封郵件, 表示 SMTP 配置<strong>正確</strong>。</p>
                <p>之後低電壓告警會發到呢個郵箱。</p>
                <hr style="border:none;border-top:1px solid #e5e7eb;margin:16px 0"/>
                <div style="font-size:12px;color:#6b7280">
                  發送時間: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                </div>
              </div>
            </div>'''
            ok, err = _send_email(merged, subject, body_text, body_html, message_type='test')
            self._send_json({'success': ok, 'error': err})
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    def _handle_daily_report_test(self):
        """POST /api/daily_report_test  → 立即執行每日檢查, 用當前保存的配置"""
        try:
            cfg = _load_email_config()
            if not cfg.get('enabled'):
                self._send_json({'success': False, 'error': '請先啟用郵件告警'})
                return
            if not _recipient_addresses(cfg, 'report'):
                self._send_json({'success': False, 'error': '請先啟用至少一位接收報表的收件人'})
                return
            # 為咗測試, 強制發送 (即使無異常)
            cfg_test = cfg.copy()
            cfg_test['daily_report_send_ok'] = True
            # 執行檢查(阻塞)
            _do_daily_health_check(cfg_test)
            self._send_json({'success': True, 'message': '每日報告已觸發, 請查收郵箱'})
        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})

    # ============ Weather forecast (Open-Meteo proxy) ============
    # 通过 bridge 代理避免浏览器跨域问题。结果缓存 30 分钟避免被限流。
    _weather_cache = {}      # (lat, lon) -> {data, expires_at}
    _weather_lock = threading.Lock()

    def _handle_weather(self, query):
        """GET /api/weather?lat=22.5&lon=114.0  → 未来 3 天云量/温度预报"""
        try:
            lat = float(query.get('lat', ['0'])[0])
            lon = float(query.get('lon', ['0'])[0])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                self._send_json({'success': False, 'error': '經緯度範圍無效'})
                return

            # 缓存键: 精确到 0.1 度(约 10km),节省 API 调用
            key = (round(lat, 1), round(lon, 1))
            now = time.time()
            with self._weather_lock:
                cached = self._weather_cache.get(key)
                if cached and cached['expires_at'] > now:
                    self._send_json({
                        'success': True,
                        'cached': True,
                        'forecast': cached['data'],
                    })
                    return

            # 调 Open-Meteo
            # 文档: https://open-meteo.com/en/docs
            url = (
                f'https://api.open-meteo.com/v1/forecast'
                f'?latitude={lat}&longitude={lon}'
                f'&daily=weather_code,cloud_cover_mean,temperature_2m_max,'
                f'temperature_2m_min,sunshine_duration,precipitation_sum'
                f'&hourly=cloud_cover'
                f'&forecast_days=4'
                f'&timezone=auto'
            )
            req = urllib.request.Request(url, headers={'User-Agent': 'TY-Solar-Bridge/3.0'})
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    raw = resp.read()
                data = json.loads(raw)
            except urllib.error.URLError as e:
                self._send_json({'success': False, 'error': f'天氣 API 不可用: {e}'})
                return

            # 提取关键字段
            daily = data.get('daily', {})
            dates = daily.get('time', [])
            cloud_mean = daily.get('cloud_cover_mean', [])
            t_max = daily.get('temperature_2m_max', [])
            t_min = daily.get('temperature_2m_min', [])
            sunshine = daily.get('sunshine_duration', [])  # seconds
            precip = daily.get('precipitation_sum', [])
            weather_code = daily.get('weather_code', [])

            # WMO weather code → 简短中文
            def code_to_label(c):
                if c is None: return '未知'
                m = {
                    0: '晴', 1: '晴朗', 2: '少雲', 3: '多雲',
                    45: '霧', 48: '霧凇',
                    51: '毛毛雨', 53: '小雨', 55: '陣雨',
                    61: '小雨', 63: '中雨', 65: '大雨',
                    71: '小雪', 73: '中雪', 75: '大雪',
                    80: '陣雨', 81: '強陣雨', 82: '暴雨',
                    95: '雷暴', 96: '雷暴含冰雹', 99: '強雷暴',
                }
                return m.get(int(c), f'代碼{c}')

            forecast = []
            for i, d in enumerate(dates):
                cc = cloud_mean[i] if i < len(cloud_mean) else None
                forecast.append({
                    'date': d,
                    'cloud_cover_mean': cc,    # 0-100,值越大越陰
                    'sunshine_hours': round(sunshine[i] / 3600, 1) if i < len(sunshine) and sunshine[i] is not None else None,
                    'precipitation_mm': precip[i] if i < len(precip) else None,
                    'temp_max': t_max[i] if i < len(t_max) else None,
                    'temp_min': t_min[i] if i < len(t_min) else None,
                    'weather_code': weather_code[i] if i < len(weather_code) else None,
                    'weather_label': code_to_label(weather_code[i] if i < len(weather_code) else None),
                    # 阴天风险评级: 云量 ≥80% 且日照 <3h 算"差"; 50-80% 算"中"; 否则"好"
                    'solar_quality': (
                        'poor' if (cc is not None and cc >= 80) else
                        'fair' if (cc is not None and cc >= 50) else
                        'good'
                    ),
                })

            with self._weather_lock:
                self._weather_cache[key] = {
                    'data': forecast,
                    'expires_at': now + 30 * 60,  # 30 分钟
                }

            self._send_json({
                'success': True,
                'cached': False,
                'forecast': forecast,
            })

        except Exception as e:
            self._send_json({'success': False, 'error': str(e)})


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args():
    parser = argparse.ArgumentParser(description='GE100B 太陽能控制器 HTTP 橋接 V3 (公网部署版)')
    parser.add_argument('--port', type=int, default=None,
                        help='HTTP 端口 (env: TY_HTTP_PORT)')
    parser.add_argument('--ge-host', type=str, default=CONFIG['ge_host'],
                        help='默认 GE100B IP (env: TY_GE_HOST)')
    parser.add_argument('--ge-port', type=int, default=CONFIG['ge_port'])
    parser.add_argument('--log-dir', type=str, default=None,
                        help='CSV 目录 (env: TY_LOG_DIR)')
    parser.add_argument('--auth-user', type=str, default=None,
                        help='Basic Auth 用户名 (env: TY_AUTH_USER)')
    parser.add_argument('--auth-pass', type=str, default=None,
                        help='Basic Auth 密码 (env: TY_AUTH_PASS); 不设则不启用认证')
    parser.add_argument('--verbose', action='store_true')
    return parser.parse_args()


def _load_dotenv():
    """读取 .env 文件到环境变量 (KEY=VALUE 格式, 已存在的不覆盖)"""
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(here, '.env')
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        sys.stderr.write(f'.env 解析失败 (跳过): {e}\n')


def main():
    global LOG_DIR
    _load_dotenv()
    args = parse_args()

    # 优先级: 命令行参数 > 环境变量 > 默认
    port = args.port or int(os.environ.get('TY_HTTP_PORT', '8080'))
    log_dir = args.log_dir or os.environ.get('TY_LOG_DIR', LOG_DIR)
    auth_user = args.auth_user or os.environ.get('TY_AUTH_USER', 'admin')
    auth_pass = args.auth_pass or os.environ.get('TY_AUTH_PASS', None)
    ge_host = args.ge_host or os.environ.get('TY_GE_HOST', CONFIG['ge_host'])

    CONFIG['http_port'] = port
    CONFIG['ge_host']   = ge_host
    CONFIG['ge_port']   = args.ge_port
    CONFIG['auth_user'] = auth_user
    CONFIG['auth_pass'] = auth_pass
    LOG_DIR             = log_dir
    _ensure_log_dir()

    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), HTML_FILE)
    if not os.path.exists(html_path):
        v3_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'solar_dashboard_v3.html')
        if os.path.exists(v3_path):
            globals()['HTML_FILE'] = 'solar_dashboard_v3.html'
        else:
            print(f'⚠ 警告: 找不到 {HTML_FILE} 或 solar_dashboard_v3.html')

    print('=' * 64)
    print(f'  TY 太陽能控制器 · HTTP 橋接 V{VERSION}')
    print('=' * 64)
    print(f'  HTTP 端口:    {port}')
    print(f'  CSV 目錄:     {LOG_DIR}')
    print(f'  Python:       {sys.version.split()[0]}')
    if auth_pass:
        print(f'  認證:         ✓ Basic Auth ({auth_user})')
    else:
        print(f'  認證:         ✗ 未啟用 — 不可放公網!')
        print(f'                 設置環境變量 TY_AUTH_PASS 或 .env 啟用')
    print('=' * 64)

    # 初始化 PV 累積系統
    global PV_ACCUM_FILE
    PV_ACCUM_FILE = os.path.join(os.path.dirname(LOG_DIR.rstrip('/')) or '.', 'pv_accum.json')
    _load_pv_accum()
    pv_thread = threading.Thread(target=_pv_poll_worker, daemon=True)
    pv_thread.start()

    # 啟動每日健康檢查線程
    daily_thread = threading.Thread(target=_daily_report_worker, daemon=True)
    daily_thread.start()

    # 啟動 CSV 自動記錄線程 (每 30 分鐘寫一次, 不依賴前端 tab)
    csv_thread = threading.Thread(target=_csv_recorder_worker, daemon=True)
    csv_thread.start()

    # 啟動 NVR 在線檢測線程 (每 60 秒 TCP 探測)
    nvr_thread = threading.Thread(target=_nvr_check_worker, daemon=True)
    nvr_thread.start()

    # 初始化郵件配置路徑
    global EMAIL_CONFIG_FILE
    EMAIL_CONFIG_FILE = os.path.join(os.path.dirname(LOG_DIR.rstrip('/')) or '.', 'email_config.json')
    _email_cfg = _load_email_config()
    if _email_cfg.get('enabled'):
        recipients = _email_cfg.get('recipients', [])
        active = len(_recipient_addresses(_email_cfg, 'test'))
        print(f'  郵件告警:     ✓ 已啟用 ({active}/{len(recipients)} 個收件人已啟用)')
    else:
        print(f'  郵件告警:     ○ 未啟用')

    try:
        httpd = ThreadedHTTPServer(('0.0.0.0', port), ModbusBridgeHandler)
    except OSError as e:
        if 'Address already in use' in str(e) or (hasattr(e, 'errno') and e.errno in (48, 98)):
            print(f'✗ 錯誤: 端口 {port} 已被佔用')
        else:
            print(f'✗ 啟動失敗: {e}')
        sys.exit(1)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n→ 正在關閉...')
        # 關閉前儲存最後一次 PV 累積
        with PV_ACCUM_LOCK:
            _save_pv_accum()
        httpd.shutdown()
        print('→ 已關閉')


if __name__ == '__main__':
    main()
