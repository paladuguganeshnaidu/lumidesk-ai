/* LumiDesk v1.2 — dependency-free, accessible student visual lessons.
   All model and research content is inserted with textContent, never innerHTML. */
(() => {
  'use strict';
  const make = (name, className = '', text = '') => {
    const el = document.createElement(name);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  };
  const add = (parent, ...children) => {children.forEach(child => parent.appendChild(child)); return parent;};
  const button = (text, action, className = '') => {
    const el = make('button', className, text);
    el.type = 'button';
    el.addEventListener('click', action);
    return el;
  };
  const list = (value, max=8) => Array.isArray(value) ? value.slice(0,max) : [];
  const string = (value) => String(value == null ? '' : value).trim();
  function externalLink(value) {
    try {
      const u = new URL(value);
      return u.protocol === 'https:' && !u.username && !u.password &&
        ['en.wikipedia.org', 'openalex.org', 'doi.org'].includes(u.hostname)
        ? u.href : null;
    } catch {return null;}
  }
  function notes(data) {
    const lines = [
      '# ' + string(data.title || 'LumiDesk lesson'),
      '', '## Explanation', string(data.explanation || ''), '',
      '## What to remember', ...list(data.key_points).map(x=>'- '+string(x)), '',
      '## Work through the topic',
      ...list(data.steps,9).map((x,i)=>(i+1)+'. '+string(x.title)+': '+string(x.detail)),
      '', '## Analogy', string(data.analogy || ''), '',
      '## Practical example', string(data.example || ''),
      '', '## Quick test',
      ...list(data.quiz,5).map((q,i)=>{
        const answer=list(q.options,5)[Number(q.answer_index)] || '';
        return (i+1)+'. '+string(q.question)+'\n   Answer: '+string(answer)+
          '\n   Why: '+string(q.explanation);
      }), '', '## Source links',
      ...list(data.research?.sources,6).map(x=>'- '+string(x.title)+': '+string(x.url)),
      '', 'AI-generated educational draft. Verify important facts against primary material.'
    ];
    return lines.join('\n');
  }
  function downloadNotes(data) {
    const text=notes(data);
    const blob=new Blob([text],{type:'text/markdown;charset=utf-8'});
    const url=URL.createObjectURL(blob);
    const link=document.createElement('a');
    link.href=url;link.download='LumiDesk-learning-notes.md';
    document.body.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),2000);
  }
  function render(article,data) {
    if (!article || !data || typeof data !== 'object') return;
    const section=make('section','learning-extras');
    section.setAttribute('aria-label','Explore lesson and research');
    const intro=make('div','learning-extras-header');
    add(intro, make('span','learning-kicker','INTERACTIVE STUDY LAB'),
      make('h3','','Explore it. Test it. Remember it.'),
      make('p','','Select a concept to see its meaning, follow the stages, then test your recall.'));
    section.append(intro);

    const diagram = data.concept_map && typeof data.concept_map === 'object'
      ? data.concept_map : {};
    const nodes=list(diagram.nodes,10).filter(x=>x && x.id && x.label);
    const edges=list(diagram.edges,15).filter(x=>x && x.from && x.to);
    if (nodes.length) {
      const view=make('div','learning-map');
      view.setAttribute('aria-label','Interactive concept map');
      const mapHeading=make('div','learning-block-heading');
      add(mapHeading,make('h4','','Concept pathway'),
        make('span','learning-meta',nodes.length+' connected ideas'));
      const track=make('div','learning-node-track');
      track.setAttribute('role','group');
      track.setAttribute('aria-label','Choose a concept');
      const detail=make('div','learning-node-detail');
      detail.setAttribute('aria-live','polite');
      const detailTitle=make('strong','','');
      const detailText=make('p','','');
      const relation=make('p','learning-relation','');
      add(detail,detailTitle,detailText,relation);
      // Draw a genuine connected graph in local SVG (no remote service, no
      // AI-generated HTML). The button pathway below is the keyboard fallback.
      const graphScroller=make('div','learning-graph-scroller');
      const SVG='http://www.w3.org/2000/svg';
      const svg=(tag,attrs={},text='')=>{
        const el=document.createElementNS(SVG,tag);
        for(const [k,v] of Object.entries(attrs))el.setAttribute(k,String(v));
        if(text)el.textContent=String(text);
        return el;
      };
      const graph=svg('svg',{viewBox:'0 0 960 292',class:'learning-graph-svg',
        role:'img','aria-label':'Connected diagram of '+nodes.length+' concepts'});
      const positions=new Map();
      nodes.forEach((node,i)=>positions.set(node.id,{
        x:(i+.5)*960/nodes.length,
        y:nodes.length===1?145:(i%2?202:91)
      }));
      const defs=svg('defs');
      const marker=svg('marker',{id:'lumi-learning-arrow',markerWidth:9,markerHeight:9,
        refX:8,refY:4,orient:'auto',markerUnits:'strokeWidth'});
      marker.append(svg('path',{d:'M 0 0 L 8 4 L 0 8 z',fill:'#6C7378'}));
      defs.append(marker);graph.append(defs);
      edges.forEach(edge=>{
        const a=positions.get(edge.from),b=positions.get(edge.to);
        if(!a||!b)return;
        const curve='M '+a.x+' '+a.y+' C '+(a.x+(b.x-a.x)*.48)+' '+a.y+
          ' '+(a.x+(b.x-a.x)*.52)+' '+b.y+' '+b.x+' '+b.y;
        graph.append(svg('path',{d:curve,fill:'none',stroke:'#6C7378',
          'stroke-width':1.3,'stroke-opacity':.72,'marker-end':'url(#lumi-learning-arrow)'}));
      });
      const graphGroups=[];
      nodes.forEach((node,i)=>{
        const loc=positions.get(node.id);
        const g=svg('g',{class:'learning-graph-node',role:'button',tabindex:0,
          'aria-label':'Explore '+string(node.label)});
        g.append(svg('rect',{x:loc.x-58,y:loc.y-31,width:116,height:62,rx:3,
          fill:'#101317',stroke:'rgba(237,231,220,.26)'}));
        g.append(svg('circle',{cx:loc.x-42,cy:loc.y-17,r:3.3,fill:'#E8913C'}));
        const title=string(node.label);
        const words=title.split(/\s+/);
        const lines=[];
        let current='';
        words.forEach(word=>{
          if((current+' '+word).trim().length>15&&current){lines.push(current);current=word;}
          else current=(current+' '+word).trim();
        });
        if(current)lines.push(current);
        if(lines.length>2){lines[1]=lines.slice(1).join(' ').slice(0,15)+'…';lines.length=2;}
        const label=svg('text',{x:loc.x,y:loc.y-(lines.length>1?3:0),
          'text-anchor':'middle','dominant-baseline':'central',
          fill:'#EDE7DC','font-size':12,'font-weight':600,'font-family':'Sora, sans-serif'});
        lines.slice(0,2).forEach((line,j)=>{
          const span=svg('tspan',{x:loc.x,dy:j===0?(lines.length>1?-5:0):17},line);
          label.append(span);
        });
        g.append(label);
        g.addEventListener('click',()=>select(i));
        g.addEventListener('keydown',event=>{
          if(event.key==='Enter'||event.key===' '){event.preventDefault();select(i);}
        });
        graphGroups.push(g);graph.append(g);
      });
      graphScroller.append(graph);
      const nodeButtons=[];
      let active=0;
      const select=(i,focus=false)=>{
        graphGroups.forEach((el,j)=>el.classList.toggle('active',j===((i+nodes.length)%nodes.length)));
        active=(i+nodes.length)%nodes.length;
        nodeButtons.forEach((btn,j)=>{
          btn.setAttribute('aria-pressed', String(active===j));
          btn.classList.toggle('active',active===j);
        });
        detailTitle.textContent=string(nodes[active].label);
        detailText.textContent=string(nodes[active].detail) || 'Review how this concept connects to the overall topic.';
        const connected=edges.filter(edge=>edge.from===nodes[active].id||edge.to===nodes[active].id);
        relation.textContent=connected.length
          ? 'CONNECTS TO · '+connected.map(edge=>{
              const id=edge.from===nodes[active].id?edge.to:edge.from;
              const node=nodes.find(x=>x.id===id);
              return node ? string(node.label) : '';
            }).filter(Boolean).slice(0,4).join(' / ')
          : 'Explore the next idea to continue.';
        if (focus) nodeButtons[active].focus();
      };
      nodes.forEach((node,i)=>{
        const el=make('button','learning-node');
        el.type='button';
        el.setAttribute('aria-label','Explore '+string(node.label));
        add(el,make('span','learning-node-index',String(i+1).padStart(2,'0')),
          make('span','learning-node-name',string(node.label)));
        el.addEventListener('click',()=>select(i));
        el.addEventListener('keydown',ev=>{
          if (ev.key==='ArrowRight'||ev.key==='ArrowLeft'){
            ev.preventDefault();select(i+(ev.key==='ArrowRight'?1:-1),true);
          }
          if(ev.key==='Home'){ev.preventDefault();select(0,true);}
          if(ev.key==='End'){ev.preventDefault();select(nodes.length-1,true);}
        });
        nodeButtons.push(el);track.append(el);
      });
      add(view,mapHeading,graphScroller,track,detail);
      section.append(view);
      select(0);
    }

    const steps=list(data.steps,9).filter(x=>x&&x.detail);
    if(steps.length){
      const shell=make('section','learning-walkthrough');
      const head=make('div','learning-block-heading');
      add(head,make('h4','','Walk through the idea'),
        make('span','learning-meta','ONE STEP AT A TIME'));
      const pane=make('div','learning-step-pane');
      const index=make('span','learning-step-index','');
      const title=make('h5','','');
      const content=make('p','','');
      const navigation=make('div','learning-step-nav');
      const counter=make('span','learning-step-counter','');
      let current=0;
      const renderStep=()=>{
        index.textContent='STEP '+String(current+1).padStart(2,'0');
        title.textContent=string(steps[current].title)||'Step '+(current+1);
        content.textContent=string(steps[current].detail);
        counter.textContent=(current+1)+' / '+steps.length;
        prev.disabled=current===0;
        next.disabled=current===steps.length-1;
      };
      const prev=button('Previous',()=>{current--;renderStep();},'learning-outline-button');
      const next=button('Next step',()=>{current++;renderStep();},'learning-outline-button');
      add(navigation,prev,counter,next);
      add(pane,index,title,content,navigation);
      add(shell,head,pane);section.append(shell);renderStep();
    }

    const glossary=list(data.glossary,8).filter(x=>x&&x.term&&x.definition);
    const myths=list(data.misconceptions,5).filter(x=>x&&x.correction);
    if(glossary.length||myths.length){
      const review=make('div','learning-review-grid');
      if(glossary.length){
        const group=make('details','learning-detail-list');
        const summary=make('summary','','Essential vocabulary ('+glossary.length+')');
        group.append(summary);
        glossary.forEach(x=>{
          const row=make('div','learning-term');
          add(row,make('b','',string(x.term)),make('p','',string(x.definition)));
          group.append(row);
        });review.append(group);
      }
      if(myths.length){
        const group=make('details','learning-detail-list');
        group.append(make('summary','','Common misconceptions ('+myths.length+')'));
        myths.forEach(x=>{
          const row=make('div','learning-term');
          add(row,make('b','',string(x.misconception)),make('p','',string(x.correction)));
          group.append(row);
        });review.append(group);
      }
      section.append(review);
    }

    if(data.practice_challenge){
      const practice=make('div','learning-practice');
      add(practice,make('span','learning-kicker','TRY IT YOURSELF'),
        make('p','',string(data.practice_challenge)));
      section.append(practice);
    }
    const research=data.research||{};
    const sourceBox=make('section','learning-sources');
    const srHead=make('div','learning-block-heading');
    add(srHead,make('h4','','Research & reading'),
      make('span','learning-meta',research.status==='available'?'EXTERNAL SOURCES':'SOURCE STATUS'));
    sourceBox.append(srHead);
    const sources=list(research.sources,6);
    if(sources.length){
      const srcList=make('div','learning-source-list');
      sources.forEach(item=>{
        const url=externalLink(item.url);
        if(!url)return;
        const a=make('a','learning-source');
        a.href=url;a.target='_blank';a.rel='noopener noreferrer';
        add(a,make('span','learning-source-id',string(item.id||'SOURCE')),
          make('span','learning-source-details'));
        const info=a.lastElementChild;
        add(info,make('b','',string(item.title)),make('small','',string(item.publisher)+
          (item.year?' · '+string(item.year):'')+' · '+string(item.kind)));
        add(a,make('span','learning-source-open','OPEN ↗'));
        srcList.append(a);
      });
      sourceBox.append(srcList);
    }else{
      sourceBox.append(make('p','learning-source-empty',
        research.status==='not_requested'
          ? 'Web search is off. Turn on “Find sources on the web” before your next question to see reading links.'
          : string(research.note||'No live reading links could be found.')));
      if(research.status==='unavailable'){
        const term=encodeURIComponent(string(research.query||data.title).slice(0,120));
        if(term){
          const searchLinks=make('div','learning-actions');
          [
            ['Search Wikipedia directly','https://en.wikipedia.org/w/index.php?search='+term],
            ['Search OpenAlex directly','https://openalex.org/works?search='+term],
          ].forEach(([name,url])=>{
            const link=make('a','learning-action-button',name);
            link.href=url;link.target='_blank';link.rel='noopener noreferrer';
            searchLinks.append(link);
          });
          sourceBox.append(searchLinks);
        }
      }
    }
    sourceBox.append(make('p','learning-source-disclaimer',
      'References are linked for further reading, not automatically verified endorsements. Read them to check AI explanations.'));
    section.append(sourceBox);

    const actions=make('div','learning-actions');
    const status=make('span','learning-action-status','');
    status.setAttribute('role','status');
    actions.append(button('Download study notes',()=>downloadNotes(data),'learning-action-button'));
    actions.append(button('Copy notes',async ()=>{
      try{
        if(!navigator.clipboard?.writeText)throw Error('Clipboard unavailable');
        await navigator.clipboard.writeText(notes(data));status.textContent='Notes copied.';
      }catch{status.textContent='Clipboard unavailable; use Download study notes.';}
    },'learning-action-button'));
    actions.append(button('Review tomorrow',()=>{
      try{
        const key='lumidesk_learning_reviews_v1';
        const recent=JSON.parse(localStorage.getItem(key)||'[]');
        const title=string(data.title).slice(0,120);
        const due=new Date(Date.now()+24*60*60*1000).toISOString();
        const next=Array.isArray(recent)?recent.filter(item=>item.title!==title).slice(-19):[];
        next.push({title,due});
        localStorage.setItem(key,JSON.stringify(next));
        status.textContent='Review saved on this device for tomorrow.';
      }catch{status.textContent='Browser storage unavailable; download notes instead.';}
    },'learning-action-button'));
    actions.append(status);section.append(actions);

    const followups=make('div','learning-followups');
    add(followups,make('span','learning-kicker','KEEP LEARNING'));
    const question=string(data.title).slice(0,150);
    [
      ['Teach me the prerequisites','Explain the basics I need before studying '+question+' with simple examples.'],
      ['Show another example','Give me a completely worked real-world example of '+question+'.'],
      ['Challenge me','Give me 5 challenging practice questions about '+question+' with explanations.'],
    ].forEach(([label,prompt])=>followups.append(
      button(label,()=>{
        const input=document.getElementById('tutor-message');
        if(input){input.value=prompt;input.focus();input.scrollIntoView({block:'nearest'});}
      },'learning-followup-button')
    ));
    section.append(followups);
    article.append(section);
  }
  window.LumiLearning={render, notes, externalLink};
})();
