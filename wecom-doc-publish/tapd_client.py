"""固定官方地址的 TAPD 客户端及可预览、可核对的交付操作。"""

from decimal import Decimal, InvalidOperation
from http.client import HTTPException
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class TapdError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 禁止认证头随重定向发送到其他地址，包括同域重定向。
        raise HTTPError('https://api.tapd.cn', code, '禁止重定向', {}, None)


class TapdClient:
    GET_ROUTES = frozenset({'stories', 'stories/get_fields_info', 'stories/get_story_step_list',
                            'workflows/step_map', 'workflows/all_transitions', 'comments', 'users/info'})
    POST_ROUTES = frozenset({'stories', 'stories/update_story_step_status', 'comments'})

    def __init__(self, credentials_path, opener=None):
        try:
            credentials = json.loads(Path(credentials_path).expanduser().read_text(encoding='utf-8'))
            token = credentials.get('access_token')
            if not isinstance(token, str) or not token.strip() or any(char.isspace() for char in token):
                raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError):
            raise TapdError('TAPD 凭据文件无效或无法读取') from None
        self._token = token
        self._opener = opener if opener is not None else build_opener(_NoRedirect())

    def get(self, route, params):
        return self._request('GET', route, params)

    def post(self, route, data):
        return self._request('POST', route, data)

    def _request(self, method, route, values):
        allowed = self.GET_ROUTES if method == 'GET' else self.POST_ROUTES
        if not isinstance(route, str) or route not in allowed:
            raise TapdError('不允许调用此 TAPD 接口')
        if not isinstance(values, dict) or any(not isinstance(key, str) for key in values):
            raise TapdError('TAPD 请求参数必须为键值对象')
        if any(key in values for key in ('access_token', 'Authorization', 's', '__format')):
            raise TapdError('不允许覆盖 TAPD 认证或响应参数')
        try:
            encoded = urlencode(values).encode('utf-8')
            query = 's=mcp'
            if method == 'GET' and encoded:
                query += '&' + encoded.decode('ascii')
            request = Request('https://api.tapd.cn/' + route + '?' + query,
                              data=encoded if method == 'POST' else None, method=method,
                              headers={'Authorization': 'Bearer ' + self._token, 'Via': 'mcp',
                                       'Accept': 'application/json',
                                       'Content-Type': 'application/x-www-form-urlencoded'})
        except (ValueError, TypeError, UnicodeError):
            raise TapdError('TAPD 请求参数无法编码') from None
        writing = method == 'POST'
        try:
            # 不自动重试；写入超时必须交由持久事件读回核对。
            with self._opener.open(request, timeout=30) as response:
                status = response.status
                if not 200 <= status < 300:
                    raise HTTPError('https://api.tapd.cn', status, '请求失败', {}, None)
                envelope = json.loads(response.read().decode('utf-8'))
        except HTTPError as error:
            error.close()
            raise TapdError('TAPD HTTP 请求失败（状态码 ' + str(error.code) + '）',
                            uncertain=writing and (error.code >= 500 or 300 <= error.code < 400)) from None
        except (OSError, URLError, TimeoutError, HTTPException):
            raise TapdError('TAPD 网络请求失败，需核对远端结果', uncertain=writing) from None
        except (ValueError, UnicodeError, TypeError):
            raise TapdError('TAPD 返回格式无效，需核对远端结果', uncertain=writing) from None
        if not isinstance(envelope, dict) or 'status' not in envelope:
            raise TapdError('TAPD 返回格式无效，需核对远端结果', uncertain=writing)
        if envelope['status'] not in (1, '1'):
            # 不输出 info 或原始响应，避免远端回显认证材料或个人信息。
            raise TapdError('TAPD 拒绝请求，请核对权限及参数')
        if 'data' not in envelope:
            raise TapdError('TAPD 返回缺少数据，需核对远端结果', uncertain=writing)
        return envelope['data']


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise TapdError('TAPD 对象 ID 必须为完整的正整数字符串')
    return str(value)


