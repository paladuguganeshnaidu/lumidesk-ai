"""Fast, network-free regression tests for 70-page PDF jobs and regeneration."""
import asyncio
import io
import os
import pytest
import pymupdf as fitz
from fastapi.testclient import TestClient

from app.ai import rewrite_report_pages
from app.report_jobs import _comparison, create_job, for_user, jobs, public_job, run_job
from app.pdf_engine import inspect_pdf
from app.report_pipeline import content_slots
from app.main import app


def create_large_pdf(page_count=70):
    doc = fitz.open()
    cover = doc.new_page(width=595, height=842)
    cover.insert_text((40, 58), 'NAGARJUNA COLLEGE OF ENGINEERING AND TECHNOLOGY', fontsize=13)
    cover.insert_text((80, 165), 'Report Titled', fontsize=14)
    cover.insert_text((100, 198), '"Cyber Suraksha Abhiyan"', fontsize=14)
    cover.insert_text((100, 242), 'Nandhushree M(1NC24CS133)', fontsize=12)
    for i in range(1, page_count):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 45), 'National Service Scheme', fontsize=10)
        if i == 1:
            page.insert_text((70, 104), 'Introduction', fontsize=17)
        elif i == 40:
            page.insert_text((70, 104), 'Methodology', fontsize=17)
        for j in range(20):
            page.insert_text((50, 141+j*24), f'The original activity explains cyber safety to community members in practical ways {i:02d}-{j:02d}.', fontsize=9)
        page.insert_text((60, 800), f'Dept of CSE,NCET                         2026-27                         {i}', fontsize=8)
    data = doc.tobytes(deflate=True)
    doc.close()
    return data


def test_70_page_report_with_continuations():
    source = create_large_pdf()
    doc, template = inspect_pdf(source)
    doc.close()
    assert template['page_count'] == 70
    slots = content_slots(template)
    assert len(slots) == 69
    assert slots[2]['section'] == 'Introduction'
    assert slots[-1]['section'] == 'Methodology'
    assert sum(len(p['slots']) for p in slots) == 1380


@pytest.mark.asyncio
async def test_model_errors_fall_back_instead_of_aborting(monkeypatch):
    class Broken:
        class Chat:
            class Completions:
                async def create(self, **kwargs):
                    raise ValueError('substring not found')
            completions = Completions()
        chat = Chat()
    monkeypatch.setattr('app.ai._client', lambda: Broken())
    sections = [{'page_index': 1, 'section': 'Abstract', 'slots': [
        {'block_id': 10, 'length': 90, 'sample': 'The original material describes online security and cyber awareness.'},
        {'block_id': 11, 'length': 85, 'sample': 'The community safety programme helps people protect their passwords.'}
    ]}]
    original = await rewrite_report_pages(title='Smart Irrigation', topic='Water sensors and intelligent crop watering systems', sections=sections)
    revised = await rewrite_report_pages(title='Smart Irrigation', topic='Water sensors and intelligent crop watering systems', sections=sections,variant_seed=9,previous_pages=original)
    assert original[0]['fallback_count'] == 2
    assert len(original[0]['blocks']) == 2
    assert all(block['text'] for block in original[0]['blocks'])
    assert all(len(x['text']) <= sections[0]['slots'][i]['length'] + 9 for i,x in enumerate(original[0]['blocks']))
    assert _comparison(original,revised) <= .30


@pytest.mark.asyncio
async def test_offline_fallback_writes_valid_downloadable_pdf(monkeypatch):
    monkeypatch.setattr('app.ai._client', lambda: (_ for _ in ()).throw(ValueError('provider offline')))
    monkeypatch.setattr('app.report_jobs.record_usage', lambda db,*args,**kw: None)
    monkeypatch.setattr('app.report_jobs.SessionLocal', lambda: (_ for _ in ()).throw(RuntimeError('test database unavailable')))
    source = create_large_pdf(4)
    jobs.clear()
    job = create_job(user_id=999, source=source, title='Smart Agriculture Project', topic='Sensor based soil monitoring and adaptive water supply for crops', student_name='Ganesh Naidu', usn='1NC24CS007')
    for _ in range(500):
        if job.status in ('completed','failed'):
            break
        await asyncio.sleep(.02)
    assert job.status == 'completed', job.error
    assert job.result.startswith(b'%PDF-')
    assert job.total_sections == 3
    assert job.done_sections == job.total_sections
    assert any('fallback' in w for w in job.warnings)
    doc=fitz.open(stream=job.result,filetype='pdf')
    assert len(doc) == 4
    assert 'Ganesh Naidu' in doc[0].get_text()
    assert 'NAGARJUNA COLLEGE' in doc[0].get_text()
    assert 'National Service Scheme' in doc[1].get_text()
    assert 'cyber safety to community members' not in doc[1].get_text()
    doc.close()
    assert for_user(job.id,1000) is None
    assert for_user(job.id,999) is job
    # Regeneration keeps the original document rather than overwriting it.
    second = create_job(user_id=999,source=job.source,title=job.title,topic=job.topic,student_name='Another Student',usn='1NC24CS111',parent=job)
    for _ in range(500):
        if second.status in ('completed','failed'):
            break
        await asyncio.sleep(.02)
    assert second.status == 'completed', second.error
    assert second.overlap is not None
    assert second.overlap <= 0.3
    out = fitz.open(stream=second.result,filetype='pdf')
    assert 'Another Student' in out[0].get_text()
    assert 'Nandhushree' not in out[0].get_text()
    out.close()


