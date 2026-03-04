/* Mimicry v5 — Frontend */
'use strict';

const API         = '';
const POLL_MS     = 2500;
const CHUNK_CHARS = 180;

// ── API key support (set via ?key=… in URL or localStorage) ───────────────
(function(){
  const p = new URLSearchParams(location.search).get('key');
  if(p){ localStorage.setItem('mimicry_key', p);
         history.replaceState(null,'',location.pathname); }
})();
const _apiKey = () => localStorage.getItem('mimicry_key') || '';

async function apiFetchRaw(path, opts={}){
  const key = _apiKey();
  if(key){
    opts.headers = Object.assign({}, opts.headers||{}, {'Authorization': `Bearer ${key}`});
  }
  return fetch(API+path, opts);
}

// ── State ──────────────────────────────────────────────────────────────────
let selectedVoiceId   = null;
let selectedVoiceName = null;
let selectedFile      = null;
let editingVoiceId    = null;
let synthJobId        = null;
let batchJobId        = null;
let synthPollTimer    = null;
let batchPollTimer    = null;
let statusPollTimer   = null;
let jobsRefreshTimer  = null;
let cachedVoices      = [];       // for mix dropdowns

// ── DOM helpers ────────────────────────────────────────────────────────────
const $  = id => document.getElementById(id);
const esc = s => String(s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const timeAgo = ts => {
  const d = Math.round(Date.now()/1000 - ts);
  if(d<60) return `${d}s ago`; if(d<3600) return `${Math.round(d/60)}m ago`;
  if(d<86400) return `${Math.round(d/3600)}h ago`; return `${Math.round(d/86400)}d ago`;
};
const estimateChunks = t =>
  t.split(/(?<=[.!?])\s+/).reduce((n,s)=>n+Math.ceil((s.length||1)/CHUNK_CHARS),0);

async function apiFetch(path, opts={}) {
  const r = await apiFetchRaw(path, opts);
  if(!r.ok){ const b=await r.json().catch(()=>({})); throw new Error(b.detail||`HTTP ${r.status}`); }
  return r.json();
}

// ── Status ─────────────────────────────────────────────────────────────────
const statusBadge = $('status-badge'), modelLoadingNotice = $('model-loading-notice');
const queueBadge  = $('queue-badge');

async function checkStatus() {
  try {
    const d = await apiFetch('/api/status');
    if(d.model_loaded){
      statusBadge.textContent='● Ready'; statusBadge.className='badge badge-ready';
      if(modelLoadingNotice) modelLoadingNotice.style.display='none';
      clearInterval(statusPollTimer); updateSynthBtn(); updateBatchBtn();
    } else {
      statusBadge.textContent='⏳ Loading…'; statusBadge.className='badge badge-loading';
      if(modelLoadingNotice) modelLoadingNotice.style.display='flex';
    }
  } catch { statusBadge.textContent='⚠ Offline'; statusBadge.className='badge badge-error'; }
}
statusPollTimer = setInterval(checkStatus, 3000);
checkStatus();

// ── Tabs ───────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p=>{ p.classList.remove('active'); p.style.display='none'; });
  tab.classList.add('active');
  const p = $('tab-'+tab.dataset.tab);
  if(p){ p.style.display='flex'; }
  if(tab.dataset.tab==='jobs') loadJobs();
  if(tab.dataset.tab==='mix') populateMixDropdowns();
}));

// ── Voice Library ──────────────────────────────────────────────────────────
const voiceList = $('voice-list'), voicesLoading = $('voices-loading'), voicesEmpty = $('voices-empty');
const selectedVoiceDisp = $('selected-voice-display');
const batchVoiceDisp    = $('batch-voice-display');

async function loadVoices() {
  voicesLoading.style.display='block'; voicesEmpty.style.display='none'; voiceList.innerHTML='';
  try {
    cachedVoices = await apiFetch('/api/voices');
    voicesLoading.style.display='none';
    if(!cachedVoices.length){ voicesEmpty.style.display='block'; return; }
    cachedVoices.forEach(renderVoiceItem);
  } catch { voicesLoading.textContent='Failed to load voices.'; }
}

