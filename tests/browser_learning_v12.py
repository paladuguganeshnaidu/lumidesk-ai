"""Browser E2E interaction matrix for the dependency-free Learning Lab.

This runs the real Jinja markup, CSS and JavaScript; model and web provider
responses are controlled test fixtures. No Google password or API key needed.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright
import re

ROOT=Path(__file__).resolve().parents[1]
html=(ROOT/"app/templates/workspace.html").read_text()
html=re.sub(r"{%[^%]+%}","",html)
html=re.sub(r"<script\b[^>]*src=[^>]*></script>","",html)
html=re.sub(r"<link\b[^>]*>","",html)
styles="\n".join((ROOT/path).read_text() for path in (
    "app/static/styles.css","app/static/workspace-dark.css","app/static/learning_lab.css"))
html=html.replace("</head>","<style>"+styles+"</style></head>")
html=html.replace('{{ csrf }}','mock-csrf')
mock_js=r"""
window.__requests=[];
const lesson={
 "title":"Understanding neural networks",
 "explanation":"A neural network transforms inputs into predictions with adjustable weights.\n\nTraining adjusts weights so predictions improve.",
 "key_points":["Weights change with training","The input layer receives values"],
 "prerequisites":["Input and output"],
 "steps":[{"title":"Start with inputs","detail":"Values go into the first layer."},
          {"title":"Transform values","detail":"Weights combine input values."},
          {"title":"Read a prediction","detail":"The last layer produces a result."}],
 "analogy":"A recipe with adjustable amounts.",
 "example":"Classify an image after training.",
 "practice_challenge":"Draw a 3-layer network and label the inputs.",
 "glossary":[{"term":"Weight","definition":"A learnable number in a model."}],
 "misconceptions":[{"misconception":"Networks are exact human brains.",
                     "correction":"They use mathematical operations and learnable parameters."}],
 "quiz":[{"question":"What changes during learning?",
          "options":["Page margins","Weights","Screen size"],"answer_index":1,
          "explanation":"Learning adjusts weights, not page margins."}],
 "concept_map":{"nodes":[{"id":"first","label":"Inputs","detail":"Start with data."},
                         {"id":"second","label":"Weights","detail":"Weights control the transformation."},
                         {"id":"third","label":"Output","detail":"Read the prediction."}],
                "edges":[{"from":"first","to":"second","label":"feeds"},
                         {"from":"second","to":"third","label":"predicts"}]},
 "research":{"status":"available","sources":[
   {"id":"W1","title":"Artificial neural network","publisher":"Wikipedia",
    "kind":"Encyclopedia overview","url":"https://en.wikipedia.org/wiki/Artificial_neural_network"},
   {"id":"P1","title":"Neural networks — a scholarly overview","publisher":"OpenAlex",
    "kind":"Scholarly work","year":2021,"url":"https://openalex.org/W123456789"}]}
};
window.fetch=async (url,opts={})=>{
 const path=String(url);let data={};
 if(path==='/api/me')data={authenticated:true,user:{id:88,name:'Student',email:'student@example.org',role:'student',active:true},csrf:'mock-csrf'};
 else if(path==='/api/student/overview')data={usage:{tutor:0,reports:0,tutor_limit:0,report_limit:0},reports:[]};
 else if(path==='/api/tutor'){
   const payload=JSON.parse(opts.body);window.__requests.push(payload);
   data={result:lesson,usage:{used:1,limit:0}};
 }
 return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});
};
"""
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=["--no-sandbox","--disable-dev-shm-usage"])
    for width in [320,360,390,768,1024,1440,1920]:
        context=browser.new_context(viewport={"width":width,"height":900},accept_downloads=True)
        page=context.new_page()
        errors=[]
        page.on("pageerror",lambda err:errors.append(str(err)))
        page.route("**/*",lambda route:route.fulfill(status=200,body=html,content_type="text/html")
                   if route.request.url.startswith("http://lumidesk.test/") else route.abort())
        page.goto("http://lumidesk.test/app",wait_until="domcontentloaded")
        page.add_script_tag(content=mock_js)
        page.add_script_tag(content=(ROOT/"app/static/learning_visualizer.js").read_text())
        page.add_script_tag(content=(ROOT/"app/static/app.js").read_text())
        page.wait_for_timeout(100)
        page.locator("#research-enabled").check()
        page.locator("#tutor-message").fill("Explain how a neural network learns, starting from the basics")
        page.locator("#tutor-form button[type=submit]").click()
        page.locator(".learning-node-track .learning-node").first.wait_for(timeout=5000)
        assert page.locator(".learning-node").count()==3
        assert page.locator(".learning-source").count()==2
        assert page.locator(".learning-source").first.get_attribute("href")=="https://en.wikipedia.org/wiki/Artificial_neural_network"
        assert page.evaluate("window.__requests[0].research") is True
        assert page.locator("#tutor-panel.has-lesson").count()==1
        nodes=page.locator(".learning-node")
        nodes.nth(1).click()
        assert "Weights control" in page.locator(".learning-node-detail").inner_text()
        nodes.nth(1).focus()
        page.keyboard.press("ArrowRight")
        assert nodes.nth(2).get_attribute("aria-pressed")=="true"
        page.locator(".learning-step-nav button").last.click()
        assert "Transform values" in page.locator(".learning-step-pane h5").inner_text()
        page.locator(".quiz-question button").first.click()
        assert "Correct answer: Weights" in page.locator(".quiz-feedback").inner_text()
        with page.expect_download(timeout=5000) as download:
            page.get_by_role("button",name="Download study notes").click()
        assert download.value.suggested_filename.endswith(".md")
        page.get_by_role("button",name="Review tomorrow").click()
        assert "saved on this device" in page.locator(".learning-action-status").inner_text()
        page.get_by_role("button",name="Show another example").click()
        assert "worked real-world example" in page.locator("#tutor-message").input_value()
        overflow=page.evaluate("Math.max(document.documentElement.scrollWidth,document.body.scrollWidth)-innerWidth")
        assert overflow<=2,(width,overflow)
        assert not errors,(width,errors)
        print("LEARNING BROWSER PASS",width,"px, concepts, sources, quiz, notes, keyboard, followup, no overflow")
        context.close()
    browser.close()
