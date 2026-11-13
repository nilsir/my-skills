"""企微发布成功后的 TAPD 交付同步；本模块从不完成工作流节点。"""
import hashlib
import html
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tapd_client import TapdClient, TapdError, plan_fields, update_fields


def target(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc not in ('www.tapd.cn', 'tapd.cn'):
        raise TapdError('TAPD 关联必须是官方 HTTPS 需求链接')
    match = re.fullmatch(r'/tapd_fe/(\d+)/story/detail/(\d{19})/?', parsed.path)
    if match:
        return match.group(1), match.group(2)
    if parsed.path.rstrip('/') == '/tapd_fe/my/work':
        values = parse_qs(parsed.query).get('dialog_preview_id', [])
        if len(values) == 1 and re.fullmatch(r'story_\d{19}', values[0]):
            return None, values[0][6:]
    raise TapdError('无法唯一识别 TAPD 需求，请提供需求详情或预览链接')


def same_target(left, right):
    try:
        lw, ls = target(left)
        rw, rs = target(right)
        return ls == rs and (lw is None or rw is None or lw == rw)
    except (TapdError, TypeError, ValueError):
        return False


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise TapdError('交付清单缺少有效内容：' + label)
    return value


def load_manifest(path, url):
    if not path:
        raise TapdError('已启用 TAPD 同步，请先依据正文生成 --tapd-handoff 交付清单')
    try:
        value = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise TapdError('TAPD 交付清单不可读取或 JSON 格式错误') from None
    keys = {'workspace_id', 'story_id', 'summary', 'verification', 'fields', 'sql', 'menus'}
    if not isinstance(value, dict) or set(value) != keys:
        raise TapdError('交付清单必须包含 workspace_id/story_id/summary/verification/fields/sql/menus，不能含未知字段')
    workspace, story = target(url)
    if (not isinstance(value['workspace_id'], str) or not value['workspace_id'].isdigit()
            or not isinstance(value['story_id'], str) or value['story_id'] != story
            or (workspace is not None and workspace != value['workspace_id'])):
        raise TapdError('交付清单与 TAPD 关联身份不一致，ID 必须为字符串')
    for key in ('summary', 'verification'):
        _text(value[key], key)
    if not isinstance(value['fields'], dict):
        raise TapdError('fields 必须是中文标签到字段值的对象')
    for key, required in (('sql', {'sql', 'environment', 'status', 'notes'}),
                          ('menus', {'name', 'parent', 'route', 'permissions', 'status'})):
        if not isinstance(value[key], list):
            raise TapdError(key + ' 必须是列表，无变更时填空列表')
        for entry in value[key]:
            if not isinstance(entry, dict) or set(entry) != required:
                raise TapdError(key + ' 条目的字段不完整或包含未知字段')
            for field in required:
                _text(entry[field], key + '.' + field)
    # 这些标签直接来自明确交接条目，不从正文关键词推断是否有变更。
    operations = value['fields'].get('发布关联操作', [])
    if not isinstance(operations, list):
        raise TapdError('发布关联操作必须是选项列表')
    operations = list(operations)
    for key, option in (('sql', '需要执行SQL'), ('menus', '需配置菜单')):
        if value[key] and option not in operations:
            operations.append(option)
    if operations:
        if '无' in operations and len(operations) > 1:
            raise TapdError('发布关联操作不能同时选择“无”和实际操作')
        value['fields']['发布关联操作'] = operations
    return value


def _story(client, workspace, story):
    rows = client.get('stories', {'workspace_id': workspace, 'id': story})
    found = [row['Story'] for row in rows if isinstance(row, dict) and isinstance(row.get('Story'), dict)
             and str(row['Story'].get('id')) == story and str(row['Story'].get('workspace_id')) == workspace]
    if len(found) != 1:
        raise TapdError('无法唯一读取目标 TAPD 需求')
    return found[0]


def prepare(settings, url, manifest_path, client=None):
    if not settings or settings.get('enabled') is not True:
        return None
    manifest = load_manifest(manifest_path, url)
    client = client or TapdClient(settings.get('credentials_path', ''))
    workspace, story_id = manifest['workspace_id'], manifest['story_id']
    story = _story(client, workspace, story_id)
    fields = client.get('stories/get_fields_info', {'workspace_id': workspace})
    plan = plan_fields(fields, story, manifest['fields'])
    nick = client.get('users/info', {}).get('nick')
    if not isinstance(nick, str) or not nick.strip():
        raise TapdError('无法确认个人令牌的评论作者身份')
    return Handoff(client, manifest, plan, nick)


def _safe(value):
    return html.escape(str(value), quote=True).replace('@', '＠')


class Handoff:
    def __init__(self, client, manifest, plan, author):
        self.client, self.manifest, self.plan, self.author = client, manifest, plan, author

    def preview(self):
        return {'status': 'planned', 'workspace_id': self.manifest['workspace_id'],
                'story_id': self.manifest['story_id'], 'fields': self.plan,
                'sql_count': len(self.manifest['sql']), 'menu_count': len(self.manifest['menus']),
                'complete_step': False}

    def _description(self, doc, marker):
        m = self.manifest
        parts = ['<h3>开发交付记录</h3>', '<p><a href="' + _safe(doc['url']) + '">'
                 + _safe(doc['title']) + '</a></p>', '<p>' + _safe(m['summary']) + '</p>',
                 '<p>验证情况：' + _safe(m['verification']) + '</p>']
        for label, value in m['fields'].items():
            text = '、'.join(value) if isinstance(value, list) else str(value)
            parts.append('<p>' + _safe(label) + '：' + _safe(text) + '</p>')
        if m['sql']:
            parts.append('<h4>SQL 变更</h4>')
        for entry in m['sql']:
            parts += ['<p>环境：' + _safe(entry['environment']) + '；状态：' + _safe(entry['status']) + '</p>',
                      '<pre><code>' + _safe(entry['sql']) + '</code></pre>', '<p>' + _safe(entry['notes']) + '</p>']
        if m['menus']:
            parts.append('<h4>菜单添加</h4>')
        for entry in m['menus']:
            parts.append('<p>' + '；'.join(_safe(label) + '：' + _safe(entry[key]) for key, label in (
                ('name', '菜单'), ('parent', '父级'), ('route', '路由'), ('permissions', '权限'), ('status', '状态'))) + '</p>')
        parts += ['<p>以上为交付说明，字段同步及节点结果以分别读回的状态为准。</p>', '<p>' + marker + '</p>']
        return '\n'.join(parts)

    def _find_comment(self, marker):
        m = self.manifest
        found = []
        for page in range(1, 51):
            rows = self.client.get('comments', {'workspace_id': m['workspace_id'], 'entry_type': 'stories',
                'entry_id': m['story_id'], 'page': page, 'limit': 200, 'order': 'id desc',
                'fields': 'id,entry_id,entry_type,workspace_id,description,author'})
            if not isinstance(rows, list):
                raise TapdError('评论读回格式异常')
            for row in rows:
                item = row.get('Comment', {}) if isinstance(row, dict) else {}
                if (str(item.get('workspace_id')) == m['workspace_id'] and str(item.get('entry_id')) == m['story_id']
                        and item.get('entry_type') == 'stories' and item.get('author') == self.author
                        and marker in str(item.get('description', ''))):
                    found.append(str(item['id']))
            if len(rows) < 200:
                if len(found) > 1:
                    raise TapdError('发现多个相同版本的 TAPD 评论，需核对后继续')
                return found[0] if found else None
        raise TapdError('评论分页超出核查范围，停止新增以避免重复')

    def sync(self, state, doc, source_hash):
        """调用方须持有 State 锁；每次写请求之前先落盘，崩溃后仅核对。"""
        signature = json.dumps([doc['docid'], source_hash, self.manifest], ensure_ascii=False, sort_keys=True)
        key = hashlib.sha256(signature.encode()).hexdigest()
        marker = 'TAPD-HANDOFF:' + key
        events = state.data.setdefault('tapd_handoffs', {})
        event = events.setdefault(key, {'workspace_id': self.manifest['workspace_id'],
            'story_id': self.manifest['story_id'], 'docid': doc['docid'], 'source_hash': source_hash,
            'comment_status': 'queued', 'fields_status': 'queued', 'field_plan': self.plan})
        state.save()
        try:
            if event['comment_status'] != 'verified':
                comment_id = self._find_comment(marker)
                if comment_id:
                    event.update(comment_status='verified', comment_id=comment_id)
                    state.save()
                elif event['comment_status'] in ('sending', 'unknown'):
                    event['comment_status'] = 'unknown'
                    state.save()
                    return self._result(event)
                else:
                    event['comment_status'] = 'sending'
                    state.save()
                    try:
                        self.client.post('comments', {'workspace_id': self.manifest['workspace_id'],
                            'entry_type': 'stories', 'entry_id': self.manifest['story_id'], 'author': self.author,
                            'description': self._description(doc, marker)})
                    except TapdError as exc:
                        event['comment_status'] = 'unknown' if exc.uncertain else 'rejected'
                        state.save()
                        raise
                    event['comment_status'] = 'unknown'
                    state.save()
                    comment_id = self._find_comment(marker)
                    if not comment_id:
                        return self._result(event)
                    event.update(comment_status='verified', comment_id=comment_id)
                    state.save()
            if (event['fields_status'] not in ('sending', 'unknown')
                    and (event['fields_status'] != 'verified' or event.get('field_conflicts')
                         or event['field_plan']['conflicts'])):
                # 确切失败或部分冲突可用新预检恢复；未知写入仍保留首次计划核对。
                event.update(field_plan=self.plan, field_conflicts=self.plan['conflicts'], fields_status='queued')
                state.save()
            if event['fields_status'] != 'verified':
                plan = event['field_plan']
                if event['fields_status'] in ('sending', 'unknown'):
                    current = _story(self.client, self.manifest['workspace_id'], self.manifest['story_id'])
                    metadata = self.client.get('stories/get_fields_info', {'workspace_id': self.manifest['workspace_id']})
                    applied = {metadata[name]['label']: self.manifest['fields'][metadata[name]['label']]
                               for name in plan['updates']}
                    check = plan_fields(metadata, current, applied)
                    if check['updates'] or check['conflicts']:
                        event['fields_status'] = 'unknown'
                        state.save()
                        return self._result(event)
                    event['fields_status'] = 'verified'
                else:
                    def before_write():
                        event['fields_status'] = 'sending'
                        state.save()
                    try:
                        result = update_fields(self.client, self.manifest['workspace_id'], self.manifest['story_id'],
                                               plan, execute=True, before_write=before_write)
                    except TapdError as exc:
                        event['fields_status'] = 'unknown' if exc.uncertain else 'rejected'
                        state.save()
                        raise
                    event['fields_status'] = result['status']
                    event['field_conflicts'] = result.get('conflicts', {})
                state.save()
            event.pop('error', None)
        except TapdError as exc:
            event['error'] = str(exc)
            state.save()
        return self._result(event)

    def _result(self, event):
        states = (event['comment_status'], event['fields_status'])
        status = 'verified' if states == ('verified', 'verified') else 'partial'
        conflicts = event.get('field_conflicts', event['field_plan']['conflicts'])
        if conflicts and status == 'verified':
            status = 'partial'
        if 'unknown' in states or 'sending' in states:
            status = 'unknown'
        return {'status': status, 'comment': event['comment_status'], 'fields': event['fields_status'],
                'comment_id': event.get('comment_id'), 'field_conflicts': conflicts,
                'error': event.get('error'), 'complete_step': False}