function renderVoiceItem(voice) {
  const li = document.createElement('li');
  li.className = 'voice-item'+(voice.id===selectedVoiceId?' active':'');
  li.dataset.id = voice.id;
  const init    = (voice.name||'?').slice(0,2).toUpperCase();
  const dur     = voice.duration?`${voice.duration}s`:'';
  const refSnip = voice.ref_text
    ?(voice.ref_text.slice(0,50)+(voice.ref_text.length>50?'…':'')):'no transcript';
  const warnHtml = voice.warnings?.length
    ?`<div class="voice-warn">⚠ ${esc(voice.warnings[0].slice(0,55))}</div>`:'';
  const mixTag = voice.is_mix
    ?`<span class="mix-tag">mix ${Math.round(voice.mix_alpha*100)}/${Math.round((1-voice.mix_alpha)*100)}</span>`:'';
  const avatarClass = voice.is_mix ? 'voice-avatar mix-avatar' : 'voice-avatar';

  li.innerHTML=`
    <div class="${avatarClass}">${esc(init)}</div>
    <div class="voice-info">
      <div class="voice-name">${esc(voice.name)}${mixTag}</div>
      <div class="voice-meta">${dur}</div>
      <div class="voice-ref" title="${esc(voice.ref_text||'')}">${esc(refSnip)}</div>
      ${warnHtml}
    </div>
    <div class="voice-actions">
      <button class="icon-btn preview" title="Preview reference audio">▶</button>
      <button class="icon-btn edit"   title="Edit reference text">✏</button>
      <button class="icon-btn export" title="Export .mimicry">⬇</button>
      <button class="icon-btn delete" title="Delete">✕</button>
    </div>`;

  li.addEventListener('click', e=>{
    if(e.target.closest('.voice-actions')) return;
    selectVoice(voice);
    document.querySelectorAll('.voice-item').forEach(el=>el.classList.remove('active'));
    li.classList.add('active');
  });
  li.querySelector('.icon-btn.preview').addEventListener('click', e=>{ e.stopPropagation(); previewVoice(voice); });
  li.querySelector('.icon-btn.edit').addEventListener('click',   ()=>openRefModal(voice));
  li.querySelector('.icon-btn.export').addEventListener('click', ()=>exportVoice(voice));
  li.querySelector('.icon-btn.delete').addEventListener('click', ()=>deleteVoice(voice.id,li));
  voiceList.appendChild(li);
  voicesEmpty.style.display='none';

  // keep cache in sync
  if(!cachedVoices.find(v=>v.id===voice.id)) cachedVoices.push(voice);
}

function selectVoice(voice) {
  selectedVoiceId=voice.id; selectedVoiceName=voice.name;
  selectedVoiceDisp.textContent=voice.name; selectedVoiceDisp.classList.remove('empty');
  batchVoiceDisp.textContent=voice.name;    batchVoiceDisp.classList.remove('empty');
  updateSynthBtn(); updateBatchBtn();
}

function previewVoice(voice) {
  const url = `${API}/api/voices/${voice.id}/audio`;
  $('audio-label').textContent = `Reference: ${voice.name}`;
  audioPlayer.src = url;
  $('wm-badge').style.display = 'none';
  $('download-wav-btn').style.display = 'none';
  downloadBtn.href = url;
  downloadBtn.download = `${voice.name}_reference.wav`;
  downloadBtn.textContent = '⬇ WAV';
  audioResult.style.display = 'flex';
  audioPlayer.play().catch(()=>{});
}

async function deleteVoice(id, li) {
  if(!confirm('Delete this voice?')) return;
  try {
    await apiFetch(`/api/voices/${id}`,{method:'DELETE'});
    li.remove(); cachedVoices=cachedVoices.filter(v=>v.id!==id);
    if(selectedVoiceId===id){
      selectedVoiceId=null; selectedVoiceName=null;
      selectedVoiceDisp.textContent='← pick a voice'; selectedVoiceDisp.classList.add('empty');
      batchVoiceDisp.textContent='← pick a voice';   batchVoiceDisp.classList.add('empty');
      updateSynthBtn(); updateBatchBtn();
    }
    if(!voiceList.children.length) voicesEmpty.style.display='block';
  } catch(e){ showError(e.message); }
}

