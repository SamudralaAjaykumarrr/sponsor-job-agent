from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.resume.generator import ResumeContent


def write_pdf(resume: ResumeContent, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], spaceAfter=6)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], spaceBefore=10, spaceAfter=4)
    normal = styles["Normal"]
    bullet = ParagraphStyle("Bullet", parent=styles["Normal"], leftIndent=14, bulletIndent=4, spaceAfter=2)

    story = []
    story.append(Paragraph(resume.full_name or "NEEDS_USER_INPUT", h1))
    contact_line = " | ".join(
        filter(None, [resume.email, resume.phone, resume.location, resume.linkedin_url, resume.github_url, resume.portfolio_url])
    )
    story.append(Paragraph(contact_line, normal))
    story.append(Spacer(1, 8))

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
