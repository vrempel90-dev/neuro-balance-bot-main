"""Parse and execute workflow behavior against synthetic HTTP only."""
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def test_all_workflow_shell_blocks_are_parseable():
    bash = shutil.which('bash') or r'C:\Program Files\Git\bin\bash.exe'
    for path in (ROOT / '.github/workflows').glob('*.yml'):
        workflow = yaml.safe_load(path.read_text(encoding='utf-8'))
        for job in workflow['jobs'].values():
            for step in job.get('steps', []):
                if 'run' in step:
                    result = subprocess.run([bash, '-n'], input=step['run'], text=True, capture_output=True)
                    assert result.returncode == 0, f'{path.name}/{step["name"]}: {result.stderr}'


def test_source_push_does_not_assert_an_unauthorized_deployment(tmp_path, monkeypatch):
    workflow = yaml.safe_load((ROOT / '.github/workflows/production-monitor.yml').read_text(encoding='utf-8'))
    step = workflow['jobs']['monitor']['steps'][0]
    # Deployment SHA verification must come from an explicit operator input.
    assert 'github.sha' not in step['env']['EXPECTED_RELEASE']
    code = re.search(r"python - <<'PY'\n(.*)\nPY", step['run'], re.S)[1]
    monkeypatch.chdir(tmp_path)
    for name, value in {'NEUROBALANCE_URL': 'https://synthetic.invalid', 'EXPECTED_RELEASE': '',
                        'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://oidc.invalid/?x=1',
                        'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic-token', 'RUN_DEPENDENCY_PROBES': 'false',
                        'GITHUB_STEP_SUMMARY': str(tmp_path / 'summary.md')}.items():
        monkeypatch.setenv(name, value)
    class Response(io.BytesIO):
        status = 200
    def response(request, **kwargs):
        value = {'value': 'synthetic'} if 'oidc.invalid' in request.full_url else {
            'ok': True, 'release_sha': 'previous-authorized-release', 'config': {'new_leads_only': True}}
        return Response(json.dumps(value).encode())
    monkeypatch.setattr(urllib.request, 'urlopen', response)
    exec(compile(code, 'monitor', 'exec'), {})
    assert json.loads((tmp_path / 'monitor-results/result.json').read_text())['failures'] == []
