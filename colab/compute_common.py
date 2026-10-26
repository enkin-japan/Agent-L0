"""Facts shared by the local launcher, the VM helper and the VM tasks.

Standard library only: this file runs under the Mac launcher venv, under the
VM's system python3 and under the VM's CUDA venv. Every rule that more than one
side must agree on lives here exactly once -- secret names, the private
`.env` grammar, redaction, VM layout, ports and the GPU envelope.
"""
import re
import shlex

# ---- VM layout -------------------------------------------------------------
# A lease records the layout it was created with. Leases written before the
# 2026-10-24 rename carry no `vm_layout` and live under the old demo paths; the
# LEGACY_* names exist only so those VMs stay manageable (stop/switch/logs).
# Delete them once `status` shows no lease without `vm_layout`.
SESSION_PREFIX = 'agent-compute-'
LEGACY_SESSION_PREFIX = 'agent-demo-'
SESSION_RE = re.compile(r'agent-(?:compute|demo)-[0-9a-f]{12}')

LAYOUT = {
    'private': '/content/agent-compute-private',
    'app': '/content/agent-compute',
    'helper': 'compute_vm.py',
    'tailscale': '/content/agent-compute-private/bin/tailscale',
}
LEGACY_LAYOUT = {
    'private': '/content/agent-demo-private',
    'app': '/content/agent-demo',
    'helper': 'demo_vm.py',
    'tailscale': 'tailscale',
}


def layout_of(lease):
    """The VM paths a lease was deployed with (legacy when it predates the field)."""
    return dict((lease or {}).get('vm_layout') or LEGACY_LAYOUT)


def is_owned_session(name):
    return bool(SESSION_RE.fullmatch(name or ''))


# ---- Compute services ------------------------------------------------------
INFER_PORT = 14161
RAG_PORT = 14162
INFER_SERVICE = 'macagent-inference'
GPU_NAME = 'A100'
GPU_MEMORY_MIB = (38000, 49152)  # A100 40GB; an 80GB allocation is refused, not used

# ---- Secrets ---------------------------------------------------------------
# The only values the launcher transfers to a VM.
TRANSFER_SECRETS = frozenset({'INFER_API_KEY', 'RAG_COMPUTE_API_KEY', 'HF_TOKEN'})
REQUIRED_SECRETS = ('INFER_API_KEY', 'RAG_COMPUTE_API_KEY')
MIN_SECRET_LENGTH = 16
_NAME_RE = re.compile(r'[A-Z][A-Z0-9_]*')


def is_secret_name(name):
    """Redaction scope: transferred secrets plus any *_KEY / *_TOKEN / TS_AUTHKEY."""
    return name in TRANSFER_SECRETS or name == 'TS_AUTHKEY' or name.endswith(('_KEY', '_TOKEN'))


def parse_env(text, names=None):
    """Parse literal NAME=value assignments; never execute shell input.

    names=None (a file this tool wrote): every non-comment line must be a valid
    single-literal assignment. With names: only those names are parsed and
    validated; any other line of a user-maintained file is ignored unread.
    """
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        name, separator, raw = line.removeprefix('export ').partition('=')
        name = name.strip()
        if names is not None and name not in names:
            continue
        if not separator or not _NAME_RE.fullmatch(name):
            raise ValueError('私密环境文件含无法识别的赋值行')
        values[name] = _literal(name, raw)
    return values


_UNQUOTED_METACHARS = ';&|<>()`'


def _strip_comment(name, raw):
    # Same reading as bash `source`: an unquoted '#' begins a comment only at the
    # start of a word after unescaped whitespace (`A=#foo`, `A='#foo'` and `A=x\ #c`
    # keep their '#'). Unquoted shell metacharacters would mean something other than
    # a literal to bash, so they are rejected instead of being read as text.
    quote = None
    escaped = False
    word_start = True
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
            word_start = False
        elif quote:
            if char == quote:
                quote = None
            elif char == '\\' and quote == '"':
                escaped = True
        elif char == '\\':
            escaped = True
        elif char in '\'"':
            quote = char
            word_start = False
        elif char in ' \t':
            word_start = True
        elif char == '#' and word_start and index > 0:
            return raw[:index]
        elif char in _UNQUOTED_METACHARS:
            raise ValueError(f'{name} 含未加引号的 shell 元字符，必须是单个字面值')
        else:
            word_start = False
    return raw


def _literal(name, raw):
    parts = shlex.split(_strip_comment(name, raw))
    if len(parts) > 1:
        raise ValueError(f'{name} 必须是单个字面值')
    return parts[0] if parts else ''


def format_env(values):
    return ''.join(f'{name}={shlex.quote(value)}\n' for name, value in values.items())


def secret_values(values):
    return [value for name, value in values.items() if value and is_secret_name(name)]


_TOKEN_RE = re.compile(r'(?:tskey-[A-Za-z0-9_-]+|hf_[A-Za-z0-9]+|Bearer\s+\S+)')
_QUERY_RE = re.compile(r'''(https?://[^\s?"'<>]+)\?[^\s"'<>]+''')


def redact(text, secrets):
    for value in secrets:
        if value:
            text = text.replace(value, '[REDACTED]')
    text = _TOKEN_RE.sub('[REDACTED]', text)
    return _QUERY_RE.sub(r'\1?[REDACTED_QUERY]', text)
