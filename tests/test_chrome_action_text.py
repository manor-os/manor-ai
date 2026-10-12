import copy
import json

from packages.core.ai.agentic_loop import _compact_tool_result_for_context


def test_normal_action_is_a_short_receipt_and_raw_result_is_preserved():
    result = {
        'ok': True, 'status': 'filled', 'tabId': 12,
        'target_resolution': {'selector': '#private-locator'},
        'post_action_page_state': {
            'state_verified': True, 'snapshot_id': 'after', 'page_status': 'ready',
            'observation_quality': 'medium', 'visual_fallback_recommended': True,
            'visual_fallback_reasons': ['embedded_content_unreadable'],
            'page_text': 'DUPLICATE PAGE ' * 1000,
            'semantic_refs': [{'ref': 'e1', 'selector': '#private-locator'}] * 100,
        },
    }
    original = copy.deepcopy(result)
    text = _compact_tool_result_for_context('mcp__chrome__fill_or_select', json.dumps(result))
    assert text.startswith('Browser action\n')
    assert 'status: filled' in text and 'snapshot_id: after' in text
    assert 'observation_quality: medium' in text
    assert 'visual_fallback_recommended: true' in text
    assert 'embedded_content_unreadable' in text
    assert len(text) < 500
    assert 'private-locator' not in text and 'DUPLICATE PAGE' not in text
    assert result == original
    result['post_action_page_state']['state_verified'] = False
    unverified = _compact_tool_result_for_context('mcp__chrome__fill_or_select', json.dumps(result))
    assert 'observation_quality:' not in unverified
    assert 'visual_fallback_recommended:' not in unverified


def test_action_receipt_does_not_turn_selection_or_timeout_into_verified_success():
    value = {'ok': True, 'status': 'files_selected', 'tabId': 12,
             'action_executed': True, 'do_not_replay': True,
             'action_receipt': {'action_id': 'upload-1', 'verification': 'pending'},
             'event_receipt': {'status': 'timeout', 'kind': 'page_state'},
             'next_required_tool': 'mcp__chrome__read_page',
             'js_dialog': {'type': 'confirm', 'message': 'Continue?'},
             'opened_tab': {'id': 14, 'url': 'https://example.test/receipt'}}
    text = _compact_tool_result_for_context('mcp__chrome__upload', json.dumps(value))
    for fact in ('files_selected', 'action_executed: true', 'do_not_replay: true', 'pending',
                 'timeout', 'upload-1', 'mcp__chrome__read_page', 'https://example.test/receipt', 'Continue?'):
        assert fact in text


def test_interrupted_and_failed_actions_keep_their_existing_error_contract():
    for result in ({'ok': False, 'status': 'interrupted', 'reason': 'user_takeover'},
                   {'ok': False, 'status': 'outcome_unknown', 'action_executed': 'unknown'}):
        text = _compact_tool_result_for_context('mcp__chrome__click_element', json.dumps(result))
        assert not text.startswith('Browser action\n')


def test_short_field_receipt_preserves_clearing_and_validation_without_normal_state_noise():
    value = {'ok': True, 'status': 'filled', 'target_resolution': {'selector': '#email'},
             'action': {'valid': False, 'validation_message': 'Please fill out this field.'},
             'post_action_page_state': {'state_verified': True, 'semantic_refs': [
                 {'ref': 'e1', 'selector': '#email', 'role': 'textbox', 'label': 'Email',
                  'value': '', 'disabled': False, 'read_only': False}]}}
    text = _compact_tool_result_for_context('mcp__chrome__fill_or_select', json.dumps(value))
    assert 'field.value: ""' in text
    assert 'validation.valid: false' in text
    assert 'Please fill out this field.' in text
    assert 'disabled: false' not in text and 'read_only: false' not in text


def test_truncated_internal_value_is_not_presented_as_complete():
    value = {'ok': True, 'status': 'filled', 'target_resolution': {'selector': '#post'},
             'post_action_page_state': {'state_verified': True, 'semantic_refs': [
                 {'ref': 'e1', 'selector': '#post', 'value': 'truncated preview…', 'value_truncated': True}]}}
    text = _compact_tool_result_for_context('mcp__chrome__fill_or_select', json.dumps(value))
    assert 'field.value:' not in text and 'truncated preview' not in text
    assert 'field.value_omitted:' in text and 'read_page' in text


def test_download_receipt_keeps_file_identity_size_and_completion_state():
    value = {'ok': True, 'status': 'downloaded', 'download_id': 3641,
             'state': 'complete', 'artifact_ready': True, 'filename': 'sample.txt',
             'local_path': '/downloads/sample.txt', 'mime': 'text/plain',
             'file_size': 31, 'bytes_received': 31, 'total_bytes': 31}
    text = _compact_tool_result_for_context('mcp__chrome__download', json.dumps(value))
    for fact in ('download_id: 3641', 'state: complete', 'file_size: 31',
                 'local_path: /downloads/sample.txt', 'mime: text/plain',
                 'bytes_received: 31', 'total_bytes: 31'):
        assert fact in text


def test_scroll_receipt_keeps_container_movement_and_boundary_evidence():
    value = {'ok': True, 'status': 'scrolled', 'tabId': 12, 'scroll_changed': True,
             'snapshot_required': True, 'action': {
                 'beforeScrollX': 0, 'beforeScrollY': 437, 'scrollX': 0, 'scrollY': 0,
                 'scrollTarget': 'element', 'atTop': True, 'atBottom': False,
                 'selector': '#internal-container',
             }}
    original = copy.deepcopy(value)
    for tool in ('scroll', 'computer'):
        text = _compact_tool_result_for_context(f'mcp__chrome__{tool}', json.dumps(value))
        text = _compact_tool_result_for_context(f'mcp__chrome__{tool}', text, max_chars=1000)
        for fact in ('scroll_changed: true', 'scroll.scrollTarget: element',
                     'scroll.beforeScrollY: 437', 'scroll.scrollY: 0',
                     'scroll.atTop: true', 'scroll.atBottom: false', 'read_page'):
            assert fact in text
        assert 'internal-container' not in text
    assert value == original

    unavailable = {'ok': True, 'status': 'scrolled', 'scroll_probe_unavailable': True}
    text = _compact_tool_result_for_context('mcp__chrome__scroll', json.dumps(unavailable))
    assert 'scroll_probe_unavailable: true' in text
    assert 'scroll.atBottom:' not in text and 'scroll.scrollY:' not in text
