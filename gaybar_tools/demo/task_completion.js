/* Local website-state simulation; automation only reads these task markers. */
const completionAsset=new URL('task_completion.html',location.href).href;
const completionParams=new URLSearchParams(location.search);
const completionView=window.frameElement?.dataset.localView||'';
const completionID=window.frameElement?.dataset.localTaskId||'';
const completionApp=document.getElementById('app');
const completionStore='task-completion-metrics';

if(completionView==='pdf-shell'){
  document.body.className='embedded';
  completionApp.innerHTML='<div id="docContainer"><iframe id="panView"></iframe></div>';
  const reader=completionApp.querySelector('iframe');
  reader.dataset.localView='reader';reader.dataset.localTaskId=completionID;
  if(window.frameElement.dataset.localCompleteAfterWheel==='1')reader.dataset.localCompleteAfterWheel='1';
  reader.src=completionAsset;
}else if(completionView==='reader'){
  document.body.className='embedded';
  completionApp.innerHTML='<div id="same-reader" class="document-viewer" data-document-viewer tabindex="0"><article class="document-page"><h2>地址与内容完全相同的独立阅读器</h2><p>正文引用“任务点已完成”仅作为说明，不是这个文档的任务标记。每个所属任务区域都有自己的独立状态。</p><p style="margin-top:950px">文档结束</p></article></div>';
  const viewer=completionApp.querySelector('.document-viewer');
  if(window.frameElement.dataset.localCompleteAfterWheel==='1')viewer.querySelector('article').style.minHeight='6000px';
  viewer.addEventListener('wheel',event=>window.top.completionEvent(completionID,'wheel',{trusted:event.isTrusted}),{passive:true});
  viewer.addEventListener('scroll',()=>window.top.completionEvent(completionID,'scroll',{
    top:viewer.scrollTop,client:viewer.clientHeight,height:viewer.scrollHeight,bottom:viewer.scrollTop+viewer.clientHeight>=viewer.scrollHeight-3
  }));
  window.top.completionReady(completionID);
}else if(completionView==='video'){
  document.body.className='embedded';
  const source=window.frameElement.dataset.emptySource==='1'?'':` src="${new URL('lesson.webm',completionAsset).href}"`;
  completionApp.innerHTML=`<div class="video-js"><video class="vjs-tech" controls preload="none"${source}></video><button class="vjs-big-play-button" aria-label="播放视频">播放</button></div>`;
  const video=completionApp.querySelector('video'),play=completionApp.querySelector('button');let startedAt=0,progressTimer=0;
  play.addEventListener('click',event=>{window.top.completionEvent(completionID,'play-click',{trusted:event.isTrusted});video.play().catch(()=>{});});
  video.addEventListener('play',()=>{
    play.hidden=true;if(!startedAt)startedAt=performance.now();window.top.completionEvent(completionID,'play');
    if(!progressTimer)progressTimer=setInterval(()=>window.top.completionEvent(completionID,'progress',{time:video.currentTime,duration:video.duration}),25);
  });
  video.addEventListener('seeking',()=>window.top.completionEvent(completionID,'seeking'));
  video.addEventListener('ratechange',()=>window.top.completionEvent(completionID,'rate',{rate:video.playbackRate}));
  video.addEventListener('timeupdate',()=>window.top.completionEvent(completionID,'progress',{time:video.currentTime,duration:video.duration}));
  video.addEventListener('ended',()=>{
    clearInterval(progressTimer);
    window.top.completionEvent(completionID,'ended',{
      time:video.currentTime,duration:video.duration,elapsed:(performance.now()-startedAt)/1000,rate:video.playbackRate
    });
  });
  window.top.completionReady(completionID);
}else{
  const layout=completionParams.get('layout')||'mixed';
  if(layout.startsWith('static-css-'))document.body.classList.add('static-css-markers');
  const metrics={order:[],nativeDone:[],events:[],tasks:{},violations:[],nextClicks:0,nextTrusted:[],mountedAfterNext:false,materialChangedAfterNext:false};
  const cards=new Map();
  function persist(){localStorage.setItem(completionStore,JSON.stringify(metrics));}
  function record(id,event,detail={}){metrics.events.push({id,event,...detail});persist();}
  function predecessors(id,event){
    const unfinished=metrics.order.slice(0,metrics.order.indexOf(id)).filter(previous=>{
      const task=metrics.tasks[previous];return task.requiredNative&&!task.nativeDone;
    });
    if(unfinished.length)metrics.violations.push({id,event,unfinished});
  }
  function updateNext(){document.getElementById('next-chapter').disabled=metrics.order.some(id=>{
    const task=metrics.tasks[id];return task.requiredNative&&!task.nativeDone;
  });}
  function markComplete(id,reason,detail={}){
    const task=metrics.tasks[id],card=cards.get(id);
    task.markedComplete=true;card.classList.add('ans-job-finished');
    const icon=card.querySelector(':scope > .ans-job-icon');
    if(icon.hasAttribute('aria-label'))icon.setAttribute('aria-label','任务点已完成');
    if(!task.started)task.requiredNative=false;
    record(id,'marked-complete',{reason,...detail});updateNext();
  }
  function nativeFinished(id){
    const task=metrics.tasks[id];if(task.nativeDone)return;
    predecessors(id,'complete');task.nativeDone=true;metrics.nativeDone.push(id);record(id,'complete');markComplete(id,'native-completion');
    if(layout==='before-turn'&&id==='document-1')markComplete('video-2','website-update-before-turn');
    updateNext();persist();
  }
  window.completionReady=id=>{metrics.tasks[id].ready=true;persist();};
  window.completionEvent=(id,event,detail={})=>{
    const task=metrics.tasks[id];
    if(event==='wheel'){
      predecessors(id,event);task.started=true;task.wheels++;task.wheelTrusted=(task.wheelTrusted??true)&&detail.trusted;record(id,event,detail);
      if(['complete-after-wheel','static-css-after-wheel'].includes(layout)&&task.wheels===1){
        task.requiredNative=false;markComplete(id,'website-update-after-first-wheel');
      }
    }else if(event==='scroll'){
      task.scrolls++;Object.assign(task,detail);if(detail.bottom&&task.wheels>0)nativeFinished(id);persist();
    }else if(event==='play-click'){
      predecessors(id,event);task.started=true;task.playClicks++;record(id,event,detail);
    }else if(event==='play')record(id,event);
    else if(event==='seeking'){task.seekEvents++;persist();}
    else if(event==='rate'){task.rate=detail.rate;persist();}
    else if(event==='progress'){
      if(layout==='progress-green'&&!task.markedComplete&&Number.isFinite(detail.duration)&&detail.time>=detail.duration*.9)
        markComplete(id,'ninety-percent',detail);
    }else if(event==='ended'){
      task.ended=true;Object.assign(task,detail);record(id,event,detail);
      if(task.playClicks>0&&task.seekEvents===0&&detail.rate===1)nativeFinished(id);
    }
  };
  window.completionFixtureReady=()=>metrics.order.every(id=>metrics.tasks[id].ready);

  completionApp.innerHTML='<main><h1>每个任务有独立的完成状态</h1><div id="task-cards"></div><nav class="navigation"><button id="next-chapter" disabled>下一节</button></nav></main><aside class="sidebar catalog"><div id="ext-gen1051" class="ans-job-icon ans-job-video ans-job-icon-clear" aria-label="任务点已完成"></div>课程目录里的完成标记不能代表正文任务。</aside>';
  function addCard(kind,id,complete,{empty=false,nested=false,cssOnly=false,classFinished=false,hideIcon=false}={}){
    const card=document.createElement('div');card.className=`ans-attach-ct ${kind==='video'?'videoContainer':''} ${complete||classFinished?'ans-job-finished':''}`;card.dataset.localTaskId=id;
    const iconClass=kind==='video'?'ans-job-video':'ans-job-icon-doc';
    card.innerHTML=`<div id="ext-gen1051" class="ans-job-icon ${iconClass} ans-job-icon-clear" aria-label="任务点${complete?'已':'未'}完成"></div><div id="ext-gen1051" class="ans-job-icon ${iconClass} ans-job-icon-clear" aria-label="任务点已完成" hidden></div>`;
    const icon=card.querySelector(':scope > .ans-job-icon');
    if(cssOnly)icon.removeAttribute('aria-label');
    if(hideIcon)icon.hidden=true;
    const frame=document.createElement('iframe');frame.className=kind==='video'?'ans-attach-online ans-insertvideo-online':'ans-attach-online';
    if(nested)frame.classList.add('insertdoc-online-pdf');
    frame.dataset.localTaskId=id;
    metrics.order.push(id);metrics.tasks[id]={kind,initialComplete:complete,markedComplete:complete,requiredNative:!complete,nativeDone:false,started:false,ready:false,wheels:0,scrolls:0,playClicks:0,seekEvents:0,rate:1};
    if(kind==='document'&&empty){
      const container=document.createElement('div');container.id='docContainer';container.appendChild(frame);card.appendChild(container);
      frame.src='about:blank';metrics.tasks[id].ready=true;
    }else{
      frame.dataset.localView=kind==='document'?(nested?'pdf-shell':'reader'):'video';
      if(empty)frame.dataset.emptySource='1';
      card.appendChild(frame);frame.src=completionAsset;
    }
    cards.set(id,card);document.getElementById('task-cards').appendChild(card);
  }
  if(layout==='mixed'){
    addCard('document','document-1',true);addCard('video','video-2',false);
    addCard('video','video-3',true);addCard('document','document-4',false);
  }else if(layout==='document-pair'){
    addCard('document','document-1',true);addCard('document','document-2',false);
  }else if(layout==='static-css-pair'){
    addCard('document','document-1',true,{nested:true,cssOnly:true});
    addCard('document','document-2',false,{nested:true,cssOnly:true});
  }else if(layout==='static-css-conflict'){
    addCard('document','document-1',false,{nested:true,classFinished:true});
  }else if(layout==='static-css-hidden'){
    addCard('document','document-1',false,{nested:true,cssOnly:true,classFinished:true,hideIcon:true});
  }else if(layout==='static-css-after-wheel'){
    addCard('document','document-1',false,{nested:true,cssOnly:true});
    cards.get('document-1').querySelector('iframe').dataset.localCompleteAfterWheel='1';
  }else if(layout==='all-complete'){
    addCard('document','document-1',true);addCard('video','video-2',true);
    addCard('document','document-3',true,{empty:true});addCard('video','video-4',true,{empty:true});
  }else if(layout==='before-turn'){
    addCard('document','document-1',false);addCard('video','video-2',false);addCard('document','document-3',false);
  }else if(layout==='progress-green')addCard('video','video-1',false);
  else if(layout==='unfinished-empty'){
    addCard('document','document-1',true,{empty:true});addCard('document','document-2',false,{empty:true});
  }else if(layout==='late-complete-mount')addCard('document','document-1',true,{empty:true});
  else if(layout==='changed-complete-video'){
    addCard('video','video-1',true);
    cards.get('video-1').querySelector('iframe').setAttribute('jobid','fixed-job');
  }else if(layout==='changed-complete-placeholder'){
    addCard('document','document-1',true,{empty:true});
    cards.get('document-1').querySelector('iframe').setAttribute('objectid','resource-A');
  }else if(layout==='complete-after-wheel'){
    addCard('document','document-1',false);
    cards.get('document-1').querySelector('iframe').dataset.localCompleteAfterWheel='1';
  }

  document.getElementById('next-chapter').addEventListener('click',event=>{
    metrics.nextClicks++;metrics.nextTrusted.push(event.isTrusted);record('next','click',{trusted:event.isTrusted});
    if(layout==='late-complete-mount'){
      document.getElementById('next-chapter').disabled=true;
      const frame=cards.get('document-1').querySelector('iframe');frame.dataset.localView='reader';
      metrics.tasks['document-1'].ready=false;frame.src=completionAsset;metrics.mountedAfterNext=true;persist();return;
    }
    if(layout==='changed-complete-video'||layout==='changed-complete-placeholder'){
      document.getElementById('next-chapter').disabled=true;
      if(layout==='changed-complete-video'){
        const video=cards.get('video-1').querySelector('iframe').contentDocument.querySelector('video');
        video.src=new URL('lesson.webm?material=2',completionAsset).href;
      }else cards.get('document-1').querySelector('iframe').setAttribute('objectid','resource-B');
      metrics.materialChangedAfterNext=true;persist();return;
    }
    location.href=new URL('complete.html',completionAsset).href;
  });
  updateNext();persist();
}
