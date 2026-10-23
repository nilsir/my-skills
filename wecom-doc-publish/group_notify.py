"""Small, secret-safe WeCom group webhook boundary. No automatic retries."""
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_webhook(value):
    try:
        if not isinstance(value, str) or value != value.strip() or any(c.isspace() for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        if (parsed.scheme != 'https' or parsed.netloc != 'qyapi.weixin.qq.com'
                or parsed.path != '/cgi-bin/webhook/send' or parsed.fragment
                or '#' in value or len(query) != 1 or query[0][0] != 'key' or not query[0][1].strip()):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Invalid group webhook configuration') from None
    return value


def response_status(response):
    if isinstance(response, dict) and type(response.get('errcode')) is int:
        return 'sent' if response['errcode'] == 0 else 'rejected'
    return 'unknown'


def send(url, payload):
    validate_webhook(url)
    content = payload.get('markdown', {}).get('content')
    if not isinstance(content, str) or len(content.encode('utf-8')) > 4096:
        raise ValueError('Invalid group notification size')
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    # One request only; HTTPError/invalid JSON are deliberately uncertain.
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            return json.loads(response.read(65537))
    except Exception:
        raise ValueError('Group notification outcome is unknown') from None


def private_path(settings):
    value = settings.get('webhook_file')
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError('Private group webhook file is not configured')
    path = Path(value)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Invalid private group webhook file')
    return path


def read_webhook(settings):
    try:
        variable = settings.get('webhook_key_env')
        if variable is not None:
            if not isinstance(variable, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', variable):
                raise ValueError()
            # A present but invalid value fails closed; never send to an old
            # robot by silently falling back after a configuration mistake.
            if variable in os.environ:
                key = os.environ[variable]
                if not re.fullmatch(r'[A-Za-z0-9_-]+', key):
                    raise ValueError()
                return validate_webhook('https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=' + key)
        # Compatibility for existing installations. New templates need only
        # the variable name and never contain the secret or a full webhook.
        path = private_path(settings)
        if path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
            raise ValueError()
        return validate_webhook(path.read_text(encoding='utf-8').strip())
    except (OSError, ValueError, UnicodeError):
        raise ValueError('Group webhook is not configured or invalid') from None


def save_webhook(settings, value):
    validate_webhook(value)
    path = private_path(settings)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix='.webhook-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        raise ValueError('Cannot save private group webhook file') from None


def payload(projects, title, first, body_url, requirement_url, tapd, permissions_pending=False):
    def label(value, limit=200):
        # Prevent mentions, injected Markdown links and line breaks from metadata.
        value = str(value).replace('@', '＠')
        return re.sub(r'[\[\]<>`*\r\n]', ' ', value)[:limit]
    def link(name, value):
        # Reserve a bounded budget for each link, even for unusual TAPD inputs.
        if not isinstance(value, str) or len(value.encode('utf-8')) > 850 or re.search(r'[\s<>)]', value):
            return name + '：链接不可用，请查看发布结果'
        return '[' + name + '](' + value.replace('@', '%40') + ')'
    content = '**' + ('首次发布' if first else '已更新') + '** · ' + label(title) + '\n'
    content += '项目：' + '、'.join(label(p, 20) for p in projects) + '\n\n'
    content += '\n'.join((link('正文文档', body_url), link('需求目录', requirement_url), link('TAPD 需求', tapd)))
    if permissions_pending:
        content += '\n\n权限待同步，部分成员可能暂时无法访问。'
    return {'msgtype': 'markdown', 'markdown': {'content': content}}
