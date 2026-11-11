'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="audit-token"]').content;
let catalog = {
    objects: [],
    snapshots: []
},
    loaded = {
        left: null,
        right: null
    };
let revision = 0,
    comparedInputs = null,
    currentQuestion = null,
    currentTask = null,
    polling = false,
    ticks = 0;
const textNode = (tag, text) => {
    const node = document.createElement(tag);
    node.textContent = text;
    return node;
};
const showError = error => {
    $('global-error').textContent = error.message || String(error);
    $('global-error').hidden = false;
};
async function api(path, value) {
    const response = await fetch(path, value === undefined ? {} : {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-Audit-Token': token
        },
        body: JSON.stringify(value)
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || '请求失败，请刷新页面。');
    return body;
}

function addOption(select, value, label) {
    const opt = textNode('option', label);
    opt.value = value;
    select.append(opt);
}

function objectOptions(select) {
    const previous = select.value;
    select.replaceChildren();
    const groups = new Map();
    for (const obj of catalog.objects) {
        const groupKey = obj.group;
        if (!groups.has(groupKey)) {
            const group = document.createElement('optgroup');
            group.label = groupKey || '代码托管';
            groups.set(groupKey, group);
            select.append(group);
        }
        addOption(groups.get(groupKey), obj.id, obj.id === 'github' ? 'GitHub' : obj.name);
    }
    if ([...select.options].some(o => o.value === previous)) select.value = previous;
    else if (!previous && select.id === 'right-host' && select.options.length > 1) select.selectedIndex = 1;
}

function recordsFor(side) {
    const object = $(side + '-host').value;
    return catalog.snapshots.filter(s => object === 'github' ? s.kind === 'github' : s.host === object);
}

function fillFiles(side, reset = false) {
    const select = $(side + '-file'),
        previous = select.value,
        records = recordsFor(side);
    select.replaceChildren();
    for (const s of [...records].reverse()) addOption(select, s.file, (s.display_time || '未知时间') + (s.complete ? ' · 完整' : ' · 不完整') + ' · ' + s.file.split('/').pop());
    if (!records.length) addOption(select, '', '暂无快照，请先采集或手动载入');
    if (!reset && records.some(s => s.file === previous)) select.value = previous;
    else select.value = (records.find(s => s.complete) || {}).file || '';
    if (!select.value && records.length) select.selectedIndex = -1;
}

function historyDefaults() {
    if (loaded.left || loaded.right || $('left-host').value === 'github' || $('left-host').value !== $('right-host').value) return;
    const records = recordsFor('left').filter(s => s.complete);
    $('left-file').value = records.length > 1 ? records[1].file : '';
    $('right-file').value = records.length ? records[0].file : '';
}

function spec(side) {
    if (loaded[side]) return loaded[side];
    const file = $(side + '-file').value;
    return file ? {
        file
    } : null;
}
async function compareSelected() {
    const requestRevision = ++revision;
    comparedInputs = null;
    $('export').disabled = true;
    $('global-error').hidden = true;
    const left = spec('left'),
        right = spec('right');
    if (!left || !right) {
        $('compare-status').textContent = '请选择两份快照；同主机比较需要两份历史，也可手动载入 JSON。';
        $('result').replaceChildren(textNode('div', '暂无可比较的快照。'));
        updatePathFilter();
        $('visible-count').textContent = '';
        return;
    }
    const value = {
        inputs: [left, right]
    };
    $('compare-status').textContent = '正在比较本地快照……';
    try {
        const response = await api('/api/compare', value);
        if (requestRevision !== revision) return;
        $('result').innerHTML = response.html;
        updatePathFilter();
        comparedInputs = value;
        $('export').disabled = false;
        $('compare-status').textContent = response.result.complete ? '比较完成 · 未生成报告文件' : '比较完成 · 含不完整或未知结果';
        filterRows();
    } catch (error) {
        if (requestRevision !== revision) return;
        $('compare-status').textContent = '比较未完成';
        $('result').replaceChildren(textNode('p', error.message));
        updatePathFilter();
        $('visible-count').textContent = '';
        showError(error);
    }
}

function updatePathFilter() {
    const select = $('path-filter'),
        previous = select.value;
    const paths = new Set();
    for (const row of $('result').querySelectorAll('tbody tr[data-status]')) {
        for (const path of JSON.parse(row.dataset.codePaths)) paths.add(path);
    }
    select.replaceChildren();
    addOption(select, 'all', '全部路径');
    for (const path of [...paths].sort()) addOption(select, path, path);
    if (paths.has(previous)) select.value = previous;
}