function exportVoice(voice) {
  const a=document.createElement('a'); a.href=`${API}/api/voices/${voice.id}/export`;
  a.download=`${voice.name}.mimicry`; a.click();
}

// ── Import ──────────────────────────────────────────────────────────────────
$('import-input').addEventListener('change', async () => {
  const file=$('import-input').files[0]; if(!file) return;
  const fd=new FormData(); fd.append('file',file);
  try {
    const voice=await apiFetch('/api/voices/import',{method:'POST',body:fd});
    renderVoiceItem(voice); selectVoice(voice);
    document.querySelectorAll('.voice-item').forEach(el=>el.classList.remove('active'));
    voiceList.querySelector(`[data-id="${voice.id}"]`)?.classList.add('active');
  } catch(e){ showError(e.message); }
  $('import-input').value='';
});

// ── Upload ─────────────────────────────────────────────────────────────────
const uploadZone=$('upload-zone'), audioInput=$('audio-input'), uploadForm=$('upload-form');
const filePreview=$('file-preview'), uploadWarnings=$('upload-warnings');
const voiceNameInput=$('voice-name-input'), refTextInput=$('ref-text-input');

uploadZone.addEventListener('click', ()=>audioInput.click());
uploadZone.addEventListener('dragover',  e=>{e.preventDefault();uploadZone.classList.add('drag-over');});
uploadZone.addEventListener('dragleave', ()=>uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop', e=>{ e.preventDefault(); uploadZone.classList.remove('drag-over'); if(e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]); });
audioInput.addEventListener('change', ()=>{ if(audioInput.files[0]) handleFile(audioInput.files[0]); });

function handleFile(file) {
  const ext=file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
  if(!['.wav','.mp3','.m4a','.ogg'].includes(ext)){ showError(`Unsupported format "${ext}".`); return; }
  selectedFile=file;
  filePreview.textContent=`📎 ${file.name}`;
  voiceNameInput.value=file.name.replace(/\.[^.]+$/,'').replace(/[_-]/g,' ');
  refTextInput.value='';
  uploadWarnings.style.display='none'; uploadWarnings.innerHTML='';
  uploadZone.style.display='none'; uploadForm.style.display='flex'; voiceNameInput.focus();
}

$('upload-cancel').addEventListener('click', resetUpload);
$('upload-submit').addEventListener('click', async () => {
  const name=voiceNameInput.value.trim();
  if(!name||!selectedFile) return;
  $('upload-btn-text').textContent='Saving…'; $('upload-submit').disabled=true;
  $('upload-spinner').style.display='inline-block';
  const fd=new FormData(); fd.append('name',name); fd.append('audio',selectedFile);
  if(refTextInput.value.trim()) fd.append('ref_text',refTextInput.value.trim());
  try {
    const voice=await apiFetch('/api/voices',{method:'POST',body:fd});
    resetUpload(); renderVoiceItem(voice); selectVoice(voice);
    document.querySelectorAll('.voice-item').forEach(el=>el.classList.remove('active'));
    voiceList.querySelector(`[data-id="${voice.id}"]`)?.classList.add('active');
    if(voice.warnings?.length){
      uploadWarnings.innerHTML='<strong>Audio warnings:</strong><ul>'
        +voice.warnings.map(w=>`<li>${esc(w)}</li>`).join('')+'</ul>';
      uploadWarnings.style.display='block';
    }
  } catch(e){ showError(e.message); }
  finally { $('upload-btn-text').textContent='Save Voice'; $('upload-submit').disabled=false; $('upload-spinner').style.display='none'; }
});

function resetUpload() {
  selectedFile=null; audioInput.value=''; voiceNameInput.value=''; refTextInput.value='';
  uploadWarnings.style.display='none'; uploadForm.style.display='none'; uploadZone.style.display='block';
}

// ── Ref-text modal ─────────────────────────────────────────────────────────
const refModalOverlay=$('ref-modal-overlay'), modalRefText=$('modal-ref-text');
function openRefModal(v){ editingVoiceId=v.id; modalRefText.value=v.ref_text||''; refModalOverlay.style.display='flex'; setTimeout(()=>modalRefText.focus(),50); }
function closeRefModal(){ refModalOverlay.style.display='none'; editingVoiceId=null; }
$('modal-cancel').addEventListener('click', closeRefModal);
refModalOverlay.addEventListener('click', e=>{ if(e.target===refModalOverlay) closeRefModal(); });
$('modal-save').addEventListener('click', async () => {
  if(!editingVoiceId) return;
  try {
    const updated=await apiFetch(`/api/voices/${editingVoiceId}`,{ method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ref_text:modalRefText.value}) });
    const li=voiceList.querySelector(`[data-id="${editingVoiceId}"]`);
    if(li){ li.remove(); cachedVoices=cachedVoices.filter(v=>v.id!==editingVoiceId); renderVoiceItem(updated); if(selectedVoiceId===updated.id) selectVoice(updated); }
    closeRefModal();
  } catch(e){ showError(e.message); }
});

