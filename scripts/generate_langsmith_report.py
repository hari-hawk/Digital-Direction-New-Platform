#!/usr/bin/env python3
"""Generate a PDF report on LangSmith integration for Digital Direction pipeline."""

from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from datetime import datetime

# Create PDF
pdf_path = "/Users/harivershan/Library/CloudStorage/GoogleDrive-hari.sr@techjays.com/My Drive/Digital Direction Latest Approach/Platform/LangSmith_Integration_Report.pdf"
doc = SimpleDocTemplate(pdf_path, pagesize=letter, rightMargin=0.75*inch, leftMargin=0.75*inch,
                        topMargin=1*inch, bottomMargin=0.75*inch)

story = []
styles = getSampleStyleSheet()

# Custom styles
title_style = ParagraphStyle(
    'CustomTitle',
    parent=styles['Heading1'],
    fontSize=24,
    textColor=colors.HexColor('#1f4788'),
    spaceAfter=6,
    alignment=TA_CENTER,
    fontName='Helvetica-Bold'
)

heading_style = ParagraphStyle(
    'CustomHeading',
    parent=styles['Heading2'],
    fontSize=14,
    textColor=colors.HexColor('#2d5fa3'),
    spaceAfter=8,
    spaceBefore=12,
    fontName='Helvetica-Bold'
)

subheading_style = ParagraphStyle(
    'SubHeading',
    parent=styles['Heading3'],
    fontSize=11,
    textColor=colors.HexColor('#4a7ba7'),
    spaceAfter=6,
    fontName='Helvetica-Bold'
)

body_style = ParagraphStyle(
    'CustomBody',
    parent=styles['BodyText'],
    fontSize=10,
    alignment=TA_JUSTIFY,
    spaceAfter=8,
)

# Title
story.append(Paragraph("LangSmith Integration for Digital Direction", title_style))
story.append(Paragraph("Telecom Document Extraction Pipeline", styles['Heading3']))
story.append(Spacer(1, 0.2*inch))

# Date & Version
story.append(Paragraph(f"<b>Date:</b> {datetime.now().strftime('%B %d, %Y')}", body_style))
story.append(Paragraph("<b>Status:</b> Recommended Phase 7 Integration", body_style))
story.append(Spacer(1, 0.3*inch))

# Executive Summary
story.append(Paragraph("Executive Summary", heading_style))
story.append(Paragraph(
    "This report outlines the business and technical case for integrating LangSmith into the Digital Direction extraction "
    "pipeline. LangSmith provides unified observability for LLM calls (Gemini extraction, Claude evaluation), enabling faster "
    "debugging, accurate cost attribution, and the foundation for Phase 7's self-healing feedback loop.",
    body_style
))
story.append(Spacer(1, 0.2*inch))

# Current Pain Points
story.append(Paragraph("1. Current Workflow Pain Points", heading_style))

pain_points = [
    ("Debugging a Bad Extraction", "15–20 minutes per issue", 
     "Must manually locate document, reconstruct LLM prompt, re-run extraction, compare outputs, and guess the fix."),
    
    ("Finding Failure Patterns", "Not visible", 
     "Eval scores live in separate JSON files. Difficult to correlate field-level accuracy drops to extraction traces."),
    
    ("Cost Attribution", "Total only ($23.45)", 
     "No breakdown by carrier, document type, or model. Cannot optimize spend where not measured."),
    
    ("Eval ↔ Extraction Linking", "Two separate systems", 
     "No way to ask: 'Which extraction calls scored poorly?' Manual integration required for Phase 7 feedback loop."),
    
    ("Human Review Context", "Incomplete", 
     "Analysts see 'USOC = null, confidence = LOW' but cannot access LLM reasoning or prompt without manual effort."),
]

for title, current, detail in pain_points:
    story.append(Paragraph(f"<b>{title}</b>", subheading_style))
    story.append(Paragraph(f"<b>Current:</b> {current}", body_style))
    story.append(Paragraph(f"<b>Detail:</b> {detail}", body_style))
    story.append(Spacer(1, 0.1*inch))

story.append(Spacer(1, 0.2*inch))

# What LangSmith Solves
story.append(Paragraph("2. What LangSmith Solves", heading_style))

solutions = [
    ("Fast Debugging", "1–2 minutes per issue",
     "Open dashboard, search traces, click to see exact prompt + response + eval scores. Pinpoint root cause instantly."),
    
    ("Automatic Pattern Detection", "Real-time alerts",
     "Dashboard shows accuracy trends. Alert: 'USOC accuracy dropped from 92% to 72% on Windstream CSRs (last 24h)'."),
    
    ("Detailed Cost Breakdown", "By carrier, model, call type",
     "See 'AT&T: $12.30 (183 docs) | Windstream: $7.50 (42 docs) | Claude: $2.10 | Eval: $1.55'. Optimize high-cost carriers."),
    
    ("Eval ↔ Extraction Linking", "Automatic correlation",
     "Traces linked to eval scores. Filter: 'Show extraction calls where eval scored <70%'. Foundation for Phase 7."),
    
    ("Team Collaboration", "Shared, annotated traces",
     "Analysts and engineers review traces together. Notes and corrections visible to entire team."),
]

