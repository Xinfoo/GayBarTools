/* Account-free course fixture. Completion comes only from native wheel/media events. */
const mixedAssetURL=new URL('mixed_tasks.html',location.href).href;
const mixedParams=new URLSearchParams(location.search);
const mixedView=window.frameElement?.dataset.localView||'';
const mixedTaskID=window.frameElement?.dataset.localTaskId||'';
const mixedStore='mixed-tasks-metrics';
const mixedApp=document.getElementById('app');

function renderMixedReader(host,id){
  host.innerHTML='<div id="same-reader" class="document-viewer" data-document-viewer tabindex="0"><article class="document-page"><h2>相同地址与相同内容的阅读材料</h2><p>每一份独立挂载的文档都需要真实滚动完成。不同文档故意使用完全相同的地址、内容和内部节点。</p><p style="margin-top:950px">文档结束</p></article></div>';
  const viewer=host.querySelector('.document-viewer');
  window.top.mixedReaderReady(id);
  viewer.addEventListener('wheel',event=>window.top.mixedEvent(id,'document','wheel',{trusted:event.isTrusted}),{passive:true});
  viewer.addEventListener('scroll',event=>{
    const bottom=viewer.scrollTop+viewer.clientHeight>=viewer.scrollHeight-3;
    window.top.mixedDocumentScroll(id,{trusted:event.isTrusted,bottom,top:viewer.scrollTop,client:viewer.clientHeight,height:viewer.scrollHeight});
  });
}

