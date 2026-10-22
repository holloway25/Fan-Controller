"""Persist exact tested platform digests across partial workflow reruns."""
import argparse
import json
import os
from pathlib import Path
import re

ARCHES = ('amd64', 'arm64')


def record(image, revision, channel, arch, digest, run, attempt):
    if (arch not in ARCHES or channel not in ('candidate','maintenance')
            or not re.fullmatch(r'[a-z0-9][a-z0-9._/-]*', image)
            or not re.fullmatch(r'[0-9a-f]{40}', revision)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest)
            or type(run) is not int or run <= 0
            or type(attempt) is not int or attempt <= 0):
        raise ValueError('Invalid tested platform artifact')
    return dict(version=1, image=image, revision=revision, channel=channel,
                arch=arch, digest=digest, run=run, attempt=attempt)


def references(folder, image, revision, channel, run, attempt):
    refs = []
    for arch in ARCHES:
        row = json.loads((Path(folder)/('platform-'+arch+'.json')).read_text())
        checked = record(row['image'],row['revision'],row['channel'],row['arch'],
                         row['digest'],row['run'],row['attempt'])
        if (row != checked or type(row['version']) is not int
                or row['version'] != 1 or row['image'] != image
                or row['revision'] != revision or row['channel'] != channel
                or row['arch'] != arch or row['run'] != run
                or row['attempt'] > attempt):
            raise ValueError('Artifact does not belong to this build/platform')
        refs.append(image+'@'+row['digest'])
    if len(set(refs)) != len(ARCHES):
        raise ValueError('Platforms must have distinct native image digests')
    return refs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=('record','references'))
    parser.add_argument('--image',required=True)
    parser.add_argument('--revision',required=True)
    parser.add_argument('--channel',choices=('candidate','maintenance'),required=True)
    parser.add_argument('--arch',choices=ARCHES)
    parser.add_argument('--digest')
    parser.add_argument('--path',required=True)
    args = parser.parse_args()
    run, attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    if args.action == 'record':
        row = record(args.image,args.revision,args.channel,args.arch,args.digest,run,attempt)
        Path(args.path).write_text(json.dumps(row,sort_keys=True)+'\n')
    else:
        print('\n'.join(references(args.path,args.image,args.revision,args.channel,run,attempt)))


if __name__ == '__main__':
    main()