// ── Speed sliders ──────────────────────────────────────────────────────────
function wireSlider(sliderId, valId, resetId) {
  const s=$('slider-'+sliderId||sliderId), v=$(valId), r=$(resetId);
  const sl=s||$('speed-slider');  // fallback
  [sliderId].forEach(id=>{ const el=$(id); if(!el) return;
    el.addEventListener('input',()=>{ $(valId).textContent=parseFloat(el.value).toFixed(2)+'×'; });
    $(resetId)?.addEventListener('click',()=>{ el.value=1.0; $(valId).textContent='1.00×'; });
  });
}
$('speed-slider').addEventListener('input',()=>{ $('speed-val').textContent=parseFloat($('speed-slider').value).toFixed(2)+'×'; });
$('speed-reset').addEventListener('click',()=>{ $('speed-slider').value=1.0; $('speed-val').textContent='1.00×'; });
$('batch-speed-slider').addEventListener('input',()=>{ $('batch-speed-val').textContent=parseFloat($('batch-speed-slider').value).toFixed(2)+'×'; });
$('batch-speed-reset').addEventListener('click',()=>{ $('batch-speed-slider').value=1.0; $('batch-speed-val').textContent='1.00×'; });

// ── Synth ──────────────────────────────────────────────────────────────────
const textInput=$('text-input'), synthesizeBtn=$('synthesize-btn');
const synthProgress=$('synth-progress'), audioResult=$('audio-result');
const audioPlayer=$('audio-player'), downloadBtn=$('download-btn');
const waveformBars=$('waveform-bars'), errorBanner=$('error-banner');
const retryBtn=$('retry-btn');

let lastSynthParams = null;

textInput.addEventListener('input',()=>{
  const len=textInput.value.length, chunks=estimateChunks(textInput.value.trim());
  $('char-count').textContent=len;
  $('chunk-hint').style.display=(len>0&&chunks>1)?'inline':'none';
  if(chunks>1) $('chunk-hint').textContent=`~${chunks} chunks`;
  updateSynthBtn();
});

function updateSynthBtn(){ synthesizeBtn.disabled=!(selectedVoiceId&&textInput.value.trim()); }

synthesizeBtn.addEventListener('click', async ()=>{
  const text=textInput.value.trim(); if(!text||!selectedVoiceId) return;
  lastSynthParams={text, voice_id:selectedVoiceId, language:$('lang-select').value, speed:parseFloat($('speed-slider').value)};
  doSynth(lastSynthParams);
});

retryBtn.addEventListener('click', ()=>{
  if(!lastSynthParams) return;
  retryBtn.style.display='none';
  $('error-banner').style.display='none';
  doSynth(lastSynthParams);
});

async function doSynth(params){
  $('error-banner').style.display='none'; audioResult.style.display='none'; synthProgress.style.display='none';
  retryBtn.style.display='none';
  setSynthLoading(true);
  try {
    const {job_id}=await apiFetch('/api/synthesize',{ method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify(params) });
    synthJobId=job_id; synthProgress.style.display='flex'; startSynthPoll(job_id);
  } catch(e){ showError(e.message); setSynthLoading(false); }
}

