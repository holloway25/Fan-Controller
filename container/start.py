"""One controller process; never copy live settings into the image."""
import json
import os
from pathlib import Path
import sys

data = Path(os.environ.get('FAN_CONTROLLER_DATA_DIR', '/data'))
if not data.is_dir() or not os.access(data, os.W_OK):
    raise SystemExit('Data directory must exist and be writable by uid/gid 10001.')
config = data/'config.json'
if not config.exists():
    example = json.loads(Path('/app/config.example.json').read_text())
    assert example['control']['mode'] == 'manual'
    with config.open('x') as stream:
        os.chmod(config, 0o600)
        json.dump(example, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
os.umask(0o077)
os.execv(sys.executable, [sys.executable, '/app/controller.py'])
