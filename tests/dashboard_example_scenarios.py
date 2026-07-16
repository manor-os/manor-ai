from __future__ import annotations

from typing import Any


BASE_CSS = """
.module-summary{display:flex;align-items:baseline;justify-content:space-between;gap:12px;padding:2px 0 10px;border-bottom:1px solid var(--module-border)}
.module-summary strong{color:var(--module-text);font-size:18px}.module-summary span,.module-empty,.module-meta{color:var(--module-muted);font-size:10px}
.module-list{display:flex;flex-direction:column}.module-row{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;min-height:44px;padding:7px 0;border-bottom:1px solid var(--module-border)}
.module-row:last-child{border-bottom:0}.module-primary{min-width:0;color:var(--module-text);font-size:12px;font-weight:650;line-height:1.35;overflow-wrap:anywhere}
.module-secondary{display:block;margin-top:2px;color:var(--module-muted);font-size:10px;font-weight:400}.module-value{color:var(--module-text);font-size:11px;text-align:right;white-space:nowrap}
.module-empty{margin:0;padding:28px 8px;text-align:center}@media(max-width:520px){.module-row{grid-template-columns:minmax(0,1fr)}.module-value{text-align:left;white-space:normal}}
""".strip()


ATTENTION_CSS = """
.attention-top{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:12px}
.attention-total{display:flex;align-items:baseline;gap:6px}.attention-total strong{font-size:26px;line-height:1;color:var(--module-text)}
.attention-total span,.attention-note,.lane-count,.task-meta{font-size:10px;color:var(--module-muted)}
.attention-board{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border:1px solid var(--module-border);border-radius:var(--module-radius-md);overflow:hidden}
.attention-lane{min-width:0;padding:10px;border-right:1px solid var(--module-border);background:var(--module-surface)}
.attention-lane:last-child{border-right:0}.lane-header{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
.lane-title{display:flex;align-items:center;gap:7px;font-size:11px;font-weight:700}.lane-dot{width:7px;height:7px;border-radius:50%;background:var(--module-muted)}
.attention-lane[data-tone="focus"] .lane-dot{background:var(--module-accent)}.attention-lane[data-tone="blocked"] .lane-dot{background:var(--module-danger)}
.task-stack{display:flex;flex-direction:column;gap:6px}.task-tile{min-width:0;padding:8px;border:1px solid var(--module-border);border-radius:6px;background:var(--module-row)}
.task-title-row{display:flex;align-items:flex-start;gap:7px}.task-priority{flex:0 0 auto;min-width:24px;padding:1px 4px;border:1px solid var(--module-border);border-radius:4px;color:var(--module-muted);font-size:9px;text-align:center}
.task-title{min-width:0;color:var(--module-text);font-size:11px;font-weight:700;line-height:1.3;overflow-wrap:anywhere}.task-meta{display:flex;justify-content:space-between;gap:8px;margin-top:6px}
.lane-empty{padding:14px 4px;color:var(--module-muted);font-size:10px;text-align:center}
@media(max-width:620px){.attention-top{align-items:flex-start}.attention-board{grid-template-columns:minmax(0,1fr)}.attention-lane{border-right:0;border-bottom:1px solid var(--module-border)}.attention-lane:last-child{border-bottom:0}}
""".strip()