function startSynthPoll(jid) {
  clearInterval(synthPollTimer);
  synthPollTimer=setInterval(async()=>{
    try {
      const job=await apiFetch(`/api/jobs/${jid}`);
      const labels={pending:'Queued…',running:'Generating speech…',done:'Done!',failed:'Failed'};
      $('progress-label').textContent=labels[job.status]||job.status;
      if(job.status==='done'){
        clearInterval(synthPollTimer); setSynthLoading(false); synthProgress.style.display='none';
        showAudioResult(job); loadHistory(); updateQueueBadge();
      } else if(job.status==='failed'){
        clearInterval(synthPollTimer); setSynthLoading(false); synthProgress.style.display='none';
        showError(job.error||'Synthesis failed.');
        if(lastSynthParams) retryBtn.style.display='inline-flex';
        updateQueueBadge();
      }
    } catch(e){ clearInterval(synthPollTimer); setSynthLoading(false); showError(e.message); }
  }, POLL_MS);
}

function setSynthLoading(on){
  synthesizeBtn.disabled=on;
  $('synth-btn-text').textContent=on?'Working…':'Synthesize';
  $('synth-spinner').style.display=on?'inline-block':'none';
  updateQueueBadge();
}

function showAudioResult(job){
  const wavUrl = `${API}${job.audio_url}`;
  const mp3Url = job.mp3_url ? `${API}${job.mp3_url}` : null;
  const playUrl = wavUrl;  // always play WAV (watermark-intact)

  audioPlayer.src = playUrl;
  $('audio-label').textContent=`${job.voice_name} · ${(job.language||'en').toUpperCase()} · ${job.speed||1}×`;

  if(mp3Url){
    downloadBtn.href = mp3Url;
    downloadBtn.download = (job.filename||'output.wav').replace('.wav','.mp3');
    downloadBtn.textContent = '⬇ MP3';
    $('download-wav-btn').href = wavUrl;
    $('download-wav-btn').download = job.filename||'output.wav';
    $('download-wav-btn').style.display = 'inline-flex';
  } else {
    downloadBtn.href = wavUrl;
    downloadBtn.download = job.filename||'output.wav';
    downloadBtn.textContent = '⬇ WAV';
    $('download-wav-btn').style.display = 'none';
  }

  if(job.watermark_id){
    const wb=$('wm-badge'); wb.textContent=`🔏 ${job.watermark_id.slice(0,8)}`;
    wb.title=`Watermark ID: ${job.watermark_id} (WAV only)`; wb.style.display='inline';
  } else {
    $('wm-badge').style.display='none';
  }

  audioResult.style.display='flex';
  const bars=waveformBars.querySelectorAll('.bar');
  const playing=on=>bars.forEach(b=>b.classList.toggle('playing',on));
  audioPlayer.addEventListener('play',()=>playing(true));
  audioPlayer.addEventListener('pause',()=>playing(false));
  audioPlayer.addEventListener('ended',()=>playing(false));
  audioPlayer.play().catch(()=>{});
}

// ── Batch ──────────────────────────────────────────────────────────────────
const batchInput=$('batch-input'), batchBtn=$('batch-btn');

batchInput.addEventListener('input',()=>{
  const n=batchInput.value.split('\n').filter(l=>l.trim()).length;
  $('batch-line-count').textContent=n; updateBatchBtn();
});
function updateBatchBtn(){
  const n=batchInput.value.split('\n').filter(l=>l.trim()).length;
  batchBtn.disabled=!(selectedVoiceId&&n>0);
}

batchBtn.addEventListener('click', async ()=>{
  const lines=batchInput.value.split('\n').filter(l=>l.trim());
  if(!lines.length||!selectedVoiceId) return;
  $('batch-error').style.display='none'; $('batch-results').style.display='none'; $('batch-list').innerHTML='';
  setBatchLoading(true); $('batch-progress').style.display='flex';
  $('batch-fill').style.width='0%'; $('batch-progress-label').textContent=`0 / ${lines.length}`;
  try {
    const {batch_id,total}=await apiFetch('/api/batch',{ method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({lines, voice_id:selectedVoiceId, language:$('batch-lang-select').value, speed:parseFloat($('batch-speed-slider').value)}) });
    batchJobId=batch_id; startBatchPoll(batch_id, total);
  } catch(e){
    $('batch-error').textContent='⚠ '+e.message; $('batch-error').style.display='block';
    setBatchLoading(false); $('batch-progress').style.display='none';
  }
});

