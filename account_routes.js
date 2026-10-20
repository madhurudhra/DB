"use strict";
const {execFile}=require('child_process');
const path=require('path');
module.exports=function(app,requireSession,sameOrigin){
 const runtimes={crypto:'/home/ubuntu/crypto-bot/paper-trend-500/venv/bin/python',us_trading:'/opt/stock-bot/.venv/bin/python',zerodha:'/opt/zerodha-bot/venv/bin/python'};
 const cache=new Map(),pending=new Map(),lastAttempt=new Map();
 function request(bot,account,force){
  const key=bot+':'+account, old=cache.get(key), at=Date.now();
  if(pending.has(key))return pending.get(key);
  if(old&&((!force&&at-old.at<30000)||(at-(lastAttempt.get(key)||0)<10000)))return Promise.resolve({...old.value,cache_hit:true});
  lastAttempt.set(key,at);
  const task=new Promise(resolve=>execFile(runtimes[bot],[path.join(__dirname,'account_worker.py'),bot,account],{timeout:120000,maxBuffer:4*1024*1024,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}},(error,stdout)=>{
   let value;
   try{if(error)throw error;value=JSON.parse(stdout);if(typeof value.ok!=='boolean')throw Error();}
   catch(_){value={ok:false,bot,connection:'READ_FAILED',error:'WORKER_TIMEOUT_OR_EXECUTION_ERROR',attempted_at:new Date().toISOString()};}
   if(!value.ok&&old){const good=old.value.ok?old.value:old.value.last_good;if(good)value.last_good=good;}
   cache.set(key,{at:Date.now(),value});resolve(value);
  })).finally(()=>pending.delete(key));
  pending.set(key,task);return task;
 }
 async function handler(req,res){
  res.set('Cache-Control','no-store');const bot=req.params.bot;
  if(!Object.prototype.hasOwnProperty.call(runtimes,bot))return res.status(400).json({ok:false,error:'Unknown bot'});
  const account=typeof req.query.account==='string'?req.query.account:'';
  if(account&&!/^[a-f0-9]{16}$/.test(account))return res.status(400).json({ok:false,error:'Invalid account selector'});
  if(account&&bot!=='crypto')return res.status(400).json({ok:false,error:'Account selector applies to Crypto only'});
  const value=await request(bot,account,req.method==='POST');
  res.json(value);
 }
 app.get('/api/accounts/:bot',sameOrigin,requireSession,handler);
 app.post('/api/accounts/:bot/refresh',sameOrigin,requireSession,handler);
};
