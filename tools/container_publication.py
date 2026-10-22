"""Reserve monotonically ordered latest publications under the workflow lock.

Both maintenance and approval must hold the container-publication concurrency
group. Reserve BEFORE changing latest: a failed publication may be retried in
a newer workflow attempt, but never permits an older build to move it backwards.
This metadata is separate from the tested image, which is not rebuilt/modified.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

REF = 'tags/container-publication'


class GitHub:
    def __init__(self, repository, token):
        self.root = 'https://api.github.com/repos/'+repository+'/'
        self.token = token

    def __call__(self, method, path, body=None):
        request = urllib.request.Request(self.root+path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization':'Bearer '+self.token,
                     'Accept':'application/vnd.github+json',
                     'X-GitHub-Api-Version':'2022-11-28',
                     'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if method == 'GET' and exc.code == 404:
                return None
            raise


def reserve(api, revision, digest, kind, run_id, attempt, anchor=None):
    # This tag is an ordering record, NOT the approved source pointer. Anchor
    # its Git object to the workflow commit the built-in token can reference;
    # keep the actual approved source identity separately in the JSON record.
    anchor = revision if anchor is None else anchor
    if (not re.fullmatch(r'[0-9a-f]{40}', revision)
            or not re.fullmatch(r'[0-9a-f]{40}', anchor)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest)
            or kind not in ('maintenance','approval')
            or type(run_id) is not int or run_id <= 0
            or type(attempt) is not int or attempt <= 0):
        raise ValueError('Invalid publication identity')
    if kind == 'maintenance':
        approved = api('GET', 'git/ref/tags/container-approved')
        if not approved or approved['object']['sha'] != revision:
            raise ValueError('Approved controller revision changed during this build')
    ref = api('GET', 'git/ref/'+REF)
    if ref is not None:
        if ref['object']['type'] != 'tag':
            raise ValueError('Publication metadata must be an annotated tag')
        tag = api('GET', 'git/tags/'+ref['object']['sha'])
        old = json.loads(tag['message'])
        if (old.get('version') != 1 or type(old.get('run')) is not int
                or type(old.get('attempt')) is not int
                or old['run'] <= 0 or old['attempt'] <= 0):
            raise ValueError('Invalid previous publication order')
        if (run_id, attempt) <= (old['run'], old['attempt']):
            return False
    record = dict(version=1, run=run_id, attempt=attempt, revision=revision,
                  digest=digest, kind=kind)
    tag = api('POST', 'git/tags', dict(tag='container-publication',
              message=json.dumps(record, sort_keys=True), object=anchor, type='commit'))
    if ref is None:
        api('POST', 'git/refs', dict(ref='refs/'+REF, sha=tag['sha']))
    else:
        api('PATCH', 'git/refs/'+REF, dict(sha=tag['sha'], force=True))
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    parser.add_argument('--digest', required=True)
    parser.add_argument('--kind', choices=('maintenance','approval'), required=True)
    args = parser.parse_args()
    api = GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN'])
    allowed = reserve(api, args.revision, args.digest, args.kind,
                      int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT']),
                      anchor=os.environ.get('GITHUB_SHA'))
    print('true' if allowed else 'false')
    if not allowed:
        print('Older or repeated publication skipped; latest is unchanged.', file=sys.stderr)


if __name__ == '__main__':
    main()
