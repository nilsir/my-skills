#!/usr/bin/env python3
"""Local-state WeCom Markdown publisher; remote mutations require --execute."""
import argparse
import contextlib
import fcntl
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
import warnings

import group_notify
from pathlib import Path
from urllib.parse import unquote, urlparse


class PublisherError(Exception):
    pass


class CliError(PublisherError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def sanitize(value):
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()
                if key not in ('security_notice', 'extra_identity_context')}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def decoded(value):
    for _ in range(8):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            break
    return value


def find_values(value, key):
    if isinstance(value, dict):
        if key in value:
            yield value[key]
        for name, item in value.items():
            if name not in ('content_file_inner', 'content', 'markdown', 'file_content', 'raw_markdown'):
                yield from find_values(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from find_values(item, key)


def field(value, *keys):
    for key in keys:
        for item in find_values(value, key):
            if item is not None:
                return item
    return None


def extract_markdown(value):
    if isinstance(value, dict) and isinstance(value.get('raw_markdown'), str):
        return value['raw_markdown']
    if isinstance(value, str):
        content = value
    elif isinstance(value, dict) and 'file_content' in value:
        return extract_markdown(value['file_content'])
    else:
        content = field(value, 'content_file_inner', 'markdown', 'content', 'file_content')
    # Only unwrap JSON string layers. A Markdown document may itself be valid JSON.
    for _ in range(8):
        if not isinstance(content, str):
            break
        try:
            inner = json.loads(content)
        except ValueError:
            break
        if not isinstance(inner, str):
            break
        content = inner
    if isinstance(content, str):
        return content
    raise PublisherError('Markdown content missing from CLI response')


def check_response(value):
    if isinstance(value, dict):
        for key in ('errcode', 'status', 'task_status'):
            item = value.get(key)
            failed = (key == 'errcode' and item not in (0, '0', None)) or (
                key != 'errcode' and str(item).lower() in ('failed', 'failure', 'fail', 'error'))
            if failed:
                detail = str(value.get('errmsg', value.get('message', '')))
                raise CliError('WeCom rejected request (' + key + '=' + str(item) + '): ' + detail)
        for key, item in value.items():
            if key not in ('content_file_inner', 'markdown', 'content', 'file_content', 'raw_markdown'):
                check_response(item)
    elif isinstance(value, list):
        for item in value:
            check_response(item)


class WeComCli:
    def __init__(self, path, timeout=120):
        self.path, self.timeout = str(path), timeout

    def call(self, route, data):
        argv = [self.path, *route, '--json', json.dumps(data, ensure_ascii=False)]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise CliError('WeCom CLI timed out; remote outcome uncertain', uncertain=True) from exc
        except OSError as exc:
            raise CliError('Cannot start WeCom CLI: ' + str(exc)) from exc
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise CliError('WeCom CLI returned invalid JSON; remote outcome uncertain', uncertain=True) from exc
        native_error = value.get('error') if isinstance(value, dict) else None
        if isinstance(native_error, dict):
            kind, code = native_error.get('type'), native_error.get('code')
            # This exact pair is the CLI's verified local path-scope rejection.
            # Other native errors may occur after a request reached the server.
            local_rejection = kind == 'PermissionError' and code in (893006, '893006')
            detail = str(native_error.get('message', 'Unspecified CLI error'))
            raise CliError('WeCom CLI ' + str(kind) + ' (' + str(code) + '): ' + detail,
                           uncertain=not local_rejection)
        check_response(value)
        if result.returncode:
            raise CliError('WeCom CLI exited with code ' + str(result.returncode) + '; remote outcome uncertain', uncertain=True)
        # A document can contain arbitrary JSON. Only transport wrappers may
        # supply a local response path; content is never interpreted as metadata.
        def response_file_path(envelope):
            if not isinstance(envelope, dict):
                return None
            if isinstance(envelope.get('file_path'), str):
                return envelope['file_path']
            for wrapper in ('data', 'result', 'response', 'output'):
                found = response_file_path(envelope.get(wrapper))
                if found:
                    return found
            pages = envelope.get('pages')
            if isinstance(pages, list):
                for page in pages:
                    found = response_file_path(page)
                    if found:
                        return found
            return None
        response_path = response_file_path(value)
        if response_path:
            try:
                raw = Path(response_path).read_text(encoding='utf-8')
                markdown_request = (data.get('content_type') == 'markdown' and tuple(route) in (
                    ('smartpage', 'pages', 'get'), ('doc', 'contents', 'get')))
                # A requested content file is the Markdown itself, regardless of
                # suffix or whether its text happens to be valid API-like JSON.
                opaque_markdown = markdown_request or Path(response_path).suffix.lower() in ('.md', '.markdown')
                if opaque_markdown:
                    value = {'response': value, 'raw_markdown': raw}
                else:
                    metadata = decoded(raw)
                    if not isinstance(metadata, str):
                        check_response(metadata)
                    value = {'response': value, 'transport_metadata': metadata}
            except OSError as exc:
                raise CliError('CLI response file unavailable: ' + str(exc), uncertain=True) from exc
        return sanitize(value)


class State:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.data = None

    @contextlib.contextmanager
    def locked(self, write=False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # A lock file persists so replacing the JSON never replaces the lock inode.
        if write or self.path.parent.exists():
            handle = open(str(self.path) + '.lock', 'a+', encoding='utf-8')
        else:
            handle = None
        try:
            if handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
            self.data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {
                'version': 1, 'documents': {}, 'requirements': {}, 'resolved_members': {}}
            yield self.data
        finally:
            if handle:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(filename, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(filename):
                os.unlink(filename)


def fenced_blocks(source):
    """Yield (start, end, language, body), respecting Markdown fence lengths."""
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    index = 0
    while index < len(lines):
        opener = re.match(r'^[ \t]*(`{3,}|~{3,})([^\r\n]*)', lines[index])
        if not opener:
            index += 1
            continue
        marker, language = opener.groups()
        language = language.strip().lower()
        closing = re.compile(r'^[ \t]*' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}[ \t]*(?:\r?\n)?$')
        end = index + 1
        while end < len(lines) and not closing.fullmatch(lines[end]):
            end += 1
        if end == len(lines) and language == 'mermaid':
            raise PublisherError('Unclosed Mermaid fenced block')
        stop = min(end + 1, len(lines))
        yield offsets[index], offsets[stop], language, ''.join(lines[index + 1:end])
        index = stop


def escaped(source, offset):
    count = 0
    offset -= 1
    while offset >= 0 and source[offset] == '\\':
        count += 1
        offset -= 1
    return count % 2 == 1


def in_spans(offset, spans):
    return any(start <= offset < end for start, end in spans)


def code_spans(source, blocks=None):
    spans = [(start, end) for start, end, _, _ in (blocks if blocks is not None else fenced_blocks(source))]
    runs = list(re.finditer(r'`+', source))
    index = 0
    while index < len(runs):
        opener = runs[index]
        if in_spans(opener.start(), spans) or escaped(source, opener.start()):
            index += 1
            continue
        close = index + 1
        while close < len(runs):
            if len(runs[close].group()) == len(opener.group()) and not in_spans(runs[close].start(), spans):
                break
            close += 1
        if close < len(runs):
            spans.append((opener.start(), runs[close].end()))
            index = close + 1
        else:
            index += 1
    return spans


IMAGE = re.compile(r'!\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+["\'][^\n]*?["\'])?\s*\)')
REFERENCE_IMAGE = re.compile(r'!\[([^\]\n]*)\](?:\[([^\]\n]*)\])?(?![\[(])')
LINK = re.compile(r'\[([^\]\n]*)\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+["\'][^\n]*?["\'])?\s*\)')
REFERENCE_LINK = re.compile(r'\[([^\]\n]+)\](?:\[([^\]\n]*)\])?(?![\[(])')
REFERENCE_DEFINITION = re.compile(r'(?m)^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*(<[^>\n]+>|[^\s]+)(?:[ \t]+["\'][^\n]*["\'])?[ \t]*$')
TAPD = re.compile(r'https://(?:www\.)?tapd\.cn/tapd_fe/([0-9]+)/story/detail/([0-9]{19})(?:[/?#].*)?\Z')


def valid_image(path):
    suffix = path.suffix.lower()
    content = path.read_bytes()
    valid = ((suffix == '.png' and content.startswith(b'\x89PNG\r\n\x1a\n')) or
             (suffix in ('.jpg', '.jpeg') and content.startswith(b'\xff\xd8\xff')) or
             (suffix == '.gif' and content.startswith((b'GIF87a', b'GIF89a'))) or
             (suffix == '.webp' and content.startswith(b'RIFF') and content[8:12] == b'WEBP'))
    if not valid:
        raise PublisherError('Unsupported or invalid image: ' + str(path))
    return content


class Publisher:
    def __init__(self, config, cli=None, renderer=None, notification_sender=None):
        if config.get('link_access', 'members') not in ('members', 'corp_read'):
            raise PublisherError('link_access must be members or corp_read')
        self.config = config
        self.state = State(config['state_path'])
        self.cli = cli or WeComCli(config.get('cli_path', 'wecom-cli'))
        self.renderer = renderer or self.render_mermaid
        self.notification_sender = notification_sender or group_notify.send
        self.warnings = []

    def temporary_directory(self):
        configured = self.config.get('temp_root')
        if configured:
            root = Path(configured).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            root = str(root)
        else:
            root = '/private/tmp' if Path('/private/tmp').is_dir() else None
        return tempfile.TemporaryDirectory(prefix='wecom-publish-', dir=root)

    def stage_image(self, image, content, directory, number):
        staged = Path(directory) / ('asset-' + str(number) + image.suffix.lower())
        staged.write_bytes(content)
        return staged

    def render_mermaid(self, source, directory, number):
        directory = Path(directory)
        input_path = directory / ('diagram-' + str(number) + '.mmd')
        output_path = input_path.with_suffix('.png')
        input_path.write_text(source, encoding='utf-8')
        puppeteer = directory / 'puppeteer-config.json'
        mermaid = directory / 'mermaid-config.json'
        chrome = self.config.get('chrome_path')
        if not chrome:
            raise PublisherError('chrome_path is required for Mermaid rendering')
        puppeteer.write_text(json.dumps({'executablePath': chrome}), encoding='utf-8')
        mermaid.write_text(json.dumps({'securityLevel': 'strict', 'fontFamily': 'PingFang SC',
                                      'flowchart': {'wrappingWidth': 320}}), encoding='utf-8')
        argv = [self.config.get('mermaid_cli', 'mmdc'), '-i', str(input_path), '-o', str(output_path),
                '-p', str(puppeteer), '-c', str(mermaid), '-b', 'white', '-s', '2']
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=120, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublisherError('Mermaid render failed: ' + str(exc)) from exc
        if result.returncode or not output_path.exists():
            raise PublisherError('Mermaid render failed: ' + result.stderr.strip())
        valid_image(output_path)
        return output_path

    def prepare(self, path, directory):
        path = Path(path).expanduser().resolve()
        source = path.read_text(encoding='utf-8')
        blocks = list(fenced_blocks(source))
        protected = code_spans(source, blocks)
        outside = source
        for start, end in sorted(protected, reverse=True):
            outside = outside[:start] + ' ' * (end - start) + outside[end:]
        unsafe = bool(re.search(r'(?i)javascript\s*:', outside))
        for tag in re.finditer(r'''<\s*([a-z][\w:-]*)\b((?:"[^"]*"|'[^']*'|[^'">])*)>''', outside, re.I):
            if escaped(outside, tag.start()):
                continue
            if tag.group(1).lower() in ('script', 'iframe', 'object', 'embed', 'svg', 'style'):
                unsafe = True
            # Consume whole quoted values so text inside them cannot become an attribute.
            attributes = re.finditer(r'''([^\s=/>]+)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?''', tag.group(2))
            if any(re.fullmatch(r'on[a-z][\w:-]*', attribute.group(1), re.I) for attribute in attributes):
                unsafe = True
        if unsafe:
            raise PublisherError('Active HTML or javascript URL is not allowed outside fenced code')
        digest = hashlib.sha256(source.encode('utf-8'))
        assets = []
        replacements = []
        fence_spans = []
        for start, end, language, body in blocks:
            fence_spans.append((start, end))
            if language == 'mermaid':
                image = Path(self.renderer(body, directory, len(assets)))
                valid_image(image)
                token = 'WECOM_ASSET_' + str(len(assets)) + '_TOKEN'
                assets.append((token, image))
                replacements.append((start, end, '![' + 'Mermaid diagram' + '](' + token + ')\n'))
        for match in IMAGE.finditer(source):
            if in_spans(match.start(), protected) or escaped(source, match.start()):
                continue
            target = match.group(2).strip('<>')
            parsed = urlparse(target)
            if parsed.scheme == 'https':
                continue
            if parsed.scheme or parsed.netloc:
                raise PublisherError('Image must be a local file or HTTPS URL: ' + target)
            image = (path.parent / unquote(parsed.path)).resolve()
            try:
                content = valid_image(image)
            except OSError as exc:
                raise PublisherError('Image unavailable: ' + str(image)) from exc
            digest.update(str(image).encode('utf-8'))
            digest.update(content)
            token = 'WECOM_ASSET_' + str(len(assets)) + '_TOKEN'
            assets.append((token, self.stage_image(image, content, directory, len(assets))))
            replacements.append((match.start(), match.end(), '![' + match.group(1) + '](' + token + ')'))
        definitions = {match.group(1).strip().casefold(): match.group(2).strip('<>')
                       for match in REFERENCE_DEFINITION.finditer(source)
                       if not in_spans(match.start(), protected) and not escaped(source, match.start())}
        for match in REFERENCE_IMAGE.finditer(source):
            if in_spans(match.start(), protected) or escaped(source, match.start()):
                continue
            label = (match.group(2) or match.group(1)).strip().casefold()
            if label not in definitions:
                continue
            target = definitions[label]
            parsed = urlparse(target)
            if parsed.scheme == 'https':
                continue
            if parsed.scheme or parsed.netloc:
                raise PublisherError('Image must be a local file or HTTPS URL: ' + target)
            image = (path.parent / unquote(parsed.path)).resolve()
            try:
                content = valid_image(image)
            except OSError as exc:
                raise PublisherError('Image unavailable: ' + str(image)) from exc
            digest.update(str(image).encode('utf-8'))
            digest.update(content)
            token = 'WECOM_ASSET_' + str(len(assets)) + '_TOKEN'
            assets.append((token, self.stage_image(image, content, directory, len(assets))))
            replacements.append((match.start(), match.end(), '![' + match.group(1) + '](' + token + ')'))
        for start, end, replacement in sorted(replacements, reverse=True):
            source = source[:start] + replacement + source[end:]
        return source, assets, digest.hexdigest()

    def render_local_links(self, source, path, documents):
        """Rewrite standard relative Markdown doc links using managed source mappings."""
        path = Path(path).resolve()
        protected = code_spans(source)
        definitions = [match for match in REFERENCE_DEFINITION.finditer(source)
                       if not in_spans(match.start(), protected) and not escaped(source, match.start())]
        definition_map = {match.group(1).strip().casefold(): match.group(2).strip('<>') for match in definitions}
        image_spans = [match.span() for pattern in (IMAGE, REFERENCE_IMAGE) for match in pattern.finditer(source)]
        definition_spans = [match.span() for match in definitions]
        replacements = []

        def local_target(target):
            parsed = urlparse(target)
            if parsed.scheme or parsed.netloc or not parsed.path or Path(unquote(parsed.path)).suffix.lower() != '.md':
                return None
            if Path(unquote(parsed.path)).is_absolute():
                raise PublisherError('Absolute local Markdown links are unsupported: ' + target)
            return parsed

        def render(label, target):
            parsed = local_target(target)
            if parsed is None:
                return None
            target_path = (path.parent / unquote(parsed.path)).resolve()
            if target_path == path:
                return '[' + label + '](' + ('#' + parsed.fragment if parsed.fragment else '#') + ')'
            urls = {doc['url'] for doc in documents.values()
                    if doc.get('source_file') == str(target_path) and doc.get('url') and doc.get('source_hash') and not doc.get('pending')}
            if len(urls) == 1:
                url = next(iter(urls))
                if parsed.fragment:
                    url += '#' + parsed.fragment
                return '[' + label + '](' + url + ')'
            filename = Path(unquote(parsed.path)).name + ('#' + parsed.fragment if parsed.fragment else '')
            self.warnings.append('unresolved_local_links: ' + filename + (' (multiple published matches)' if len(urls) > 1 else ' (not published)'))
            return label + '（' + filename + '）'

        for match in LINK.finditer(source):
            if (in_spans(match.start(), protected + image_spans + definition_spans)
                    or escaped(source, match.start())):
                continue
            replacement = render(match.group(1), match.group(2).strip('<>'))
            if replacement is not None:
                replacements.append((match.start(), match.end(), replacement))
        for match in REFERENCE_LINK.finditer(source):
            if (in_spans(match.start(), protected + image_spans + definition_spans)
                    or escaped(source, match.start())):
                continue
            target = definition_map.get((match.group(2) or match.group(1)).strip().casefold())
            if target is None:
                continue
            replacement = render(match.group(1), target)
            if replacement is not None:
                replacements.append((match.start(), match.end(), replacement))
        for match in definitions:
            if local_target(match.group(2).strip('<>')) is not None:
                replacements.append((match.start(), match.end(), ''))
        for start, end, replacement in sorted(replacements, reverse=True):
            source = source[:start] + replacement + source[end:]
        return source

    def _pending_guard(self):
        pending = [item for item in self.state.data['documents'].values() if item.get('pending')]
        if pending:
            raise PublisherError('pending remote mutation requires manual reconciliation in state before retry')

    def _mutation(self, doc, operation, route, data):
        doc['pending'] = operation
        self.state.save()
        try:
            result = self.cli.call(route, data)
        except CliError as exc:
            if not exc.uncertain:
                doc.pop('pending', None)
                self.state.save()
            raise
        return result

    def _access_mutation(self, doc, operation, route, data):
        # Exact property/member setters can safely be retried. Keep their
        # uncertainty separate from create/content/image hard-stop markers.
        doc['pending_access'] = operation
        self.state.save()
        try:
            result = self.cli.call(route, data)
        except CliError as exc:
            if not exc.uncertain:
                doc.pop('pending_access', None)
                self.state.save()
            raise
        doc.pop('pending_access', None)
        self.state.save()
        return result

    def _restrict(self, doc, force=False):
        rules = {'enable_member_join_admin_check': True}
        if self.config.get('link_access', 'members') == 'corp_read':
            rules = {'enable_member_join_admin_check': False,
                     'corp_internal_join_auth': 'read', 'corp_external_join_auth': 'deny'}
        applied = doc.get('applied_join_rules')
        if applied is None and doc.get('restricted'):
            applied = {'enable_member_join_admin_check': True}
        if force or applied != rules or doc.get('pending_access') == 'rules_update':
            self._access_mutation(doc, 'rules_update', ('doc', 'rules', 'update'), {
                'docid': doc['docid'], **rules})
            doc['restricted'] = rules['enable_member_join_admin_check']
            doc['applied_join_rules'] = rules
            self.state.save()

    def _ensure_doc(self, key, title, projects):
        documents = self.state.data['documents']
        doc = documents.setdefault(key, {'title': title, 'projects': projects})
        doc['projects'] = projects
        if doc.get('pending'):
            raise PublisherError('pending mutation requires reconciliation: ' + title)
        if not doc.get('docid'):
            result = self._mutation(doc, 'create', ('smartpage', 'create'), {'name': title})
            docid, url = field(result, 'docid'), field(result, 'url')
            if not docid or not url:
                raise CliError('Created document response lacks mapping; pending reconciliation required', uncertain=True)
            doc.update(docid=str(docid), url=url, content_hash=None)
            doc.pop('pending', None)
            self.state.save()  # Persist identity before rules, pages, members or body writes.
        self._restrict(doc)
        return doc

    def _visible_pages(self, doc):
        tree = self.cli.call(('smartpage', 'pages', 'get'), {'docid': doc['docid']})
        pages = field(tree, 'pages')
        if not isinstance(pages, list):
            raise PublisherError('Page tree missing pages')
        visible = [page for page in pages if isinstance(page, dict) and page.get('page_id')]
        if not visible:
            raise PublisherError('Document has no visible page')
        return visible

    def _read_page(self, doc, page_id):
        # Choose first returned visible page; parent_id can name a hidden root.
        result = self.cli.call(('smartpage', 'pages', 'get'), {
            'docid': doc['docid'], 'page_id': page_id, 'content_type': 'markdown'})
        return extract_markdown(result)

    def _read_doc(self, doc):
        page_id = self._visible_pages(doc)[0]['page_id']
        return page_id, self._read_page(doc, page_id)

    def _write(self, doc, content, directory):
        page_id, current = self._read_doc(doc)
        digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
        if current != content:
            content_path = Path(directory) / 'content.md'
            content_path.write_text(content, encoding='utf-8')
            operation = 'append' if not current else 'overwrite'
            self._mutation(doc, operation, ('smartpage', 'pages', operation), {
                'docid': doc['docid'], 'page_id': page_id, 'content_type': 'markdown', 'file_path': str(content_path)})
            doc.pop('pending', None)
        doc['content_hash'] = digest
        self.state.save()

    def _all_projects(self):
        return [name for name in ('api', 'admin', 'mall') if name in self.config['projects']]

    def _ensure_indexes(self):
        if 'shared_catalog' in self.config:
            raise PublisherError('Shared catalog configuration requires shared_publisher.py; use that entry point')
        projects = self._all_projects()
        self._ensure_doc('root', '研发文档目录', projects)
        for project in projects:
            self._ensure_doc('project:' + project, project + ' 文档目录', [project])

    def _render_indexes(self, directory):
        state = self.state.data
        documents, requirements = state['documents'], state['requirements']
        root = '# 研发文档目录\n\n'
        for project in self._all_projects():
            root += '- [' + project + '](' + documents['project:' + project]['url'] + ')\n'
        root += '\n## 需求\n\n'
        if not requirements:
            root += '暂无已发布需求。\n'
        for identity, requirement in sorted(requirements.items()):
            doc = documents['requirement:' + identity]
            label = '需求 ' + requirement['story'] if requirement['story'] else 'TAPD 需求链接'
            root += '- [' + label + '](' + doc['url'] + ')\n'
            content = '# ' + label + '\n\n[TAPD 需求](' + requirement['tapd'] + ')\n\n'
            for project in self._all_projects():
                content += '## ' + {'api': 'API', 'admin': 'Admin', 'mall': 'Mall'}[project] + '\n\n'
                bodies = [(key, body) for key, body in requirement['bodies'].items()
                          if project in body.get('projects', [body['project']]) and documents[key].get('source_hash')]
                if not bodies:
                    content += '暂无已发布文档。\n'
                for key, body in sorted(bodies, key=lambda pair: (pair[1]['kind'], pair[1]['filename'], pair[0])):
                    content += '- [' + body['kind'] + ' · ' + body['filename'] + '](' + documents[key]['url'] + ')\n'
                content += '\n'
            self._write(doc, content, directory)
        for project in self._all_projects():
            content = '# ' + project + ' 文档目录\n\n'
            matching = [(identity, req) for identity, req in sorted(requirements.items())
                        if any(project in body.get('projects', [body['project']]) and documents[key].get('source_hash')
                               for key, body in req['bodies'].items())]
            if not matching:
                content += '暂无已发布需求。\n'
            for identity, req in matching:
                label = '需求 ' + req['story'] if req['story'] else 'TAPD 需求链接'
                content += '- [' + label + '](' + documents['requirement:' + identity]['url'] + ')\n'
            self._write(documents['project:' + project], content, directory)
        self._write(documents['root'], root, directory)

    def _resolve_members(self):
        resolved = self.state.data['resolved_members']
        resolved.update(self.config.get('resolved_members', {}))
        names = sorted({name for project in self.config['projects'].values()
                        for role in ('maintainers', 'readers') for name in project.get(role, [])})
        unresolved = [name for name in names if name not in resolved]
        for start in range(0, len(unresolved), 10):
            batch = unresolved[start:start + 10]
            try:
                result = self.cli.call(('contact', 'users', 'search'), {'keywords': batch})
            except CliError as exc:
                self.warnings.append('permissions_pending: contact lookup unavailable: ' + str(exc))
                continue
            candidates = []
            def collect(value):
                value = decoded(value)
                if isinstance(value, dict):
                    if value.get('userid'):
                        candidates.append(value)
                    for item in value.values():
                        collect(item)
                elif isinstance(value, list):
                    for item in value:
                        collect(item)
            collect(result)
            for name in batch:
                exact = {}
                for candidate in candidates:
                    aliases = candidate.get('alias', [])
                    labels = [candidate.get('name', '')] + (aliases if isinstance(aliases, list) else [aliases])
                    labels = [str(item).strip() for item in labels if item]
                    if any(name == label for label in labels):
                        exact[candidate['userid']] = candidate
                if len(exact) == 1:
                    resolved[name] = str(next(iter(exact)))
                else:
                    self.warnings.append('permissions_pending: no unique contact match for ' + name)
        self.state.save()

    def _members(self):
        self._resolve_members()
        resolved = self.state.data['resolved_members']
        for doc in self.state.data['documents'].values():
            if not doc.get('docid') or doc.get('external_catalog'):
                continue
            try:
                self._restrict(doc)
            except CliError as exc:
                doc['permissions_pending'] = True
                self.warnings.append('permissions_pending: join restriction unconfirmed for ' + doc['title'] + ': ' + str(exc))
                self.state.save()
                continue
            desired, missing = {}, []
            for project in doc['projects']:
                settings = self.config['projects'][project]
                for role, auth in (('readers', 'read'), ('maintainers', 'edit')):
                    for name in settings.get(role, []):
                        if name not in resolved:
                            missing.append(name)
                        elif desired.get(resolved[name]) != 'edit':
                            desired[resolved[name]] = auth
            doc['permissions_pending'] = bool(missing)
            if desired != doc.get('applied_members', {}) or doc.get('pending_access') == 'members_update':
                items = [{'userid': userid, 'user_type': 'user', 'user_auth': auth} for userid, auth in sorted(desired.items())]
                if items:
                    try:
                        self._access_mutation(doc, 'members_update', ('doc', 'members', 'update'), {
                            'docid': doc['docid'], 'add_member_list': {'items': items}})
                        doc['applied_members'] = desired
                    except CliError as exc:
                        doc['permissions_pending'] = True
                        self.warnings.append('permissions_pending: member update unavailable for ' + doc['title'] + ': ' + str(exc))
            self.state.save()

    def _result(self, **extra):
        pending = any(doc.get('permissions_pending') or doc.get('pending_access') for doc in self.state.data['documents'].values())
        return dict(permissions_pending=pending, warnings=list(dict.fromkeys(self.warnings)), **extra)

    def init(self, execute=False):
        if not execute:
            return {'status': 'dry_run', 'planned': ['root index'] + [name + ' index' for name in self._all_projects()]}
        self.warnings = []
        with self.state.locked(write=True), self.temporary_directory() as directory:
            self._pending_guard()
            self._ensure_indexes()
            self._render_indexes(directory)
            self._members()
            return self._result(status='ready', url=self.state.data['documents']['root']['url'])

    def publish(self, project, tapd, file, kind, execute=False, also_projects=None):
        if project not in self.config['projects']:
            raise PublisherError('Project is not configured: ' + project)
        assigned_projects = list(dict.fromkeys([project] + list(also_projects or [])))
        if any(name not in self._all_projects() for name in assigned_projects):
            raise PublisherError('Additional projects must be configured api, admin or mall')
        match = TAPD.fullmatch(tapd)
        parsed_tapd = urlparse(tapd)
        if parsed_tapd.scheme != 'https' or parsed_tapd.netloc not in ('www.tapd.cn', 'tapd.cn'):
            raise PublisherError('Unsupported TAPD URL; expected HTTPS tapd.cn or www.tapd.cn link')
        if not str(kind).strip() or '\n' in kind:
            raise PublisherError('kind must be a nonempty single line')
        if match:
            workspace, story = match.groups()
            identity = workspace + ':' + story
        else:
            story = None
            identity = 'url:' + hashlib.sha256(tapd.encode('utf-8')).hexdigest()
        path = Path(file).expanduser().resolve()
        key = 'body:' + hashlib.sha256(json.dumps([identity, project, kind, str(path)]).encode()).hexdigest()
        self.warnings = []
        with self.temporary_directory() as directory:
            content, assets, digest = self.prepare(path, directory)  # Validate/render before remote mutation.
            if not execute:
                with self.state.locked():
                    self.render_local_links(content, path, self.state.data['documents'])
                    prior_projects = self.state.data['documents'].get(key, {}).get('projects', [])
                    body_projects = list(dict.fromkeys(assigned_projects + prior_projects))
                return {'status': 'dry_run', 'project': project, 'projects': body_projects,
                        'kind': kind, 'file': str(path), 'tapd': tapd, 'images': len(assets),
                        'changed': None, 'warnings': list(dict.fromkeys(self.warnings))}
            with self.state.locked(write=True):
                self._pending_guard()
                content = self.render_local_links(content, path, self.state.data['documents'])
                digest = hashlib.sha256((digest + '\0' + content).encode('utf-8')).hexdigest()
                self._ensure_indexes()
                req = self.state.data['requirements'].setdefault(identity, {'story': story, 'tapd': tapd, 'bodies': {}})
                title = '需求 ' + story if story else 'TAPD 需求链接'
                self._ensure_doc('requirement:' + identity, title, self._all_projects())
                prior_projects = self.state.data['documents'].get(key, {}).get('projects', [])
                body_projects = list(dict.fromkeys(assigned_projects + prior_projects))
                doc = self._ensure_doc(key, project + ' · ' + kind + ' · ' + path.name, body_projects)
                doc['source_file'] = str(path)
                self.state.save()
                changed = doc.get('source_hash') != digest
                first_publish = not doc.get('source_hash')
                event_id = None
                if changed:
                    for token, image in assets:
                        result = self._mutation(doc, 'image_upload', ('smartpage', 'images', 'upload'), {
                            'docid': doc['docid'], 'file_path': str(image)})
                        url = field(result, 'url', 'image_url')
                        if not url:
                            raise CliError('Image upload response lacks URL; pending reconciliation required', uncertain=True)
                        doc.pop('pending', None)
                        self.state.save()
                        content = content.replace(token, url)
                    self._write(doc, content, directory)
                    doc['source_hash'] = digest
                    doc.pop('notification_event_id', None)
                    if self._group_settings().get('enabled') is True:
                        event_id = str(uuid.uuid4())
                        doc['notification_event_id'] = event_id
                        event = {'doc_key': key, 'hash': digest, 'status': 'queued', 'publish_complete': False,
                                 'payload': group_notify.payload(body_projects, doc['title'], first_publish, doc['url'],
                                     self.state.data['documents']['requirement:' + identity]['url'], tapd)}
                        self.state.data.setdefault('group_notifications', {})[event_id] = event
                    # The source hash and its new event share one atomic save.
                    self.state.save()
                req['bodies'][key] = {'project': project, 'projects': body_projects, 'kind': kind, 'filename': path.name}
                self.state.save()
                self._render_indexes(directory)
                self._members()
                current_event_id = doc.get('notification_event_id')
                event = self.state.data.get('group_notifications', {}).get(current_event_id)
                if event and event['hash'] == digest and event['status'] == 'queued':
                    event['publish_complete'] = True
                    if (self._result()['permissions_pending'] and
                            '权限待同步，部分成员可能暂时无法访问。' not in event['payload']['markdown']['content']):
                        event['payload']['markdown']['content'] += '\n\n权限待同步，部分成员可能暂时无法访问。'
                    self.state.save()
                notification = {'status': 'disabled' if changed else 'unchanged'}
                if changed and event_id:
                    notification = self._notify_event(event_id)
                return self._result(status='published' if changed else 'unchanged', url=doc['url'], changed=changed,
                                    notification=notification)

    def _group_settings(self):
        settings = self.config.get('group_notification', {})
        return settings if isinstance(settings, dict) else {}

    def _notify_event(self, event_id):
        event = self.state.data['group_notifications'][event_id]
        if self._group_settings().get('enabled') is not True:
            return {'status': 'disabled', 'event_id': event_id}
        try:
            webhook = group_notify.read_webhook(self._group_settings())
        except ValueError:
            event['status'] = 'not_configured'
            self.state.save()
            return {'status': 'not_configured', 'event_id': event_id}
        event['status'] = 'sending'
        self.state.save()  # Crash safety: never silently retry an uncertain delivery.
        try:
            response = self.notification_sender(webhook, event['payload'])
            event['status'] = group_notify.response_status(response)
        except Exception:
            # Transport exceptions and server errmsg can contain the secret URL.
            event['status'] = 'unknown'
        self.state.save()
        return {'status': event['status'], 'event_id': event_id}

    def notify_retry(self, event_id, execute=False):
        with self.state.locked(write=execute):
            event = self.state.data.get('group_notifications', {}).get(event_id)
            if not event:
                raise PublisherError('Notification event was not found')
            doc = self.state.data['documents'].get(event['doc_key'], {})
            if (event['status'] not in ('not_configured', 'rejected', 'queued')
                    or doc.get('pending')
                    or not event.get('publish_complete') or event['hash'] != doc.get('source_hash')
                    or doc.get('notification_event_id') != event_id):
                raise PublisherError('Notification event is not eligible for explicit retry')
            if not execute:
                return {'status': 'dry_run', 'event_id': event_id, 'event_status': event['status']}
            return self._notify_event(event_id)

    def configure_group(self, execute=False):
        if self._group_settings().get('enabled') is not True:
            return {'status': 'disabled'}
        try:
            group_notify.private_path(self._group_settings())
        except ValueError:
            return {'status': 'not_configured'}
        if not execute:
            return {'status': 'dry_run', 'planned': 'save group webhook privately; no notification'}
        if not sys.stdin.isatty():
            raise PublisherError('Group webhook configuration requires an interactive TTY')
        try:
            with warnings.catch_warnings():
                # Abort before getpass can fall back to echoed terminal input.
                warnings.simplefilter('error', getpass.GetPassWarning)
                value = getpass.getpass('WeCom group webhook (hidden): ')
            group_notify.save_webhook(self._group_settings(), value)
        except (ValueError, EOFError, OSError, getpass.GetPassWarning):
            raise PublisherError('Cannot configure private group webhook') from None
        return {'status': 'configured'}

    def sync_members(self, execute=False):
        if not execute:
            return {'status': 'dry_run', 'planned': 'resolve configured contacts and update existing document members'}
        self.warnings = []
        with self.state.locked(write=True):
            self._members()
            result = self._result(status='members_synced')
            if result['permissions_pending']:
                result['status'] = 'members_pending'
            return result

    def sync_rules(self, execute=False, include_shared=False):
        """Apply configured link access without contacts, content writes or notifications.

        Adopted shared catalogs require explicit inclusion; normal publication
        continues to leave other authors' access rules untouched.
        """
        with self.state.locked(write=execute):
            docs = [doc for doc in self.state.data['documents'].values()
                    if doc.get('docid') and (include_shared or not doc.get('external_catalog'))]
            plan = {'link_access': self.config.get('link_access', 'members'),
                    'documents': [{'title': d['title'], 'url': d.get('url')} for d in docs]}
            if not execute:
                return dict(status='dry_run', **plan)
            self._pending_guard()
            if any(d.get('pending_access') not in (None, 'rules_update') for d in docs):
                raise PublisherError('pending member update requires reconciliation before sync-rules')
            for doc in docs:
                self._restrict(doc, force=True)
            return dict(status='rules_synced', updated=len(docs), **plan)

    def status(self):
        with self.state.locked():
            docs = self.state.data['documents'].values()
            return {'status': 'local', 'documents': [{'title': doc['title'], 'url': doc.get('url'),
                    'pending': doc.get('pending'), 'pending_access': doc.get('pending_access'),
                    'permissions_pending': doc.get('permissions_pending', False)} for doc in docs]}

    def read(self, url):
        with self.state.locked():
            doc = next((item for item in self.state.data['documents'].values() if item.get('url') == url), None)
            if doc is None:
                parsed = urlparse(url)
                match = re.fullmatch(r'/(smartpage|doc)/([A-Za-z0-9_-]+)/?', parsed.path)
                if parsed.scheme != 'https' or parsed.netloc != 'doc.weixin.qq.com' or not match:
                    raise PublisherError('Unsupported document URL; expected WeCom smartpage or doc URL')
                kind, docid = match.groups()
                if kind == 'doc':
                    result = self.cli.call(('doc', 'contents', 'get'), {'docid': docid, 'content_type': 'markdown'})
                    return {'url': url, 'markdown': extract_markdown(result)}
                doc = {'docid': docid}
            markdown = '\n\n---\n\n'.join(self._read_page(doc, page['page_id']) for page in self._visible_pages(doc))
            return {'url': url, 'markdown': markdown}


def main(argv=None, publisher_class=Publisher):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='local JSON configuration')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'sync-members'):
        command = commands.add_parser(name)
        command.add_argument('--execute', action='store_true')
    command = commands.add_parser('sync-rules')
    command.add_argument('--execute', action='store_true')
    command.add_argument('--include-shared', action='store_true',
                         help='explicitly include adopted shared directories and requirement entries')
    command = commands.add_parser('configure-group')
    command.add_argument('--execute', action='store_true')
    command = commands.add_parser('notify-retry')
    command.add_argument('--event', required=True)
    command.add_argument('--execute', action='store_true')
    command = commands.add_parser('publish')
    command.add_argument('--project', required=True, choices=('api', 'admin', 'mall'))
    command.add_argument('--also-project', action='append', default=[], choices=('api', 'admin', 'mall'),
                         help='also index this same document under another explicitly assigned project')
    command.add_argument('--tapd', required=True)
    command.add_argument('--file', required=True)
    command.add_argument('--kind', required=True)
    command.add_argument('--execute', action='store_true')
    command = commands.add_parser('read')
    command.add_argument('--url', required=True)
    commands.add_parser('status')
    args = parser.parse_args(argv)
    try:
        config_path = Path(args.config).expanduser().resolve()
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config['state_path'] = str((config_path.parent / config['state_path']).resolve())
        settings = config.get('group_notification')
        if isinstance(settings, dict) and settings.get('webhook_file'):
            settings['webhook_file'] = str((config_path.parent / Path(settings['webhook_file']).expanduser()).resolve())
        app = publisher_class(config)
        if args.command == 'publish':
            result = app.publish(args.project, args.tapd, args.file, args.kind, execute=args.execute,
                                 also_projects=args.also_project)
        elif args.command == 'configure-group':
            result = app.configure_group(execute=args.execute)
        elif args.command == 'notify-retry':
            result = app.notify_retry(args.event, execute=args.execute)
        elif args.command == 'read':
            result = app.read(args.url)
        elif args.command == 'status':
            result = app.status()
        elif args.command == 'init':
            result = app.init(execute=args.execute)
        elif args.command == 'sync-rules':
            result = app.sync_rules(execute=args.execute, include_shared=args.include_shared)
        else:
            result = app.sync_members(execute=args.execute)
        print(json.dumps(sanitize(result), ensure_ascii=False, indent=2))
        return 0
    except (PublisherError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
