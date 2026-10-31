
import pytest

from packages.core.i18n import t


LANGUAGE_PROGRESS = [
    ("en", "Execution plan started", "Layer"),
    ("zh", "执行计划已启动", "第 1 层"),
    ("es", "Plan de ejecución iniciado", "Nivel"),
    ("fr", "Plan d'exécution démarré", "Niveau"),
    ("de", "Ausführungsplan gestartet", "Ebene"),
    ("ja", "実行計画を開始", "第 1 層"),
    ("vi", "Đã bắt đầu kế hoạch thực thi", "Lớp"),
]


@pytest.mark.parametrize("locale,_,__", LANGUAGE_PROGRESS)
def test_task_progress_templates_preserve_agent_and_human_quote(locale, _, __):
    agent_name = "Atlas / 智能体 {agent_name}"
    human_quote = '保持原文: "yes", {"status":"ready"}'
    started = t("task.execution.started", locale=locale, agent_name=agent_name)
    resumed = t("task.execution.hitl_resumed", locale=locale, response=human_quote)
    assert started.startswith("[AI] ")
    assert resumed.startswith("[AI] ")
    assert agent_name in started
    assert human_quote in resumed
    if locale != "en":
        assert "Execution started" not in started
        assert "Resuming after HITL" not in resumed


