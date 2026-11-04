"""Optional read-only UniFi maintenance context; never controls the fan."""
import re


def make_client_type(protect_base, protect_error):
    """Reuse the controller's bounded Protect transport without importing it."""
    class _UniFiUpdateClient(protect_base):
        def protect_history(self, since, until, page=0):
            def read():
                with self._lock:
                    if self._s is None:
                        self._login_direct()
                    headers = {'Accept': 'application/json'}
                    if self._csrf:
                        headers['X-CSRF-Token'] = self._csrf
                    response = self._s.get(
                        self.base+'/proxy/protect/api/events', headers=headers,
                        timeout=15, allow_redirects=False,
                        params={'start': int((since-3600)*1000), 'end': int(until*1000),
                                'types[]': ['applicationUpdate', 'fwUpdate', 'update'],
                                'orderDirection': 'desc', 'limit': 100, 'offset': page*100})
                if response.status_code != 200:
                    raise protect_error('Protect update history unavailable')
                data = response.json()
                if not isinstance(data, list):
                    raise protect_error('Invalid Protect update history')
                return data
            return self._request(read)

        def update_history(self, since, until, page=0):
            def read():
                with self._lock:
                    if self._s is None:
                        self._login_direct()
                    headers = {'Accept': 'application/json'}
                    if self._csrf:
                        headers['X-CSRF-Token'] = self._csrf
                    response = self._s.post(
                        self.base+'/proxy/network/v2/api/site/default/system-log/all',
                        headers=headers, timeout=15, allow_redirects=False,
                        json={'timestampFrom': int(since*1000), 'timestampTo': int(until*1000),
                              'categories': ['SOFTWARE_UPDATES'], 'pageNumber': page, 'pageSize': 100})
                if response.status_code != 200:
                    raise protect_error('Update history unavailable')
                data = response.json()
                if not isinstance(data, dict):
                    raise protect_error('Invalid update history')
                return data
            return self._request(read)

        def _login_direct(self):
            super()._login_direct()
            session = self._s
            def expire(response, *args, **kwargs):
                # A session expiry needs a fresh login on the next read. A 403
                # permission denial does not: optional views must not churn logins.
                if response.status_code == 401 and self._s is session:
                    self._s = None
            session.hooks['response'].append(expire)
    return _UniFiUpdateClient


