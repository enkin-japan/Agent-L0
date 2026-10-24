"""Probe the compute services' unauthenticated health endpoints on loopback."""
import json
import urllib.request


def main(params):
    result = {}
    for name, port in params['ports'].items():
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=10) as response:
                result[name] = bool(json.load(response))
        except Exception:
            result[name] = False
    print('COMPUTE_HEALTH ' + json.dumps(result))
