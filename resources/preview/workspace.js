/* Trusted local shell. No web channel, fetch, eval or user-supplied script. */
window.MathJax = {
  loader: {load: ['ui/safe']},
  tex: {inlineMath: [['\\(', '\\)']], displayMath: [['\\[', '\\]']],
    packages: {'[-]': ['autoload', 'require']}},
  options: {enableMenu: false, safeOptions: {allow: {URLs: 'none', classes: 'none', cssIDs: 'none', styles: 'none'}}},
  startup: {typeset: false}
};
window.previewState = {ready:false, revision:null, mathCount:0, errors:0};
window.previewQueue = Promise.resolve();
window.previewGeneration = 0;
window.motionEnabled = true;
window.previewMotion = null;
window.previewZoom = 1;
function fitPaper(){
  const paper=document.getElementById('paper');
  const width=paper.offsetWidth;
  const scale=Math.min(1, Math.max(.15, (document.documentElement.clientWidth-48)/width))*window.previewZoom;
  paper.style.zoom=String(scale);
}
window.setPreviewMotion=function(enabled){
  window.motionEnabled=enabled;
  if(!enabled && window.previewMotion)window.previewMotion.revert();
};
window.setPreviewZoom=function(value){window.previewZoom=value;fitPaper();};
window.localSelection=null;
window.invalidatePreview=function(){window.previewGeneration+=1;window.previewState.ready=false;window.localSelection=null;};
function captureLocalSelection(){
  if(!window.previewState.ready)return;
  const selection=window.getSelection();
  if(!selection || selection.isCollapsed || !selection.rangeCount)return;
  const range=selection.getRangeAt(0),paper=document.getElementById('paper');
  if(!paper.contains(range.commonAncestorContainer))return;
  const blocks=[];
  for(const block of paper.querySelectorAll('div[data-lf-block]')){
    let start=Infinity,end=-1;
    for(const leaf of block.querySelectorAll('span[data-lf-start]')){
      if(!range.intersectsNode(leaf))continue;
      const leafRange=document.createRange();leafRange.selectNodeContents(leaf);
      const part=range.cloneRange();
      if(range.compareBoundaryPoints(Range.START_TO_START,leafRange)<0)part.setStart(leaf,0);
      if(range.compareBoundaryPoints(Range.END_TO_END,leafRange)>0)part.setEnd(leaf,leaf.childNodes.length);
      let a=Number(leaf.dataset.lfStart),b=Number(leaf.dataset.lfEnd);
      if(!leaf.classList.contains('local-object')){
        const prefix=document.createRange();prefix.selectNodeContents(leaf);prefix.setEnd(part.startContainer,part.startOffset);
        a+=Array.from(prefix.toString()).length;
        b=Math.min(b,a+Array.from(part.toString()).length);
      }
      if(a<b){start=Math.min(start,a);end=Math.max(end,b);}
    }
    if(start<end)blocks.push({block:block.dataset.lfBlock,start,end});
    if(blocks.length>64){window.localSelection={revision:window.previewState.revision,tooLarge:true,blocks:[]};return;}
  }
  if(blocks.length)window.localSelection={revision:window.previewState.revision,blocks};
}
window.getLocalSelection=function(){return window.previewState.ready?window.localSelection:null;};
document.addEventListener('selectionchange',captureLocalSelection);
document.addEventListener('mouseup',()=>{if(window.getSelection().isCollapsed)window.localSelection=null;else captureLocalSelection();});
function selectionPoint(block,position,end){
  const leaves=Array.from(block.querySelectorAll('span[data-lf-start]'));
  const leaf=leaves.find(node=>Number(node.dataset.lfStart)<=position && (end?position<=Number(node.dataset.lfEnd):position<Number(node.dataset.lfEnd)));
  if(!leaf)return null;
  if(leaf.classList.contains('local-object'))return [leaf,end?leaf.childNodes.length:0];
  let remaining=position-Number(leaf.dataset.lfStart);
  const walker=document.createTreeWalker(leaf,NodeFilter.SHOW_TEXT);
  for(let node=walker.nextNode();node;node=walker.nextNode()){
    const chars=Array.from(node.textContent);
    if(remaining<=chars.length)return [node,chars.slice(0,remaining).join('').length];
    remaining-=chars.length;
  }
  return [leaf,leaf.childNodes.length];
}
function restoreLocalSelection(blocks){
  if(!blocks || !blocks.length)return;
  const paper=document.getElementById('paper'),first=blocks[0],last=blocks[blocks.length-1];
  const a=paper.querySelector('div[data-lf-block="'+first.block+'"]'),b=paper.querySelector('div[data-lf-block="'+last.block+'"]');
  if(!a || !b)return;
  const start=selectionPoint(a,first.start,false),end=selectionPoint(b,last.end,true);
  if(!start || !end)return;
  const range=document.createRange();range.setStart(...start);range.setEnd(...end);
  const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
  captureLocalSelection();
}
window.renderDocument=function(payload){
  const generation=++window.previewGeneration;
  window.localSelection=null;
  window.previewState={ready:false, revision:payload.revision, mathCount:0, errors:0};
  window.previewQueue=window.previewQueue.catch(()=>{}).then(async()=>{
    if(generation!==window.previewGeneration)return;
    const paper=document.getElementById('paper');
    const scroll=document.scrollingElement.scrollTop;
    if(window.previewMotion){window.previewMotion.revert();window.previewMotion=null;}
    await MathJax.startup.promise;
    if(generation!==window.previewGeneration)return;
    MathJax.typesetClear([paper]);
    paper.innerHTML=payload.html;
    document.getElementById('document-style').textContent=payload.css;
    // Pandoc alone decides which nodes are Math. Scanning the whole paper
    // would reinterpret literal TeX delimiters in plain text/escaped Markdown.
    const mathNodes=Array.from(paper.querySelectorAll('span.math.inline, span.math.display'));
    if(mathNodes.length)await MathJax.typesetPromise(mathNodes);
    await document.fonts.ready;
    if(generation!==window.previewGeneration)return;
    fitPaper();
    document.scrollingElement.scrollTop=scroll;
    if(window.gsap && window.motionEnabled){
      window.previewMotion=gsap.matchMedia();
      window.previewMotion.add('(prefers-reduced-motion: no-preference)',()=>{
        gsap.fromTo(paper,{autoAlpha:.6,y:6},{autoAlpha:1,y:0,duration:.24,ease:'power2.out',clearProps:'transform,opacity,visibility'});
      });
    }
    // DOM completion precedes the compositor; wait for real glyph/layout frames.
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    if(generation!==window.previewGeneration)return;
    window.previewState={ready:true,revision:payload.revision,
      mathCount:paper.querySelectorAll('mjx-container').length,
      fontsLoaded:Array.from(document.fonts).filter(font=>font.status==='loaded').length,
      errors:paper.querySelectorAll('mjx-merror').length,gsap:window.gsap?gsap.version:null};
    restoreLocalSelection(payload.selectionRestore);
  }).catch(()=>{if(generation===window.previewGeneration)window.previewState={ready:false,revision:payload.revision,errors:1};});
};
window.addEventListener('resize',fitPaper);
window.addEventListener('DOMContentLoaded',fitPaper);