class UniFiObserver:
    """Read a baseline once, then check update evidence only after recovery."""

    def __init__(self, client, sensor_id, *, clock, finite, update_code, restart_code):
        self._clock = clock
        self._finite = finite
        self._update_code = update_code
        self._restart_code = restart_code
        self.client = client
        self.sensor_id = sensor_id
        self.versions = {}
        self.seen = set()
        self.uptimes = {}
        self.boot_times = {}

    def _observe(self, key, label, version):
        if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9._+\-]{1,64}', version):
            return []
        previous = self.versions.get(key)
        self.versions[key] = version
        if previous is not None and version != previous:
            return ['%s %s updated' % (self._update_code, label)]
        return []

    def _near_recovery(self, occurred, since, until):
        # A bad wall-clock window must not discard independently detected
        # changes. Valid windows require evidence dated to this interruption.
        if (isinstance(since, bool) or isinstance(until, bool)
                or not self._finite(since) or not self._finite(until) or until < since):
            return True
        return max(since-120, until-86400) <= occurred <= until+120

    def _uptime(self, key, label, value, since=None, until=None):
        if isinstance(value, bool) or not self._finite(value) or not 0 <= value <= 10*366*86400:
            return []
        now = self._clock.monotonic()
        previous = self.uptimes.get(key)
        self.uptimes[key] = (value, now)
        if previous is not None:
            elapsed = max(0, now-previous[1])
            if (value+max(10, elapsed*0.001) < previous[0]+elapsed
                    and self._near_recovery(self._clock.time()-value, since, until)):
                return ['%s %s restarted' % (self._restart_code, label)]
        return []

    def _boot_time(self, key, label, value, since=None, until=None):
        # Protect's upSince is a millisecond process-start timestamp. A
        # reconnect/connectedSince change is not a device or application reboot.
        if isinstance(value, bool) or not self._finite(value) or not 0 < value < 1e15:
            return []
        previous = self.boot_times.get(key)
        self.boot_times[key] = value
        if (previous is not None and value > previous+10000
                and self._near_recovery(value/1000, since, until)):
            return ['%s %s restarted' % (self._restart_code, label)]
        return []

    def check(self, since=None, until=None):
        messages = []
        version_changes = []
        kinds = {}
        try:
            rows = self.client._get('/api/apps', retry=False).get('controllers')
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get('name') in ('protect', 'network'):
                        name = row['name']
                        version_changes += self._observe(('app', name), 'UniFi '+name.capitalize(), row.get('version'))
        except Exception:
            pass
        try:
            data = self.client._get('/api/system', retry=False)
            hardware = data.get('hardware')
            if isinstance(hardware, dict):
                version_changes += self._observe('os', 'UniFi OS', hardware.get('firmwareVersion'))
            messages += self._uptime('os', 'UniFi OS', data.get('uptime'), since, until)
            if isinstance(data.get('mac'), str):
                kinds[data['mac']] = 'UniFi OS'
        except Exception:
            pass
        try:
            bootstrap = self.client._get('/proxy/protect/api/bootstrap', retry=False)
            nvr = bootstrap.get('nvr')
            if isinstance(nvr, dict):
                messages += self._boot_time('protect', 'UniFi Protect', nvr.get('upSince'), since, until)
            rows = bootstrap.get('sensors')
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get('id') == self.sensor_id:
                        version_changes += self._observe('sensor', 'Air Sensor', row.get('firmwareVersion'))
                        if row.get('isConnected') is True:
                            messages += self._uptime('sensor', 'Air Sensor', row.get('uptime'), since, until)
        except Exception:
            pass
        try:
            rows = self.client._get('/proxy/network/api/s/default/stat/device', retry=False).get('data')
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and isinstance(row.get('mac'), str):
                        label = {'uap': 'UniFi AP', 'usw': 'UniFi switch', 'udm': 'UniFi OS'}.get(row.get('type'))
                        if label:
                            kinds[row['mac']] = label
                            if (row.get('type') != 'udm' and type(row.get('state')) is int
                                    and row['state'] == 1):
                                messages += self._uptime(('device', row['mac']), label, row.get('uptime'), since, until)
        except Exception:
            pass
        if since is None or until is None:
            return []  # Startup is a silent baseline, not an event.
        if (isinstance(since, bool) or isinstance(until, bool)
                or not self._finite(since) or not self._finite(until) or until < since):
            # Version/uptime evidence is independent of the wall-clock window.
            return list(dict.fromkeys(version_changes+messages))
        try:
            # Exact timestamp range, limited pages, and update-category records
            # only. This POST is a read-only history query, never a command.
            low = max(since-120, until-86400)
            for page in range(3):
                data = self.client.update_history(low, until+120, page)
                rows = data.get('data')
                if not isinstance(rows, list):
                    break
                for row in rows:
                    if not isinstance(row, dict) or row.get('category') != 'SOFTWARE_UPDATES':
                        continue
                    stamp = row.get('timestamp')
                    if (isinstance(stamp, bool) or not self._finite(stamp)
                            or not low*1000 <= stamp <= (until+120)*1000):
                        continue
                    parameters = row.get('parameters')
                    if not isinstance(parameters, dict):
                        continue
                    device = parameters.get('DEVICE', {})
                    if not isinstance(device, dict):
                        continue
                    device_id = device.get('id', '')
                    if not isinstance(device_id, str) or len(device_id) > 128:
                        continue
                    event = row.get('event')
                    label = {'NETWORK_UPDATED': 'UniFi Network', 'PROTECT_UPDATED': 'UniFi Protect'}.get(event) if isinstance(event, str) else None
                    if event == 'FIRMWARE_UPDATED':
                        label = kinds.get(device_id)
                    if label is None:
                        continue
                    signature = (float(stamp), device_id, event)
                    if signature not in self.seen:
                        self.seen.add(signature)
                        messages.append('%s %s updated' % (self._update_code, label))
                pages = data.get('total_page_count', 1)
                if not isinstance(pages, int) or page+1 >= pages:
                    break
            self.seen = set(sorted(self.seen)[-300:])
        except Exception:
            pass  # Missing permissions/history never alters control or errors.
        try:
            low = max(since-120, until-86400)
            for page in range(3):
                rows = self.client.protect_history(low, until+120, page)
                if not isinstance(rows, list):
                    break
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    start, end = row.get('start'), row.get('end')
                    if (isinstance(start, bool) or isinstance(end, bool)
                            or not self._finite(start) or not self._finite(end) or end < start
                            or not low*1000 <= end <= (until+120)*1000):
                        continue
                    metadata = row.get('metadata')
                    if not isinstance(metadata, dict):
                        continue
                    kind = row.get('type')
                    label = None
                    device_id = ''
                    if kind == 'fwUpdate':
                        label = 'UniFi OS'
                    elif kind == 'applicationUpdate':
                        description = row.get('description')
                        event = description.get('eventMetadata') if isinstance(description, dict) else None
                        if isinstance(event, dict) and event.get('title') == 'Protect Updated':
                            label = 'UniFi Protect'
                    elif kind == 'update' and metadata.get('deviceModelKey') == 'sensor':
                        device = metadata.get('deviceId')
                        if isinstance(device, dict) and device.get('text') == self.sensor_id:
                            label = 'Air Sensor'
                            device_id = self.sensor_id
                    if label is None:
                        continue
                    signature = (float(end), device_id, 'protect-'+kind)
                    if signature not in self.seen:
                        self.seen.add(signature)
                        messages.append('%s %s updated' % (self._update_code, label))
                if len(rows) < 100:
                    break
        except Exception:
            pass
        self.seen = set(sorted(self.seen)[-300:])
        return list(dict.fromkeys(messages))
