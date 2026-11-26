# Solar Monitoring Platform

Version 3.6.5. Multi-station monitoring for GE100B solar controllers, with a Python HTTP bridge and a single HTML frontend.

[繁體中文說明](README.md)

## Features

- Live station readings, maps, NVR connectivity and battery alerts.
- Long-term CSV recording, historical file selection, daily energy statistics and supply forecasts.
- System settings for project sizing, devices, branding, collection and email notifications.
- Per-recipient enable switches and separate alert/report subscriptions.
- Project sizing with a single 60, 120, 200 or 400 Ah battery and an insufficient-capacity warning.

## Fresh installation

Use Python 3.10 or newer. The backend uses only the standard library. The browser loads Chart.js, Leaflet, fonts and map tiles online. Weather forecasts use Open-Meteo.

Run these commands from the project directory on Linux:

```sh
mkdir -p data
cp config.example.json config.json
cp email_config.example.json email_config.json
cp .env.example .env
# Edit .env and set your own TY_AUTH_USER and a strong TY_AUTH_PASS.
python3 -B bridge.py --log-dir ./data
```

Visit http://localhost:8080/ and add devices under System Settings → Devices. Configure authentication and an HTTPS reverse proxy before public access. Authentication is disabled when TY_AUTH_PASS is unset.

On Windows, create/copy the files with PowerShell New-Item / Copy-Item, then run `python -B bridge.py --log-dir ./data`. The backend reads .env beside bridge.py.

## Existing installations and data

Back up first. Replace only bridge.py and solar_dashboard.html, keeping them at the same version. Do not overwrite existing configuration or data with the examples.

--log-dir selects the CSV directory. config.json, email_config.json and pv_accum.json are stored in its parent directory. For example, --log-dir /var/www/solar/data uses /var/www/solar/config.json. Preserve your existing paths and startup arguments.

The published source defaults its fallback controller address to 127.0.0.1. Configure your own device addresses in settings, or set the fallback using --ge-host.

Real site configuration, recipients, passwords, CSV files and accumulated energy data are excluded from this repository and covered by .gitignore. Back these up separately.

If your existing Linux service is named ty-solar.service, restart after replacing the program files:

```sh
sudo systemctl restart ty-solar.service
sudo systemctl status ty-solar.service --no-pager
```

## Sizing formulas

Enter average total load P (W), daily operating hours H, days without solar D, nominal battery voltage V, and select one battery capacity.

- Daily energy: E = P × H (Wh).
- PV capacity: round up E × 1.2 ÷ (4 × 0.70) (Wp).
- Theoretical battery requirement: round up E × 1.2 × D ÷ (V × 0.95 × 0.95) (Ah).
- Available backup days: selected capacity × V × 0.95 × 0.95 ÷ (E × 1.2).

Fixed assumptions: 20% load allowance, 4 peak sun hours/day, 70% PV efficiency, 95% usable battery capacity and 95% battery-to-load efficiency. The calculator compares the requirement with one selected battery; it does not combine batteries. The battery specifications and BMS must permit 95% discharge. PV sizing covers daily supply and does not include extra capacity to recharge a multi-day deficit within a target recovery period. Account for shade, seasons and equipment limits when selecting hardware.

## Tests

Python tests use isolated data and mocked SMTP. JavaScript tests use the built-in Node.js test runner. No additional packages are needed; tests do not poll actual devices or send email.

```sh
PYTHONUTF8=1 python3 -B -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_settings.cjs tests/test_prediction.cjs tests/test_daily_energy.cjs tests/test_recipients.cjs
```

In PowerShell, set `$env:PYTHONUTF8='1'` before Python tests and use python instead of python3.
