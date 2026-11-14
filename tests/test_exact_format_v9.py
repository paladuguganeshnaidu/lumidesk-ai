"""Exact-format report font, positioning and visual wrapping regressions."""
from __future__ import annotations

import pymupdf as fitz
import pytest

from app.pdf_engine import inspect_pdf, render_replacement_pdf
from app.pdf_quality import verify_pdf_layout
from app.report_pipeline import static_replacements


def make_reference():
    doc=fitz.open()
    p=doc.new_page(width=595,height=842)
    p.insert_text((232,100),"Old Topic",fontname="hebo",fontsize=16)
    for i,txt in enumerate([
        "Project research and careful design benefit from clear problem descriptions.",
        "Students can compare methods and select appropriate ways to verify outcomes.",
        "Documenting assumptions can help the team understand unresolved challenges.",
        "A final review records remaining questions and areas for future improvement."
    ]):
        p.insert_text((42,170+22*i),txt,fontname="tiro",fontsize=12)
    data=doc.tobytes();doc.close()
    return data


def test_cover_title_never_shrinks_to_fit_longer_title():
    src=make_reference()
    doc,template=inspect_pdf(src)
    doc.close()
    box=template["pages"][0]["blocks"][0]
    assert round(box["font_size"])==16
    replacement="Different Project Subject And Idea"
    plan={"pages":[{"page_index":0,"blocks":[{"block_id":box["block_id"],"text":replacement}]}]}
    result,warnings=render_replacement_pdf(src,template,plan)
    with fitz.open(stream=result,filetype="pdf") as pdf:
        spans=[s for b in pdf[0].get_text("dict")["blocks"] if b["type"]==0
               for line in b["lines"] for s in line["spans"]
               if "Different" in s["text"]]
        assert spans, "Updated title must be visible"
        assert all(abs(s["size"]-16)<.05 for s in spans)
    assert verify_pdf_layout(src,result,template,plan)["status"]=="passed"


def test_paragraph_reflow_preserves_original_font_sizes_and_page_count():
    src=make_reference()
    doc,template=inspect_pdf(src)
    doc.close()
    orig=template["pages"][0]["blocks"]
    assert len(orig)>=5
    replacements=[
        "An original proposal explores transparent methods for improving learning.",
        "Its design considers needs and evaluates practical limitations carefully.",
        "Project stages document each assumption and explain testing objectives.",
        "The conclusion discusses improvements that could follow verification."
    ]
    plan={"pages":[{"page_index":0,"blocks":[
        {"block_id":orig[i+1]["block_id"],"text":replacements[i],"body":True}
        for i in range(4)]}]}
    out,warnings=render_replacement_pdf(src,template,plan)
    with fitz.open(stream=out,filetype="pdf") as result:
        assert len(result)==1
        lines=[s for b in result[0].get_text("dict")["blocks"] if b["type"]==0
               for line in b["lines"] for s in line["spans"] if line["bbox"][1]>145]
        assert lines
        assert all(abs(s["size"]-12)<.1 for s in lines)
    assert verify_pdf_layout(src,out,template,plan)["status"]=="passed"


def test_overlong_title_raises_instead_of_distorting_font():
    src=make_reference()
    doc,template=inspect_pdf(src)
    doc.close()
    plan={"pages":[{"page_index":0,"blocks":[
        {"block_id":0,"text":"EXTREMELY LONG UNSUPPORTED PROJECT TITLE " * 9}]}]}
    with pytest.raises(ValueError,match="shorten"):
        render_replacement_pdf(src,template,plan)


def test_preserve_reference_citations_without_fake_substitutions():
    doc=fitz.open()
    p=doc.new_page()
    p.insert_text((70,75),"References",fontsize=16)
    p.insert_text((70,140),"1. Verified reference to Old Topic for academic review.",fontsize=12)
    source=doc.tobytes();doc.close()
    parsed,template=inspect_pdf(source)
    parsed.close()
    original={"source_title":"Old Topic","source_name":"","source_usn":""}
    edits=static_replacements(template,original,"New Different Topic","Student","USN",
                               preserve_references=True)
    assert not edits, "Source citations require review, not fabricated edits"
