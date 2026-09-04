const http = require('http');
const fs = require('fs');
const path = require('path');
const PORT = process.env.PORT || 4173;
let capsule = { sessionId: 'pcmcp-demo', color: 'pink', message: 'PCMCP', version: 1, updatedAt: Date.now() };
const mime = {'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json'};
const server = http.createServer((req,res)=>{
  if(req.url === '/api/capsule' && req.method === 'GET'){
    res.writeHead(200,{'Content-Type':'application/json','Access-Control-Allow-Origin':'*'}); return res.end(JSON.stringify(capsule));
  }
  if(req.url === '/api/capsule' && req.method === 'POST'){
    let body=''; req.on('data',d=>body+=d); req.on('end',()=>{
      try { const next=JSON.parse(body||'{}'); capsule={...capsule,...next,version:capsule.version+1,updatedAt:Date.now()};
        res.writeHead(200,{'Content-Type':'application/json','Access-Control-Allow-Origin':'*'}); res.end(JSON.stringify(capsule));
      } catch(e){res.writeHead(400);res.end('bad json');}
    }); return;
  }
  const clean = req.url.split('?')[0] === '/' ? '/index.html' : req.url.split('?')[0];
  const file = path.join(__dirname,'public',clean);
  if(!file.startsWith(path.join(__dirname,'public'))) {res.writeHead(403);return res.end();}
  fs.readFile(file,(err,data)=>{if(err){res.writeHead(404);return res.end('not found');}res.writeHead(200,{'Content-Type':mime[path.extname(file)]||'text/plain'});res.end(data);});
});
server.listen(PORT,'0.0.0.0',()=>console.log(`PCMCP http://localhost:${PORT}`));
