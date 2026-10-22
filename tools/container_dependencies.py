"""Resolve patch-only runtime updates without editing the approved source pins."""
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
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==(\d+)\.(\d+)\.(\d+)', line.strip())
        if not match:
            raise ValueError('Runtime dependency must have an exact three-part release pin')
        name, major, minor, patch = match.groups()
        key = re.sub(r'[-_.]+','-',name).lower()
        if key in result:
            raise ValueError('Duplicate dependency')
        result[key] = (name, (int(major), int(minor), int(patch)))
    if not result:
        raise ValueError('No dependencies')
    return result


def resolved(source, report):
    selected = {}
    for item in report['install']:
        meta = item['metadata']
        key = re.sub(r'[-_.]+','-',meta['name']).lower()
        match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)',meta['version'])
        if key not in source or not match:
            raise ValueError('New dependencies or prereleases require a candidate review')
        version = tuple(map(int,match.groups()))
        old = source[key][1]
        if version[:2] != old[:2] or version < old or key in selected:
            raise ValueError('Only non-downgrading patch updates are permitted')
        selected[key] = source[key][0]+'=='+meta['version']
    if set(selected) != set(source):
        raise ValueError('Resolution omitted a pinned runtime dependency')
    return '\n'.join(selected[k] for k in sorted(selected))+'\n'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',default='requirements.txt')
    parser.add_argument('--output',default='container/requirements.lock')
    args=parser.parse_args()
    source=pins(Path(args.source).read_text())
    with tempfile.TemporaryDirectory() as folder:
        requirements=Path(folder)/'bounds.txt'
        report=Path(folder)/'report.json'
        requirements.write_text('\n'.join('%s>=%d.%d.%d,<%d.%d.0' %
            (name,*v,v[0],v[1]+1) for name,v in source.values()))
        subprocess.run([sys.executable,'-m','pip','install','--dry-run','--ignore-installed',
                        '--report',str(report),'-r',str(requirements)],check=True)
        value=resolved(source,json.loads(report.read_text()))
    Path(args.output).write_text(value)
    print('Resolved patch-only runtime lock; approved source requirements unchanged.')


if __name__=='__main__':
    main()