def _story(client, workspace_id, story_id):
    workspace_id, story_id = _id(workspace_id), _id(story_id)
    data = client.get('stories', {'workspace_id': workspace_id, 'id': story_id})
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise TapdError('TAPD 未返回唯一的目标需求')
    story = data[0].get('Story')
    if not isinstance(story, dict) or str(story.get('id')) != story_id or str(story.get('workspace_id')) != workspace_id:
        raise TapdError('TAPD 需求身份与目标项目或需求不匹配')
    return story


def _steps(client, workspace_id, story_id):
    data = client.get('stories/get_story_step_list', {'workspace_id': workspace_id, 'story_id': story_id})
    if not isinstance(data, list):
        raise TapdError('TAPD 节点信息格式未知')
    names = set()
    for item in data:
        item = item.get('WorkitemStepInfo') if isinstance(item, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get('step'), str) or not item['step']:
            raise TapdError('TAPD 节点信息格式未知')
        if (str(item.get('workspace_id')) != workspace_id or str(item.get('workitem_id')) != story_id
                or item.get('entity_type') != 'story'):
            raise TapdError('TAPD 节点不属于目标需求')
        if item['step'] in names or str(item.get('status')) not in ('-1', '0', '1', '90'):
            raise TapdError('TAPD 节点状态不唯一或未知')
        names.add(item['step'])
    return data


def inspect_story(client, workspace_id, story_id):
    workspace_id, story_id = _id(workspace_id), _id(story_id)
    story = _story(client, workspace_id, story_id)
    workitem_type_id = _id(story.get('workitem_type_id'))
    fields = client.get('stories/get_fields_info', {'workspace_id': workspace_id})
    if not isinstance(fields, dict):
        raise TapdError('TAPD 字段信息格式未知')
    steps = _steps(client, workspace_id, story_id)
    params = {'workspace_id': workspace_id, 'system': 'story', 'workitem_type_id': workitem_type_id}
    step_map = client.get('workflows/step_map', params)
    transitions = client.get('workflows/all_transitions', params)
    if not isinstance(step_map, list) or not isinstance(transitions, list):
        raise TapdError('TAPD 工作流规则格式未知')
    return {'story': story, 'fields': fields, 'steps': steps, 'step_map': step_map, 'transitions': transitions}


FIELD_TYPES = {'涉及端口': 'multi_select', '需求关联系统': 'multi_select', '发布关联操作': 'multi_select',
               '代码分支': 'textarea', '实际工时（人/日）': 'float'}


def _empty(value):
    return value is None or value == '' or isinstance(value, str) and not value.strip() or value == []


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)) or _empty(value):
        raise TapdError('工时必须明确填写有限的非负数')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise TapdError('工时必须明确填写有限的非负数') from None
    if not number.is_finite() or number < 0:
        raise TapdError('工时必须明确填写有限的非负数')
    return format(number.normalize(), 'f')


def _serialize(metadata, value):
    kind = metadata['html_type']
    if kind == 'float':
        return _number(value)
    if kind == 'textarea':
        if not isinstance(value, str) or not value.strip():
            raise TapdError('代码分支必须为非空文本')
        return value
    if not isinstance(value, list) or not value:
        raise TapdError('多选字段必须提供非空选项列表')
    options = metadata.get('options')
    if not isinstance(options, dict) or not options:
        raise TapdError('多选候选值格式未知')
    selected = []
    for choice in value:
        if not isinstance(choice, str) or not choice or '|' in choice:
            raise TapdError('多选字段包含非法选项')
        matches = [str(key) for key, label in options.items() if choice == str(key) or choice == label]
        if len(matches) != 1 or not matches[0] or '|' in matches[0]:
            raise TapdError('多选字段包含未知或不唯一的选项')
        if matches[0] not in selected:
            selected.append(matches[0])
    # 实际 GET stories 的多选返回值已只读确认使用竖线，如 API|Admin。
    return '|'.join(selected)


def _equivalent(actual, expected, kind=None):
    if kind == 'multi_select':
        return isinstance(actual, str) and set(actual.split('|')) == set(expected.split('|'))
    if kind == 'float':
        try:
            return _number(actual) == _number(expected)
        except TapdError:
            return False
    return actual == expected