function startBatchPoll(bid,total){
  clearInterval(batchPollTimer);
  batchPollTimer=setInterval(async()=>{
    try {
      const job=await apiFetch(`/api/batch/${bid}`);
      const pct=total>0?Math.round((job.completed/total)*100):0;
      $('batch-fill').style.width=pct+'%';
      $('batch-progress-label').textContent=`${job.completed} / ${total}`;
      renderBatchItems(job.items||[]);
      if(job.status==='done'){
        clearInterval(batchPollTimer); setBatchLoading(false); $('batch-progress').style.display='none';
        $('batch-results').style.display='block';
        const done=job.items.filter(i=>i.status==='done').length;
        $('batch-results-label').textContent=`${done} of ${total} generated`;
        if(job.zip_url){
          $('batch-zip-btn').href=`${API}${job.zip_url}`; $('batch-zip-btn').download=`batch_${bid}.zip`;
          $('batch-zip-btn').style.display='inline-flex';
        }
        loadHistory(); updateQueueBadge();
      }
    } catch(e){ clearInterval(batchPollTimer); setBatchLoading(false); }
  }, POLL_MS);
}

function renderBatchItems(items){
  $('batch-list').innerHTML=''; $('batch-results').style.display=items.length?'block':'none';
  items.forEach(item=>{
    const li=document.createElement('li'); li.className='batch-item';
    const icon=item.status==='done'?'✅':item.status==='failed'?'❌':item.status==='running'?'⚙':'⏳';
    li.innerHTML=`<span class="batch-status-icon">${icon}</span>
      <span class="batch-text" title="${esc(item.text)}">${esc(item.text)}</span>
      <button class="batch-play" ${item.status!=='done'?'disabled':''}>▶</button>`;
    if(item.status==='done'){
      li.querySelector('.batch-play').addEventListener('click',()=>{
        audioPlayer.src=`${API}${item.audio_url}`; audioResult.style.display='flex';
        audioPlayer.play().catch(()=>{});
      });
    }
    $('batch-list').appendChild(li);
  });
}

function setBatchLoading(on){
  batchBtn.disabled=on; $('batch-btn-text').textContent=on?'Generating…':'Generate All';
  $('batch-spinner').style.display=on?'inline-block':'none'; updateQueueBadge();
}

// ── Mix Voices ─────────────────────────────────────────────────────────────
const mixAlpha=$('mix-alpha');

function populateMixDropdowns(){
  ['mix-voice-a','mix-voice-b'].forEach(id=>{
    const sel=$(id), cur=sel.value;
    sel.innerHTML='<option value="">— select —</option>';
    cachedVoices.forEach(v=>{
      const opt=document.createElement('option');
      opt.value=v.id; opt.textContent=v.name+(v.is_mix?' (mix)':'');
      sel.appendChild(opt);
    });
    sel.value=cur;
  });
  updateMixBtn();
}

function updateMixAlphaDisplay(){
  const a=parseInt(mixAlpha.value);
  const b=100-a;
  $('mix-label-a').textContent=`A ${a}%`;
  $('mix-label-b').textContent=`${b}% B`;
  const va=$('mix-voice-a').options[$('mix-voice-a').selectedIndex]?.text||'A';
  const vb=$('mix-voice-b').options[$('mix-voice-b').selectedIndex]?.text||'B';
  $('mix-pill-a').textContent=`${va.slice(0,10)} · ${a}%`; $('mix-pill-a').className=`mix-pill ${a>=50?'a':''}`;
  $('mix-pill-b').textContent=`${b}% · ${vb.slice(0,10)}`;  $('mix-pill-b').className=`mix-pill ${b>=50?'b':''}`;
  updateMixBtn();
}

