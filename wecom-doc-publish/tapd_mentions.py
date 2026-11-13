#!/usr/bin/env python3
"""只读解析 TAPD 当前需求及直接父子分工，生成企业微信真实 @ 行。"""
import argparse
import json
from pathlib import Path
import re

from publisher import WeComCli, decoded
from tapd_client import TapdClient, _story
from tapd_handoff import target


ROLES = {'前端人员': '前端', '后端人员': '后端', '测试人员': '测试'}
ROLE_ORDER = ('前端', '后端', '测试', '开发')
STORY_ID = r'[1-9][0-9]{18}'
EMAIL = re.compile(r'[^\s<>;；@]+@[^\s<>;；@]+')
# 人员字段保守识别 7–15 位号码及国际拨号前缀，不对需求 ID 使用此规则。
PHONE = re.compile(r'(?<![0-9])(?:\+|00)?[0-9](?:[\s()./_-]*[0-9]){6,14}(?![0-9])')


class MentionError(Exception):
    """只用于本模块的固定错误文案，不接受远端响应。"""


def _error(message):
    return {'status': 'error', 'message': message, 'scope': None, 'people': [],
            'matched': [], 'unresolved': [], 'userids': [], 'mention_markdown': ''}


def _display(name):
    if EMAIL.search(name) or PHONE.search(name):
        return '[人员名称已脱敏]'
    return re.sub(r'[\x00-\x1f\x7f]', ' ', name).replace('<', '＜').replace('>', '＞').replace('@', '＠')


def _valid_userid(value):
    return (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.@-]{1,64}', value) is not None
            and value.lower() not in ('all', '@all') and not EMAIL.search(value) and not PHONE.search(value))


def _parent(story):
    value = story.get('parent_id')
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch('0|' + STORY_ID, str(value)):
        raise MentionError('TAPD 父需求 ID 格式未知')
    if str(value) == str(story['id']):
        raise MentionError('TAPD 父子关系存在循环')
    return str(value)


def _children(story):
    value = story.get('children_id')
    if value in ('', '|'):
        return []
    if not isinstance(value, str) or not re.fullmatch(r'\|\|' + STORY_ID + r'(?:\|' + STORY_ID + ')*', value):
        raise MentionError('TAPD 子需求列表格式未知')
    children = value[2:].split('|')
    if len(children) > 50:
        raise MentionError('TAPD 直接子需求超过 50 个，未截断或生成提醒')
    if len(set(children)) != len(children) or str(story['id']) in children:
        raise MentionError('TAPD 子需求列表重复或存在循环')
    return children


def _scope(client, workspace, story_id):
    current = _story(client, workspace, story_id)
    parent_id = _parent(current)
    root = current if parent_id == '0' else _story(client, workspace, parent_id)
    root_id = str(root['id'])
    root_parent = _parent(root)
    children = _children(root)
    if parent_id != '0' and story_id not in children:
        raise MentionError('当前需求不在直接父需求的子需求列表中')
    if root_parent in children:
        raise MentionError('TAPD 父子关系存在循环')
    stories = [root]
    for child_id in children:
        child = current if child_id == story_id else _story(client, workspace, child_id)
        if _parent(child) != root_id:
            raise MentionError('TAPD 子需求的父级与范围根不一致')
        # 不读取孙级，但已返回的关系不能指回根或同级，也不能隐瞒未知格式。
        if set(_children(child)) & {root_id, *children, root_parent}:
            raise MentionError('TAPD 子需求关系存在循环或同级冲突')
        stories.append(child)
    return stories, {'workspace_id': workspace, 'current_story_id': story_id,
                     'root_story_id': root_id, 'story_ids': [str(story['id']) for story in stories]}


def _fields(metadata):
    if not isinstance(metadata, dict):
        raise MentionError('TAPD 人员字段元数据格式未知')
    fields = {}
    for key, item in metadata.items():
        if not isinstance(item, dict) or item.get('label') not in (*ROLES, '开发人员', '处理人'):
            continue
        if item.get('html_type') != 'user_chooser':
            continue
        label, name = item['label'], item.get('name', key)
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', name) or label in fields:
            raise MentionError('TAPD 人员字段名称无效或中文标签不唯一')
        fields[label] = name
    return fields


def _people(stories, fields):
    people = {}
    for story in stories:
        title = story.get('name', '')
        prefix = re.match(r'^【(前端[^】\r\n]*任务|后端[^】\r\n]*任务|测试任务)】', title) if isinstance(title, str) else None
        role = prefix.group(1)[:2] if prefix else None
        for label, field in fields.items():
            assigned_role = ROLES.get(label) or (role if label == '处理人' else role or '开发')
            if label == '处理人' and not role:
                continue
            value = story.get(field)
            if value in (None, ''):
                continue
            if not isinstance(value, str):
                raise MentionError('TAPD 人员字段不是分号分隔的用户名文本')
            for name in (name.strip() for name in re.split('[;；]', value)):
                if not name:
                    continue
                person = people.setdefault(name, {'tapd_name': _display(name), 'roles': [], 'sources': []})
                if assigned_role not in person['roles']:
                    person['roles'].append(assigned_role)
                source = {'story_id': str(story['id']), 'field': field, 'label': label, 'role': assigned_role}
                if source not in person['sources']:
                    person['sources'].append(source)
    for person in people.values():
        roles = person['roles']
        person['roles'] = [role for role in ROLE_ORDER if role in roles and (role != '开发' or len(roles) == 1)]
    return people