def plan_fields(fields_info, story, labels):
    if not isinstance(fields_info, dict) or not isinstance(story, dict) or not isinstance(labels, dict):
        raise TapdError('字段计划输入格式无效')
    plan = {'updates': {}, 'expected': {}, 'conflicts': {}}
    for label, value in labels.items():
        try:
            if label not in FIELD_TYPES:
                raise TapdError('不在允许回填的交付字段范围内')
            candidates = [item for item in fields_info.values() if isinstance(item, dict) and item.get('label') == label]
            if len(candidates) != 1:
                raise TapdError('字段标签不存在或不唯一')
            metadata = candidates[0]
            name = metadata.get('name')
            if not isinstance(name, str) or not re.fullmatch(r'custom_field_[A-Za-z0-9_]+', name):
                raise TapdError('交付字段内部名称无效')
            if metadata.get('readonly') not in (False, 0, '0'):
                raise TapdError('字段只读或可写权限未知')
            if metadata.get('html_type') != FIELD_TYPES[label]:
                raise TapdError('字段类型与交付约定不符')
            if name not in story:
                raise TapdError('需求未返回字段原值，不能安全回填')
            serialized = _serialize(metadata, value)
            old = story[name]
            if _equivalent(old, serialized, metadata['html_type']):
                continue
            if not _empty(old):
                raise TapdError('已有非空值与本次内容不同，未覆盖')
            plan['updates'][name] = serialized
            plan['expected'][name] = old
        except TapdError as error:
            plan['conflicts'][label] = str(error)
    return plan


def _checkpoint(before_write):
    if not callable(before_write):
        raise TapdError('执行写入前必须提供持久化回调')
    before_write()


def update_fields(client, workspace_id, story_id, plan, execute=False, before_write=None):
    workspace_id, story_id = _id(workspace_id), _id(story_id)
    if not isinstance(plan, dict) or any(not isinstance(plan.get(key), dict) for key in ('updates', 'expected', 'conflicts')):
        raise TapdError('字段计划格式无效')
    updates, expected = plan['updates'], plan['expected']
    if (set(updates) != set(expected) or any(not isinstance(name, str) or not re.fullmatch(r'custom_field_[A-Za-z0-9_]+', name)
                                            or not isinstance(value, str) for name, value in updates.items())):
        raise TapdError('字段计划包含非法或未经核对的更新')
    result = {'status': 'planned', 'updates': dict(updates), 'conflicts': dict(plan['conflicts'])}
    if not updates:
        result['status'] = 'conflict' if result['conflicts'] else ('verified' if execute else 'planned')
        return result
    current = _story(client, workspace_id, story_id)
    changed = [name for name, value in expected.items() if name not in current or current[name] != value]
    if changed:
        result['status'] = 'conflict'
        result['conflicts'].update({name: '字段在计划后发生变化，未写入' for name in changed})
        return result
    metadata = client.get('stories/get_fields_info', {'workspace_id': workspace_id})
    if not isinstance(metadata, dict):
        raise TapdError('TAPD 字段信息格式未知')
    # 计划可能跨进程保存；执行时再验证字段权限、候选值和交付范围。
    kinds = {}
    for name, value in updates.items():
        matches = [item for item in metadata.values() if isinstance(item, dict) and item.get('name') == name]
        if len(matches) != 1:
            result['conflicts'][name] = '执行时无法唯一定位字段'
            continue
        item = matches[0]
        kind = item.get('html_type')
        labels = {item.get('label'): value.split('|') if kind == 'multi_select' else value}
        checked = plan_fields(metadata, current, labels)
        if checked['conflicts'] or checked['updates'].get(name) != value:
            result['conflicts'][name] = '执行时字段范围、权限或候选值已不符合计划'
        else:
            kinds[name] = kind
    if len(kinds) != len(updates):
        result['status'] = 'conflict'
        return result
    if not execute:
        return result
    _checkpoint(before_write)
    client.post('stories', dict(updates, workspace_id=workspace_id, id=story_id))
    try:
        actual = _story(client, workspace_id, story_id)
    except TapdError:
        raise TapdError('字段请求已发送，但读回失败，需人工核对', uncertain=True) from None
    mismatched = [name for name, value in updates.items() if not _equivalent(actual.get(name), value, kinds[name])]
    if mismatched:
        raise TapdError('字段请求已发送，但写后读回与目标值不符，需核对', uncertain=True)
    result['status'] = 'verified'
    return result


