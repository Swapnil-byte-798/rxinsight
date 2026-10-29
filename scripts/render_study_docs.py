"""Render the maintained Markdown study notes into readable, linked PDFs."""
from __future__ import annotations
import hashlib
import os
import html
import json
import re
import textwrap
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, LongTable,
                               Table, TableStyle, Preformatted, KeepTogether)
from pypdf import PdfReader
import pdfplumber

ROOT = Path(__file__).resolve().parents[1] / 'docs'
CONFIG = json.loads((ROOT / 'study-material.json').read_text())
OUT = ROOT
OUT.mkdir(parents=True, exist_ok=True)
FONT_ROOT = Path(os.environ.get('STUDY_FONT_DIR','/usr/share/fonts/truetype/dejavu'))
for name, file in [('Study','DejaVuSans.ttf'),('StudyBold','DejaVuSans-Bold.ttf'),
                   ('StudyMono','DejaVuSansMono.ttf')]:
    pdfmetrics.registerFont(TTFont(name, str(FONT_ROOT / file)))
pdfmetrics.registerFontFamily('Study',normal='Study',bold='StudyBold',italic='Study',boldItalic='StudyBold')
NAVY=colors.HexColor('#173247');TEAL=colors.HexColor('#167d8d')
INK=colors.HexColor('#263746');MUTED=colors.HexColor('#526779')
PALE=colors.HexColor('#edf5f7');BORDER=colors.HexColor('#d5e1e7')
WIDTH,HEIGHT=A4;LEFT=52;RIGHT=52;BODY=WIDTH-LEFT-RIGHT
styles={
 'body':ParagraphStyle('body',fontName='Study',fontSize=9.7,leading=13.8,textColor=INK,spaceAfter=6,splitLongWords=True),
 'h1':ParagraphStyle('h1',fontName='StudyBold',fontSize=24,leading=29,textColor=NAVY,spaceBefore=5,spaceAfter=14,keepWithNext=True),
 'h2':ParagraphStyle('h2',fontName='StudyBold',fontSize=14,leading=18,textColor=NAVY,spaceBefore=12,spaceAfter=6,keepWithNext=True),
 'h3':ParagraphStyle('h3',fontName='StudyBold',fontSize=11,leading=15,textColor=TEAL,spaceBefore=8,spaceAfter=4,keepWithNext=True),
 'quote':ParagraphStyle('quote',fontName='Study',fontSize=9.5,leading=14,textColor=NAVY),
 'cell':ParagraphStyle('cell',fontName='Study',fontSize=8.5,leading=11.5,textColor=INK,splitLongWords=True),
 'th':ParagraphStyle('th',fontName='StudyBold',fontSize=8.5,leading=11.5,textColor=colors.white,splitLongWords=True),
 'code':ParagraphStyle('code',fontName='StudyMono',fontSize=7.9,leading=11.3,backColor=PALE,borderColor=BORDER,borderWidth=.5,borderPadding=9,spaceBefore=5,spaceAfter=12),
 'bullet':ParagraphStyle('bullet',fontName='Study',fontSize=9.7,leading=14.2,textColor=INK,leftIndent=11,bulletIndent=0,spaceAfter=6),
}

def normalise(s):
    return s.translate(str.maketrans({'\u2013':'-','\u2014':'-','\u2011':'-',
                                     '\u2019':"'",'\u2018':"'",'\u201c':'"','\u201d':'"'}))

def inline(text,base):
    """Preserve safe links/code/emphasis; resolve Markdown links for PDF use."""
    kept=[]
    def token(value):
        key=f'@@MARKUP{len(kept)}@@';kept.append(value);return key
    def link(match):
        label,url=match.groups()
        if not re.match(r'https?://',url):url=base+url
        return token(f'<link href="{html.escape(url,quote=True)}" color="#167d8d"><u>{html.escape(label)}</u></link>')
    text=re.sub(r'\[([^\]]+)\]\(([^)]+)\)',link,normalise(text))
    text=re.sub(r'`([^`]+)`',lambda m:token('<font name="StudyMono" size="8.4">'+html.escape(m[1])+'</font>'),text)
    text=html.escape(text)
    text=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',text)
    for i,value in enumerate(kept):text=text.replace(f'@@MARKUP{i}@@',value)
    return text

def para(text,style,base):return Paragraph(inline(text,base),styles[style])

def widths(rows):
    n=len(rows[0]);scores=[]
    for col in range(n):
        longest=max(len(re.sub(r'[`*]','',r[col])) for r in rows)
        scores.append(max(12,min(longest,38)))
    total=sum(scores)
    return [BODY*score/total for score in scores]