function filterRows() {
    const query = $('search').value.toLowerCase(),
        filter = $('filter').value,
        codePath = $('path-filter').value;
    const rows = [...$('result').querySelectorAll('tbody tr[data-status]')];
    let count = 0;
    for (const row of rows) {
        const details = row.nextElementSibling;
        const searchable = row.textContent + (details?.classList.contains('repo-details') ? details.textContent : '');
        row.hidden = !searchable.toLowerCase().includes(query) || (filter !== 'all' && row.dataset.status !== filter) ||
            (codePath !== 'all' && !JSON.parse(row.dataset.codePaths).includes(codePath));
        if (!row.hidden) count++;
    }
    $('visible-count').textContent = rows.length ? '显示 ' + count + ' / ' + rows.length + ' 个仓库' : '';
}

function syncChecks() {
    const hosts = [...$('object-list').querySelectorAll('input[data-target]')].filter(n => n.value !== 'github');
    $('all-hosts').checked = hosts.length > 0 && hosts.every(n => n.checked);
    $('all-hosts').indeterminate = hosts.some(n => n.checked) && !hosts.every(n => n.checked);
}

function drawObjects() {
    const selected = new Set([...$('object-list').querySelectorAll('input:checked')].map(n => n.value));
    $('object-list').replaceChildren();
    const groups = new Map();
    for (const obj of catalog.objects) {
        const groupKey = obj.group;
        if (!groups.has(groupKey)) {
            const section = document.createElement('fieldset');
            section.className = 'object-group';
            const legend = textNode('legend', groupKey || '代码托管');
            section.append(legend);
            groups.set(groupKey, section);
            $('object-list').append(section);
        }
        const label = document.createElement('label');
        label.className = 'object-option';
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.value = obj.id;
        input.dataset.target = obj.id;
        input.checked = selected.has(obj.id);
        input.addEventListener('change', syncChecks);
        label.append(input, document.createTextNode(' ' + (obj.id === 'github' ? 'GitHub' : obj.name)));
        groups.get(groupKey).append(label);
    }
    syncChecks();
}