function updateMixBtn(){
  const a=$('mix-voice-a').value, b=$('mix-voice-b').value, n=$('mix-name').value.trim();
  $('mix-btn').disabled=!(a&&b&&a!==b&&n);
}

mixAlpha.addEventListener('input', updateMixAlphaDisplay);
$('mix-voice-a').addEventListener('change', updateMixAlphaDisplay);
$('mix-voice-b').addEventListener('change', updateMixAlphaDisplay);
$('mix-name').addEventListener('input', updateMixBtn);

$('mix-btn').addEventListener('click', async ()=>{
  const a=$('mix-voice-a').value, b=$('mix-voice-b').value;
  const alpha=parseInt(mixAlpha.value)/100;
  const name=$('mix-name').value.trim();
  if(!a||!b||!name) return;
  $('mix-result').style.display='none'; $('mix-error').style.display='none';
  $('mix-btn-text').textContent='Mixing…'; $('mix-btn').disabled=true;
  $('mix-spinner').style.display='inline-block';
  try {
    const voice=await apiFetch('/api/voices/mix',{ method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({voice_id_a:a, voice_id_b:b, alpha, name}) });
    $('mix-result').textContent=`✅ Created "${voice.name}" — select it from the library to synthesize.`;
    $('mix-result').className='notice notice-ok'; $('mix-result').style.display='block';
    $('mix-name').value='';
    renderVoiceItem(voice);
  } catch(e){
    $('mix-error').textContent='⚠ '+e.message; $('mix-error').style.display='block';
  } finally {
    $('mix-btn-text').textContent='Create Mixed Voice'; $('mix-spinner').style.display='none';
    updateMixBtn();
  }
});

// ── Jobs tab ───────────────────────────────────────────────────────────────
$('jobs-refresh').addEventListener('click', loadJobs);

async function loadJobs(){
  $('jobs-loading').style.display='block'; $('jobs-empty').style.display='none';
  $('jobs-table-wrap').style.display='none'; $('jobs-tbody').innerHTML='';
  try {
    const jobs=await apiFetch('/api/queue');
    $('jobs-loading').style.display='none';
    if(!jobs.length){ $('jobs-empty').style.display='block'; return; }
    $('jobs-table-wrap').style.display='block';
    jobs.forEach(renderJobRow);
    updateQueueBadge(jobs);
  } catch { $('jobs-loading').textContent='Failed to load jobs.'; }
}

function renderJobRow(job){
  const tr=document.createElement('tr');
  const typeLabel=job.type==='batch'?'batch':'synth';
  const typeCls=job.type==='batch'?'job-type-batch':'job-type-synth';
  const canPlay=job.status==='done'&&job.audio_url;
  const wm=job.watermark_id?`<div class="job-wm">${job.watermark_id.slice(0,8)}</div>`:'';
  const preview=(job.text_preview||'').slice(0,40)+(((job.text_preview||'').length>40)?'…':'');
  const speed=job.speed?`${job.speed}×`:'-';
  const age=timeAgo(job.created_at);
  const statusDot=`<span class="status-dot ${job.status}"></span>`;

  let audioCell='—';
  if(canPlay) audioCell=`<button class="job-play">▶</button>${wm}`;

  tr.innerHTML=`
    <td><span class="job-type-badge ${typeCls}">${typeLabel}</span></td>
    <td>${statusDot}${job.status}</td>
    <td>${esc(job.voice_name||'—')}</td>
    <td style="max-width:180px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(preview)}</td>
    <td>${speed}</td><td>${age}</td>
    <td>${audioCell}</td>`;

  if(canPlay){
    tr.querySelector('.job-play').addEventListener('click',()=>{
      audioPlayer.src=`${API}${job.audio_url}`; audioResult.style.display='flex';
      audioPlayer.play().catch(()=>{});
      document.querySelectorAll('.tab').forEach(t=>{ if(t.dataset.tab==='synth') t.click(); });
    });
  }
  $('jobs-tbody').appendChild(tr);
}

async function updateQueueBadge(jobs=null){
  try {
    const data=jobs||await apiFetch('/api/queue');
    const active=data.filter(j=>j.status==='pending'||j.status==='running').length;
    queueBadge.textContent=active; queueBadge.style.display=active>0?'inline':'none';
  } catch { queueBadge.style.display='none'; }
}

