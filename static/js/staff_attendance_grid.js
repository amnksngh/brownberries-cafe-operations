(() => {
 const form=document.querySelector('#attendance-paint-form'); if(!form)return;
 const grid=document.querySelector('#attendance-paint-grid'), brush=document.querySelector('#attendance-brush'), span=document.querySelector('#attendance-span'), drag=document.querySelector('#attendance-drag'), edits=new Map(); let painting=false;
 const summary=document.querySelector('#attendance-paint-summary');
 const refresh=()=>{summary.textContent=edits.size ? `${edits.size} half-days selected across ${new Set([...edits.values()].map(x=>x.day)).size} dates. Changes are not saved yet.`:'No changes selected.';};
 function paint(button){if(!button||button.disabled)return;const buttons=span.value==='day'?[...grid.querySelectorAll('button')].filter(x=>x.dataset.day===button.dataset.day):[button];for(const b of buttons){edits.set(b.dataset.day+':'+b.dataset.half,{day:b.dataset.day,half:b.dataset.half,status:brush.value});b.querySelector('span').textContent=brush.value;b.classList.add('painted');b.setAttribute('aria-label',`${b.dataset.day} ${b.dataset.half.replace('_',' ')}: ${brush.value}`);} refresh();}
 grid.addEventListener('pointerdown',e=>{const b=e.target.closest('button');if(!b)return;if(e.pointerType==='touch'&&!drag.checked)return;painting=true;e.preventDefault();paint(b);});
 document.addEventListener('pointermove',e=>{if(!painting)return;const b=document.elementFromPoint(e.clientX,e.clientY)?.closest('button');if(b&&grid.contains(b))paint(b);});
 document.addEventListener('pointerup',()=>painting=false);document.addEventListener('pointercancel',()=>painting=false);
 grid.addEventListener('click',e=>{if(e.detail===0||(!drag.checked&&e.pointerType==='touch'))paint(e.target.closest('button'));});
 drag.addEventListener('change',()=>grid.classList.toggle('paint-drag',drag.checked));
 document.querySelector('#attendance-clear').addEventListener('click',()=>{edits.clear();for(const b of grid.querySelectorAll('button')){b.querySelector('span').textContent=b.dataset.original;b.classList.remove('painted');}refresh();});
 form.addEventListener('submit',e=>{if(!edits.size||!confirm(`Save ${edits.size} selected half-days? Attendance and salary fractions will be recalculated; unnotified absences require admin penalty review.`)){e.preventDefault();return;}document.querySelector('#attendance-cells').value=JSON.stringify([...edits.values()]);});
 window.addEventListener('beforeunload',e=>{if(edits.size&&!form.dataset.saving){e.preventDefault();e.returnValue='';}});form.addEventListener('submit',e=>{if(!e.defaultPrevented)form.dataset.saving='yes';});
})();
