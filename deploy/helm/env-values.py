"""Generate Helm JSON values from production .env files; never read .env.local."""
import argparse
import base64
import json
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

SERVICES = ("orchestrator", "guardrails", "extraction", "structuring", "scoring")
UPSTREAMS = {
    "orchestrator": SERVICES[1:],
    "extraction": ("structuring",),
    "structuring": ("scoring",),
}


def generate(root, release):
    result = {"services": {}}
    for name in SERVICES:
        path = root / "services" / name / ".env"
        if not path.is_file():
            raise ValueError(f"Missing {path}")
        env = dict(dotenv_values(path, interpolate=False))
        if any(value is None for value in env.values()):
            raise ValueError(f"{path}: every variable must have an assignment")
        if env.get("ENVIRONMENT") != "production":
            raise ValueError(f"{path}: ENVIRONMENT must be production")
        if not env.get("API_KEY"):
            raise ValueError(f"{path}: API_KEY is required")
        # A local address means the next process on a laptop. In GKE each service
        # has its own pod, so use the chart's component Service instead.
        for upstream in UPSTREAMS.get(name, ()):
            key = f"{upstream.upper()}_SERVICE_URL"
            if not env.get(key) or urlsplit(env[key]).hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
                env[key] = f"http://{release}-{upstream}:{8040 + SERVICES.index(upstream)}"
        result["services"][name] = {
            "dotenv": {
                "enabled": True,
                "data": {key: base64.b64encode(value.encode()).decode() for key, value in env.items()},
            }
        }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default="nlm-k2")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    values = generate(Path(__file__).resolve().parents[2], args.release)
    output = Path(args.output)
    # Generated values contain secrets; use a private temporary file.
    with output.open("w", encoding="utf-8") as handle:
        output.chmod(0o600)
        json.dump(values, handle)