def _step_requirements(info, requested):
    mapping = []
    for state in info['step_map']:
        if not isinstance(state, dict) or not isinstance(state.get('steps'), list):
            return None, ['节点映射规则未知']
        for item in state['steps']:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str):
                return None, ['节点映射规则未知']
            mapping.append((item['name'], item.get('label'), state.get('name')))
    internal = [item for item in mapping if item[0] == requested]
    matches = internal or [item for item in mapping if item[1] == requested]
    if len(matches) != 1:
        return None, ['节点名称不存在或不唯一']
    name, _, state = matches[0]
    nodes = [item['WorkitemStepInfo'] for item in info['steps'] if item['WorkitemStepInfo']['step'] == name]
    if len(nodes) != 1 or str(nodes[0]['status']) != '0':
        return name, ['指定节点当前不在进行中']
    relevant = []
    for transition in info['transitions']:
        if not isinstance(transition, dict) or 'StepPrevious' not in transition or 'StepNext' not in transition:
            return name, ['工作流流转规则未知']
        if transition['StepPrevious'] in (name, state):
            relevant.append(transition)
    if not relevant:
        return name, ['未取得当前节点的流转必填规则']
    conflicts = []
    for transition in relevant:
        rules = transition.get('Appendfield')
        if not isinstance(rules, list):
            conflicts.append('工作流必填规则格式未知')
            continue
        for rule in rules:
            if not isinstance(rule, dict) or rule.get('Notnull') not in ('yes', 'no'):
                conflicts.append('工作流必填规则无法确认')
                continue
            if rule.get('LinkageRules'):
                conflicts.append('工作流包含尚不能判断的动态联动规则')
                continue
            if rule['Notnull'] == 'no':
                continue
            field = rule.get('FieldName')
            if rule.get('DBModel') != 'Story' or not isinstance(field, str) or field not in info['story']:
                conflicts.append('工作流存在无法核对的必填字段')
            elif _empty(info['story'][field]):
                metadata = info['fields'].get(field, {})
                label = metadata.get('label', field) if isinstance(metadata, dict) else field
                conflicts.append('缺少必填字段：' + label)
    return name, list(dict.fromkeys(conflicts))


def complete_step(client, workspace_id, story_id, step, evidence, execute=False, before_write=None):
    workspace_id, story_id = _id(workspace_id), _id(story_id)
    if not isinstance(step, str) or not step or not isinstance(evidence, str) or not evidence.strip():
        raise TapdError('节点完成必须指定精确节点及非空完成证据')
    info = inspect_story(client, workspace_id, story_id)
    name, conflicts = _step_requirements(info, step)
    result = {'status': 'conflict' if conflicts else 'planned', 'step': name, 'conflicts': conflicts}
    if conflicts or not execute:
        return result
    _checkpoint(before_write)
    # 只完成这一节点；不传 status，不重置工作流，不关闭后续节点。
    client.post('stories/update_story_step_status', {'workspace_id': workspace_id, 'story_id': story_id, 'step': name})
    try:
        _story(client, workspace_id, story_id)
        steps = _steps(client, workspace_id, story_id)
    except TapdError:
        raise TapdError('节点完成请求已发送，但读回失败，需人工核对', uncertain=True) from None
    matching = [item['WorkitemStepInfo'] for item in steps if item['WorkitemStepInfo']['step'] == name]
    if len(matching) == 1 and str(matching[0]['status']) == '1':
        result['status'] = 'verified'
    else:
        raise TapdError('节点完成请求已发送，但读回未确认指定节点完成', uncertain=True)
    return result
