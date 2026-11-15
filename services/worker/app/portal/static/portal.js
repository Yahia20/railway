'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const state={user:null,tab:'chats',offset:0,rubrics:null,agents:[]};
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=x=>x===null||x===undefined?'بيانات غير كافية':new Intl.NumberFormat('ar-EG',{maximumFractionDigits:1}).format(x);
const when=x=>x?new Date(x).toLocaleDateString('ar-EG'):'—';
const statusNames={scored:'تم التقييم',graded:'تم التقييم',claimed:'جاري المعالجة',pending:'لسه متقيّمش',transcribed:'تم التفريغ',empty:'تسجيل فاضي',too_short:'تسجيل قصير',review:'محتاج مراجعة',failed:'تعذرت المعالجة',dead_letter:'محتاج تدخل',single:'موظف واحد',shared:'شات مشترك',uncertain:'الموظف غير مؤكد',not_gradeable:'غير قابل للتقييم',stored:'محفوظ',fetching:'جاري التحميل',retry_wait:'محاولة لاحقة',missing:'التسجيل غير متاح',rejected:'غير صالح'};
const ownerNames={attributed:'معروف صاحبها',journey_not_read:'لسه بنقرأ مين رد',extension_unmapped:'الخط مش مربوط بموظف',mapping_not_approved:'الربط مستني موافقة',account_excluded:'حساب مستبعد من التقييم',answered:'رد عليها موظف واحد',multiple_answering_extensions:'رد عليها أكتر من موظف',no_answer_evidence:'مفيش رد مسجل',answer_identity_missing:'الرد من غير رقم خط',supervision_requires_review:'فيها مراقبة من مشرف',journey_incomplete:'الرحلة ناقصة',malformed:'رد غير مفهوم من السنترال',not_inbound:'مش واردة',claimed:'جاري القراءة',retry_wait:'محاولة لاحقة',failed:'تعذرت القراءة'};
function note(text=''){$('#notice').textContent=text;}
async function api(path,options={}){
  const headers={'Content-Type':'application/json',...(state.user?{'X-CSRF-Token':state.user.csrf_token}:{})};
  const r=await fetch('/portal/api'+path,{...options,headers,credentials:'same-origin',cache:'no-store'});
  let data;try{data=await r.json();}catch{throw Error('الخدمة مش متاحة دلوقتي. جرّب تاني.');}
  if(!r.ok){if(r.status===401&&state.user){state.user=null;showAuth();}throw Error(r.status===429?'محاولات كتير. جرّب بعد ربع ساعة.':r.status===401?'راجع اسم المستخدم وكلمة المرور.':r.status===403?'الطلب ده مش متاح لحسابك.':r.status===503?'البوابة لسه تحت التجهيز.':r.status===404?'المحادثة مش متاحة.':r.status===409?'اسم المستخدم موجود، أو الإجراء غير متاح.':'تعذر تنفيذ الطلب. راجع البيانات وحاول تاني.');}return data;
}
function showAuth(){const u=state.user;$('#login').hidden=!!u;$('#password').hidden=!u?.must_change_password;$('#workspace').hidden=!u||u.must_change_password;$('#logout').hidden=!u;$('#identity').textContent=u?.username||'';}
async function enter(){showAuth();if(!state.user||state.user.must_change_password)return;
  state.agents=await api('/agents');state.rubrics=await api('/rubrics');
  const team=state.user.role!=='agent';$$('[data-team]').forEach(x=>x.hidden=!team);$('#employee-label').hidden=!team;$('#users-tab').hidden=state.user.role!=='manager';
  $('#employee').innerHTML='<option value="">كل الفريق</option>'+state.agents.map(a=>`<option value="${esc(a.agent_id)}">${esc(a.full_name)}</option>`).join('');
  $('#page-title').textContent=team?'أداء الفريق':'تحليل الموظف';state.tab=team?'team':'chats';state.offset=0;await load();
}
function query(){const p=new URLSearchParams({start:$('#start').value,end:$('#end').value,offset:state.offset});if($('#employee').value)p.set('agent_id',$('#employee').value);return p;}
function empty(text='مفيش بيانات للفترة دي'){return `<div class="empty"><h3>${esc(text)}</h3><p>جرّب فترة تانية. المحادثات اللي لسه متقيّمتش مش بتتحسب بصفر.</p></div>`;}
function table(head,rows){return `<div class="table-wrap"><table><thead><tr>${head.map(x=>`<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;}
function badge(x){return `<span class="badge ${['review','shared','uncertain'].includes(x)?'warn':''}">${esc(statusNames[x]||x||'لسه متقيّمش')}</span>`;}
function metric(n,label){return `<div class="metric"><span>${esc(label)}</span><strong>${fmt(n)}</strong></div>`;}
async function load(){note();$$('nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab===state.tab));$('#content').innerHTML='<p>جاري تحميل البيانات…</p>';$('#weekly').hidden=true;$('.pagination').hidden=true;$('#metrics').innerHTML='';
  const tab=state.tab,p=query();
  if(['chats','calls','unattributed'].includes(tab)){
    const s=await api('/summary?'+p);if(tab!==state.tab)return;
    const c=tab==='chats'?s.chats:s.calls;
    $('#metrics').innerHTML=tab==='chats'
      ?metric(c.total,'كل المحادثات')+metric(c.graded,'تم تقييمها')+metric(c.numeric,'لها درجة فردية')+metric(Math.max(0,c.total-c.graded),'لسه متقيّمتش')
      :metric(c.total,'كل المكالمات')+metric(c.minutes,'دقايق مكالمات')+metric(c.graded,'تم تقييمها')+(state.user.role!=='agent'?metric(c.unattributed,'بدون موظف مؤكد'):'');
    const weeks=tab==='chats'?s.weekly_chats:s.weekly_calls;$('#weekly').hidden=false;
    $('#weekly').innerHTML='<h3>متوسط كل أسبوع</h3><p class="muted">'+(tab==='chats'?'متوسط الدرجات الفردية خلال الأسبوع؛ الشات المشترك مستبعد من المتوسط.':'متوسط المكالمات اللي اتعرف مين رد عليها بس؛ المكالمة اللي مش معروف صاحبها مش بتتحسب على حد.')+'</p>'+(weeks.length?`<div class="weeks">${weeks.map(w=>`<div class="week"><small>أسبوع ${when(w.week)}</small><strong>${fmt(w.score)}</strong><small>${fmt(w.count)} محادثة بدرجة</small></div>`).join('')}</div>`:empty('لسه مفيش متوسط أسبوعي'));
    if(tab==='unattributed')p.set('unattributed','true');const kind=tab==='chats'?'chats':'calls',rs=await api('/'+kind+'?'+p);if(tab!==state.tab)return;
    const who=r=>kind==='calls'&&state.user.role!=='agent'?` · ${esc(r.employee||ownerNames[r.attribution_reason]||'بدون موظف')}`:'';
    const mins=r=>r.duration_seconds?` · ${fmt(Math.round(r.duration_seconds/6)/10)} دقيقة`:'';
    $('#content').innerHTML=rs.length?table(['التاريخ','المحادثة','الدرجة','الحالة',''],rs.map(r=>`<tr><td>${when(r.started_at||r.calldate_local)}</td><td class="wrap">${esc(r.title||(kind==='chats'?'شات':r.call_type==='IN'?'مكالمة واردة':'مكالمة صادرة'))}${who(r)}${mins(r)}</td><td>${fmt(r.score)}</td><td>${badge(r.status)} ${r.attribution?badge(r.attribution):''}</td><td><button data-detail="${kind}" data-id="${esc(r.interaction_id||r.uniqueid)}" ${!r.status?'disabled':''}>التفاصيل</button></td></tr>`)):empty();
    $('.pagination').hidden=false;$('#previous').disabled=state.offset===0;$('#next').disabled=rs.length<50;$('#page-number').textContent=`صفحة ${fmt(state.offset/50+1)}`;
  }else if(tab==='team'){
    const rs=await api('/team?'+p);$('#content').innerHTML='<h2>أداء الفريق خلال الفترة</h2><p class="muted">كل متوسط محسوب من المحادثات الفردية المتاحة. افتح الموظف لمتابعة المتوسط أسبوع بأسبوع.</p>'+table(['الموظف','متوسط الشات','شات بدرجة فردية','مكالمات','دقايق','متوسط المكالمات','مكالمات مقيّمة','أخطاء للمراجعة',''],rs.map(r=>`<tr><td>${esc(r.full_name)} ${r.qa_excluded?'<span class="badge warn">مستبعد من التقييم</span>':''}</td><td>${fmt(r.chat_score)}</td><td>${fmt(r.chat_numeric)}</td><td>${fmt(r.call_count)}</td><td>${fmt(r.call_minutes)}</td><td>${fmt(r.call_score)}</td><td>${fmt(r.call_graded)}</td><td>${fmt(r.critical_count)}</td><td><button data-agent="${esc(r.agent_id)}">فتح الملف</button></td></tr>`));
  }else if(tab==='health'){
    const h=await api('/health');const mode=k=>h.config.find(x=>x.key===k)?.value==='on'?'شغالة':'متوقفة';
    const block=(title,rows,names)=>`<h3>${esc(title)}</h3>`+(rows.length?table(['الحالة','العدد'],rows.map(r=>`<tr><td>${esc((names||statusNames)[r.status]||r.status)}</td><td>${fmt(r.count)}</td></tr>`)):'<p>لسه مفيش.</p>');
    $('#content').innerHTML='<h2>حالة معالجة المكالمات</h2>'
      +`<p>قراءة "مين رد": ${mode('journey_mode')} · تفريغ الصوت: ${mode('audio_mode')} · التقييم: ${mode('grade_mode')}</p>`
      +`<p>المصروف على المكالمات لحد دلوقتي: ${fmt(h.spend.spent_usd)}$ من حد ${fmt(h.spend.cap_usd)}$ (موديلات ${fmt(h.spend.model_usd)}$ · صوت ${fmt(h.spend.audio_usd)}$).</p>`
      +block('تحميل التسجيلات',h.recordings)+block('مين رد على المكالمة',h.owners,ownerNames)+block('قراءة رحلة المكالمة',h.journeys,ownerNames)+block('التفريغ والتقييم',h.processing);
  }else if(tab==='users')await userList();
}
async function userList(){const rs=await api('/users');$('#content').innerHTML='<h2>حسابات الفريق</h2><p>كلمة المرور المؤقتة بتظهر مرة واحدة، ولازم تتغيّر عند أول دخول.</p><form id="new-user" class="account-form"><label>اسم المستخدم<input name="username" required pattern="[a-z0-9][a-z0-9._-]{2,99}" dir="ltr"></label><label>الصلاحية<select name="role"><option value="agent">موظف</option><option value="leader">قائد فريق</option><option value="manager">مدير</option></select></label><label>الموظف<select name="agent_id"><option value="">اختار الموظف</option>'+state.agents.map(a=>`<option value="${esc(a.agent_id)}">${esc(a.full_name)}</option>`).join('')+'</select></label><button class="primary">إنشاء حساب</button></form><div id="account-result" role="status"></div>'+table(['الحساب','الصلاحية','الحالة',''],rs.map(r=>`<tr><td>${esc(r.username)}</td><td>${esc({agent:'موظف',leader:'قائد فريق',manager:'مدير'}[r.role])}</td><td>${r.active?'نشط':'متوقف'}</td><td><button data-reset="${esc(r.user_id)}">كلمة مرور جديدة</button> <button data-disable="${esc(r.user_id)}" ${!r.active||r.user_id===state.user.user_id?'disabled':''}>تعطيل</button></td></tr>`));}
function linesButtons(lines){return (lines||[]).map(n=>`<button data-line="${esc(n)}">سطر ${esc(n)}</button>`).join('');}
async function details(kind,id){$('#detail-title').textContent=kind==='chats'?'تفاصيل الشات':'تفاصيل المكالمة';$('#detail-content').textContent='جاري التحميل…';$('#detail').showModal();const d=await api('/'+kind+'/'+encodeURIComponent(id));
  let checks='',messages='',alerts='',extras='';
  if(kind==='chats'){
    const labels=Object.fromEntries(state.rubrics.chat.flatMap(x=>x.checks.map(c=>[c.key,c.ar])));
    checks=Object.entries(d.result?.checks||{}).map(([k,c])=>`<div class="check"><strong>${esc(labels[k]||k)}</strong><p>${c.status==='review'?'محتاج مراجعة':c.v===null?'غير منطبق':c.v===1?'متحقق':c.v===0?'غير متحقق':`متحقق جزئيًا: ${fmt(c.v*100)}٪`}</p>${linesButtons(c.lines)}</div>`).join('');
    const alertNames={review:'محتاج مراجعة',wrong_customer:'بيانات عميل تاني',contradiction:'معلومات متعارضة',misleading:'معلومة محتاجة تحقق',no_consent:'موافقة العميل غير واضحة',calc_error:'حساب محتاج مراجعة',impatience:'العميل مستعجل',complaint:'شكوى من العميل',form_repeat:'تكرار طلب البيانات',sent_away:'إحالة محتاجة مراجعة',no_alternative:'مفيش بديل معروض'};
    alerts=(d.result?.alerts||[]).map(a=>`<div class="check"><span class="badge warn">${esc(alertNames[a.kind]||'ملاحظة للمراجعة')}</span>${linesButtons(a.lines)}</div>`).join('');
    messages=(d.transcript?.messages||[]).map((m,i)=>`<div id="line-${i+1}" class="message ${m.role==='agent'?'agent':'customer'}"><small>${i+1} · ${esc(m.role==='agent'?'الموظف':m.role==='customer'?'العميل':'النظام')} · ${esc(m.time||'')}</small>${esc(m.text)}</div>`).join('');
  }else{
    const items=Array.isArray(d.items)?d.items:Object.values(d.items||{});checks=items.map(c=>`<div class="check"><strong>${esc(c.label||state.rubrics.call.items.find(x=>x.id===c.id)?.label||c.id)}</strong><p>${c.status==='review'?'محتاج مراجعة':c.status==='na'?'غير منطبق':fmt(c.score)+' / 5'}</p>${linesButtons(c.lines)}</div>`).join('');
    messages=(d.transcript?.segments||[]).map((m,i)=>`<div id="line-${esc(m.id||i+1)}" class="message ${m.speaker==='agent'?'agent':'customer'}"><small>${esc(m.id||i+1)} · ${m.speaker==='agent'?'الموظف':'العميل'} · ${fmt(m.start)} ثانية</small>${esc(m.text)}</div>`).join('');
    alerts=(d.critical_errors||[]).map(a=>`<div class="check"><span class="badge bad">مراجعة خطأ جوهري</span><p>${esc(a.reason||a.kind)}</p>${linesButtons(a.lines)}</div>`).join('');
    if(d.metrics)extras=`<p>وقت الكلام: الموظف ${fmt(d.metrics.agent_speech_seconds)} ث · العميل ${fmt(d.metrics.customer_speech_seconds)} ث · الصمت ${fmt(d.metrics.silence_seconds)} ث · التداخل ${fmt(d.metrics.overlap_seconds)} ث</p>`;
    if(d.audio_available)extras+='<button id="play-audio">تشغيل التسجيل: الموظف شمال / العميل يمين</button><div id="audio-player"></div>';
    extras+=`<p>الوزن اللي اتقيّم: ${fmt(d.assessed_weight)} من 100.</p>`;
  }
  $('#detail-content').innerHTML=`<p>الدرجة: <strong>${fmt(d.score)}</strong> ${badge(d.status)} ${d.attribution?badge(d.attribution):''}</p>${extras}<div class="detail-grid"><section class="card"><h3>بنود التقييم</h3>${checks||empty('التقييم لسه مش متاح')}<h3>ملاحظات للمراجعة</h3>${alerts||'<p>مفيش ملاحظات مسجلة.</p>'}</section><section><h3>نص المحادثة</h3>${messages||'<p>النص غير متاح، أو انتهت مدة الاحتفاظ بيه.</p>'}</section></div>`;
  if($('#play-audio'))$('#play-audio').onclick=()=>guard(async()=>{const a=await api('/calls/'+encodeURIComponent(id)+'/audio');const player=document.createElement('audio');player.controls=true;player.src=a.url;$('#audio-player').replaceChildren(player);});
}
async function guard(fn){try{await fn();}catch(e){note(e.message);}}
$('#login-form').onsubmit=e=>{e.preventDefault();guard(async()=>{state.user=await api('/login',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});e.target.reset();note();await enter();});};
$('#password-form').onsubmit=e=>{e.preventDefault();guard(async()=>{await api('/password',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});e.target.reset();state.user=null;showAuth();note('تم تغيير كلمة المرور. سجّل دخولك بالكلمة الجديدة.');});};
$('#logout').onclick=()=>guard(async()=>{await api('/logout',{method:'POST'});state.user=null;$('#detail').close();$('#detail-content').replaceChildren();$('#content').replaceChildren();showAuth();});
$('#refresh').onclick=()=>guard(async()=>{state.offset=0;await load();});
$('#previous').onclick=()=>guard(async()=>{state.offset=Math.max(0,state.offset-50);await load();});$('#next').onclick=()=>guard(async()=>{state.offset+=50;await load();});
$('#close-detail').onclick=()=>{$('#detail').close();$('#detail-content').replaceChildren();};
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;guard(async()=>{
  if(b.dataset.tab){state.tab=b.dataset.tab;state.offset=0;await load();}
  if(b.dataset.agent){$('#employee').value=b.dataset.agent;state.tab='chats';state.offset=0;await load();}
  if(b.dataset.detail)await details(b.dataset.detail,b.dataset.id);
  if(b.dataset.line){$$('.message.highlight').forEach(x=>x.classList.remove('highlight'));const line=document.getElementById('line-'+b.dataset.line);if(line){line.classList.add('highlight');line.scrollIntoView({behavior:'smooth',block:'center'});}}
  if(b.dataset.disable&&confirm('تعطيل الحساب وإيقاف جلساته الحالية؟')){await api('/users/'+b.dataset.disable+'/disable',{method:'POST'});await userList();}
  if(b.dataset.reset&&confirm('إصدار كلمة مرور مؤقتة وإيقاف الجلسات الحالية؟')){const r=await api('/users/'+b.dataset.reset+'/reset',{method:'POST'});$('#account-result').className='secret';$('#account-result').textContent='كلمة المرور المؤقتة: '+r.temporary_password;}
});});
document.addEventListener('submit',e=>{if(e.target.id!=='new-user')return;e.preventDefault();guard(async()=>{const data=Object.fromEntries(new FormData(e.target));data.agent_id=data.agent_id||null;const r=await api('/users',{method:'POST',body:JSON.stringify(data)});await userList();$('#account-result').className='secret';$('#account-result').textContent='الحساب: '+data.username+'\nكلمة المرور المؤقتة: '+r.temporary_password;});});
$('#theme').onclick=()=>{const dark=document.documentElement.dataset.theme!=='dark';document.documentElement.dataset.theme=dark?'dark':'light';localStorage.setItem('portal-theme',dark?'dark':'light');};
document.documentElement.dataset.theme=localStorage.getItem('portal-theme')||'light';
const today=new Date(),start=new Date(today);start.setDate(start.getDate()-27);$('#end').value=today.toISOString().slice(0,10);$('#start').value=start.toISOString().slice(0,10);
guard(async()=>{try{state.user=await api('/me');}catch{}await enter();});
