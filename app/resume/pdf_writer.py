from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.resume.generator import ResumeContent

# One-page compression ladder (CLAUDE.md one-click-agent section 8, step 5
# "reduce nonessential spacing slightly"). Bounded: compression_level is
# clamped to [0, _MAX_LEVEL], and font size never drops below
# app.config.ONE_PAGE_MIN_FONT_SIZE regardless of level -- never shrink font
# until unreadable. compression_level=0 (the default) renders byte-for-byte
# identical to the pre-one-page-contract layout.
_MAX_LEVEL = 6
_BASE_FONT_SIZE = 10.0
_BASE_MARGIN_TOP_BOTTOM = 0.6
_BASE_MARGIN_LEFT_RIGHT = 0.7
_MIN_MARGIN = 0.4


def _clamp(level: int) -> int:
    return max(0, min(level, _MAX_LEVEL))


def write_pdf(resume: ResumeContent, out_path: Path, compression_level: int = 0) -> Path:
    level = _clamp(compression_level)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    from app import config

    min_font = config.ONE_PAGE_MIN_FONT_SIZE
    font_size = max(min_font, _BASE_FONT_SIZE - 0.3 * level)
    margin_tb = max(_MIN_MARGIN, _BASE_MARGIN_TOP_BOTTOM - 0.03 * level) * inch
    margin_lr = max(_MIN_MARGIN, _BASE_MARGIN_LEFT_RIGHT - 0.03 * level) * inch
    space_scale = max(0.4, 1.0 - 0.1 * level)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        topMargin=margin_tb, bottomMargin=margin_tb,
        leftMargin=margin_lr, rightMargin=margin_lr,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=max(min_font + 4, 18 - level),
                         leading=max(min_font + 6, 22 - level), spaceAfter=round(6 * space_scale))
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=max(min_font + 1, 14 - 0.3 * level),
                         leading=max(min_font + 3, 16 - 0.3 * level),
                         spaceBefore=round(10 * space_scale), spaceAfter=round(4 * space_scale))
    normal = ParagraphStyle("NormalCompressed", parent=styles["Normal"], fontSize=font_size,
                             leading=font_size * 1.2)
    bullet = ParagraphStyle("Bullet", parent=normal, leftIndent=14, bulletIndent=4,
                             spaceAfter=max(0, round(2 * space_scale)))

    story = []
    story.append(Paragraph(resume.full_name or "NEEDS_USER_INPUT", h1))
    contact_line = " | ".join(
        filter(None, [resume.email, resume.phone, resume.location, resume.linkedin_url, resume.github_url, resume.portfolio_url])
    )
    story.append(Paragraph(contact_line, normal))
    story.append(Spacer(1, max(2, round(8 * space_scale))))

    story.append(Paragraph("Summary", h2))
    story.append(Paragraph(resume.summary, normal))

    story.append(Paragraph("Skills", h2))
    story.append(Paragraph(", ".join(resume.skills_ordered) if resume.skills_ordered else "NEEDS_USER_INPUT", normal))

    if resume.experience:
        story.append(Paragraph("Experience", h2))
        for e in resume.experience:
            story.append(Paragraph(f"<b>{e.title} — {e.company}</b>", normal))
            story.append(Paragraph(f"{e.start_date} - {e.end_date} | {e.location}", normal))
            for b in e.bullets:
                story.append(Paragraph(f"• {b}", bullet))

    if resume.projects:
        story.append(Paragraph("Projects", h2))
        for proj in resume.projects:
            story.append(Paragraph(f"<b>{proj.name}</b>", normal))
            if proj.url:
                story.append(Paragraph(proj.url, normal))
            story.append(Paragraph(proj.description, normal))
            for b in proj.bullets:
                story.append(Paragraph(f"• {b}", bullet))

    if resume.education:
        story.append(Paragraph("Education", h2))
        for ed in resume.education:
            story.append(Paragraph(f"{ed.degree} in {ed.field_of_study} — {ed.school} ({ed.graduation_date})", normal))

    doc.build(story)
    return out_path


def count_pdf_pages(path: Path) -> int:
    """PDF page count via pypdf -- the same library app.resume_optimizer.
    ats_parse already depends on for text extraction, so no new dependency."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return len(reader.pages)
