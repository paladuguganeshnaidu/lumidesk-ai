from pathlib import Path
from playwright.sync_api import sync_playwright
import re

ROOT=Path(__file__).resolve().parents[1]
html=(ROOT/'app/templates/workspace.html').read_text()
html=re.sub(r'{%[^%]+%}','',html)
html=re.sub(r'<script\b[^>]*src=[^>]*></script>','',html)
html=re.sub(r'<link\b[^>]*>','',html)
html=html.replace('</head>','<style>'+ (ROOT/'app/static/styles.css').read_text()+'\n'+(ROOT/'app/static/workspace-dark.css').read_text()+'</style></head>')
mock_js=r'''
window.__imageReplaced=false;
window.fetch=async (url,options={})=>{
 let data={};let status=200;
 if(url==='/api/me')data={authenticated:true,user:{id:91,name:'Tester',email:'test@example.org',role:'student',active:true},csrf:'fake'};
 else if(url==='/api/student/overview')data={usage:{tutor:0,reports:0,tutor_limit:0,report_limit:0},reports:[]};
 else if(url==='/api/reports/analyze')data={page_count:14,editable_line_count:120,editable_sections:[{name:'Introduction'}],image_count:8};
 else if(url==='/api/reports/start')data={job_id:'mock',status:'queued'};
 else if(url==='/api/reports/jobs/mock/images')data={images:[{id:'p10-i0',page:11,index:1,edited:window.__imageReplaced,preview_url:'/api/reports/jobs/mock/images/p10-i0/preview'},{id:'p10-i1',page:11,index:2,edited:false,preview_url:'/api/reports/jobs/mock/images/p10-i1/preview'}],count:2,edited_count:window.__imageReplaced?1:0};
 else if(url.includes('/images/p10-i0/replace') && options.method==='POST') {window.__imageReplaced=true;data={job_id:'mock',status:'completed',downloads:{pdf:'/api/reports/jobs/mock/download?format=pdf',docx:'/api/reports/jobs/mock/download?format=docx'},image_edit_count:1};}
 else if(url.includes('/images/p10-i0/restore') && options.method==='POST') {window.__imageReplaced=false;data={job_id:'mock',status:'completed',downloads:{pdf:'/api/reports/jobs/mock/download?format=pdf',docx:'/api/reports/jobs/mock/download?format=docx'},image_edit_count:0};}
 else if(url.includes('/download?')){const format=url.split('format=')[1];return new Response(new Uint8Array(Array(256).fill(65)),{status:200,headers:{'Content-Type':format==='pdf'?'application/pdf':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','Content-Disposition':`attachment; filename="Edited_LumiDesk.${format}"`}});}
 else if(url==='/api/reports/jobs/mock')data={job_id:'mock',status:'completed',pages:14,warnings:[],downloads:{pdf:'/api/reports/jobs/mock/download?format=pdf',docx:'/api/reports/jobs/mock/download?format=docx'}};
 else if(url.includes('/api/reports/jobs/'))data={job_id:'mock',status:'completed',pages:14,warnings:[],downloads:{pdf:'/api/reports/jobs/mock/download?format=pdf',docx:'/api/reports/jobs/mock/download?format=docx'}};
 return new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
};
'''
with sync_playwright() as p:
    executable = '/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None
    browser=p.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
    for width in [320,390,768,1024,1440,1920]:
        page=browser.new_page(viewport={'width':width,'height':900},accept_downloads=True)
        page.route('**/*',lambda route:route.abort())
        errors=[];page.on('pageerror',lambda err:errors.append(str(err)))
        page.set_content(html)
        page.add_script_tag(content=mock_js)
        page.add_script_tag(content=(ROOT/'app/static/app.js').read_text())
        page.wait_for_timeout(200)
        page.evaluate("document.querySelector('[data-tab=reports]').click()")
        page.set_input_files('#reference-pdf',{'name':'template.pdf','mimeType':'application/pdf','buffer':b'%PDF-1.4\n'+b'X'*400})
        page.fill('#report-form [name=title]','Innovation Report')
        page.fill('#report-form [name=topic]','This is a sufficient concept length about smart learning technology')
        page.fill('#report-form [name=student_name]','User Tester')
        page.fill('#report-form [name=usn]','1NC24CS555')
        page.click('#generate-report')
        page.locator('#report-success:not(.hidden)').wait_for(timeout=6000)
        page.click('#edit-report-images')
        page.locator('#image-edit-modal:not(.hidden)').wait_for(timeout=6000)
        page.locator('.image-editor-card').first.wait_for(timeout=9000)
        assert page.locator('.image-editor-card').count()==2,(width,errors)
        overflow=page.evaluate('Math.max(document.documentElement.scrollWidth,document.body.scrollWidth)-innerWidth')
        assert overflow<=1,(width,overflow)
        page.locator('.image-editor-card button').first.click()
        page.set_input_files('#image-editor-upload',{'name':'myphoto.jpg','mimeType':'image/jpeg','buffer':b'\xff\xd8\xff\xdb'+b'A'*100})
        page.wait_for_function('window.__imageReplaced===true',timeout=4000)
        page.locator('.image-editor-card button').filter(has_text='Restore original').wait_for(timeout=6000)
        assert '1 replaced' in page.locator('#image-editor-status').inner_text().lower(),(width,page.locator('#image-editor-status').inner_text())
        with page.expect_download(timeout=5000) as item:page.click('#image-editor-pdf')
        assert item.value.suggested_filename.endswith('.pdf')
        with page.expect_download(timeout=5000) as item:page.click('#image-editor-word')
        assert item.value.suggested_filename.endswith('.docx')
        page.get_by_text('Restore original').click()
        page.wait_for_function('window.__imageReplaced===false',timeout=4000)
        page.click('#image-editor-done')
        assert page.locator('#image-edit-modal').is_hidden()
        assert not errors,errors
        print('BROWSER PASS',width,'px image chooser, replace, restore, download both, no page errors, overflow',overflow)
        page.close()
    browser.close()