def flowables(markdown,base):
    lines=markdown.splitlines();story=[];i=0
    def boundary(line):return not line.strip() or line.startswith(('#','```','|','> ','- '))
    while i<len(lines):
        line=lines[i].strip()
        if not line:i+=1;continue
        if line.startswith('```'):
            i+=1;code=[]
            while i<len(lines) and not lines[i].startswith('```'):
                raw=normalise(lines[i]);max_chars=int((BODY-22)/pdfmetrics.stringWidth('M','StudyMono',7.9))
                code.extend(textwrap.wrap(raw,width=max_chars,replace_whitespace=False,drop_whitespace=False) or [''])
                i+=1
            story.append(Preformatted('\n'.join(code),styles['code']));i+=1;continue
        if line in {'## Sources', '## References'}:
            links=[];i+=1
            while i<len(lines):
                candidate=lines[i].strip()
                if candidate.startswith('- '):links.append(candidate[2:])
                elif candidate:links.append(candidate)
                i+=1
            story.extend([Spacer(1,6),para('Sources: '+' | '.join(links),'cell',base)])
            continue
        if line.startswith('|'):
            rows=[]
            while i<len(lines) and lines[i].strip().startswith('|'):
                cells=[x.strip() for x in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?',x.replace(' ','')) for x in cells):rows.append(cells)
                i+=1
            assert rows and all(len(r)==len(rows[0]) for r in rows)
            rendered=[[para(c,'th' if j==0 else 'cell',base) for c in r] for j,r in enumerate(rows)]
            table=LongTable(rendered,colWidths=widths(rows),repeatRows=1,hAlign='LEFT')
            table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),NAVY),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,PALE]),
                ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),
                ('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),7),
                ('BOTTOMPADDING',(0,0),(-1,-1),7),('LINEBELOW',(0,0),(-1,0),.7,TEAL),
                ('LINEBELOW',(0,1),(-1,-1),.3,BORDER)]))
            story.extend([table,Spacer(1,10)]);continue
        if line.startswith('#'):
            level=len(line)-len(line.lstrip('#'));story.append(para(line[level:].strip(),'h'+str(min(level,3)),base));i+=1;continue
        if line.startswith('> '):
            quote=[]
            while i<len(lines) and lines[i].startswith('> '):quote.append(lines[i][2:]);i+=1
            box=Table([['',para(' '.join(quote),'quote',base)]],colWidths=[4,BODY-4])
            box.setStyle(TableStyle([('BACKGROUND',(0,0),(0,0),TEAL),('BACKGROUND',(1,0),(1,0),PALE),('LEFTPADDING',(1,0),(1,0),11),('RIGHTPADDING',(1,0),(1,0),11),('TOPPADDING',(1,0),(1,0),9),('BOTTOMPADDING',(1,0),(1,0),9)]))
            story.extend([box,Spacer(1,11)]);continue
        if line.startswith('- '):
            story.append(Paragraph(inline(line[2:],base),styles['bullet'],bulletText='-'));i+=1;continue
        paragraph=[line];i+=1
        while i<len(lines) and not boundary(lines[i]):paragraph.append(lines[i].strip());i+=1
        story.append(para(' '.join(paragraph),'body',base))
    return story

class NumberedCanvas(Canvas):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.saved=[]
    def showPage(self):
        self.saved.append(dict(self.__dict__));self._startPage()
    def save(self):
        total=len(self.saved)
        for state in self.saved:
            self.__dict__.update(state)
            self.setStrokeColor(BORDER);self.line(LEFT,36,WIDTH-RIGHT,36)
            self.setFont('Study',8);self.setFillColor(MUTED)
            self.drawString(LEFT,23,'Study notes | Updated 29 October 2026')
            self.drawRightString(WIDTH-RIGHT,23,f'{self._pageNumber} / {total}')
            super().showPage()
        super().save()

def render(project,stem,name,kind):
    src=ROOT/(stem+'.md');target=OUT/name
    title=src.read_text().splitlines()[0][2:]
    base='https://github.com/'+CONFIG['repository']+'/blob/main/docs/'
    def header(c,doc):
        c.setFillColor(TEAL);c.rect(0,HEIGHT-9,WIDTH,9,fill=1,stroke=0)
        c.setFillColor(MUTED);c.setFont('Study',8)
        c.drawString(LEFT,HEIGHT-29,CONFIG['name'])
        c.drawRightString(WIDTH-RIGHT,HEIGHT-29,kind)
    doc=SimpleDocTemplate(str(target),pagesize=A4,rightMargin=RIGHT,leftMargin=LEFT,
                         topMargin=49,bottomMargin=51,title=title,author='Swapnil Dwivedi',
                         subject='Project study material aligned with execution audit')
    doc.build(flowables(src.read_text(),base),onFirstPage=header,onLaterPages=header,canvasmaker=NumberedCanvas)
    reader=PdfReader(target)
    assert len(reader.pages)>1
    alltext='\n'.join(p.extract_text() or '' for p in reader.pages)
    assert '\ufffd' not in alltext and '\u25a0' not in alltext
    issues=[]
    with pdfplumber.open(target) as pdf:
        for index,page in enumerate(pdf.pages,1):
            for word in page.extract_words():
                if word['x0']<LEFT-2 or word['x1']>WIDTH-RIGHT+2:
                    issues.append({'page':index,'word':word['text'],'x0':word['x0'],'x1':word['x1']})
    if issues:raise ValueError(f'Horizontal text overflow in {name}: {issues[:6]}')
    return {'project':project,'source':src.name,'pdf':target.name,'pages':len(reader.pages),
            'words':len(src.read_text().split()),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}

items=[(CONFIG['slug'],'how-it-works',CONFIG['prefix']+'-How-It-Works.pdf','Architecture and workflow'),
       (CONFIG['slug'],'interview-questions',CONFIG['prefix']+'-Interview-Questions.pdf','Interview questions'),
       (CONFIG['slug'],'tools-primer',CONFIG['prefix']+'-Tools-Primer.pdf','Tools and concepts')]
manifest=[render(*item) for item in items]
(ROOT/'pdf-manifest.json').write_text(json.dumps(manifest,indent=2))
for item in manifest:print(Path(item['pdf']).name,item['pages'],'pages;',item['words'],'source words')