// ── History ────────────────────────────────────────────────────────────────
$('history-refresh').addEventListener('click', loadHistory);

async function loadHistory(){
  $('history-loading').style.display='block'; $('history-empty').style.display='none';
  $('history-list').querySelectorAll('.history-item').forEach(el=>el.remove());
  try {
    const items=await apiFetch('/api/history');
    $('history-loading').style.display='none';
    if(!items.length){ $('history-empty').style.display='block'; return; }
    items.forEach(renderHistoryItem);
  } catch { $('history-loading').textContent='Failed.'; }
}

function renderHistoryItem(item){
  const div=document.createElement('div'); div.className='history-item';
  const url=`${API}${item.audio_url}`;
  const meta=[
    (item.language||'en').toUpperCase(),
    item.speed&&item.speed!==1.0?`${item.speed}×`:null,
    item.watermark_id?`🔏 ${item.watermark_id.slice(0,6)}`:null,
  ].filter(Boolean).join(' · ');
  div.innerHTML=`
    <button class="history-play">▶</button>
    <div class="history-info">
      <div class="history-voice">${esc(item.voice_name)}</div>
      <div class="history-text">${esc(item.text_preview||'')}</div>
      <div class="history-time">${timeAgo(item.created_at)} · ${meta}</div>
    </div>
    <a class="history-dl" href="${esc(url)}" download="${esc(item.filename)}">⬇</a>`;
  div.querySelector('.history-play').addEventListener('click',()=>{
    audioPlayer.src=url; audioResult.style.display='flex'; audioPlayer.play().catch(()=>{});
  });
  $('history-list').appendChild(div);
}

// ── Verify watermark ────────────────────────────────────────────────────────
const verifyModal=$('verify-modal-overlay'), verifyResult=$('verify-result');

$('verify-btn').addEventListener('click',()=>{ verifyModal.style.display='flex'; verifyResult.style.display='none'; });
$('verify-close').addEventListener('click',()=>verifyModal.style.display='none');
verifyModal.addEventListener('click',e=>{ if(e.target===verifyModal) verifyModal.style.display='none'; });

$('verify-input').addEventListener('change', async()=>{
  const file=$('verify-input').files[0]; if(!file) return;
  verifyResult.style.display='none';
  const fd=new FormData(); fd.append('audio',file);
  try {
    const r=await apiFetch('/api/verify',{method:'POST',body:fd});
    if(r.watermark_found){
      verifyResult.className='notice notice-ok';
      verifyResult.textContent=`✅ Mimicry watermark found — ID: ${r.watermark_id}`;
    } else {
      verifyResult.className='notice notice-error';
      verifyResult.textContent='❌ No Mimicry watermark detected in this file.';
    }
    verifyResult.style.display='block';
  } catch(e){ verifyResult.className='notice notice-error'; verifyResult.textContent='⚠ '+e.message; verifyResult.style.display='block'; }
  $('verify-input').value='';
});

// ── Error helpers ──────────────────────────────────────────────────────────
function showError(msg){ errorBanner.textContent='⚠ '+msg; errorBanner.style.display='flex'; setTimeout(()=>errorBanner.style.display='none',9000); }

// ── Keyboard shortcuts ─────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  // Ctrl/Cmd+Enter → synthesize or batch
  if((e.ctrlKey || e.metaKey) && e.key === 'Enter'){
    e.preventDefault();
    const active = document.querySelector('.tab.active')?.dataset.tab;
    if(active === 'synth' && !synthesizeBtn.disabled) synthesizeBtn.click();
    else if(active === 'batch' && !batchBtn.disabled) batchBtn.click();
  }
  // Escape → close any open modal
  if(e.key === 'Escape'){
    if(refModalOverlay.style.display !== 'none') closeRefModal();
    if(verifyModal.style.display !== 'none') verifyModal.style.display = 'none';
  }
});

// ── Init ───────────────────────────────────────────────────────────────────
loadVoices();
loadHistory();
updateMixAlphaDisplay();
setInterval(updateQueueBadge, 5000);
