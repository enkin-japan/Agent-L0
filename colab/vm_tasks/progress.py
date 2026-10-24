"""Report the bootstrap progress file (or a starting placeholder) as one line."""
import json
from pathlib import Path


def main(params):
    path = Path(params['private']) / params.get('file', 'progress.json')
    placeholder = json.dumps(params.get('placeholder', {'phase': 'starting'}))
    print(params['marker'] + ' ' + (path.read_text() if path.exists() else placeholder))
