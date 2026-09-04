const card=document.querySelector('#card'), stateText=document.querySelector('#stateText'), statusEl=document.querySelector('#status'), platform=document.querySelector('#platform'), code=document.querySelector('#code');
let local={color:'pink'}, lastVersion=0;
function render(){card.className='card '+local.color;stateText.textContent=local.color.toUpperCase();const p=platform.value;code.textContent=p==='mac'?`# Mac target
state = {"color": "${local.color}"}
apply_state(state)`:`// Windows target
const state = { color: "${local.color}" };
applyState(state);`;}
async function pull(){try{const r=await fetch('/api/capsule');const c=await r.json();if(c.version!==lastVersion){lastVersion=c.version;local.color=c.color;render();statusEl.textContent=`Received v${c.version}`;}}catch{statusEl.textContent='Relay unavailable';}}
async function push(input={}){const payload={sessionId:'pcmcp-demo',color:input.color||local.color,message:'PCMCP'};const r=await fetch('/api/capsule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const c=await r.json();lastVersion=c.version;local.color=c.color;render();statusEl.textContent=`Sent v${c.version}`;return c;}
document.querySelectorAll('[data-color]').forEach(b=>b.onclick=()=>{local.color=b.dataset.color;render();statusEl.textContent='Changed locally';});document.querySelector('#send').onclick=()=>push();platform.onchange=render;
if(navigator.platform.toLowerCase().includes('win')) platform.value='windows';render();setInterval(pull,700);pull();
if(document.modelContext?.registerTool){
  document.modelContext.registerTool({name:'send_capsule',description:'Send the current PCMCP task state so another computer can continue it.',inputSchema:{type:'object',properties:{color:{type:'string',enum:['pink','blue']}},required:['color']},execute:async(input)=>({content:[{type:'text',text:JSON.stringify(await push(input))}]})});
  document.modelContext.registerTool({name:'receive_capsule',description:'Receive the latest PCMCP task state from the other computer.',inputSchema:{type:'object',properties:{}},execute:async()=>{const r=await fetch('/api/capsule');const c=await r.json();local.color=c.color;lastVersion=c.version;render();return {content:[{type:'text',text:JSON.stringify(c)}]};}});
  statusEl.textContent='WebMCP tools registered';
}