WORKSPACE_RISK_CSS = """
.risk-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:10px}.risk-head strong{font-size:20px;color:var(--module-text)}
.risk-legend{display:flex;align-items:center;gap:12px;color:var(--module-muted);font-size:10px}.risk-key{display:flex;align-items:center;gap:5px}.risk-swatch{width:8px;height:8px;border-radius:2px;background:var(--module-border)}
.risk-swatch.running{background:var(--module-accent)}.risk-swatch.failed{background:var(--module-danger)}.risk-swatch.pending{background:var(--module-muted)}
.risk-chart{display:flex;flex-direction:column;border-top:1px solid var(--module-border)}.risk-row{display:grid;grid-template-columns:minmax(130px,.85fr) minmax(160px,2fr) 72px;align-items:center;gap:14px;min-height:56px;border-bottom:1px solid var(--module-border)}
.risk-name{min-width:0;font-size:11px;font-weight:700;color:var(--module-text);overflow-wrap:anywhere}.risk-name span{display:block;margin-top:2px;color:var(--module-muted);font-size:9px;font-weight:400}
.risk-track{display:flex;height:12px;overflow:hidden;border-radius:3px;background:var(--module-row)}.risk-segment{height:100%;min-width:0}.risk-segment.running{background:var(--module-accent)}.risk-segment.failed{background:var(--module-danger)}.risk-segment.pending{background:var(--module-muted);opacity:.55}
.risk-score{text-align:right}.risk-score strong{display:block;font-size:13px}.risk-score span{font-size:9px;color:var(--module-muted)}.risk-empty{padding:24px;text-align:center;color:var(--module-muted);font-size:10px}
@media(max-width:540px){.risk-head{align-items:flex-start;flex-direction:column}.risk-row{grid-template-columns:minmax(0,1fr) 56px;gap:8px;padding:9px 0}.risk-track{grid-column:1/-1;grid-row:2}.risk-score{grid-column:2;grid-row:1}.risk-legend{flex-wrap:wrap}}
""".strip()


AUTOMATION_CSS = """
.automation-overview{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:10px}.automation-overview strong{font-size:20px;color:var(--module-text)}
.automation-chips{display:flex;gap:6px}.automation-chip{padding:3px 7px;border:1px solid var(--module-border);border-radius:5px;color:var(--module-muted);font-size:9px}.automation-chip.is-alert{color:var(--module-danger)}
.automation-table{border:1px solid var(--module-border);border-radius:var(--module-radius-md);overflow:hidden}.automation-row{display:grid;grid-template-columns:minmax(150px,1.4fr) minmax(120px,.9fr) 92px 70px;align-items:center;gap:12px;min-height:48px;padding:7px 10px;border-bottom:1px solid var(--module-border)}
.automation-row:last-child{border-bottom:0}.automation-row.is-head{min-height:30px;background:var(--module-row);color:var(--module-muted);font-size:9px;text-transform:uppercase}.automation-name{min-width:0;font-size:11px;font-weight:700;overflow-wrap:anywhere}.automation-name span,.automation-schedule{display:block;color:var(--module-muted);font-size:9px;font-weight:400}
.health-state{display:flex;align-items:center;gap:6px;font-size:10px}.health-dot{width:7px;height:7px;border-radius:50%;background:var(--module-muted)}.health-state.is-ok .health-dot{background:var(--module-accent)}.health-state.is-failed .health-dot{background:var(--module-danger)}
.switch{justify-self:end;width:28px;height:16px;padding:2px;border-radius:8px;background:var(--module-border)}.switch span{display:block;width:12px;height:12px;border-radius:50%;background:var(--module-surface)}.switch.is-on{background:var(--module-accent)}.switch.is-on span{margin-left:12px}.automation-empty{padding:24px;text-align:center;color:var(--module-muted);font-size:10px}
@media(max-width:590px){.automation-overview{align-items:flex-start}.automation-row.is-head{display:none}.automation-row{grid-template-columns:minmax(0,1fr) auto;gap:7px;padding:10px}.automation-schedule{grid-column:1}.health-state{grid-column:1}.switch{grid-column:2;grid-row:1}.automation-chips{flex-wrap:wrap;justify-content:flex-end}}
""".strip()


