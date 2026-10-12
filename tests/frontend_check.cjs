/* Render and event-state checks with a minimal DOM fixture; no browser dependency. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
const get = name => {
  if (!elements.has(name)) elements.set(name, {innerHTML:'',textContent:'',hidden:false,value:'',children:[],classList:{toggle(){}},showModal(){},close(){}});
  return elements.get(name);
};
get('#tabs').children = ['overview','workflow','code','tests','versions','try'].map(tab=>({dataset:{tab},classList:{toggle(){}}}));
const fixture = {run_id:'abcdef123456',status:'completed',version:1,phase:'completed',repairs:0,max_repairs:3,created_at:1,
  request:'<script>alert(1)</script>',requirements:{task_type:'document_qa',acceptance_criteria:['引用原文']},
  resources:{documents:[{id:'p'}],datasets:[]},spec:{name:'<img src=x onerror=alert(1)>',entry:'search',max_steps:8,tools:['documents.search','code.transform'],
    nodes:[{id:'search',tool:'documents.search',arguments:{question:{ref:'input.question'}},next:'code',on_empty:null,on_error:null}]},
  report:{partition:'development',passed:1,total:1,cases:[{id:'positive',passed:true,purpose:'有来源',criterion:'引用',input:{question:'政策'},actual:{answer:'text'}}]},
  acceptance_report:null,diagnosis:null,events:[],source:'def run(context):\n    return context["result"]\n',
  changes:[{version:1,explanation:'创建',code_diff:'<unsafe>',artifact_hash:'hash',workflow_before:null,workflow_after:{}}]};
const requests=[];
const memory=new Map();
const context = vm.createContext({document:{querySelector:get,activeElement:null},fixture,console,setInterval(){},
  sessionStorage:{getItem:k=>memory.get(k),setItem:(k,v)=>memory.set(k,v)},
  fetch:async path=>{requests.push(path);return {ok:true,json:async()=>path.startsWith('/api/jobs?')?
    {jobs:[],total:451,status_counts:{queued:7,completed:444},next_cursor:path.includes('cursor=')?null:'page-2'}:fixture};}});
vm.runInContext(fs.readFileSync('agentforge/web/app.js','utf8'),context);
for (const tab of ['overview','workflow','code','tests','versions','try']) {
  vm.runInContext(`detail = fixture; tab = '${tab}'; renderDetail();`,context);
  assert.ok(get('#detail-content').innerHTML.length > 20,tab);
  assert.ok(!get('#detail-head').innerHTML.includes('<img src=x'), 'unsafe name escaped');
  assert.ok(!get('#detail-content').innerHTML.includes('<script>alert'), 'unsafe request escaped');
}
vm.runInContext('notice("失败证据");',context);
assert.equal(get('#notice').textContent,'失败证据');
assert.equal(get('#notice').hidden,false);
vm.runInContext('notice("");',context);
assert.equal(get('#notice').hidden,true);
const adaptive = {...fixture,spec:{name:'Dynamic agent',runtime:'codex_loop',max_steps:8,instructions:'Use <evidence>',
  planning_hints:['Inspect','Compute','Verify'],tools:['csv.query','custom.quality'],custom_tools:[{id:'custom.quality',description:'Check missing cells',
    required:true,parameters:[],output_schema:{type:'object'},source:'def run(context):\n    return {"missing": 0}\n'}]},
  changes:[{...fixture.changes[0],tool_diffs:{'custom.quality':'+ actual code'}}]};
context.adaptive=adaptive;
for(const tab of ['workflow','code','versions','try']){
  vm.runInContext(`detail=adaptive;tab='${tab}';renderDetail();`,context);
  const html=get('#detail-content').innerHTML;
  assert.ok(html.includes(tab==='workflow'?'Codex 决策循环':tab==='code'?'custom_tools/quality.py':tab==='versions'?'actual code':'运行记录'));
}
vm.runInContext('trialState={status:"running",trace:[{type:"decision",step:1,action:"call_tool",tool:"custom.quality",plan:["<unsafe>"],reason:"Evidence"}]};renderTrial();',context);
assert.ok(get('#trial-progress').innerHTML.includes('custom.quality'));
assert.ok(get('#trial-progress').innerHTML.includes('&lt;unsafe&gt;'));
vm.runInContext('trialState={execution_id:"abc",status:"waiting_input",checkpoint:{},error:{message:"请选择指标"},trace:[]};trialReply="<unsafe reply>";renderTrial();',context);
assert.ok(get('#trial-progress').innerHTML.includes('trial-continue'));
assert.ok(get('#trial-progress').innerHTML.includes('&lt;unsafe reply&gt;'));
context.benchFixture={...adaptive,benchmarks:[{suite_version:'unseen-v1',passed:0,total:1,version:1,cases:[{id:'unseen',passed:false,issues:['<unsafe issue>']}]}]};
vm.runInContext('detail=benchFixture;tab="tests";renderDetail();',context);
assert.ok(get('#detail-content').innerHTML.includes('独立陌生任务评测'));
assert.ok(get('#detail-content').innerHTML.includes('&lt;unsafe issue&gt;'));
context.recoveryFixture={...adaptive,evaluation_reports:[{partition:'acceptance',passed:1,total:2,new_model_calls:2,
  execution_conditions:{max_seconds:0.001,model_configuration:{requested_model:'model-b'}},
  condition_change:{rule:'tightened_runtime_limit',previous:{max_seconds:100}},
  reused_cases:['finished'],resumed_cases:['interrupted'],cases:[
    {id:'host-negative',kind:'host_contract',passed:true,input:{rows:'not-an-array'},
      expected_error:{code:'INVALID_ARGUMENTS',origin:'decision',layer:'host'},actual:null,
      failure:{code:'INVALID_ARGUMENTS',details:{field_path:'input.rows',expected_type:'array',actual_type:'string'}}},
    {id:'interrupted',passed:false,issues:['模型调用中断'],actual:{error:{code:'MODEL_ERROR'},
      model_failure:{category:'network',exit_code:1,basis:{source:'stdout.json_event',signal:'network_error'}}}}]}],
  evaluation_recovery:[{partition:'acceptance',completed:['<unsafe-id>'],interrupted:['interrupted'],inflight:[],
    last_attempt:{executed:['interrupted'],reused:['finished'],resumed:['interrupted']}}]};
vm.runInContext('detail=recoveryFixture;tab="tests";renderDetail();',context);
const recoveryHTML=get('#detail-content').innerHTML;
assert.ok(recoveryHTML.includes('宿主拒绝验证'));
assert.ok(recoveryHTML.includes('expected_error'));
assert.ok(recoveryHTML.includes('本次模型调用 2'));
assert.ok(recoveryHTML.includes('调用中断 1'));
assert.ok(recoveryHTML.includes('network_error'));
assert.ok(recoveryHTML.includes('0.001 秒'));
assert.ok(recoveryHTML.includes('model-b'));
assert.ok(recoveryHTML.includes('tightened_runtime_limit'));
assert.ok(recoveryHTML.includes('&lt;unsafe-id&gt;'));
assert.ok(!recoveryHTML.includes('<unsafe-id>'));
assert.ok(!recoveryHTML.includes('positive'), 'current interrupted report replaces stale state report');
context.stoppedFixture={...adaptive,status:'stopped',phase:'diagnosis',repairs:3,max_repairs:3,
  performance:{elapsed_ms:62000,max_seconds:3600}};
vm.runInContext('detail=stoppedFixture;tab="overview";renderDetail();',context);
assert.ok(get('#detail-content').innerHTML.includes('id="repair-limit"'));
assert.ok(get('#detail-content').innerHTML.includes('id="repair-scope"'));
assert.ok(get('#detail-content').innerHTML.includes('id="retry"'));
assert.ok(get('#detail-head').innerHTML.includes('累计耗时 62 秒'));
for (const kind of ['direct','program']) {
  context.deliveryFixture={...fixture,schema_version:5,spec:null,requirements:null,delivery:{kind,reason:'<route>'},
    frozen_hashes:{resources:'hash'},delivery_recipe:{parameters:{region:{type:'string'}}},
    delivery_result:{answer:'<script>unsafe</script>'},delivery_validation:{scope:'not_evaluated',execution_check:'passed'},
    call_metrics:{model_calls:0},executions:[]};
  for(const name of ['overview','workflow','code','tests','versions','try']) {
    vm.runInContext(`detail=deliveryFixture;tab='${name}';renderDetail();`,context);
    const html=get('#detail-content').innerHTML;
    assert.ok(html.length>20);
    assert.ok(!html.includes('<script>unsafe</script>'));
    if(name==='tests')assert.ok(html.includes('尚无独立业务验收'));
    if(name==='try')assert.equal(html.includes('id="trial-run"'),kind==='program');
  }
}
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(get('#job-page').textContent.includes('451'));
  assert.equal(get('#jobs-next').disabled,false);
  get('#jobs-next').onclick();
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(requests.some(p=>p.includes('cursor=page-2')));
  assert.ok(get('#job-page').textContent.includes('第 2 页'));
  assert.equal(get('#jobs-next').disabled,true);
  assert.equal(JSON.parse(memory.get('agentforge.jobPage')).page,1);
  get('#jobs-prev').onclick();
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(get('#job-page').textContent.includes('第 1 页'));
  console.log('Frontend rendering, HTML escaping, status controls and pagination recovery passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