def _excluded(name, excluded):
    for label in excluded:
        if name == label:
            return True
        if (re.fullmatch(r'[\u3400-\u9fff]+', label) and name.endswith(label)
                and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*\s*', name[:-len(label)])):
            return True
    return False


def _contacts(value):
    value = decoded(value)
    if isinstance(value, dict):
        if 'userid' in value:
            yield value
        for child in value.values():
            yield from _contacts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _contacts(child)


def _matched(person, userid, source):
    if _valid_userid(userid):
        person.update(userid=userid, match_source=source)
    else:
        person.update(reason='invalid_userid', detail='需要有效的企业 userid，不能使用邮箱、手机号或广播标记')


def _resolve(people, config, state, cli):
    settings = config.get('tapd_mentions', {})
    explicit = settings.get('userids', {})
    cached = [config.get('resolved_members', {}), state.get('resolved_members', {})]
    excluded = config.get('member_lookup_excluded', [])
    if (not all(isinstance(value, dict) for value in [explicit, *cached]) or not isinstance(excluded, list)
            or any(not isinstance(name, str) or not name for name in excluded)):
        raise MentionError('本机人员映射或禁止查询名单格式无效')
    pending = []
    for name, person in people.items():
        if name in explicit:
            _matched(person, explicit[name], 'tapd_mentions.userids')
            continue
        values = [cache[name] for cache in cached if name in cache]
        if values:
            if any(value != values[0] for value in values[1:]):
                person['reason'] = 'mapping_conflict'
            else:
                _matched(person, values[0], 'resolved_members')
        elif _excluded(name, excluded):
            person['reason'] = 'lookup_excluded'
        else:
            pending.append(name)
    for start in range(0, len(pending), 10):
        batch = pending[start:start + 10]
        try:
            # 这是本模块唯一的企微调用；不保存查找结果，也不调整文档成员权限。
            result = cli.call(('contact', 'users', 'search'), {'keywords': batch})
            candidates = list(_contacts(result))
        except Exception:
            for name in batch:
                people[name]['reason'] = 'lookup_failed'
            continue
        for name in batch:
            matches = []
            for candidate in candidates:
                aliases = candidate.get('alias', [])
                labels = [candidate.get('name')] + (aliases if isinstance(aliases, list) else [aliases])
                if name in labels:
                    matches.append(candidate['userid'])
            if not matches:
                people[name]['reason'] = 'no_exact_contact'
            elif any(not _valid_userid(userid) for userid in matches):
                _matched(people[name], None, 'contact_search')
            elif len(set(matches)) != 1:
                people[name]['reason'] = 'ambiguous_contact'
            else:
                _matched(people[name], matches[0], 'contact_search')


def resolve_mentions(config, url, workspace=None, client=None, cli=None, state=None):
    """返回只读结果；客户端和状态可注入，不修改配置、状态或任何远端对象。"""
    try:
        linked_workspace, story_id = target(url)
        if linked_workspace and workspace is not None and linked_workspace != str(workspace):
            raise MentionError('链接和指定项目不一致')
        workspace = linked_workspace or workspace
        if (not isinstance(workspace, str) or not re.fullmatch(r'[1-9][0-9]*', workspace)
                or not re.fullmatch(STORY_ID, story_id)):
            raise MentionError('预览链接必须通过 --workspace 明确项目 ID，需求 ID 必须完整')
        if not isinstance(config, dict) or not isinstance(config.get('tapd_mentions', {}), dict):
            raise MentionError('本机配置格式无效')
        if state is None:
            path = Path(config['state_path']).expanduser() if config.get('state_path') else None
            state = json.loads(path.read_text(encoding='utf-8')) if path and path.exists() else {}
        if not isinstance(state, dict):
            raise MentionError('本机状态格式无效')
        if client is None:
            client = TapdClient(config.get('tapd_sync', {}).get('credentials_path', ''))
        stories, scope = _scope(client, workspace, story_id)
        fields = _fields(client.get('stories/get_fields_info', {'workspace_id': workspace}))
        people = _people(stories, fields)
        cli = cli if cli is not None else WeComCli(config.get('cli_path', 'wecom-cli'))
        _resolve(people, config, state, cli)
        matched = [person for person in people.values() if 'userid' in person]
        unresolved = [person for person in people.values() if 'userid' not in person]
        userids = list(dict.fromkeys(person['userid'] for person in matched))
        return {'status': 'partial' if unresolved else 'read_only', 'scope': scope,
                'people': list(people.values()), 'matched': matched, 'unresolved': unresolved,
                'userids': userids,
                'mention_markdown': '**相关人员：** ' + ' '.join('<@' + userid + '>' for userid in userids) if userids else ''}
    except MentionError as error:
        return _error(str(error))
    except Exception:
        # 外部库的异常可能回显响应、用户名、手机号或凭据，不透传原始异常。
        return _error('TAPD 读取或本机输入无效，未生成提醒；请核对链接、权限与配置')


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise MentionError('命令参数无效；请提供 --config、--tapd，预览链接还需 --workspace')


def main(argv=None, client=None, cli=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--tapd', required=True)
    parser.add_argument('--workspace', help='预览链接必须明确指定项目 ID')
    try:
        args = parser.parse_args(argv)
        config_path = Path(args.config).expanduser().resolve()
        config = json.loads(config_path.read_text(encoding='utf-8'))
        if config.get('state_path'):
            config['state_path'] = str(config_path.parent / Path(config['state_path']).expanduser())
        settings = config.get('tapd_sync', {})
        if settings.get('credentials_path'):
            settings['credentials_path'] = str(config_path.parent / Path(settings['credentials_path']).expanduser())
        result = resolve_mentions(config, args.tapd, args.workspace, client=client, cli=cli)
    except MentionError as error:
        result = _error(str(error))
    except Exception:
        result = _error('本机配置无法读取或格式无效，未生成提醒')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'read_only' else 1


if __name__ == '__main__':
    raise SystemExit(main())
