"""Image editor regression: immutable reference, per-slot changes, per-user checks."""
import io
from pathlib import Path
import time

import pymupdf as fitz
import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.main import app
from app import main
from app.image_editor import image_slots, normalize_upload, apply_replacements
from app.report_jobs import ReportJob, jobs

SAMPLE = Path('/mnt/data/NANDHUSHREE_NSS.pdf')


def sample():
    if SAMPLE.exists():
        return SAMPLE.read_bytes()
    d=fitz.open()
    for p in range(14):
        page=d.new_page(width=595,height=842)
        page.insert_text((40,40),f'Heading {p+1}',fontsize=14)
        if p in (0,10,12):
            pic=Image.new('RGB',(320,220),(120,85,35));buf=io.BytesIO();pic.save(buf,format='JPEG')
            page.insert_image(fitz.Rect(50,150,260,300),stream=buf.getvalue())
            if p==10:page.insert_image(fitz.Rect(280,150,490,300),stream=buf.getvalue())
    result=d.tobytes();d.close();return result


def jpeg(col=(12,150,190)):
    im=Image.new('RGB',(750,500),col);out=io.BytesIO();im.save(out,'JPEG');return out.getvalue()


def test_image_edit_preserves_all_text_and_other_images():
    raw=sample(); slots=image_slots(raw)
    assert slots
    target=next((s for s in slots if s['page']==11),slots[0]); key=target['id']
    replacement=normalize_upload(jpeg())
    edited=apply_replacements(raw,{key:replacement})
    with fitz.open(stream=raw,filetype='pdf') as before,fitz.open(stream=edited,filetype='pdf') as after:
        assert len(before)==len(after)==14
        assert all(a.rect==b.rect for a,b in zip(before,after))
        assert all(a.get_text('text')==b.get_text('text') for a,b in zip(before,after))
        selected=target['page']-1
        before_pix=before[selected].get_pixmap(clip=fitz.Rect(target['rect'])).samples
        after_pix=after[selected].get_pixmap(clip=fitz.Rect(target['rect'])).samples
        assert before_pix!=after_pix
        # A different page must not have any changes, even when an xref is reused.
        other=0 if selected else 1
        assert before[other].get_pixmap().samples==after[other].get_pixmap().samples
    assert apply_replacements(raw,{})==raw
    assert len(image_slots(raw))==len(slots)


def test_image_editor_rejects_bad_inputs():
    with pytest.raises(ValueError):normalize_upload(b'not actually an image')
    with pytest.raises(ValueError):normalize_upload(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
    with pytest.raises(ValueError):normalize_upload(b'X'* (8*1024*1024+1))
    with pytest.raises(ValueError):apply_replacements(sample(),{'p99-i3':jpeg()})


def test_api_lists_uploads_restores_and_protects_ownership(monkeypatch):
    monkeypatch.setattr(main,'require_csrf',lambda request:None)
    monkeypatch.setattr(main,'current_user',lambda request:{'id':48})
    monkeypatch.setattr(main,'export_editable_docx',lambda raw:(b'PKfake-docx', []))
    jobs.clear();raw=sample()
    job=ReportJob(id='image-job',user_id=48,source=raw,title='Test',topic='Topic',student_name='Tester',usn='USN48',status='completed',result=raw,word_result=b'PKoriginal')
    jobs[job.id]=job
    with TestClient(app) as client:
        response=client.get('/api/reports/jobs/image-job/images')
        assert response.status_code==200,response.text
        listing=response.json();assert listing['count']>=1
        slot=listing['images'][0]['id']
        preview=client.get(f'/api/reports/jobs/image-job/images/{slot}/preview')
        assert preview.status_code==200 and preview.content.startswith(b'\x89PNG')
        response=client.post(f'/api/reports/jobs/image-job/images/{slot}/replace',files={'file':('myphoto.jpg',jpeg(),'image/jpeg')})
        assert response.status_code==200,response.text
        assert response.json()['image_edit_count']==1
        assert response.json()['downloads']['docx']
        assert job.base_result==raw and job.result!=raw
        with fitz.open(stream=job.result,filetype='pdf') as doc:
            assert len(doc)==14
        # Other users cannot even enumerate image slots, view previews or update.
        monkeypatch.setattr(main,'current_user',lambda request:{'id':49})
        assert client.get('/api/reports/jobs/image-job/images').status_code==404
        assert client.get(f'/api/reports/jobs/image-job/images/{slot}/preview').status_code==404
        assert client.post(f'/api/reports/jobs/image-job/images/{slot}/restore').status_code==404
        monkeypatch.setattr(main,'current_user',lambda request:{'id':48})
        invalid=client.post(f'/api/reports/jobs/image-job/images/{slot}/replace',files={'file':('bad.svg',b'<svg/>','image/svg+xml')})
        assert invalid.status_code==422
        assert job.image_edit_count if hasattr(job,'image_edit_count') else len(job.image_edits)==1
        reset=client.post(f'/api/reports/jobs/image-job/images/{slot}/restore')
        assert reset.status_code==200,reset.text
        assert reset.json()['image_edit_count']==0
        assert job.result==raw
    jobs.clear()


def test_frontend_image_editor_present():
    root=Path(__file__).resolve().parents[1]
    html=(root/'app/templates/workspace.html').read_text()
    js=(root/'app/static/app.js').read_text()
    for key in ['edit-report-images','edit-approved-images','image-editor-grid','image-editor-upload']:
        assert f'id="{key}"' in html
    assert 'reportImageEditor.open' in js
    assert '/images/' in js