function taskView(task) {
    currentTask = task;
    const busy = task && ['running', 'stopping'].includes(task.state);
    $('collect-start').disabled = !!busy;
    $('collect-stop').hidden = !busy;
    $('collect-stop').disabled = !!task && task.state === 'stopping';
    $('task-status').textContent = !task ? '还没有网页采集任务。' : ({
        running: '采集中',
        stopping: '正在停止……',
        cancelled: '采集已停止，已保存快照保留',
        complete: '采集完整',
        partial: '采集结束，部分对象失败',
        failed: '采集失败'
    }[task.state] || task.state) + (task.error ? '：' + task.error : '');
    $('task-log').hidden = !task || !task.events.length;
    $('task-log').textContent = task ? task.events.map(e => '[' + e.label + '] ' + e.message).join('\n') : '';
    $('task-log').scrollTop = $('task-log').scrollHeight;
    const q = task && task.state === 'running' && task.question;
    if (!q) {
        $('question').replaceChildren();
        currentQuestion = null;
    } else if (currentQuestion !== q.id) {
        currentQuestion = q.id;
        const box = document.createElement('div');
        box.className = 'prompt';
        box.append(textNode('b', '首次 SSH 连接 · 确认主机密钥'), textNode('p', q.message), textNode('small', '120 秒内选择；等待确认不计入采集超时。'));
        for (const [label, accepted] of [
            ['接受此密钥', true],
            ['拒绝', false]
        ]) {
            const btn = textNode('button', label);
            btn.type = 'button';
            btn.addEventListener('click', async () => {
                btn.disabled = true;
                try {
                    await api('/api/confirm', {
                        task: task.id,
                        question: q.id,
                        accepted
                    });
                    await poll();
                } catch (e) {
                    showError(e);
                    btn.disabled = false;
                }
            });
            box.append(btn);
        }
        $('question').replaceChildren(box);
    }
    $('task-results').replaceChildren();
    if (task && task.result)
        for (const item of task.result.items) {
            const p = textNode('p', item.target + ' · ' + (item.complete ? '完整' : '不完整') + ' · 仓库 ' + item.repositories);
            p.append(textNode('small', item.path));
            $('task-results').append(p);
        }
}
async function refresh() {
    const response = await api('/api/state');
    catalog = response;
    drawObjects();
    for (const side of ['left', 'right']) {
        objectOptions($(side + '-host'));
        fillFiles(side);
        $(side + '-host').disabled = $(side + '-file').disabled = !!loaded[side];
    }
    taskView(response.task);
    const invalid = catalog.snapshots.filter(s => s.error);
    if (invalid.length) showError(new Error('有 ' + invalid.length + ' 份无法读取的快照，请重新采集。' + invalid[0].error));
}
async function poll() {
    if (polling) return;
    polling = true;
    try {
        const previous = currentTask ? currentTask.id + ':' + currentTask.state : '';
        const {
            task
        } = await api('/api/task');
        taskView(task);
        const state = task ? task.id + ':' + task.state : '';
        if (++ticks % 5 === 0 || state !== previous) await refresh();
    } catch (e) {
        $('task-status').textContent = '无法连接服务，请确认 python audit.py serve 正在运行。';
    } finally {
        polling = false;
    }
}
for (const tab of ['compare', 'collect']) $('tab-' + tab).addEventListener('click', () => {
    for (const item of ['compare', 'collect']) {
        $(item + '-view').hidden = item !== tab;
        $('tab-' + item).classList.toggle('active', item === tab);
    }
});
for (const side of ['left', 'right']) {
    $(side + '-host').addEventListener('change', () => {
        fillFiles(side, true);
        historyDefaults();
        compareSelected();
    });
    $(side + '-file').addEventListener('change', compareSelected);
    $(side + '-upload').addEventListener('change', async (event) => {
        const file = event.target.files[0];
        if (!file) return;
        try {
            if (file.size > 12 * 1024 * 1024) throw new Error('文件过大，请使用精简 Git 状态快照（最多 12 MB）。');
            loaded[side] = {
                data: JSON.parse(await file.text()),
                label: file.name
            };
            $(side + '-upload-name').textContent = '已载入：' + file.name;
            $(side + '-clear').hidden = false;
            $(side + '-host').disabled = $(side + '-file').disabled = true;
            compareSelected();
        } catch (e) {
            showError(e);
        }
    });
    $(side + '-clear').addEventListener('click', () => {
        loaded[side] = null;
        $(side + '-upload').value = '';
        $(side + '-clear').hidden = true;
        $(side + '-upload-name').textContent = '文件仅用于比较，不保存到服务器。';
        $(side + '-file').disabled = $(side + '-host').disabled = false;
        compareSelected();
    });
}
$('search').addEventListener('input', filterRows);
$('filter').addEventListener('change', filterRows);
$('path-filter').addEventListener('change', filterRows);
$('compare-now').addEventListener('click', compareSelected);
$('result').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-copy]');
    if (!button) return;
    try {
        await navigator.clipboard.writeText(button.dataset.copy);
        button.textContent = '已复制';
        setTimeout(() => button.textContent = button.dataset.copy.slice(0, 10), 1000);
    } catch (e) {
        showError(new Error('请在展开详情中复制完整 SHA。'));
    }
});
$('all-hosts').addEventListener('change', () => {
    $('object-list').querySelectorAll('input').forEach(n => {
        if (n.value !== 'github') n.checked = $('all-hosts').checked;
    });
    syncChecks();
});
$('collect-start').addEventListener('click', async () => {
    const targets = [...$('object-list').querySelectorAll('input:checked')].map(n => n.value);
    $('global-error').hidden = true;
    try {
        const {
            task
        } = await api('/api/collect', {
            targets
        });
        taskView(task);
    } catch (e) {
        showError(e);
    }
});
$('collect-stop').addEventListener('click', async () => {
    if (!currentTask) return;
    try {
        const {
            task
        } = await api('/api/stop', {
            task: currentTask.id
        });
        taskView(task);
    } catch (e) {
        showError(e);
    }
});
$('export').addEventListener('click', async () => {
    if (!comparedInputs) return;
    try {
        const response = await fetch('/api/export', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Audit-Token': token
            },
            body: JSON.stringify(comparedInputs)
        });
        if (!response.ok) throw new Error((await response.json()).error);
        const url = URL.createObjectURL(await response.blob()),
            link = document.createElement('a');
        link.href = url;
        link.download = (response.headers.get('Content-Disposition').match(/filename="([^"]+)"/) || [])[1] || 'code-audit.zip';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (e) {
        showError(e);
    }
});
refresh().then(() => {
    historyDefaults();
    compareSelected();
}).catch(showError);
setInterval(poll, 1000);
