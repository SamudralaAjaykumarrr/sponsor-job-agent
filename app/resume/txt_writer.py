from pathlib import Path

from app.resume.generator import ResumeContent


def write_txt(resume: ResumeContent, out_path: Path) -> Path:
    lines = []
    lines.append(resume.full_name or "NEEDS_USER_INPUT")
    contact_line = " | ".join(
        filter(None, [resume.email, resume.phone, resume.location, resume.linkedin_url, resume.github_url, resume.portfolio_url])
    )
    lines.append(contact_line)
    lines.append("")
    lines.append("SUMMARY")
    lines.append(resume.summary)
    lines.append("")
    lines.append("SKILLS")
    lines.append(", ".join(resume.skills_ordered) if resume.skills_ordered else "NEEDS_USER_INPUT")

    if resume.experience:
        lines.append("")
        lines.append("EXPERIENCE")
        for e in resume.experience:
            lines.append(f"{e.title} — {e.company}")
            lines.append(f"{e.start_date} - {e.end_date} | {e.location}")
            for b in e.bullets:
                lines.append(f"- {b}")
            lines.append("")

    if resume.projects:
        lines.append("PROJECTS")
        for proj in resume.projects:
            lines.append(proj.name + (f" ({proj.url})" if proj.url else ""))
            lines.append(proj.description)
            for b in proj.bullets:
                lines.append(f"- {b}")
            lines.append("")

    if resume.education:
        lines.append("EDUCATION")
        for ed in resume.education:
            lines.append(f"{ed.degree} in {ed.field_of_study} — {ed.school} ({ed.graduation_date})")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    return out_path
