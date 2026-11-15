"""Execute the actual monitor script with synthetic HTTP, never the providers."""
import io
import json
from pathlib import Path
import re
import urllib.request

import yaml


def test_monitoring_never_migrates_provider_configuration(tmp_path, monkeypatch):
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/production-monitor.yml").read_text(encoding="utf-8"))
    script = next(step["run"] for step in workflow["jobs"]["monitor"]["steps"] if "run" in step)
    code = re.search(r"python - <<'PY'\n(.*)\nPY", script, re.S)[1]
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "NEUROBALANCE_URL": "https://synthetic.invalid", "EXPECTED_RELEASE": "",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.invalid/?request=synthetic",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-token", "RUN_DEPENDENCY_PROBES": "true",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }.items():
        monkeypatch.setenv(key, value)
    requests = []
    class Response(io.BytesIO):
        status = 200
    def urlopen(request, **kwargs):
        requests.append((request.get_method(), request.full_url))
        value = {"value": "synthetic-oidc"} if "oidc.invalid" in request.full_url else {"ok": True, "config": {"new_leads_only": True}}
        return Response(json.dumps(value).encode())
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    exec(compile(code, "production-monitor-workflow", "exec"), {})
    result = json.loads((tmp_path / "monitor-results/result.json").read_text())
    assert result["failures"] == []
    assert any(url.endswith("/internal/monitor/status") for method, url in requests)
    assert any(url.endswith("/internal/monitor/probe") for method, url in requests)
    assert not any("secure-webhook" in url for method, url in requests), "routine monitoring must not mutate Wazzup configuration"
