#!/usr/bin/env python3
"""Author-local publishing into an explicitly configured existing WeCom catalog.

Shared indexes are read before each addition and changed only with native block
inserts. This preserves other authors' content but is not a distributed lock.
"""
import copy
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urlparse

from publisher import Publisher, PublisherError, decoded, field, main

PROJECT_TITLES = {'api': 'API', 'admin': 'Admin', 'mall': 'Mall'}
MARKDOWN_LINK = re.compile(r'\[(?:\\.|[^\]\\\n])*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+[\"\'][^\n]*?[\"\'])?\s*\)')


def smartpage(url):
    parsed = urlparse(url)
    match = re.fullmatch(r'/smartpage/([A-Za-z0-9_-]+)/?', parsed.path)
    if parsed.scheme != 'https' or parsed.netloc != 'doc.weixin.qq.com' or not match:
        raise PublisherError('shared_catalog requires HTTPS WeCom smartpage URLs')
    return {'docid': match.group(1), 'url': url}


class _AnchorLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.targets = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            for name, value in attrs:
                if name == 'href' and value is not None:
                    # HTMLParser decodes character references in attributes.
                    self.targets.append(value)
                    break


def links(markdown):
    anchors = _AnchorLinks()
    anchors.feed(markdown)
    return [match.group(1).strip('<>') for match in MARKDOWN_LINK.finditer(markdown)] + anchors.targets


def display_label(value):
    return str(value).replace('\\', '\\\\').replace('[', r'\[').replace(']', r'\]').replace('\r', ' ').replace('\n', ' ')


def section(markdown, title):
    headings = list(re.finditer(r'^##[ \t]+([^\r\n]+?)[ \t]*#*[ \t]*$', markdown, re.M))
    found = [i for i, heading in enumerate(headings) if heading.group(1).strip() == title]
    if len(found) > 1:
        raise PublisherError('duplicate project heading: ' + title)
    if not found:
        return None
    index = found[0]
    end = headings[index + 1].start() if index + 1 < len(headings) else len(markdown)
    return markdown[headings[index].end():end]