if(mixedView==='wrapper'){
  document.body.className='wrapper';
  const frame=document.createElement('iframe');
  frame.dataset.localView='reader';frame.dataset.localTaskId=mixedTaskID;frame.src=mixedAssetURL;mixedApp.appendChild(frame);
}else if(mixedView==='reader'){
  document.body.className='embedded';
  const delay=Number(window.frameElement.dataset.localDelay||0);
  if(delay>0)window.top.mixedRegisterDeferredReader(()=>renderMixedReader(mixedApp,mixedTaskID),delay);
  else renderMixedReader(mixedApp,mixedTaskID);
}else{
  const mixedMetrics={expected:[],completed:[],events:[],tasks:{},violations:[],nextClicks:0,nextTrusted:[],directoryWheels:0};
  let dynamicAdded=false;
  let deferredReadersStarted=false;
  const deferredReaders=[];
  window.mixedRegisterDeferredReader=(render,delay)=>{
    if(deferredReadersStarted)setTimeout(render,delay);
    else deferredReaders.push({render,delay});
  };
  window.startMixedDeferredReaders=()=>{
    if(deferredReadersStarted)return;
    deferredReadersStarted=true;
    deferredReaders.splice(0).forEach(({render,delay})=>setTimeout(render,delay));
  };
  function persist(){localStorage.setItem(mixedStore,JSON.stringify(mixedMetrics));}
  function requirePredecessors(id,event){
    const index=mixedMetrics.expected.indexOf(id);
    const unfinished=mixedMetrics.expected.slice(0,index).filter(previous=>!mixedMetrics.completed.includes(previous));
    if(unfinished.length)mixedMetrics.violations.push({id,event,unfinished});
  }
  function appendEvent(id,kind,event,detail={}){
    mixedMetrics.events.push({id,kind,event,...detail});persist();
  }
  function updateNext(){document.getElementById('next-chapter').disabled=!mixedMetrics.expected.length||!mixedMetrics.expected.every(id=>mixedMetrics.completed.includes(id));}
  function finish(id,kind){
    if(mixedMetrics.completed.includes(id))return;
    requirePredecessors(id,'complete');
    mixedMetrics.completed.push(id);appendEvent(id,kind,'complete');
    if(mixedParams.get('layout')==='dynamic'&&id==='video-1'&&!dynamicAdded){
      dynamicAdded=true;addTask('document','document-2',{inline:true});appendEvent('document-2','document','appended');
    }
    updateNext();persist();
  }
  window.mixedReaderReady=id=>{
    mixedMetrics.tasks[id]??={kind:'document',wheels:0,scrolls:0};mixedMetrics.tasks[id].ready=true;persist();
  };
  window.mixedEvent=(id,kind,event,detail={})=>{
    const task=mixedMetrics.tasks[id];
    if(!task){mixedMetrics.violations.push({id,event,unexpected:true});persist();return;}
    if(event==='wheel'){requirePredecessors(id,event);task.wheels++;task.wheelTrusted=(task.wheelTrusted??true)&&detail.trusted;}
    appendEvent(id,kind,event,detail);
  };
  window.mixedDocumentScroll=(id,detail)=>{
    const task=mixedMetrics.tasks[id];task.scrolls++;Object.assign(task,detail);
    if(detail.bottom&&task.wheels>0)finish(id,'document');persist();
  };
  window.mixedFixtureReady=()=>mixedMetrics.expected.every(id=>mixedMetrics.tasks[id]?.ready);

  mixedApp.innerHTML='<main><h1>同一学习页面的多个内容任务</h1><p>外层课程页面有较长说明文字和播放器海报外壳，这些界面元素不属于需要阅读完成的文档任务。只有独立文档阅读器和真实视频需要按位置逐个处理。</p><div class="video-js empty-player"><canvas width="260" height="90"></canvas></div><div id="tasks"></div><nav class="navigation"><button id="next-chapter" disabled>下一节</button></nav></main><aside class="sidebar catalog"><div>课程目录和章节导航<br>这里的滚动区域不能作为课程文档完成。<br>导航项一<br>导航项二</div></aside>';
  // Exercise the known outer course path, so text/canvas in the shell cannot
  // become an accidental document task merely because this is a cards page.
  history.replaceState(null,'','/knowledge/cards?chapterId=local-mixed&courseId=local');
  document.querySelector('.sidebar').addEventListener('wheel',()=>{mixedMetrics.directoryWheels++;persist();},{passive:true});
  document.querySelector('canvas').getContext('2d').fillRect(0,0,260,90);

  function addTask(kind,id,{deep=false,inline=false,hidden=false,delay=0}={}){
    const section=document.createElement('section');section.className='local-task';section.dataset.localTaskId=id;
    if(hidden)section.hidden=true;
    section.innerHTML=`<h2>${kind==='document'?'文档':'视频'} ${id}</h2>`;
    document.getElementById('tasks').appendChild(section);
    mixedMetrics.tasks[id]={kind,ready:kind==='video',wheels:0,scrolls:0,playClicks:0,seekEvents:0,playbackRate:1};
    if(!hidden)mixedMetrics.expected.push(id);
    if(kind==='document'){
      if(inline)renderMixedReader(section,id);
      else{
        const frame=document.createElement('iframe');frame.dataset.localView=deep?'wrapper':'reader';frame.dataset.localTaskId=id;
        if(delay){section.id='docContainer';frame.id='panView';frame.dataset.localDelay=String(delay);}
        frame.src=mixedAssetURL;section.appendChild(frame);
      }
    }else{
      const stage=document.createElement('div');stage.className='video-js';
      const file=mixedParams.get('fail')===id?'missing-local-video.webm':'lesson.webm';
      stage.innerHTML=`<video class="vjs-tech" id="${id}" controls preload="none" src="${new URL(file,mixedAssetURL).href}"></video><button class="vjs-big-play-button" aria-label="播放视频">播放</button>`;
      const chrome=document.createElement('div');chrome.id='reader';
      chrome.innerHTML='<p>视频模块自己的阅读标记、说明文字和海报图片只是播放器外壳，不是第二个文档任务。</p><img alt="播放器海报" width="48" height="32" src="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2248%22 height=%2232%22%3E%3Crect width=%2248%22 height=%2232%22 fill=%22black%22/%3E%3C/svg%3E">';
      chrome.appendChild(stage);section.appendChild(chrome);
      const video=stage.querySelector('video'),play=stage.querySelector('button');let startedAt=0;
      play.addEventListener('click',event=>{
        requirePredecessors(id,'play-click');mixedMetrics.tasks[id].playClicks++;
        appendEvent(id,'video','play-click',{trusted:event.isTrusted});video.play().catch(()=>{});
      });
      video.addEventListener('play',()=>{
        play.hidden=true;if(!startedAt)startedAt=performance.now();appendEvent(id,'video','play');
        if(typeof window.onMixedPlay==='function')window.onMixedPlay(id);
      });
      video.addEventListener('seeking',()=>{mixedMetrics.tasks[id].seekEvents++;persist();});
      video.addEventListener('ratechange',()=>{mixedMetrics.tasks[id].playbackRate=video.playbackRate;persist();});
      video.addEventListener('ended',()=>{
        Object.assign(mixedMetrics.tasks[id],{ended:true,currentTime:video.currentTime,duration:video.duration,elapsed:(performance.now()-startedAt)/1000});
        if(mixedMetrics.tasks[id].playClicks>0&&mixedMetrics.tasks[id].seekEvents===0&&video.playbackRate===1)finish(id,'video');
      });
    }
    persist();return section;
  }

  const layout=mixedParams.get('layout')||'doc-video-doc-video';
  const kinds=layout==='documents'?['document','document','document']:
    layout==='video-doc'?['video','document']:layout==='loading-doc-video'?['document','video']:
    ['dynamic','tail-plugin'].includes(layout)?['video']:
    layout==='none'?[]:['document','video','document','video'];
  kinds.forEach((kind,index)=>addTask(kind,`${kind}-${index+1}`,{
    deep:kind==='document'&&index>0,delay:layout==='loading-doc-video'&&kind==='document'?1500:0
  }));
  addTask('document','old-hidden-reader',{hidden:true});
  const old=addTask('video','old-hidden-video',{hidden:true});old.hidden=false;old.classList.add('old-transparent');
  mixedMetrics.expected=mixedMetrics.expected.filter(id=>id!=='old-hidden-video');
  window.appendMixedPlugin=()=>{
    if(document.getElementById('late-unsupported-plugin'))return;
    const plugin=document.createElement('object');plugin.id='late-unsupported-plugin';
    plugin.type='application/x-mixed-local-plugin';plugin.data='data:application/octet-stream,local-unsupported-attachment';
    plugin.style.cssText='display:block;width:800px;height:320px';
    document.getElementById('tasks').appendChild(plugin);mixedMetrics.pluginPresent=true;
    document.getElementById('next-chapter').disabled=true;appendEvent('late-plugin','attachment','appended');
  };
  document.getElementById('next-chapter').addEventListener('click',event=>{
    mixedMetrics.nextClicks++;mixedMetrics.nextTrusted.push(event.isTrusted);appendEvent('next','navigation','click');
    location.href=new URL('complete.html',mixedAssetURL).href;
  });
  updateNext();window.mixedFixtureMounted=true;persist();
}