AGENT_CSS = """
.agent-overview{display:flex;align-items:end;justify-content:space-between;gap:16px;margin-bottom:11px}.agent-overview strong{font-size:20px;color:var(--module-text)}.agent-overview span{font-size:10px;color:var(--module-muted)}
.agent-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.agent-card{min-width:0;padding:11px;border:1px solid var(--module-border);border-radius:var(--module-radius-md);background:var(--module-row)}
.agent-card-head{display:flex;align-items:center;gap:9px}.agent-avatar{display:grid;place-items:center;flex:0 0 auto;width:34px;height:34px;border:1px solid var(--module-border);border-radius:50%;background:var(--module-surface);color:var(--module-text);font-size:10px;font-weight:750}
.agent-identity{min-width:0}.agent-name{color:var(--module-text);font-size:11px;font-weight:750;line-height:1.25;overflow-wrap:anywhere}.agent-category{margin-top:3px;color:var(--module-muted);font-size:9px}.agent-status{margin-left:auto;width:7px;height:7px;border-radius:50%;background:var(--module-accent)}
.agent-metrics{display:grid;grid-template-columns:repeat(3,1fr);margin-top:11px;border-top:1px solid var(--module-border)}.agent-metric{padding-top:8px;text-align:center}.agent-metric strong{display:block;font-size:13px}.agent-metric span{font-size:8px;color:var(--module-muted)}.agent-empty{grid-column:1/-1;padding:24px;text-align:center;color:var(--module-muted);font-size:10px}
@media(max-width:650px){.agent-grid{grid-template-columns:minmax(0,1fr)}.agent-overview{align-items:flex-start}.agent-card{padding:10px}.agent-metrics{margin-left:43px;margin-top:8px}}
""".strip()


NEWS_CSS = """
.news-kicker{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px;color:var(--module-muted);font-size:9px;text-transform:uppercase}.news-kicker strong{color:var(--module-text);font-size:10px}
.news-layout{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(260px,.85fr);gap:18px;border-top:1px solid var(--module-border);padding-top:12px}.lead-story{min-width:0;padding-right:18px;border-right:1px solid var(--module-border)}
.lead-source{display:flex;align-items:center;gap:7px;color:var(--module-muted);font-size:9px}.source-mark{display:grid;place-items:center;width:24px;height:24px;border:1px solid var(--module-border);border-radius:50%;color:var(--module-text);font-size:10px;font-weight:800}
.lead-title,.story-title{display:block;width:100%;padding:0;border:0;background:transparent;color:var(--module-text);text-align:left;cursor:pointer}.lead-title{margin-top:13px;font-size:20px;font-weight:760;line-height:1.18}.lead-title:hover,.story-title:hover{text-decoration:underline}
.lead-time,.story-meta{margin-top:10px;color:var(--module-muted);font-size:9px}.news-stack{display:flex;flex-direction:column}.story-row{padding:9px 0;border-bottom:1px solid var(--module-border)}.story-row:first-child{padding-top:0}.story-row:last-child{border-bottom:0}.story-title{font-size:11px;font-weight:700;line-height:1.35}.story-meta{margin-top:4px}.news-empty{grid-column:1/-1;padding:24px;text-align:center;color:var(--module-muted);font-size:10px}
@media(max-width:620px){.news-layout{grid-template-columns:minmax(0,1fr)}.lead-story{padding-right:0;padding-bottom:14px;border-right:0;border-bottom:1px solid var(--module-border)}.lead-title{font-size:17px}}
""".strip()


def _module_code(
    *,
    html: str,
    javascript: str,
    data_requests: list[dict[str, Any]],
    css: str = BASE_CSS,
) -> dict[str, Any]:
    return {
        "version": 1,
        "runtime": "sandboxed_html",
        "html": html,
        "css": css,
        "javascript": javascript,
        "data_requests": data_requests,
    }