class SharedPublisher(Publisher):
    def _require_label(self):
        label = self.config.get('publisher_label')
        if not isinstance(label, str) or not label.strip() or '\n' in label or '\r' in label:
            raise PublisherError('publisher_label must be a nonempty single-line display name')
        return label.strip()

    def _catalog(self):
        catalog = self.config.get('shared_catalog')
        if not isinstance(catalog, dict) or not isinstance(catalog.get('projects'), dict):
            raise PublisherError('shared_catalog root and project URLs are required')
        result = {'root': smartpage(catalog.get('root', ''))}
        for project in self._all_projects():
            result['project:' + project] = smartpage(catalog['projects'].get(project, ''))
        if len({doc['docid'] for doc in result.values()}) != len(result):
            raise PublisherError('shared_catalog URLs must identify distinct documents')
        return result

    def _ensure_indexes(self):
        self._require_label()
        catalog = self._catalog()
        # Read every fixed index before creating or writing any author document.
        for doc in catalog.values():
            self._read_doc(doc)
        for key, mapping in catalog.items():
            doc = self.state.data['documents'].setdefault(key, {})
            doc.update(mapping, title='研发文档目录' if key == 'root' else key[8:] + ' 文档目录',
                       projects=self._all_projects() if key == 'root' else [key[8:]], external_catalog=True)
        self.state.save()

    def _requirement_matches(self, tapd):
        catalog_ids = {doc['docid'] for doc in self._catalog().values()}
        _, root_markdown = self._read_doc(self.state.data['documents']['root'])
        candidates = {}
        for url in links(root_markdown):
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.netloc != 'doc.weixin.qq.com' or not parsed.path.startswith('/smartpage/'):
                continue
            doc = smartpage(url)
            if doc['docid'] not in catalog_ids:
                candidates.setdefault(doc['docid'], doc)
        matches = []
        for doc in candidates.values():
            _, markdown = self._read_doc(doc)
            if tapd in links(markdown):
                matches.append(doc)
        if len(matches) > 1:
            raise PublisherError('shared requirement conflict: multiple documents have the exact TAPD link')
        return matches

    def _ensure_doc(self, key, title, projects):
        if key.startswith('body:'):
            return super()._ensure_doc(key, self._require_label() + ' · ' + title, projects)
        if not key.startswith('requirement:'):
            raise PublisherError('Shared catalogs cannot create root or project indexes')
        identity = key[len('requirement:'):]
        tapd = self.state.data['requirements'][identity]['tapd']
        known = self.state.data['documents'].get(key)
        if known and known.get('docid'):
            _, markdown = self._read_doc(known)
            if tapd not in links(markdown) and not (known.get('seed_pending') and not markdown.strip()):
                raise PublisherError('Known shared requirement lacks the exact original TAPD link')
            matches = self._requirement_matches(tapd)
            if matches and matches[0]['docid'] != known['docid']:
                raise PublisherError('shared requirement conflict with local mapping')
            if not known.get('external_catalog'):
                self._restrict(known)
            return known
        matches = self._requirement_matches(tapd)
        if matches:
            doc = dict(matches[0], title=title, projects=projects, external_catalog=True)
            self.state.data['documents'][key] = doc
            self.state.save()
            return doc
        # Persist seed intent before create/restrict can be interrupted. The
        # superclass saves a new identity before applying its initial rules.
        doc = self.state.data['documents'].setdefault(key, {'title': title, 'projects': projects})
        doc['seed_pending'] = True
        self.state.save()
        return super()._ensure_doc(key, title, projects)

    def _block_nodes(self, doc, page_id):
        response = self.cli.call(('smartpage', 'pages', 'get'), {
            'docid': doc['docid'], 'page_id': page_id, 'content_type': 'block'})
        # The CLI transport may load a large response file. Consume only its
        # decoded transport result, never file paths or metadata inside content.
        envelope = response.get('transport_metadata', response) if isinstance(response, dict) else response
        if isinstance(envelope, list):
            nodes = envelope
        else:
            pages = field(envelope, 'pages')
            if not isinstance(pages, list):
                raise PublisherError('Block response missing pages')
            page = next((item for item in pages if isinstance(item, dict) and item.get('page_id') == page_id), None)
            nodes = decoded(page.get('content_file_inner')) if page else None
        if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
            raise PublisherError('Block response missing the actual block list')
        return nodes

    def _insert(self, doc, page_id, mdx, block_id=None):
        data = {'docid': doc['docid'], 'page_id': page_id,
                'method': 'insertAfter' if block_id else 'append', 'mdx': mdx}
        if block_id:
            data['block_id'] = block_id
        self._mutation(doc, 'shared_index_insert', ('smartpage', 'blocks', 'update'), data)
        doc.pop('pending', None)
        self.state.save()

    def _append_link(self, doc, url, label):
        page_id, markdown = self._read_doc(doc)
        if url not in links(markdown):
            self._block_nodes(doc, page_id)
            self._insert(doc, page_id, '- [' + display_label(label) + '](' + url + ')')

    def _project_link(self, doc, project, url, label):
        title = PROJECT_TITLES[project]
        page_id, markdown = self._read_doc(doc)
        body = section(markdown, title)
        if body is not None and url in links(body):
            return
        nodes = self._block_nodes(doc, page_id)
        headings = [node for node in nodes if node.get('type') == 'header_2'
                    and node.get('property', {}).get('title', {}).get('text') == title]
        if len(headings) > 1:
            raise PublisherError('duplicate project heading in block tree: ' + title)
        mdx = '- [' + display_label(label) + '](' + url + ')'
        if body is None:
            if headings:
                raise PublisherError('Project heading differs between Markdown and block tree')
            self._insert(doc, page_id, '## ' + title + '\n\n' + mdx)
        elif len(headings) != 1 or not headings[0].get('id'):
            raise PublisherError('Actual project heading block ID unavailable: ' + title)
        else:
            self._insert(doc, page_id, mdx, headings[0]['id'])

    def _render_indexes(self, directory):
        documents = self.state.data['documents']
        root = documents['root']
        for project in self._all_projects():
            self._append_link(root, documents['project:' + project]['url'], project)
        for identity, requirement in self.state.data['requirements'].items():
            doc = documents['requirement:' + identity]
            page_id, markdown = self._read_doc(doc)
            if not markdown.strip() and doc.get('seed_pending'):
                self._block_nodes(doc, page_id)
                content = '# ' + doc['title'] + '\n\n[TAPD 需求](' + requirement['tapd'] + ')\n\n'
                content += '\n\n'.join('## ' + PROJECT_TITLES[p] for p in self._all_projects())
                self._insert(doc, page_id, content)
                doc.pop('seed_pending', None)
                self.state.save()
                _, markdown = self._read_doc(doc)
            if requirement['tapd'] not in links(markdown):
                raise PublisherError('Shared requirement lacks the exact original TAPD link')
            for project in self._all_projects():
                # Reject ambiguous headings even when a URL happens to appear under one.
                section(markdown, PROJECT_TITLES[project])
                for key, body in requirement['bodies'].items():
                    if not documents[key].get('source_hash') or project not in body.get('projects', [body['project']]):
                        continue
                    title = documents[key]['title']
                    # Preserve an existing author's display name when their own config label changes.
                    suffix = body['project'] + ' · ' + body['kind'] + ' · ' + body['filename']
                    author = (title[:-len(suffix)].removesuffix(' · ') if title.endswith(suffix) else '') or self._require_label()
                    self._project_link(doc, project, documents[key]['url'], author + ' · ' + body['kind'] + ' · ' + body['filename'])
                    self._append_link(documents['project:' + project], doc['url'], doc['title'])
            self._append_link(root, doc['url'], doc['title'])
            matches = self._requirement_matches(requirement['tapd'])
            if not matches or matches[0]['docid'] != doc['docid']:
                raise PublisherError('shared requirement conflict after catalog link; reconcile manually')

    def _members(self):
        self._require_label()
        original = self.config
        self.config = copy.deepcopy(original)
        excluded = set(self.config.get('member_lookup_excluded', []))
        for project in self.config['projects'].values():
            for role in ('maintainers', 'readers'):
                project[role] = [name for name in project.get(role, []) if name not in excluded]
        try:
            return super()._members()
        finally:
            self.config = original


if __name__ == '__main__':
    sys.exit(main(publisher_class=SharedPublisher))