def test_71_pages_rejected_with_clear_error():
    data=create_large_pdf(71)
    with pytest.raises(ValueError,match='70'):
        inspect_pdf(data)

@pytest.mark.asyncio
async def test_valid_ai_chunks_are_written_without_fallback(monkeypatch):
    from types import SimpleNamespace
    class Client:
        class Chat:
            class Completions:
                async def create(self, **kwargs):
                    import json
                    data = json.loads(kwargs['messages'][-1]['content'])
                    rows = []
                    for item in data['slots']:
                        size = item['target_characters']
                        text = 'Planned software components can be reviewed for accuracy and useful results.'
                        rows.append({'block_id': item['block_id'], 'text': text[:size]})
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'blocks': rows})))])
            completions = Completions()
        chat = Chat()
    monkeypatch.setattr('app.ai._client', lambda: Client())
    sections = [{'page_index':1, 'section':'Methodology', 'slots': [
        {'block_id': n, 'length': 80, 'sample': f'Original cyber safety activities discussed internet fraud and prevention {n}'} for n in range(24)
    ]}]
    output = await rewrite_report_pages(title='AI Tutor', topic='An interactive learning and document preparation platform', sections=sections)
    assert len(output[0]['blocks']) == 24
    assert output[0]['fallback_count'] == 0
    assert output[0]['blocks'][0]['text'].startswith('Planned software')


def test_http_job_start_poll_download_regenerate(monkeypatch):
    from app import main as main_module
    monkeypatch.setattr('app.ai._client', lambda: (_ for _ in ()).throw(RuntimeError('provider offline')))
    monkeypatch.setattr(main_module, 'current_user', lambda request: {'id': 7001, 'active':True})
    monkeypatch.setattr(main_module, 'require_csrf', lambda request: None)
    # On production, CSRF and Google sign-in still run; mock only within tests.
    monkeypatch.setattr('app.report_jobs.record_usage', lambda db,*args,**kw: None)
    monkeypatch.setattr('app.report_jobs.SessionLocal', lambda: (_ for _ in ()).throw(RuntimeError('test DB intentionally absent')))
    jobs.clear()
    original = create_large_pdf(3)
    with TestClient(app) as client:
        result = client.post('/api/reports/start', files={'file':('reference.pdf',original,'application/pdf')}, data={
            'title':'LumiDesk Research Study',
            'topic':'A student-focused AI platform for clear learning and report generation',
            'student_name':'Ganesh Naidu','usn':'1NC24CS007',
        })
        assert result.status_code == 202, result.text
        first = result.json(); jid = first['job_id']
        assert first['status'] in ('queued','running','completed')
        import time
        for _ in range(150):
            status = client.get('/api/reports/jobs/'+jid)
            assert status.status_code == 200
            if status.json()['status'] in ('completed','failed'):
                break
            time.sleep(.03)
        assert status.json()['status'] == 'completed', status.text
        output = client.get('/api/reports/jobs/'+jid+'/download')
        assert output.status_code == 200
        assert output.headers['content-type'].startswith('application/pdf')
        assert output.content.startswith(b'%PDF-')
        invalid = client.get('/api/reports/jobs/unknown/download')
        assert invalid.status_code == 404
        reg = client.post('/api/reports/jobs/'+jid+'/regenerate', data={'student_name':'Second Student','usn':'1NC24CS111'})
        assert reg.status_code == 202,reg.text
        second_id=reg.json()['job_id']
        for _ in range(150):
            check=client.get('/api/reports/jobs/'+second_id)
            if check.json()['status'] in ('completed','failed'):break
            time.sleep(.03)
        assert check.json()['status'] == 'completed',check.text
        assert check.json()['overlap_estimate'] <= .30
        pdf = client.get('/api/reports/jobs/'+second_id+'/download')
        doc=fitz.open(stream=pdf.content,filetype='pdf')
        assert 'Second Student' in doc[0].get_text()
        assert '1NC24CS111' in doc[0].get_text()
        doc.close()
        # Other authenticated students cannot fetch the current user's job.
        monkeypatch.setattr(main_module, 'current_user', lambda request: {'id': 8002, 'active':True})
        assert client.get('/api/reports/jobs/'+jid+'/download').status_code == 404
        assert client.get('/api/reports/jobs/'+jid).status_code == 404
