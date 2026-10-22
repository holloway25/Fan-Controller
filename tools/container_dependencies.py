"""Resolve tested dependency updates into an exact per-image lock.

Allow stable minor/patch releases within a dependency's major version.
Certifi uses calendar versions: allow newer dates, not just the same month.
Python's minor version remains controlled separately by the Docker base tag.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def pins(text):
    result = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==(\d+)\.(\d+)(?:\.(\d+))?', line.strip())
        if not match:
            raise ValueError('Runtime dependency must have an exact stable release pin')
        name, major, minor, patch = match.groups()
        key = re.sub(r'[-_.]+','-',name).lower()
        if key in result:
            raise ValueError('Duplicate dependency')
        result[key] = (name, (int(major), int(minor), int(patch or 0)))
    if not result:
        raise ValueError('No dependencies')
    return result


def resolved(source, report):
    selected = {}
    for item in report['install']:
        meta = item['metadata']
        key = re.sub(r'[-_.]+','-',meta['name']).lower()
        match = re.fullmatch(r'(\d+)\.(\d+)(?:\.(\d+))?',meta['version'])
        if key not in source or not match:
            raise ValueError('New dependencies or prereleases require a candidate review')
        version = tuple(int(part or 0) for part in match.groups())
        old = source[key][1]
        if ((key != 'certifi' and version[0] != old[0])
                or version < old or key in selected):
            raise ValueError('Downgrades or dependency major upgrades require review')
        selected[key] = source[key][0]+'=='+meta['version']
    if set(selected) != set(source):
        raise ValueError('Resolution omitted a pinned runtime dependency')
    return '\n'.join(selected[k] for k in sorted(selected))+'\n'


def bounds(source):
    return '\n'.join(
        '%s>=%d.%d.%d%s' % (name, *v,
            '' if key == 'certifi' else ',<%d.0.0' % (v[0]+1))
        for key, (name, v) in source.items())


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',default='requirements.txt')
    parser.add_argument('--output',default='container/requirements.lock')
    args=parser.parse_args()
    source=pins(Path(args.source).read_text())
    with tempfile.TemporaryDirectory() as folder:
        requirements=Path(folder)/'bounds.txt'
        report=Path(folder)/'report.json'
        requirements.write_text(bounds(source))
        subprocess.run([sys.executable,'-m','pip','install','--dry-run','--ignore-installed',
                        '--report',str(report),'-r',str(requirements)],check=True)
        value=resolved(source,json.loads(report.read_text()))
    Path(args.output).write_text(value)
    print('Resolved tested-update runtime lock; approved source requirements unchanged.')


if __name__=='__main__':
    main()