def dashboard_example_scenarios() -> list[dict[str, Any]]:
    return [
        {
            "id": "attention_queue",
            "prompt": "显示我需要优先处理的任务，按 Workspace 标明状态、优先级和截止时间。",
            "title": "优先处理",
            "size": "wide",
            "tool_calls": [],
            "visible_text": ["需要处理", "任务"],
            "code": _module_code(
                html=(
                    '<div class="attention-top"><div class="attention-total">'
                    '<strong data-count>0</strong><span>需要处理</span></div>'
                    '<span class="attention-note">按当前行动状态分组</span></div>'
                    '<div class="attention-board">'
                    '<section class="attention-lane" data-lane="focus" data-tone="focus">'
                    '<div class="lane-header"><span class="lane-title"><i class="lane-dot"></i>'
                    '现在处理</span><span class="lane-count" data-lane-count="focus">0</span></div>'
                    '<div class="task-stack" data-stack="focus"></div></section>'
                    '<section class="attention-lane" data-lane="waiting">'
                    '<div class="lane-header"><span class="lane-title"><i class="lane-dot"></i>'
                    '等待输入</span><span class="lane-count" data-lane-count="waiting">0</span></div>'
                    '<div class="task-stack" data-stack="waiting"></div></section>'
                    '<section class="attention-lane" data-lane="blocked" data-tone="blocked">'
                    '<div class="lane-header"><span class="lane-title"><i class="lane-dot"></i>'
                    '阻塞或失败</span><span class="lane-count" data-lane-count="blocked">0</span></div>'
                    '<div class="task-stack" data-stack="blocked"></div></section></div>'
                ),
                javascript="""
window.renderDashboardModule = function(data, context) {
  const items = Array.isArray(data.tasks) ? data.tasks : [];
  document.querySelector('[data-count]').textContent = String(items.length);
  const groups = {focus: [], waiting: [], blocked: []};
  items.slice(0, 15).forEach(function(item) {
    const key = item.status === 'waiting_on_customer'
      ? 'waiting'
      : (item.status === 'failed' || item.status === 'blocked' ? 'blocked' : 'focus');
    groups[key].push(item);
  });
  Object.keys(groups).forEach(function(key) {
    const stack = document.querySelector('[data-stack="' + key + '"]');
    stack.replaceChildren();
    document.querySelector('[data-lane-count="' + key + '"]').textContent = String(groups[key].length);
    if (groups[key].length === 0) {
      const empty = document.createElement('div'); empty.className = 'lane-empty';
      empty.textContent = '目前没有任务'; stack.append(empty); return;
    }
    groups[key].forEach(function(item) {
      const tile = document.createElement('article'); tile.className = 'task-tile';
      const titleRow = document.createElement('div'); titleRow.className = 'task-title-row';
      const priority = document.createElement('span'); priority.className = 'task-priority';
      priority.textContent = 'P' + String(item.priority || '-');
      const title = document.createElement('span'); title.className = 'task-title';
      title.textContent = String(item.title || '未命名任务'); titleRow.append(priority, title);
      const meta = document.createElement('div'); meta.className = 'task-meta';
      const workspace = document.createElement('span'); workspace.textContent = String(item.workspace_name || '未分配');
      const dueNode = document.createElement('span');
    const due = item.due_at ? new Date(item.due_at).toLocaleDateString(context.locale || undefined, {month:'short',day:'numeric'}) : '无截止时间';
      dueNode.textContent = due; meta.append(workspace, dueNode); tile.append(titleRow, meta); stack.append(tile);
    });
  });
};
""".strip(),
                css=ATTENTION_CSS,
                data_requests=[
                    {
                        "key": "tasks",
                        "source": "tasks",
                        "params": {
                            "statuses": [
                                "pending",
                                "in_progress",
                                "waiting_on_customer",
                                "blocked",
                                "failed",
                            ],
                            "days": 60,
                            "limit": 50,
                        },
                    }
                ],
            ),
        },
        {
            "id": "workspace_risk",
            "prompt": "给我一个 Workspace 风险视图，比较每个 Workspace 的进行中、失败和待处理任务。",
            "title": "Workspace 风险",
            "size": "wide",
            "tool_calls": [],
            "visible_text": ["Workspace", "失败"],
            "code": _module_code(
                html=(
                    '<div class="risk-head"><strong><span data-count>0</span> Workspace</strong>'
                    '<div class="risk-legend"><span class="risk-key"><i class="risk-swatch running"></i>进行中</span>'
                    '<span class="risk-key"><i class="risk-swatch failed"></i>失败</span>'
                    '<span class="risk-key"><i class="risk-swatch pending"></i>待处理</span></div></div>'
                    '<div class="risk-chart" data-chart></div>'
                    '<p class="risk-empty" data-empty hidden>还没有 Workspace 数据。</p>'
                ),
                javascript="""
window.renderDashboardModule = function(data) {
  const workspaces = Array.isArray(data.workspaces) ? data.workspaces : [];
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  const chart = document.querySelector('[data-chart]'); const empty = document.querySelector('[data-empty]');
  chart.replaceChildren(); document.querySelector('[data-count]').textContent = String(workspaces.length); empty.hidden = workspaces.length > 0;
  const rows = workspaces.map(function(workspace) {
    const scoped = tasks.filter(function(task) { return String(task.workspace_id || '') === String(workspace.id || ''); });
    const failed = scoped.filter(function(task) { return task.status === 'failed'; }).length;
    const running = scoped.filter(function(task) { return task.status === 'in_progress'; }).length;
    const pending = scoped.filter(function(task) { return task.status === 'pending'; }).length;
    return {workspace: workspace, failed: failed, running: running, pending: pending, score: failed * 3 + pending};
  }).sort(function(a, b) { return b.score - a.score; });
  rows.forEach(function(item) {
    const total = Math.max(1, item.running + item.failed + item.pending);
    const row = document.createElement('div'); row.className = 'risk-row';
    const name = document.createElement('div'); name.className = 'risk-name'; name.textContent = String(item.workspace.name || '未命名 Workspace');
    const status = document.createElement('span'); status.textContent = String(item.workspace.status || 'active'); name.append(status);
    const track = document.createElement('div'); track.className = 'risk-track';
    [['running', item.running], ['failed', item.failed], ['pending', item.pending]].forEach(function(entry) {
      const segment = document.createElement('span'); segment.className = 'risk-segment ' + entry[0];
      segment.style.width = String(entry[1] / total * 100) + '%'; segment.title = entry[0] + ': ' + entry[1]; track.append(segment);
    });
    const score = document.createElement('div'); score.className = 'risk-score';
    const scoreValue = document.createElement('strong'); scoreValue.textContent = String(item.score);
    const scoreLabel = document.createElement('span'); scoreLabel.textContent = '风险分'; score.append(scoreValue, scoreLabel);
    row.append(name, track, score); chart.append(row);
  });
};
""".strip(),
                css=WORKSPACE_RISK_CSS,
                data_requests=[
                    {
                        "key": "workspaces",
                        "source": "workspaces",
                        "params": {"limit": 50},
                    },
                    {
                        "key": "tasks",
                        "source": "tasks",
                        "params": {"days": 30, "limit": 200},
                    },
                ],
            ),
        },
        {
            "id": "automation_health",
            "prompt": "显示所有 Automation 的启用状态、计划、最近结果和连续错误数。",
            "title": "Automation 健康",
            "size": "wide",
            "tool_calls": ["query_scheduled_jobs"],
            "visible_text": ["Automation", "连续错误"],
            "code": _module_code(
                html=(
                    '<div class="automation-overview"><strong><span data-count>0</span> Automation</strong>'
                    '<div class="automation-chips"><span class="automation-chip" data-enabled>0 已启用</span>'
                    '<span class="automation-chip is-alert" data-errors>0 连续错误</span></div></div>'
                    '<div class="automation-table"><div class="automation-row is-head">'
                    '<span>Automation</span><span>计划</span><span>最近结果</span><span>状态</span></div>'
                    '<div data-rows></div><p class="automation-empty" data-empty hidden>还没有 Automation。</p></div>'
                ),
                javascript="""
window.renderDashboardModule = function(data) {
  const payload = data.automations && typeof data.automations === 'object' ? data.automations : {};
  const items = Array.isArray(payload.automations) ? payload.automations : [];
  const rows = document.querySelector('[data-rows]'); const empty = document.querySelector('[data-empty]');
  rows.replaceChildren(); document.querySelector('[data-count]').textContent = String(items.length); empty.hidden = items.length > 0;
  document.querySelector('[data-enabled]').textContent = String(items.filter(function(item) { return item.enabled !== false; }).length) + ' 已启用';
  document.querySelector('[data-errors]').textContent = String(items.reduce(function(total, item) { return total + Number(item.consecutive_errors || 0); }, 0)) + ' 连续错误';
  items.forEach(function(item) {
    const row = document.createElement('div'); row.className = 'automation-row';
    const name = document.createElement('div'); name.className = 'automation-name'; name.textContent = String(item.name || item.job_id || '未命名 Automation');
    const id = document.createElement('span'); id.textContent = String(item.job_id || ''); name.append(id);
    const schedule = item.cron_expr || (item.every_seconds ? '每 ' + item.every_seconds + ' 秒' : item.run_at || '未设置计划');
    const scheduleNode = document.createElement('span'); scheduleNode.className = 'automation-schedule'; scheduleNode.textContent = schedule;
    const health = document.createElement('span');
    const failed = item.last_status === 'failed' || Number(item.consecutive_errors || 0) > 0;
    health.className = 'health-state ' + (failed ? 'is-failed' : 'is-ok');
    const dot = document.createElement('i'); dot.className = 'health-dot';
    const healthText = document.createElement('span'); healthText.textContent = failed ? String(item.consecutive_errors || 0) + ' 错误' : String(item.last_status || '未运行'); health.append(dot, healthText);
    const toggle = document.createElement('span'); toggle.className = 'switch' + (item.enabled === false ? '' : ' is-on');
    toggle.setAttribute('aria-label', item.enabled === false ? '已停用' : '已启用'); toggle.append(document.createElement('span'));
    row.append(name, scheduleNode, health, toggle); rows.append(row);
  });
};
""".strip(),
                css=AUTOMATION_CSS,
                data_requests=[
                    {
                        "key": "automations",
                        "source": "tool",
                        "params": {},
                        "tool_name": "query_scheduled_jobs",
                        "tool_arguments": {"limit": 100},
                        "refresh_seconds": 300,
                    }
                ],
            ),
        },
        {
            "id": "agent_directory",
            "prompt": "显示当前可用 Agent，按类别展示状态、工具、Skill 和部署数量。",
            "title": "Agent 目录",
            "size": "wide",
            "tool_calls": ["query_entity_agents"],
            "visible_text": ["Agent", "部署"],
            "code": _module_code(
                html=(
                    '<div class="agent-overview"><strong><span data-count>0</span> Agent</strong>'
                    '<span>当前 Entity 可用 roster</span></div><div class="agent-grid" data-grid></div>'
                ),
                javascript="""
window.renderDashboardModule = function(data) {
  const payload = data.agents && typeof data.agents === 'object' ? data.agents : {};
  const items = Array.isArray(payload.agents) ? payload.agents : [];
  const grid = document.querySelector('[data-grid]'); grid.replaceChildren();
  document.querySelector('[data-count]').textContent = String(items.length);
  if (items.length === 0) { const empty = document.createElement('p'); empty.className = 'agent-empty'; empty.textContent = '还没有可用 Agent。'; grid.append(empty); return; }
  items.forEach(function(item) {
    const card = document.createElement('article'); card.className = 'agent-card';
    const head = document.createElement('div'); head.className = 'agent-card-head';
    const avatar = document.createElement('span'); avatar.className = 'agent-avatar';
    avatar.textContent = String(item.name || 'A').split(/\\s+/).slice(0, 2).map(function(part) { return part.charAt(0); }).join('').toUpperCase();
    const identity = document.createElement('div'); identity.className = 'agent-identity';
    const name = document.createElement('div'); name.className = 'agent-name'; name.textContent = String(item.name || '未命名 Agent');
    const category = document.createElement('div'); category.className = 'agent-category'; category.textContent = String(item.category || '未分类'); identity.append(name, category);
    const status = document.createElement('i'); status.className = 'agent-status'; status.title = String(item.status || 'unknown'); head.append(avatar, identity, status);
    const metrics = document.createElement('div'); metrics.className = 'agent-metrics';
    [['工具', item.tool_count], ['Skill', item.skill_count], ['部署', item.deployment_count]].forEach(function(entry) {
      const metric = document.createElement('div'); metric.className = 'agent-metric';
      const value = document.createElement('strong'); value.textContent = String(entry[1] || 0);
      const label = document.createElement('span'); label.textContent = entry[0]; metric.append(value, label); metrics.append(metric);
    });
    card.append(head, metrics); grid.append(card);
  });
};
""".strip(),
                css=AGENT_CSS,
                data_requests=[
                    {
                        "key": "agents",
                        "source": "tool",
                        "params": {},
                        "tool_name": "query_entity_agents",
                        "tool_arguments": {
                            "statuses": ["active"],
                            "include_templates": False,
                            "limit": 100,
                        },
                        "refresh_seconds": 600,
                    }
                ],
            ),
        },
        {
            "id": "external_briefing",
            "prompt": "每天显示与运营可靠性相关的新闻，包含标题、来源和发布时间。",
            "title": "运营可靠性简报",
            "size": "wide",
            "tool_calls": [],
            "visible_text": ["新闻", "来源"],
            "code": _module_code(
                html=(
                    '<div class="news-kicker"><strong>Daily briefing</strong>'
                    '<span><span data-count>0</span> 条新闻 · 来源与发布时间</span></div>'
                    '<div class="news-layout"><article class="lead-story" data-lead></article>'
                    '<div class="news-stack" data-stack></div>'
                    '<p class="news-empty" data-empty hidden>暂时没有相关新闻。</p></div>'
                ),
                javascript="""
window.renderDashboardModule = function(data, context) {
  const items = Array.isArray(data.headlines) ? data.headlines : [];
  const lead = document.querySelector('[data-lead]'); const stack = document.querySelector('[data-stack]'); const empty = document.querySelector('[data-empty]');
  lead.replaceChildren(); stack.replaceChildren(); document.querySelector('[data-count]').textContent = String(items.length); empty.hidden = items.length > 0;
  const formatTime = function(item) { return item.published_at ? new Date(item.published_at).toLocaleString(context.locale || undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '时间未知'; };
  const makeButton = function(item, className) { const button = document.createElement('button'); button.type = 'button'; button.className = className; button.textContent = String(item.title || '未命名新闻'); if (typeof item.url === 'string') button.setAttribute('data-manor-url', item.url); return button; };
  if (items.length > 0) {
    const first = items[0];
    const source = document.createElement('div'); source.className = 'lead-source';
    const mark = document.createElement('span'); mark.className = 'source-mark'; mark.textContent = String(first.source || 'N').charAt(0).toUpperCase();
    const sourceName = document.createElement('span'); sourceName.textContent = String(first.source || '来源未知'); source.append(mark, sourceName);
    const title = makeButton(first, 'lead-title');
    const time = document.createElement('div'); time.className = 'lead-time'; time.textContent = formatTime(first); lead.append(source, title, time);
  }
  items.slice(1, 6).forEach(function(item) {
    const row = document.createElement('article'); row.className = 'story-row';
    const title = makeButton(item, 'story-title');
    const meta = document.createElement('div'); meta.className = 'story-meta'; meta.textContent = String(item.source || '来源未知') + ' · ' + formatTime(item);
    row.append(title, meta); stack.append(row);
  });
};
""".strip(),
                css=NEWS_CSS,
                data_requests=[
                    {
                        "key": "headlines",
                        "source": "news",
                        "params": {
                            "query": "operations reliability",
                            "days": 1,
                            "limit": 8,
                        },
                    }
                ],
            ),
        },
    ]
