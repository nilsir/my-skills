#!/usr/bin/env python3
"""独立读取 TAPD 开发节点；完成节点必须明确传入 --execute。"""
import argparse
import hashlib
import json
from pathlib import Path

from publisher import State
from tapd_client import FIELD_TYPES, TapdClient, TapdError, _step_requirements, complete_step, inspect_story
from tapd_handoff import target


def _node(info, requested):
    mappings = [step for group in info['step_map'] for step in group.get('steps', [])]
    exact = [step for step in mappings if step.get('name') == requested]
    matches = exact or [step for step in mappings if step.get('label') == requested]
    if len(matches) != 1:
        raise TapdError('节点名称不存在或不唯一')
    name = matches[0]['name']
    rows = [row['WorkitemStepInfo'] for row in info['steps'] if row['WorkitemStepInfo']['step'] == name]
    if len(rows) != 1:
        raise TapdError('目标需求不存在唯一的指定节点')
    return rows[0]


def inspect_result(info):
    story = info['story']
    fields = {name: {key: meta.get(key) for key in ('label', 'html_type', 'options', 'readonly')}
              for name, meta in info['fields'].items() if meta.get('label') in FIELD_TYPES}
    for name, metadata in fields.items():
        metadata['current_value'] = story.get(name)
    labels = {step['name']: step.get('label') for group in info['step_map'] for step in group.get('steps', [])}
    nodes = [{key: row['WorkitemStepInfo'].get(key) for key in ('step', 'status', 'begin_time', 'complete_time')}
             for row in info['steps']]
    for node in nodes:
        node['label'] = labels.get(node['step'])
    checks = []
    for node in nodes:
        if str(node['status']) == '0':
            name, conflicts = _step_requirements(info, node['step'])
            checks.append({'step': name, 'label': node['label'], 'conflicts': conflicts})
    return {'status': 'read_only', 'story': {key: story.get(key) for key in ('id', 'workspace_id', 'name', 'status', 'step')},
            'fields': fields, 'nodes': nodes, 'active_node_checks': checks, 'write_permissions_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials', required=True, help='本机个人令牌 JSON 文件路径')
    parser.add_argument('--state', help='独立节点操作记录路径；默认位于凭据文件旁')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'complete-step'):
        command = commands.add_parser(name)
        command.add_argument('--tapd', required=True)
        command.add_argument('--workspace', help='预览链接不带项目 ID 时必须明确指定')
        if name == 'complete-step':
            command.add_argument('--step', required=True)
            command.add_argument('--evidence-file', required=True, help='已有完成证据文本，不能只是完成意愿')
            command.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    result = None
    try:
        workspace, story_id = target(args.tapd)
        if workspace and args.workspace and workspace != args.workspace:
            raise TapdError('链接和指定项目不一致')
        workspace = workspace or args.workspace
        if not workspace or not workspace.isdigit():
            raise TapdError('预览链接必须通过 --workspace 明确项目 ID')
        evidence = None
        if args.command == 'complete-step':
            evidence = Path(args.evidence_file).expanduser().read_text(encoding='utf-8')
            if not evidence.strip():
                raise TapdError('节点完成证据不能为空')
        client = TapdClient(args.credentials)
        info = inspect_story(client, workspace, story_id)
        if args.command == 'inspect':
            result = inspect_result(info)
        elif not args.execute:
            result = complete_step(client, workspace, story_id, args.step, evidence, execute=False)
        else:
            node = _node(info, args.step)
            key = hashlib.sha256(json.dumps([workspace, story_id, node['step'], node.get('begin_time')]).encode()).hexdigest()
            state = State(args.state or Path(args.credentials).expanduser().parent / 'node-state.json')
            with state.locked(write=True):
                events = state.data.setdefault('tapd_steps', {})
                event = events.setdefault(key, {'status': 'queued', 'workspace_id': workspace,
                    'story_id': story_id, 'step': node['step'], 'begin_time': node.get('begin_time'),
                    'evidence_sha256': hashlib.sha256(evidence.encode()).hexdigest()})
                if str(node['status']) == '1':
                    event['status'] = 'verified'
                    result = {'status': 'verified', 'step': node['step'], 'reconciled': True}
                elif event['status'] in ('sending', 'unknown'):
                    event['status'] = 'unknown'
                    result = {'status': 'unknown', 'step': node['step'],
                              'message': '上次节点请求结果尚未确认，本次仅读回，未重复写入'}
                elif event['status'] == 'verified':
                    result = {'status': 'conflict', 'message': '已完成记录与当前节点状态不同，请核对流程是否被重置'}
                else:
                    def before_write():
                        event['status'] = 'sending'
                        state.save()
                    try:
                        result = complete_step(client, workspace, story_id, node['step'], evidence,
                                               execute=True, before_write=before_write)
                        event['status'] = result['status']
                    except TapdError as exc:
                        event['status'] = 'unknown' if exc.uncertain else 'rejected'
                        result = {'status': event['status'], 'message': str(exc), 'step': node['step']}
                state.save()
    except TapdError as exc:
        result = {'status': 'unknown' if exc.uncertain else 'error', 'message': str(exc)}
    except (OSError, ValueError, KeyError, TypeError):
        result = {'status': 'error', 'message': '本机输入文件或 TAPD 返回数据无效，未确认操作完成'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in ('read_only', 'planned', 'verified') else 1


if __name__ == '__main__':
    raise SystemExit(main())
