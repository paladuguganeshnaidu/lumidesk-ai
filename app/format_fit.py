"""Fit original project prose into the reference PDF's existing line geometry.

This module never changes page sizes, fonts or baselines; it redistributes the
AI's original prose across the same line boxes using real font advance widths.
"""
from __future__ import annotations
import re
from functools import lru_cache


def _paragraph_groups(lines: list[dict]) -> list[list[dict]]:
    if not lines:
        return []
    widths = sorted((a['block']['rect'][2] - a['block']['rect'][0] for a in lines), reverse=True)
    column = widths[min(2, len(widths)-1)] if len(widths) >= 3 else widths[0]
    groups, working = [], []
    for row in lines:
        if working:
            previous = working[-1]['block']
            cur = row['block']
            prev_width = previous['rect'][2] - previous['rect'][0]
            gap = cur['rect'][1] - previous['rect'][1]
            prior = previous['text'].rstrip()
            end_para = (prev_width < column * .76
                        or (prev_width < column * .96 and prior.endswith(('.', '!', '?', ':')))
                        or gap > max(32, float(previous['font_size']) * 2.65)
                        or bool(re.match(r'^\s*\d+[.)]\s', cur['text'])))
            if end_para:
                groups.append(working)
                working = []
        working.append(row)
    if working:
        groups.append(working)
    return groups


def _plan_group(group: list[dict], font) -> list[str] | None:
    """Dynamic-programming word reflow with original line count and width."""
    if len(group) == 1:
        return [group[0]['replacement']['text']]
    words = ' '.join(row['replacement']['text'] for row in group).split()
    if not words or len(words) < len(group):
        return None
    n = len(words)
    origchars = [len(row['block']['text'].strip()) for row in group]
    targets = [row['block']['rect'][2] - row['block']['rect'][0] for row in group]
    sizes = [float(row['block']['font_size']) for row in group]
    orig_word_counts = [max(1, len(row['block']['text'].split())) for row in group]
    # Cache the source font glyph widths. This keeps 70-page wrap searches fast.
    widths_by_size={}
    space_by_size={}
    for size in set(sizes):
        prefix=[0.0]
        for word in words:
            prefix.append(prefix[-1]+font.text_length(word,fontsize=size))
        widths_by_size[size]=prefix
        space_by_size[size]=font.text_length(' ',fontsize=size)

    @lru_cache(None)
    def best(i: int, pos: int):
        if i == len(group):
            return (0.0, ()) if pos == n else (float('inf'), ())
        remaining = len(group) - i
        if n-pos < remaining:
            return float('inf'), ()
        hint = orig_word_counts[i]
        hi = min(n-pos-(remaining-1), max(5, int(hint*1.7)+6))
        lo = max(1, int(hint*.45)-3)
        if i == len(group)-1:
            lo = hi = n-pos
        result = (float('inf'), ())
        for k in range(lo,hi+1):
            phrase = ' '.join(words[pos:pos+k])
            glyphs=widths_by_size[sizes[i]][pos+k]-widths_by_size[sizes[i]][pos]
            usual=space_by_size[sizes[i]]
            natural=glyphs+usual*(k-1)
            target = targets[i]
            full = i < len(group)-1 and target > .9*max(targets)
            if full:
                spaces=k-1
                spacing=(target-glyphs)/max(1,spaces)
                if spaces<2 or spacing < .65*usual or spacing > 3.0*usual:
                    continue
                width_penalty=abs(spacing/usual-1)*.5
            else:
                if natural > target+4:
                    continue
                width_penalty=abs(natural-target)/max(10.0,target)*2.2
            chars_penalty=abs(len(phrase)-origchars[i])/max(10,origchars[i])*1.9
            next_cost,next_lines=best(i+1,pos+k)
            score=next_cost+width_penalty+chars_penalty
            if score<result[0]:
                result=(score,(phrase,*next_lines))
        return result

    _, phrases=best(0,0)
    return list(phrases) if phrases else None


def fit_body_text(page_info: dict, replacements: dict, fonts: dict) -> list[str]:
    """Mutate only marked body text and keep its original paragraph line count."""
    blocks={row['block_id']:row for row in page_info['blocks']}
    lines=[dict(block=blocks[i],replacement=item) for i,item in replacements.items()
           if i in blocks and item.get('body') and item.get('text')]
    lines.sort(key=lambda row: (row['block']['rect'][1],row['block']['rect'][0]))
    warnings=[]
    for group in _paragraph_groups(lines):
        font=fonts.get(group[0]['block']['block_id'])
        if font is None:
            warnings.append('Source typeface unavailable for original-width wrapping.')
            continue
        fitted=_plan_group(group,font)
        if fitted is None:
            warnings.append('Paragraph could not match its original line lengths without clipping.')
            continue
        widest=max(x['block']['rect'][2]-x['block']['rect'][0] for x in group)
        for row,phrase in zip(group,fitted):
            item=row['replacement']
            item['text']=phrase
            if 'plan_item' in item:
                item['plan_item']['text']=phrase
            source=row['block']
            width=source['rect'][2]-source['rect'][0]
            item['justify']=(len(group)>1 and row is not group[-1] and
                             width>.9*widest and len(phrase.split())>=3)
            if 'plan_item' in item:
                item['plan_item']['justify']=item['justify']
    return warnings
