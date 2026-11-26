"""Run with: python -B -m unittest discover -s tests -p test_history.py -v"""
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import http.client
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('solar_bridge', ROOT / 'bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'tests')
        self.addCleanup(self.temp.cleanup)
        self.old_dir = bridge.LOG_DIR
        bridge.LOG_DIR = self.temp.name
        self.addCleanup(setattr, bridge, 'LOG_DIR', self.old_dir)
        self.device_id = 'd_station123456'

    def write_csv(self, name, rows, columns=None):
        target = Path(self.temp.name) / name
        fields = columns or list(bridge.CSV_COLUMNS) + ['custom_value']
        with target.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return target

    def row(self, ts, **kwargs):
        return dict(timestamp_ms=str(ts), iso_time='2026-10-21T12:00:00+08:00',
                    device_id=self.device_id, device_name='舊站名', pv_power_w='100',
                    load_power_w='20', **kwargs)

    def get(self, **query):
        handler = object.__new__(bridge.ModbusBridgeHandler)
        captured = []
        handler._send_json = lambda data, status=200: captured.append((data, status))
        handler._handle_log_get({key: [str(value)] for key, value in query.items()})
        return captured[0]

    def download(self, **query):
        handler = object.__new__(bridge.ModbusBridgeHandler)
        handler.wfile = io.BytesIO()
        headers, statuses = {}, []
        handler.send_response = statuses.append
        handler.send_header = headers.__setitem__
        handler.end_headers = lambda: None
        handler.send_error = lambda code, message: statuses.append(code)
        handler._handle_log_download({key: [str(value)] for key, value in query.items()})
        return handler.wfile.getvalue(), headers, statuses

    def test_merge_renamed_files_full_id_sort_and_exact_duplicates(self):
        duplicate = self.row(2000, custom_value='preserve extra column')
        self.write_csv('舊站名_123456.csv', [duplicate, self.row(1000)])
        self.write_csv('改名_123456.csv', [self.row(3000), duplicate])
        # ID in the row is authoritative, even when filename has no matching suffix.
        self.write_csv('archive.csv', [self.row(4000)])
        collision = self.row(9000)
        collision['device_id'] = 'd_another123456'
        self.write_csv('碰巧同後綴_123456.csv', [collision])
        # A malformed row belonging to another device must not break this device's history.
        (Path(self.temp.name) / 'unrelated.csv').write_text(
            'device_id,timestamp_ms\nother,1,extra\n', encoding='utf-8')
        data, status = self.get(deviceId=self.device_id, deviceName='新站名', limit=50000)
        self.assertEqual(status, 200)
        self.assertEqual([row['timestamp_ms'] for row in data['rows']], [1000, 2000, 3000, 4000])
        self.assertEqual(data['total_rows'], 4)
        self.assertEqual(data['count'], 4)
        self.assertEqual(len(data['paths']), 3)
        self.assertEqual(data['rows'][1]['custom_value'], 'preserve extra column')

    def test_global_limit_applied_after_merging_and_since(self):
        self.write_csv('A_123456.csv', [self.row(5000), self.row(1000)])
        self.write_csv('B_123456.csv', [self.row(4000), self.row(2000), self.row(3000)])
        data, status = self.get(deviceId=self.device_id, limit=2, since=3000)
        self.assertEqual(status, 200)
        self.assertEqual([row['timestamp_ms'] for row in data['rows']], [4000, 5000])
        self.assertEqual(data['total_rows'], 5)
        self.assertEqual(data['returned_rows'], 2)

    def test_conflicting_readings_at_same_timestamp_are_not_discarded(self):
        a, b = self.row(1000), self.row(1000)
        b['pv_power_w'] = '150'
        self.write_csv('A_123456.csv', [a])
        self.write_csv('B_123456.csv', [b])
        data, _ = self.get(deviceId=self.device_id)
        self.assertEqual(len(data['rows']), 2)

    def test_legacy_missing_ids_and_name_only_queries(self):
        self.write_csv('舊站名_123456.csv', [dict(timestamp_ms='1000', device_name='舊站名')],
                       ['timestamp_ms', 'device_name'])
        data, _ = self.get(deviceId=self.device_id)
        self.assertEqual(data['total_rows'], 1)
        self.write_csv('anything.csv', [self.row(2000)])
        data, _ = self.get(deviceName='舊站名')
        self.assertEqual(data['total_rows'], 2)
        other, _ = self.get(deviceName='舊站')
        self.assertEqual(other['rows'], [])
        self.write_csv('無名稱_654321.csv', [dict(timestamp_ms='3000')], ['timestamp_ms'])
        legacy, _ = self.get(deviceName='無名稱')
        self.assertEqual(legacy['total_rows'], 1)

    def test_download_matches_merged_get_and_leaves_files_unchanged(self):
        self.write_csv('舊名_123456.csv', [self.row(1000), self.row(3000)])
        self.write_csv('新名_123456.csv', [self.row(2000), self.row(3000)])
        def hashes():
            return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in Path(self.temp.name).glob('*.csv')}
        before = hashes()
        data, _ = self.get(deviceId=self.device_id, limit=50000)
        body, headers, statuses = self.download(deviceId=self.device_id, deviceName='新名')
        self.assertEqual(statuses, [200])
        self.assertEqual(int(headers['Content-Length']), len(body))
        downloaded = list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
        self.assertEqual([int(row['timestamp_ms']) for row in downloaded],
                         [row['timestamp_ms'] for row in data['rows']])
        self.assertEqual(len(downloaded), 3)
        self.assertEqual(before, hashes())

    def test_http_read_and_download_routes(self):
        self.write_csv('A_123456.csv', [self.row(1000), self.row(3000)])
        self.write_csv('B_123456.csv', [self.row(2000)])

        class MemorySocket:
            def __init__(self, request):
                self.request, self.sent = request, bytearray()

            def makefile(self, *args):
                return io.BytesIO(self.request)

            def sendall(self, data):
                self.sent.extend(data)

        def request(url):
            raw = f'GET {url} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'.encode()
            connection = MemorySocket(raw)
            server = SimpleNamespace(server_name='localhost', server_port=8080)
            bridge.ModbusBridgeHandler(connection, ('127.0.0.1', 12345), server)
            response = http.client.HTTPResponse(MemorySocket(bytes(connection.sent)))
            response.begin()
            self.assertEqual(response.status, 200)
            return response

        response = request(f'/api/log_get?deviceId={self.device_id}&limit=1')
        data = json.loads(response.read())
        self.assertEqual(data['total_rows'], 3)
        self.assertEqual(data['rows'][0]['timestamp_ms'], 3000)
        response = request(f'/api/log_download?deviceId={self.device_id}')
        rows = list(csv.DictReader(io.StringIO(response.read().decode('utf-8-sig'))))
        self.assertIn('attachment;', response.headers['Content-Disposition'])
        self.assertEqual([int(row['timestamp_ms']) for row in rows], [1000, 2000, 3000])

    def test_missing_device_and_invalid_limits(self):
        data, status = self.get(deviceId='does_not_exist')
        self.assertEqual((data['rows'], data['total_rows'], status), ([], 0, 200))
        _, _, statuses = self.download(deviceId='does_not_exist')
        self.assertEqual(statuses, [404])
        for query in ({'limit': 0}, {'limit': -1}, {'limit': 'bad'}, {'since': 'nan'}):
            data, status = self.get(deviceId=self.device_id, **query)
            self.assertFalse(data['success'])
            self.assertEqual(status, 400)


if __name__ == '__main__':
    unittest.main()
