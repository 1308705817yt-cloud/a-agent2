"use strict";
const $ = s => document.querySelector(s);
const names = {queued:"排队中",running:"构建中",completed:"已交付",failed:"调用失败",waiting_for_guidance:"等待方向",stopped:"修复预算已用完",rejected:"验收未通过",unsupported:"需求暂不支持",time_limit:"已达时间上限"};
const executionNames = {queued:"等待执行",running:"执行中",completed:"已完成",failed:"执行失败",waiting_input:"等待人工补充",retryable:"可以重试",interrupted:"执行中断",budget:"决策预算用完",time_limit:"已达时间上限"};
const phaseNames = {requirements:"理解需求",workflow:"设计工作流",tests:"生成测试",code:"生成代码",evaluate:"开发测试",diagnosis:"分析失败",repair:"修复代码",redesign:"重新设计工具",redesign_tests:"生成新接口测试",redesign_code:"实现新设计",acceptance:"保留验收",export:"导出产物",completed:"已完成"};
let jobCursors = [null], jobPage = 0, nextJobCursor = null, refreshing = false;
let selected = null, tab = "overview", detail = null, jobs = [], trialInput = "", trialState = null, trialBusy = false, trialReply = "", trialParameters = "";
try{const saved=JSON.parse(globalThis.sessionStorage?.getItem("agentforge.jobPage") || "null");if(saved && Number.isInteger(saved.page) && saved.page>=0 && saved.page<saved.cursors.length && saved.cursors[0]===null && saved.cursors.length<=1000 && saved.cursors.slice(1).every(c=>typeof c==="string" && c.length<=256)){jobPage=saved.page;jobCursors=saved.cursors;}}catch{}
function savePage(){try{globalThis.sessionStorage?.setItem("agentforge.jobPage",JSON.stringify({page:jobPage,cursors:jobCursors}));}catch{}}
const esc = text => String(text ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const json = x => esc(JSON.stringify(x, null, 2));
const badge = s => `<span class="badge ${esc(s)}">${esc(names[s] || s)}</span>`;
const time = t => new Date(t * 1000).toLocaleString("zh-CN",{hour12:false});
function evaluationView(s) {
  const reports = s.evaluation_reports?.length ? s.evaluation_reports : [s.report,s.acceptance_report].filter(Boolean);
  const recovery = (s.evaluation_recovery || []).map(r=>`<div class="panel"><h3>${r.partition==="development"?"开发测试":"保留验收"}恢复</h3><p>已完成 ${r.completed.length} · 调用中断 ${r.interrupted.length} · 尚未提交 ${r.inflight.length}</p><p>重试复用已完成用例，继续中断用例的已保存观察。产物或测试变化后重新验证。</p><details><summary>用例与恢复记录</summary><pre>${json(r)}</pre></details></div>`).join("");
  return recovery + (reports.map(r=>`<div class="panel"><h3>${r.partition==="development"?"开发测试":"保留验收"} · ${r.passed}/${r.total}</h3>${r.new_model_calls!==undefined?`<p>本次模型调用 ${r.new_model_calls} · 复用 ${(r.reused_cases || []).length} 个用例 · 接续 ${(r.resumed_cases || []).length} 个用例</p>`:""}${r.execution_conditions?`<p>本次运行时限 ${r.execution_conditions.max_seconds===0?"不限":esc(r.execution_conditions.max_seconds)+" 秒"} · 模型配置 ${esc(r.execution_conditions.model_configuration.requested_model || "未指定")}</p><details><summary>执行条件、历史复用与计算覆盖</summary><pre>${json({conditions:r.execution_conditions,change:r.condition_change,groups:r.result_groups,coverage:r.tool_coverage,historical_reuse:r.historical_reuse,current_executions:r.current_executions})}</pre></details>`:""}${r.behavior_skipped?"<p>工具检查未通过，模型行为测试尚未执行。</p>":""}<table><thead><tr><th>场景</th><th>结果</th><th>需求依据与失败证据</th></tr></thead><tbody>${r.cases.map(c=>`<tr><td><span class="mono">${esc(c.id)}</span><p>${esc(c.purpose || "构建检查")}</p>${c.kind==="host_contract"?"<p>宿主拒绝验证</p>":c.kind==="business_error"?"<p>业务错误结果验证</p>":""}${c.evaluation?`<p>${c.evaluation.mode==="historical_reuse"?"历史复用":c.evaluation.mode==="resumed"?"接续执行":"本次执行"}</p>`:""}</td><td class="${c.passed?"test-pass":"test-fail"}">${c.passed?"通过":"失败"}</td><td>${esc(c.criterion || "")}${(c.issues || []).map(i=>`<p class="error-text">${esc(i)}</p>`).join("")}<details><summary>查看输入、断言和执行记录</summary><pre>${json({input:c.input,expected:c.expected,expected_error:c.expected_error,actual:c.actual,failure:c.failure,location:c.location,evaluation:c.evaluation})}</pre></details></td></tr>`).join("")}</tbody></table></div>`).join("") || `<div class="panel"><h3>已生成的验收场景</h3><pre>${json(s.test_suite || "测试还未生成")}</pre></div>`);
}
async function api(path, data) {const r = await fetch(path, data === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}); const x = await r.json(); if(!r.ok) throw new Error(x.error || "请求失败"); return x;}
function notice(message) {$("#notice").textContent=message; $("#notice").hidden=!message;}
function views(name) {for (const view of ["jobs","tools","detail"]) $(`#${view}-view`).hidden=view!==name; $("#nav-jobs").classList.toggle("active",name!=="tools"); $("#nav-tools").classList.toggle("active",name==="tools");}
async function refresh() {
  if(refreshing)return;
  refreshing=true;
  try {
    const page = await api(`/api/jobs?limit=50${jobCursors[jobPage]?`&cursor=${encodeURIComponent(jobCursors[jobPage])}`:""}`);
    jobs=page.jobs;nextJobCursor=page.next_cursor;
    const counts=page.status_counts || {};
    const stats = [["全部任务",page.total ?? jobs.length],["构建与排队",(counts.running || 0)+(counts.queued || 0)],["可运行产物",counts.completed || 0],["需要处理",["waiting_for_guidance","failed","rejected","stopped","time_limit"].reduce((n,s)=>n+(counts[s] || 0),0)]];
    $("#job-page").textContent=`第 ${jobPage+1} 页 · 共 ${page.total ?? jobs.length} 个任务`;
    $("#jobs-prev").disabled=jobPage===0;$("#jobs-next").disabled=!nextJobCursor;
    $("#stats").innerHTML=stats.map(([name,value])=>`<div class="stat"><span>${esc(name)}</span><strong>${value}</strong></div>`).join("");
    $("#jobs").innerHTML=jobs.length ? jobs.map(j=>`<article class="job" data-id="${j.run_id}" tabindex="0"><div class="job-top"><span class="mono">${j.run_id}</span>${badge(j.status)}</div><h3>${esc(j.requirements?.summary || j.request.slice(0,60))}</h3><p>${esc(j.request)}</p><div class="job-foot"><span>${esc(phaseNames[j.phase] || j.phase)}</span><span>v${j.version} · ${j.repairs} 次修复</span><span>${j.report ? `${j.report.passed}/${j.report.total} 开发` : "待测试"}</span></div></article>`).join("") : `<div class="empty"><h3>创建你的第一个 Agent</h3><p>提供需求和资源，Codex 会生成工具工作流、代码和验收测试。</p></div>`;
    if(selected){detail=await api(`/api/jobs/${selected}`);renderDetail();}
  }catch(e){notice(e.message);}
  finally{refreshing=false;}
}
$("#jobs-prev").onclick=()=>{if(!refreshing && jobPage>0){jobPage--;savePage();refresh();}};
$("#jobs-next").onclick=()=>{if(!refreshing && nextJobCursor){jobCursors[++jobPage]=nextJobCursor;savePage();refresh();}};
function renderDetail() {
  const s=detail;
  for(const b of $("#tabs").children)b.classList.toggle("selected",b.dataset.tab===tab);
  $("#detail-head").innerHTML=`<div class="detail-title"><h2>${esc(s.spec?.name || "构建任务")}</h2>${badge(s.status)}</div><div class="detail-meta"><span class="mono">${s.run_id}</span><span>${esc(phaseNames[s.phase] || s.phase)}</span><span>v${s.version} · ${s.repairs}/${s.max_repairs} 次修复</span><span>${esc(time(s.created_at))}</span>${s.performance?`<span>累计耗时 ${Math.round(s.performance.elapsed_ms/1000)} 秒 · ${s.performance.max_seconds?`上限 ${Math.round(s.performance.max_seconds/60)} 分钟`:"时间不限"}</span>`:""}</div>`;
  // Preserve typed guidance and runtime inputs across refreshes.
  const focus=$("#direction"), direction=focus?.value;
  if((tab==="try" || tab==="overview") && document.activeElement?.matches("textarea"))return;
  const content=$("#detail-content");
  if(tab==="overview") {
    content.innerHTML=`<div class="two-col"><div><div class="panel"><h3>需求契约</h3><p>${esc(s.request)}</p><ul>${(s.requirements?.acceptance_criteria || []).map(c=>`<li>${esc(c)}</li>`).join("")}</ul><p>资源：${s.resources.documents.length} 份文档 / ${s.resources.datasets.length} 份 CSV</p></div>${s.diagnosis ? `<div class="panel"><h3>失败点与修改建议</h3><p>${esc(s.diagnosis.summary)}</p><ul>${s.diagnosis.failure_points.map(p=>`<li><strong>${esc(p.location)}</strong>：${esc(p.cause)}<br>${esc(p.fix)}</li>`).join("")}</ul></div>` : ""}<div class="panel"><h3>当前结果</h3><p>${s.report?`开发检查 ${s.report.passed}/${s.report.total}`:"尚未运行开发检查"}${s.acceptance_report?` · 保留验收 ${s.acceptance_report.passed}/${s.acceptance_report.total}`:""}</p>${s.last_error?`<p class="error-text">${esc(s.last_error)}</p>`:""}${s.stop_reason?`<p class="error-text">${esc(s.stop_reason)}</p>`:""}${s.status==="waiting_for_guidance"?`<label for="direction">可选修改方向</label><textarea id="direction" rows="3" placeholder="例如：保留来源原文，修复排序后输出逻辑"></textarea><div class="actions"><button class="primary" id="resume">继续修复</button></div>`:""}${s.status==="time_limit" && s.error_info?.scope==="runtime"?`<label for="run-time-limit">测试中子 Agent 上限（分钟，0 表示不限）</label><input id="run-time-limit" type="number" min="0" max="1440" value="${Math.ceil((s.performance?.run_max_seconds || 900)/60)+15}">`:""}${s.status==="time_limit"?`<label for="build-time-limit">累计构建上限（分钟，0 表示不限）</label><input id="build-time-limit" type="number" min="0" max="1440" value="${Math.ceil((s.performance?.elapsed_ms || 0)/60000)+30}">`:""}${s.status==="stopped"?`<label for="repair-limit">累计修复轮次上限</label><input id="repair-limit" type="number" min="${s.repairs+1}" max="20" value="${Math.min(20,s.repairs+2)}"><label for="repair-scope">修复范围</label><select id="repair-scope"><option value="auto">根据失败自动选择</option><option value="code">修改代码与指令</option><option value="design">重新设计工具接口</option></select><label for="direction">可选修改方向</label><textarea id="direction" rows="3" placeholder="说明需要调整的行为"></textarea>`:""}${["failed","time_limit","stopped"].includes(s.status)?`<div class="actions"><button id="retry">从当前阶段重试</button></div>`:""}${s.status==="completed"?`<div class="actions"><a class="download" href="/api/jobs/${s.run_id}/download">下载独立运行 ZIP ↗</a></div>`:""}</div></div><div class="panel"><h3>模型调用</h3><p>${s.call_metrics?.available?`累计 ${s.call_metrics.model_calls} 次 · 完成 ${s.call_metrics.completed_calls} · 失败 ${s.call_metrics.failed_calls} · 未提交 ${s.call_metrics.inflight_calls}`:"历史运行时未记录完整调用量"}</p><h3>构建记录</h3><ol class="timeline">${s.events.map(e=>`<li><strong>${esc(phaseNames[e.phase] || e.phase)}</strong><p class="${e.error?"error-text":""}">${esc(e.message)}</p><p>${esc(time(e.time))}</p></li>`).join("")}</ol></div></div>`;
    if(direction!==undefined && $("#direction"))$("#direction").value=direction;
  } else if(tab==="workflow") {
    if(s.spec?.runtime==="codex_loop") {
      content.innerHTML=`<div class="panel"><h3>Codex 决策循环 · ${s.spec.max_steps} 步目标${s.spec.execution_policy?.adaptive_steps?" · 有进展时自动延长":""}</h3><p>目标 → 计划 → 选择工具 → 观察结果 → 调整计划 → 校验并返回</p><pre>${esc(s.spec.instructions)}</pre><ol>${s.spec.planning_hints.map(h=>`<li>${esc(h)}</li>`).join("")}</ol></div><div class="workflow-nodes">${s.spec.tools.map(id=>{const t=s.spec.custom_tools.find(t=>t.id===id) || (s.spec.trusted_tools || []).find(t=>t.id===id);return `<div class="node"><h3>${esc(id)}</h3><p>${esc(t?.description || "运行时由 Codex 按需选择的内置工具")}</p>${t?`<p>${t.kind?"可信只读工具":t.required?"完成前必须执行":"可选"}</p><details><summary>参数与输出契约</summary><pre>${json({parameters:t.parameters || t.input_schema,output_schema:t.output_schema})}</pre></details>`:""}</div>`;}).join("")}</div>`;
    } else content.innerHTML=s.spec ? `<div class="panel"><h3>生成的工作流 · 入口 ${esc(s.spec.entry)}</h3><p>${s.spec.tools.length} 个注册工具 · ${s.spec.nodes.length} 个节点 · 最多 ${s.spec.max_steps} 步</p></div><div class="workflow-nodes">${s.spec.nodes.map(n=>`<div class="node"><div class="eyebrow">${esc(n.id)}</div><h3>${esc(n.tool)}</h3><code>${json(n.arguments)}</code><p>→ ${esc(n.next || "返回结果")}</p>${n.on_empty?`<p>空结果 → ${esc(n.on_empty)}</p>`:""}${n.on_error?`<p>错误 → ${esc(n.on_error)}</p>`:""}</div>`).join("")}</div>` : `<div class="empty">工作流还未生成</div>`;
  } else if(tab==="code") {
    content.innerHTML=`<div class="section-line"><h2>logic.py · v${s.version}</h2><span>受约束 Python · 解释执行</span></div><pre>${esc(s.source || "代码还未生成")}</pre>${(s.spec?.custom_tools || []).map(t=>`<div class="panel"><h3>custom_tools/${esc(t.id.split(".")[1])}.py</h3><p>${esc(t.description)}</p><pre>${esc(t.source || "源码尚未生成")}</pre></div>`).join("")}`;
  } else if(tab==="tests") {
    content.innerHTML=evaluationView(s);
    content.innerHTML += (s.benchmarks || []).map(r=>`<div class="panel"><h3>独立陌生任务评测 · ${esc(r.suite_version)} · ${r.passed}/${r.total}</h3><p>手写答案与生成、修复流程隔离；Agent v${r.version} · ${esc(names[r.build_status] || r.build_status || "")}</p><table><thead><tr><th>陌生场景</th><th>结果</th><th>失败证据</th></tr></thead><tbody>${r.cases.map(c=>`<tr><td>${esc(c.id)}</td><td>${c.passed?"通过":"失败"}</td><td>${(c.issues || []).map(esc).join("；")}<details><summary>完整评测记录</summary><pre>${json(c)}</pre></details></td></tr>`).join("")}</tbody></table></div>`).join("");
  } else if(tab==="versions") {
    content.innerHTML=s.changes.map(c=>`<div class="panel"><h3>v${c.version} · ${esc(c.explanation)}</h3><details open><summary>代码差异</summary><pre>${esc(c.code_diff || "源码未改变")}</pre></details>${Object.entries(c.tool_diffs || {}).map(([id,diff])=>`<details open><summary>${esc(id)} 源码差异</summary><pre>${esc(diff || "源码未改变")}</pre></details>`).join("")}<details><summary>指令与工具定义差异</summary><pre>${json({before:c.workflow_before,after:c.workflow_after})}</pre></details>${c.runtime_before && c.runtime_before!==c.runtime_after?`<p>运行时已更新，冻结验收重新执行。</p>`:""}<p class="mono">${esc(c.artifact_hash)}</p></div>`).join("") || `<div class="empty">还没有版本记录</div>`;
  } else {
    content.innerHTML=`<div class="panel"><h3>运行当前版本</h3><p>${s.requirements?.task_type==="csv_analysis"?"输入自然语言目标或 JSON；{} 使用默认条件，也支持 goal 和 parameters 组成的目标对象。":"输入问题，查看回答、证据以及 Codex 计划和工具观察。"}</p><label for="trial-input">${s.requirements?.task_type==="csv_analysis"?"目标 / JSON 参数":"问题"}</label><textarea id="trial-input" rows="4">${esc(trialInput || (s.requirements?.task_type==="csv_analysis"?"{}":""))}</textarea><div class="actions"><button class="primary" id="trial-run" ${trialBusy?"disabled":""}>${trialBusy?"Codex 决策中…":"运行 Agent"}</button></div></div><div class="panel" id="trial-progress" ${trialState?"":"hidden"}>${trialView()}</div><pre id="trial-result" ${trialState?.result?"":"hidden"}>${trialState?.result?json(trialState.result):""}</pre><div class="panel"><h3>运行记录</h3>${(s.executions || []).map(r=>`<p><button data-execution="${r.execution_id}">${esc(r.execution_id)} · v${r.version} · ${esc(executionNames[r.status] || r.status)}</button> ${esc(time(r.created_at))}</p>`).join("") || "尚无运行记录"}</div>`;
  }
}
$("#new-job").onclick=()=>$("#create-dialog").showModal();
$("#close-dialog").onclick=()=>$("#create-dialog").close();
$("#create-form").onsubmit=async e=>{e.preventDefault();const button=$("#submit-build");button.disabled=true;$("#create-error").textContent="";try{const files=await Promise.all([...$("#files").files].map(async f=>({name:f.name,content:await f.text()})));const x=await api("/api/jobs",{request:$("#request").value,files,review:$("#review").checked,max_repairs:Number($("#repairs").value),max_seconds:Number($("#build-minutes").value)*60,run_max_seconds:Number($("#run-minutes").value)*60,adaptive_steps:$("#adaptive-steps").checked});$("#create-dialog").close();jobPage=0;jobCursors=[null];savePage();selected=x.run_id;trialInput="";trialState=null;tab="overview";views("detail");notice("");await refresh();}catch(e){$("#create-error").textContent=e.message;notice(e.message);}finally{button.disabled=false;}};
$("#jobs").onclick=e=>{const card=e.target.closest("[data-id]");if(card){selected=card.dataset.id;trialInput="";trialState=null;tab="overview";views("detail");refresh();}};
$("#jobs").onkeydown=e=>{if(e.key==="Enter")e.target.click();};
$("#back").onclick=$("#nav-jobs").onclick=()=>{selected=null;views("jobs");};
$("#nav-tools").onclick=async()=>{selected=null;views("tools");try{const x=await api("/api/tools");$("#tools").innerHTML=x.tools.map(t=>`<div class="tool"><h3 class="mono">${esc(t.id)}</h3><p>${esc(t.description)}</p><small>${esc(t.input_kind)} → ${esc(t.output_kind)}<br>参数：${esc(t.parameters.join(", ") || "无")}<br>${esc(t.task_types.join(" / "))}</small></div>`).join("");}catch(e){notice(e.message);}};
$("#tabs").onclick=e=>{if(e.target.dataset.tab){tab=e.target.dataset.tab;for(const b of $("#tabs").children)b.classList.toggle("selected",b.dataset.tab===tab);renderDetail();}};
async function pollExecution(job,x){
  trialState=x;renderTrial();
  while(["queued","running"].includes(x.status)){
    await new Promise(resolve=>setTimeout(resolve,1500));
    x=await api(`/api/jobs/${job}/executions/${x.execution_id}`);
    if(selected!==job)break;
    trialState=x;renderTrial();
  }
}
$("#detail-content").oninput=e=>{if(e.target.id==="trial-reply")trialReply=e.target.value;if(e.target.id==="trial-parameters")trialParameters=e.target.value;};
$("#detail-content").onclick=async e=>{try{
  if(e.target.id==="resume" || e.target.id==="retry"){
    await api(`/api/jobs/${selected}/${e.target.id}`,{direction:$("#direction")?.value || "",...(detail.status==="time_limit"?{max_seconds:Number($("#build-time-limit").value)*60,...($("#run-time-limit")?{run_max_seconds:Number($("#run-time-limit").value)*60}:{})}:{}),...(detail.status==="stopped"?{max_repairs:Number($("#repair-limit").value),repair_scope:$("#repair-scope").value}:{})});await refresh();return;
  }
  if(e.target.dataset.execution || e.target.id==="trial-run" || e.target.id==="trial-continue"){
    const job=selected;trialBusy=true;
    try{
      let x;
      if(e.target.dataset.execution){x=await api(`/api/jobs/${job}/executions/${e.target.dataset.execution}`);trialReply="";trialParameters="";}
      else if(e.target.id==="trial-continue"){
        const reply=needsReply(trialState)?{text:trialReply,parameters:trialParameters.trim()?JSON.parse(trialParameters):{}}:undefined;
        x=await api(`/api/jobs/${job}/executions/${trialState.execution_id}/continue`,{...(reply?{reply}:{}),...(trialState.status==="time_limit"?{max_seconds:Number($("#trial-time-limit").value)*60}:{})});
        trialReply="";trialParameters="";
      }else{
        trialInput=$("#trial-input").value;const text=trialInput.trim();
        const input=detail.requirements.task_type==="csv_analysis"?(text.startsWith("{")?JSON.parse(text):{goal:text}):{question:text};
        trialState={status:"queued",trace:[]};renderDetail();
        x=await api(`/api/jobs/${job}/run`,{input});
      }
      if(x.execution_id){await pollExecution(job,x);}else{trialState={status:x.error?"failed":"completed",result:x,trace:x.trace || []};}
    }finally{trialBusy=false;if(selected===job)renderDetail();}
  }
}catch(e){notice(e.message);}};
function needsReply(state){return state.status==="waiting_input" || (state.checkpoint?.phase==="waiting_input" && !state.reply);}
function trialView(){if(!trialState)return ""; return `<h3>${esc(executionNames[trialState.status] || trialState.status)} · ${trialState.trace?.filter(e=>e.type==="decision").length || 0} 次决策</h3>${trialState.error?`<p class="error-text">${esc(trialState.error.message)}</p>`:""}${needsReply(trialState)?`<label for="trial-reply">人工补充</label><textarea id="trial-reply" rows="3">${esc(trialReply)}</textarea>${detail?.requirements?.task_type==="csv_analysis"?`<label for="trial-parameters">可选参数 JSON</label><textarea id="trial-parameters" rows="2">${esc(trialParameters)}</textarea>`:""}`:""}${["waiting_input","retryable","interrupted","budget","time_limit"].includes(trialState.status) && trialState.checkpoint?`<div class="actions"><button class="primary" id="trial-continue" ${trialBusy?"disabled":""}>${needsReply(trialState)?"提交补充并继续":"从保存状态继续"}</button></div>`:""}${trialState.status==="time_limit"?`<label for="trial-time-limit">累计运行上限（分钟，0 表示不限）</label><input id="trial-time-limit" type="number" min="0" max="1440" value="${Math.ceil((trialState.checkpoint?.elapsed_ms || 0)/60000)+15}">`:""}<ol class="timeline">${(trialState.trace || []).map(e=>`<li><strong>${esc(e.type==="decision"?`${e.step}. ${e.action} ${e.tool || ""}`:e.type==="tool"?`${e.tool} · ${e.status}`:e.type)}</strong>${e.plan?`<ol>${e.plan.map(p=>`<li>${esc(p)}</li>`).join("")}</ol><p>${esc(e.reason)}</p>`:`<details><summary>观察结果</summary><pre>${json(e.result || e.error || e)}</pre></details>`}</li>`).join("")}</ol>`;}
function renderTrial(){const p=$("#trial-progress");if(p){p.hidden=false;p.innerHTML=trialView();}}
refresh();setInterval(()=>{if(tab!=="try" || !selected)refresh();},3000);
