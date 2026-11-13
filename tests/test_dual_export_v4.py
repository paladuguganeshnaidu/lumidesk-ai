"""Validate measured typography, PDF/Word parity, and download security."""
from __future__ import annotations
import io
import time
import zipfile
import pytest
import pymupdf as fitz
from fastapi.testclient import TestClient
from app.pdf_engine import inspect_pdf
from app.typography import summarize_typography
from app.docx_export import export_editable_docx
from app.main import app
from app.report_jobs import jobs
from tests.test_report_jobs_v3 import create_large_pdf


def test_typography_precise_baselines_and_family():
    doc=fitz.open()
    page=doc.new_page(width=612,height=792)
    page.insert_text((56, 110),'TEST DOCUMENT HEADING',fontname='tiro',fontsize=15)
    page.insert_text((62, 171),'A normal original body line with measured positions.',fontname='tiro',fontsize=11)
    page.insert_text((62, 187),'Second normal body line with its next baseline.',fontname='tiro',fontsize=11)
    data=doc.tobytes();doc.close()
    original,template=inspect_pdf(data);original.close()
    blocks=template['pages'][0]['blocks']
    assert len(blocks)==3
    assert blocks[0]['font_family'] in ('Times New Roman','Times-Roman')
    assert blocks[1]['origin'][1] == pytest.approx(171,abs=0.1)
    assert blocks[2]['line_spacing_pt'] == pytest.approx(16,abs=0.1)
    summary=summarize_typography(template)
    assert summary['pages']==1
    assert summary['first_page']['page_size_pt']==[612.0,792.0]
    assert summary['font_styles']


def test_docx_is_editable_and_artwork_preserved():
    pdf=create_large_pdf(2)
    data,warnings=export_editable_docx(pdf)
    assert data.startswith(b'PK')
    assert warnings
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        document=z.read('word/document.xml')
        assert document.count(b'<w:txbxContent>') > 25
        assert b'National Service Scheme' in document
        assert b'Original activity' not in document
        assert b'\x00' not in document
        assert len([x for x in z.namelist() if x.startswith('word/media/')]) >= 1


def test_70_page_word_export_has_text_on_last_page():
    src=create_large_pdf(70)
    word, _=export_editable_docx(src,dpi=80)
    with zipfile.ZipFile(io.BytesIO(word)) as z:
        xml=z.read('word/document.xml')
        assert xml.count(b'lastRenderedPageBreak')==0
        assert xml.count(b'<w:br w:type="page"')==69
        assert b'69-19' in xml


def test_api_word_pdf_downloads_and_typography(monkeypatch):
    from app import main as m
    from app import report_jobs
    monkeypatch.setattr(m,'current_user',lambda request:{'id':2001,'active':True})
    monkeypatch.setattr(m,'require_csrf',lambda request:None)
    monkeypatch.setattr(report_jobs,'SessionLocal',lambda: (_ for _ in ()).throw(RuntimeError('mock DB')))
    monkeypatch.setattr('app.ai._client',lambda: (_ for _ in ()).throw(RuntimeError('test offline AI')))
    jobs.clear()
    with TestClient(app) as client:
        original=create_large_pdf(3)
        analysis=client.post('/api/reports/analyze',files={'file':('template.pdf', original,'application/pdf')})
        assert analysis.status_code==200,analysis.text
        assert 'typography' in analysis.json()
        assert analysis.json()['typography']['font_styles']
        r=client.post('/api/reports/start',files={'file':('template.pdf',original,'application/pdf')},data={
            'title':'Example Research Report',
            'topic':'A student learning tool with interactive diagrams and revised academic writing',
            'student_name':'Ganesh Naidu','usn':'1NC24CS007',
        })
        assert r.status_code==202,r.text
        job_id=r.json()['job_id']
        for _ in range(150):
            status=client.get(f'/api/reports/jobs/{job_id}')
            if status.json()['status'] in ('failed','completed'):break
            time.sleep(.05)
        assert status.json()['status']=='completed',status.text
        assert status.json()['downloads']['pdf']
        assert status.json()['downloads']['docx']
        pdf=client.get(status.json()['downloads']['pdf'])
        word=client.get(status.json()['downloads']['docx'])
        assert pdf.status_code==200
        assert word.status_code==200
        assert pdf.content.startswith(b'%PDF-')
        assert word.content.startswith(b'PK')
        assert 'wordprocessingml.document' in word.headers['content-type']
        with zipfile.ZipFile(io.BytesIO(word.content)) as z:
            xml=z.read('word/document.xml')
            assert b'Ganesh Naidu' in xml
            assert b'National Service Scheme' in xml
        assert client.get(f'/api/reports/jobs/{job_id}/download?format=txt').status_code==422
        monkeypatch.setattr(m,'current_user',lambda request:{'id':9000,'active':True})
        assert client.get(f'/api/reports/jobs/{job_id}/download?format=docx').status_code==404