for title, outcome, detail in solutions:
    story.append(Paragraph(f"<b>{title}</b>", subheading_style))
    story.append(Paragraph(f"<b>Outcome:</b> {outcome}", body_style))
    story.append(Paragraph(f"<b>How:</b> {detail}", body_style))
    story.append(Spacer(1, 0.1*inch))

story.append(Spacer(1, 0.2*inch))

# Comparison Table
story.append(Paragraph("3. LangFuse vs LangSmith Comparison", heading_style))

comparison_data = [
    ["Aspect", "LangFuse", "LangSmith"],
    ["Hosting", "Self-hosted (Docker)", "Cloud (LangChain Inc.)"],
    ["Cost", "Free (open-source)", "Free tier + $10–500/mo"],
    ["Setup", "Medium (need Docker)", "Low (API key only)"],
    ["Data Privacy", "Your infrastructure", "Cloud (encrypted)"],
    ["Dashboard Quality", "Good", "Excellent"],
    ["Cost Breakdown", "Basic", "Advanced (by carrier/model)"],
    ["Regression Alerts", "Manual detection", "Automatic alerts"],
    ["Prompt Versioning", "Manual (git)", "Built-in A/B testing"],
    ["Self-Healing API", "DIY", "Native SDK support"],
]

t = Table(comparison_data, colWidths=[1.8*inch, 1.8*inch, 1.8*inch])
t.setStyle(TableStyle([
    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2d5fa3')),
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ('FONTSIZE', (0, 0), (-1, 0), 10),
    ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
    ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
    ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ('FONTSIZE', (0, 1), (-1, -1), 9),
    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f5f5f5')]),
]))
story.append(t)
story.append(Spacer(1, 0.2*inch))

# Recommendation
story.append(Paragraph("4. Recommendation", heading_style))
story.append(Paragraph(
    "<b>Use LangSmith</b> for the following reasons:",
    body_style
))

reasons = [
    "Production-grade observability (not DIY) — faster debugging, fewer wasted hours",
    "Enables Phase 7 self-healing feedback loop — automatic config tuning based on eval patterns",
    "Cost-effective for POC scale — free tier covers 1–5K traces/month; POC should fit comfortably",
    "Faster time-to-value — 30 min setup saves 10+ hours of debugging over next month",
    "Team-ready — stakeholders can review traces without CLI access or log files",
]

for reason in reasons:
    story.append(Paragraph(f"• {reason}", body_style))

story.append(Spacer(1, 0.2*inch))

# Phase Roadmap
story.append(Paragraph("5. Implementation Roadmap", heading_style))

story.append(Paragraph("<b>Phase 6 (Current):</b> Eval Framework", subheading_style))
story.append(Paragraph(
    "Integrate LangSmith tracing into existing LLM calls (Gemini extraction, Claude evaluation). "
    "Enable field-level accuracy tracking and cost attribution by carrier.",
    body_style
))
story.append(Spacer(1, 0.1*inch))

story.append(Paragraph("<b>Phase 7 (Self-Healing):</b> Feedback Loop", subheading_style))
story.append(Paragraph(
    "Build automated feedback loop: Extract → Eval → Detect Pattern → Auto-Tune Config → Re-Extract. "
    "Use LangSmith traces + eval scores to identify failures and trigger config updates.",
    body_style
))
story.append(Spacer(1, 0.2*inch))

# Key Metrics
story.append(Paragraph("6. Expected Impact (Metrics)", heading_style))

metrics_data = [
    ["Metric", "Without LangSmith", "With LangSmith", "Impact"],
    ["Debug Time", "15–20 min/issue", "1–2 min/issue", "~15x faster"],
    ["Pattern Detection", "Manual", "Automated alerts", "Real-time visibility"],
    ["Cost Attribution", "Total only", "By carrier/model", "Informed optimization"],
    ["Phase 7 Feasibility", "Very hard (DIY)", "Viable (SDK)", "Enables self-healing"],
]

t2 = Table(metrics_data, colWidths=[1.3*inch, 1.4*inch, 1.4*inch, 1.4*inch])
t2.setStyle(TableStyle([
    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2d5fa3')),
    ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ('FONTSIZE', (0, 0), (-1, 0), 9),
    ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
    ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
    ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ('FONTSIZE', (0, 1), (-1, -1), 8),
    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f5f5f5')]),
]))
story.append(t2)
story.append(Spacer(1, 0.2*inch))

# Next Steps
story.append(Paragraph("7. Next Steps", heading_style))

steps = [
    "Create LangSmith account (free tier) at https://smith.langchain.com",
    "Generate API key for authentication",
    "Integrate LangSmith SDK into backend/services/llm.py",
    "Hook Claude eval calls to LangSmith tracing",
    "Deploy and test end-to-end with sample documents",
    "Configure carrier-specific cost tracking in dashboard",
    "Document self-healing feedback loop patterns for Phase 7",
]

for i, step in enumerate(steps, 1):
    story.append(Paragraph(f"{i}. {step}", body_style))

story.append(Spacer(1, 0.3*inch))

# Footer
story.append(Paragraph("—", styles['Normal']))
story.append(Paragraph(
    "For questions about this integration, contact the Digital Direction team.",
    ParagraphStyle('Footer', parent=styles['Normal'], fontSize=9, textColor=colors.grey)
))

# Build PDF
doc.build(story)
print(f"✅ PDF generated: {pdf_path}")
print(f"📊 Report is ready to share with stakeholders.")
