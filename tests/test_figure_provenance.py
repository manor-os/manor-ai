"""Figures in a delegation brief need a source the Agent did not write itself."""
from packages.core.ai.runtime.figure_provenance import (
    UNSOURCED_FIGURES_CODE,
    delegation_figures_refusal,
    source_numbers,
    unsourced_figures,
)

USER_NO_FIGURES = [{"role": "user", "content": "请为周末新客活动处理一项测试促销申请，先检查规则。"}]


def _delegate(task: str) -> dict:
    return {"tasks": [{"agent": "成本分析员", "task": task}]}


def test_invented_figures_in_a_brief_go_back_to_the_agent():
    """Seen live: the brief invented a price, a cost and daily orders; the reply called it a conclusion."""
    brief = "评估周末新客活动：拿铁原价38元，成本10元，8折，预计每天50单，算毛利率。"
    refusal = delegation_figures_refusal("delegate_to_agent", _delegate(brief), USER_NO_FIGURES)
    assert refusal is not None and refusal["error"] == UNSOURCED_FIGURES_CODE
    assert refusal["details"]["unsourced"] == ["38元", "10元", "8折", "50单"]


def test_figures_the_user_or_a_tool_gave_are_sourced():
    messages = [
        {"role": "user", "content": "拿铁售价28、现行成本11、优惠6，目标20人。"},
        {"role": "tool", "content": '{"budget": 740}'},
    ]
    brief = "售价28元，成本11元，每人优惠6元，目标20人，预算740元。"
    assert delegation_figures_refusal("delegate_to_agent", _delegate(brief), messages) is None


def test_one_step_from_sourced_figures_is_sourced():
    sources = source_numbers([{"role": "user", "content": "售价28，成本11，优惠6，20人"}])
    # 28-11=17 margin and 6*20=120 discount total are one step. A two-step
    # result (17/28 = 61%) is flagged: a brief passes inputs, not conclusions.
    assert unsourced_figures("单位毛利17元，优惠总额120元。", sources) == []
    assert unsourced_figures("毛利率 61%。", sources) == ["61%"]


def test_an_assumption_is_allowed_only_when_the_user_asked_for_an_estimate():
    """Live: "数值（全部假设）" carried invented figures into a "建议批准" conclusion."""
    brief = "假设原价38元、每天50单，仅作估算示意。"
    assert delegation_figures_refusal("delegate_to_agent", _delegate(brief), USER_NO_FIGURES) is not None
    asked = [{"role": "user", "content": "没有数据，先用假设数字大概估算一下。"}]
    assert delegation_figures_refusal("delegate_to_agent", _delegate(brief), asked) is None


def test_list_markers_dates_and_times_are_not_figures():
    brief = "1. 检查规则\n2. 写3条文案\n营业时间 07:00-18:00，2026-10-29 上线。"
    assert delegation_figures_refusal("delegate_to_agent", _delegate(brief), USER_NO_FIGURES) is None


def test_the_agents_own_earlier_words_are_not_a_source():
    messages = [*USER_NO_FIGURES, {"role": "assistant", "content": "原价38元，每天50单。"}]
    assert source_numbers(messages).exact == set()


def test_incidental_numbers_in_tool_results_source_only_exact_figures():
    """Ids and times in tool output would make any sum look derived."""
    messages = [*USER_NO_FIGURES, {"role": "tool", "content": '{"id": "01M64", "t": "12:30", "price": 25}'}]
    sources = source_numbers(messages)
    assert unsourced_figures("售价25元", sources) == []
    assert unsourced_figures("成本37元", sources) == ["37元"]  # 25+12 is not the user's arithmetic


def test_other_tools_and_a_repr_string_brief():
    assert delegation_figures_refusal("generate_file", {"content": "38元"}, USER_NO_FIGURES) is None
    refusal = delegation_figures_refusal(
        "delegate_to_agent", {"tasks": "[{'agent': 'x', 'task': '原价38元'}]"}, USER_NO_FIGURES,
    )
    assert refusal is not None and refusal["details"]["unsourced"] == ["38元"]


def test_system_text_sources_only_figures_it_states_with_a_unit():
    """The system prompt's prose is full of bare numbers (100, 32, 12...) that sourced invented figures."""
    messages = [
        {"role": "system", "content": "Keep at most 100 items; use 32 characters; retry 12 times. 目标 20 人。"},
        *USER_NO_FIGURES,
    ]
    sources = source_numbers(messages)
    assert unsourced_figures("目标新客100人，售价32元，成本12元", sources) == ["100人", "32元", "12元"]
    assert unsourced_figures("目标 20 人", sources) == []


def test_a_figure_named_before_its_number_is_still_a_figure():
    """Live: "售价28、成本12……50位" slipped past a number-then-unit pattern."""
    brief = "1) 售价28、成本12\n2) 写3条文案\n3) 预计50位新客，营业 07:00-18:00"
    assert unsourced_figures(brief, source_numbers(USER_NO_FIGURES)) == ["28", "12", "50位"]
