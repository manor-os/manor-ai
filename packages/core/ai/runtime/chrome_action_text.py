"""Short model-facing action receipts. Raw execution evidence stays unchanged."""

import json

ACTIONS = {
    'click_element', 'fill_or_select', 'click_point', 'type_text', 'press_key', 'keyboard',
    'computer', 'hover', 'scroll', 'scroll_wheel', 'wait', 'upload', 'download', 'wait_download',
    'navigate', 'back', 'forward', 'reload', 'handle_js_dialog',
}
FACTS = (
    'status', 'action_executed', 'do_not_replay', 'verified', 'retryable',
    'tabId', 'acted_tab_id', 'target_tab_id', 'snapshot_id', 'url',
    'read_page_required', 'next_required_tool', 'reason', 'message',
    'action_receipt', 'event_receipt', 'verification', 'pending_verification', 'action_verification',
    'observation_quality', 'visual_fallback_recommended', 'visual_fallback_reasons',
    'dialog', 'js_dialog', 'opened_tab', 'page_blockers', 'page_feedback',
    'website_upload_confirmed', 'selected_files_count', 'selected_file_names',
    'artifact_ready', 'artifact', 'downloadId', 'download_id', 'state',
    'filename', 'path', 'local_path', 'mime', 'file_size', 'bytes_received', 'total_bytes',
    'files', 'file_count',
    'scroll_changed', 'scroll_probe_unavailable',
)
INTERNAL = {
    'semantic_refs', 'refMap', 'candidate_summary', 'candidates', 'selector', 'selector_candidates',
    'bounds', 'pageContent', 'page_text', 'visible_text', 'page_text_sample', 'observation',
    'post_action_page_state', 'target_resolution', 'interaction_trace', '__manorPerformance',
}


def _scalar(value):
    if isinstance(value, str) and '\n' not in value and '\r' not in value:
        return value
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _leaves(key, value, depth=0):
    if depth > 5:
        yield f'{key}: [details omitted]'
    elif isinstance(value, dict):
        for field, child in value.items():
            if field not in INTERNAL:
                yield from _leaves(f'{key}.{field}', child, depth + 1)
    elif isinstance(value, list):
        for index, child in enumerate(value[:8]):
            yield from _leaves(f'{key}[{index}]', child, depth + 1)
        if len(value) > 8:
            yield f'{key}: {len(value) - 8} more entries omitted'
    elif key == 'field.value' and value == '':
        yield f'{key}: ""'
    elif value is not None and value != '':
        yield f'{key}: {_scalar(value)}'


def render_action_result(tool_name, value):
    name = tool_name.removeprefix('mcp__chrome__').removeprefix('browser_')
    if (name not in ACTIONS or not isinstance(value, dict) or value.get('ok') is not True
            or value.get('error') or any(part in str(value.get('status', ''))
                                       for part in ('approval', 'interrupted', 'takeover'))):
        return None
    facts = {key: value[key] for key in FACTS if key in value}
    post = value.get('post_action_page_state') or {}
    if post.get('state_verified') is True:
        if post.get('snapshot_id'):
            facts['snapshot_id'] = post['snapshot_id']
        if post.get('page_status') and post['page_status'] != 'ready':
            facts['page_status'] = post['page_status']
        for key in ('observation_quality', 'visual_fallback_recommended', 'visual_fallback_reasons'):
            if key in post:
                facts[key] = post[key]
        for key in ('page_blockers', 'page_feedback'):
            if not facts.get(key) and post.get(key):
                facts[key] = post[key]
        target = value.get('target_resolution') or {}
        matches = [item for item in post.get('semantic_refs', []) if
                   item.get('selector') == target.get('selector') and
                   (item.get('frame_id') or 0) == (target.get('frame_id') or 0)] if target.get('selector') else []
        if len(matches) == 1:
            facts['field'] = {key: matches[0][key] for key in
                              ('ref', 'role', 'label', 'value', 'checked', 'selected', 'disabled', 'read_only')
                              if key in matches[0]}
            for key in ('disabled', 'read_only'):
                if facts['field'].get(key) is False:
                    del facts['field'][key]
            if matches[0].get('value_truncated') is True or (
                    isinstance(facts['field'].get('value'), str) and len(facts['field']['value']) > 2000):
                facts['field'].pop('value', None)
                facts['field']['value_omitted'] = 'long or bounded value; use read_page to inspect'
    action = value.get('action') or {}
    scroll = {key: action[key] for key in (
        'scrollTarget', 'beforeScrollX', 'beforeScrollY', 'scrollX', 'scrollY', 'atTop', 'atBottom',
    ) if key in action}
    if scroll:
        facts['scroll'] = scroll
    if action.get('valid') is False:
        facts['validation'] = {'valid': False, 'message': action.get('validation_message'),
                               'flags': action.get('validity_flags')}
    interaction = action.get('interaction')
    if isinstance(interaction, str) and 'timeout' in interaction:
        facts['interaction'] = interaction
    if value.get('snapshot_required') is True and not facts.get('next_required_tool'):
        facts['next_required_tool'] = 'read_page'
    navigation = value.get('navigation') or {}
    if navigation.get('same_tab_url_changed') and navigation.get('after_url'):
        facts['url'] = navigation['after_url']
    if navigation.get('opened_new_tab'):
        facts['opened_new_tab'] = True
    return '\n'.join(['Browser action', *(line for key, item in facts.items() for line in _leaves(key, item))])
