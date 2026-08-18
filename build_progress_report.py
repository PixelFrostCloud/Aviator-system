import base64
import html
import os
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "reports"
ASSET_DIR = OUT_DIR / "assets"

SCREENSHOTS = [
    ("01", "Live Aviator Play Screen", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 000845.png",
     "Main play interface showing the multiplier flight, live betting table, wallet balance, dual bet controls, recent multipliers, and live chat."),
    ("02", "Expanded Game Flight View", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 000900.png",
     "Wider play view focused on the multiplier graph and betting controls, demonstrating responsive game layout and real-time flight animation."),
    ("03", "Account Menu And Settings", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 000912.png",
     "User account drawer with avatar control, sound/music/animation toggles, free bets, bet history, limits, rules, and provably fair settings."),
    ("04", "Free Bets Management", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 000926.png",
     "Free bets modal showing the active free bet area, archive access, and cash/free-bet play mode selection."),
    ("05", "Wallet Overview", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 000939.png",
     "Wallet dashboard with user identity, verification tier, balance cards, XP progress, deposit limit, KYC status, and recent activity."),
    ("06", "Deposit And Withdrawal Forms", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 000953.png",
     "Wallet action area for deposits and withdrawals using payment providers such as MTN Mobile Money, plus VIP and preference summaries."),
    ("07", "Transaction History", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001008.png",
     "Transaction ledger listing deposits, withdrawals, bet debits, win credits, status, date, amount, and total record count."),
    ("08", "Profile Overview", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001028.png",
     "Profile dashboard with player ID, VIP level, verification status, betting totals, winnings, biggest win, and highest multiplier."),
    ("09", "Profile Wallet And Recent Activity", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001040.png",
     "Profile continuation showing balance breakdown, daily limits, deposit/withdraw actions, and recent account activity."),
    ("10", "Statistics And Compliance Controls", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001053.png",
     "Statistics cards plus compliance settings, KYC submission fields, and self-exclusion protection controls."),
    ("11", "Top Dashboard", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001125.png",
     "Top area summarizing balance, recent bets, open rooms, readiness state, and XP progress."),
    ("12", "Recent Sessions And Open Rooms", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001138.png",
     "Session history with won/lost outcomes and room availability panel for multiplayer game flow."),
    ("13", "Chat Rain Reward Panel", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001207.png",
     "Chat rain interaction where a user can distribute wallet balance across a number of players with calculated per-player amount."),
    ("14", "Emoji Picker", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001220.png",
     "Chat input enhancements including emoji search and picker, message input, reaction affordance, and send action."),
    ("15", "Active Betting And Cash Out", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001304.png",
     "Active round with provably fair hash display and cash-out buttons for two simultaneous bets."),
    ("16", "High Multiplier Cash Out State", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001314.png",
     "Later round state showing a higher multiplier and updated cash-out value while the round is still flying."),
    ("17", "How To Play Modal", "C:/Users/Aureon/Pictures/Screenshots/Screenshot 2026-07-10 001352.png",
     "Instruction modal explaining the basic three-step user flow: place bet, watch multiplier, cash out before crash."),
]

FEATURES = [
    ("Authentication", "Implemented", "Register, login, logout, account profile, and automatic redirects into gameplay."),
    ("Aviator game loop", "Implemented", "Two bet panels, animated multiplier, crash/cash-out settlement, recent multipliers, and live table."),
    ("Provably fair rounds", "Implemented", "Server seed hash, client seed, crash point, reveal flow, and audit logging for round verification."),
    ("Wallet", "Implemented", "UGX balance, deposits, withdrawals, payment requests, transaction records, and balance updates."),
    ("Payments", "Implemented", "MTN Mobile Money, Airtel Money, card, bank transfer, and Pesapal provider options in form logic."),
    ("Transactions", "Implemented", "Bet debit, win credit, deposit, withdrawal, refund, and bonus transaction types with status tracking."),
    ("Profile and statistics", "Implemented", "Player ID, VIP level display, XP progress, win rate, games played, winnings, and activity panels."),
    ("Compliance", "Implemented", "Phone verification, KYC submission, deposit/loss limits, risk alerts, and self-exclusion controls."),
    ("Free bets and rewards", "Implemented", "Free-bet management UI, reward events, reward wallet, claims, reward transactions, and chat rain."),
    ("Chat", "Implemented", "Live chat UI, emoji picker, reactions, flagged-message checks, and online player display."),
    ("Multiplayer rooms", "Implemented", "Room creation, room joining, room status, host start flow, participants, and room cash-out logic."),
    ("Admin analytics", "Implemented", "Staff analytics route, financial report generation, payment review data, KYC/risk counts, and admin model controls."),
    ("Audit logging", "Implemented", "System activity logging for registration, deposits, withdrawals, game starts, cash-outs, rooms, rewards, and admin actions."),
    ("Responsive UI", "Implemented", "Dark themed dashboard layout with side navigation, modals, mobile chat panels, and compact game controls."),
]

PAGES = [
    ("Executive Summary", [
        "The Aviator System is now a working Django web application with core gameplay, wallet, profile, compliance, chat, reward, and admin features implemented.",
        "The screenshots show the application progressing from the live play screen through account management, financial operations, player statistics, social chat, rewards, and user guidance.",
        "The current build demonstrates both the player-facing experience and the supporting backend workflows needed for deposits, withdrawals, game verification, responsible gaming, and reporting.",
    ]),
    ("Technical Foundation", [
        "Backend: Django application with models for Profile, GameRound, GameSession, Transaction, PaymentRequest, KYCSubmission, SelfExclusion, RiskAlert, RewardEvent, RewardWallet, ChatRain, Room, and AuditLog.",
        "Frontend: HTML templates, CSS, and JavaScript power the live game interface, drawers, modal views, wallet forms, chat tools, and responsive layouts.",
        "Data: SQLite is used locally during development, with Django migrations defining persistent tables for users, payments, gameplay, rewards, rooms, and audits.",
    ]),
]


def png_size(path):
    with open(path, "rb") as f:
        sig = f.read(24)
    if sig[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a PNG file: {path}")
    return struct.unpack(">II", sig[16:24])


def safe_name(path):
    return Path(path).name.replace(" ", "_")


def ensure_assets():
    OUT_DIR.mkdir(exist_ok=True)
    ASSET_DIR.mkdir(exist_ok=True)
    copied = []
    for idx, title, src, desc in SCREENSHOTS:
        src_path = Path(src)
        if not src_path.exists():
            raise FileNotFoundError(src)
        dest = ASSET_DIR / f"{idx}_{safe_name(src)}"
        shutil.copy2(src_path, dest)
        copied.append((idx, title, dest, desc))
    return copied


def run_xml(text, bold=False, color=None, size=22):
    props = [f'<w:sz w:val="{size}"/>', f'<w:szCs w:val="{size}"/>']
    if bold:
        props.append("<w:b/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    props.append('<w:rFonts w:ascii="Arial" w:hAnsi="Arial"/>')
    return f"<w:r><w:rPr>{''.join(props)}</w:rPr><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r>"


def para(text="", style=None, bold=False, color=None, size=22, align=None):
    ppr = []
    if style:
        ppr.append(f'<w:pStyle w:val="{style}"/>')
    if align:
        ppr.append(f'<w:jc w:val="{align}"/>')
    return f"<w:p><w:pPr>{''.join(ppr)}</w:pPr>{run_xml(text, bold=bold, color=color, size=size)}</w:p>"


def bullet(text):
    return (
        '<w:p><w:pPr><w:pStyle w:val="ListParagraph"/>'
        '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        f'{run_xml(text, size=21)}</w:p>'
    )


def page_break():
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def image_xml(rid, width_px, height_px, max_width_in=9.25, max_height_in=4.75):
    width_in = width_px / 144.0
    height_in = height_px / 144.0
    scale = min(max_width_in / width_in, max_height_in / height_in, 1.0)
    cx = int(width_in * scale * 914400)
    cy = int(height_in * scale * 914400)
    return f"""
<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:drawing>
<wp:inline distT="0" distB="0" distL="0" distR="0" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>
<wp:docPr id="{rid[3:]}" name="Screenshot {rid}"/><wp:cNvGraphicFramePr/>
<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
<pic:nvPicPr><pic:cNvPr id="0" name="screenshot.png"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip r:embed="{rid}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
</pic:pic></a:graphicData></a:graphic>
</wp:inline></w:drawing></w:r></w:p>
"""


def table_xml(rows):
    xml = ['<w:tbl><w:tblPr><w:tblW w:w="14040" w:type="dxa"/><w:tblBorders>'
           '<w:top w:val="single" w:sz="4" w:color="B8C2CC"/><w:left w:val="single" w:sz="4" w:color="B8C2CC"/>'
           '<w:bottom w:val="single" w:sz="4" w:color="B8C2CC"/><w:right w:val="single" w:sz="4" w:color="B8C2CC"/>'
           '<w:insideH w:val="single" w:sz="4" w:color="D6DCE2"/><w:insideV w:val="single" w:sz="4" w:color="D6DCE2"/>'
           '</w:tblBorders></w:tblPr><w:tblGrid><w:gridCol w:w="2800"/><w:gridCol w:w="2200"/><w:gridCol w:w="9040"/></w:tblGrid>']
    for i, row in enumerate(rows):
        xml.append("<w:tr>")
        for cell in row:
            shade = '<w:shd w:fill="EAF8EF"/>' if i == 0 else ""
            xml.append(f'<w:tc><w:tcPr><w:tcW w:w="4680" w:type="dxa"/>{shade}</w:tcPr>{para(str(cell), bold=(i == 0), size=20)}</w:tc>')
        xml.append("</w:tr>")
    xml.append("</w:tbl>")
    return "".join(xml)


def styles_xml():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:sz w:val="22"/></w:rPr><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:b/><w:color w:val="111827"/><w:sz w:val="42"/></w:rPr><w:pPr><w:spacing w:after="240"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:rPr><w:b/><w:color w:val="16A34A"/><w:sz w:val="32"/></w:rPr><w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:rPr><w:b/><w:color w:val="111827"/><w:sz w:val="26"/></w:rPr><w:pPr><w:spacing w:before="160" w:after="80"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:style>
</w:styles>"""


def numbering_xml():
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="&#8226;"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>"""


def build_docx(copied):
    rels = []
    body = []
    body.append(para("Aviator Game System", style="Title", align="center"))
    body.append(para("Project Progress Report", bold=True, color="16A34A", size=34, align="center"))
    body.append(para("Prepared from project screenshots and verified implementation files", size=22, align="center"))
    body.append(para("Date: 10 July 2026", size=20, align="center"))
    body.append(para("This editable report summarizes the implemented features, explains the submitted screenshots, and organizes the user journey page by page.", size=23))
    body.append(page_break())

    for title, points in PAGES:
        body.append(para(title, style="Heading1"))
        for point in points:
            body.append(bullet(point))
        body.append(page_break())

    body.append(para("Implemented Feature Coverage", style="Heading1"))
    rows = [("Feature Area", "Status", "Progress Explanation")] + FEATURES
    body.append(table_xml(rows))
    body.append(page_break())

    body.append(para("Screenshot Walkthrough In Page Order", style="Heading1"))
    body.append(para("The following pages arrange the screenshots in the order a reviewer would naturally encounter the system while testing the application.", size=22))
    body.append(page_break())

    for idx, title, path, desc in copied:
        rid = f"rId{100 + int(idx)}"
        rels.append((rid, f"media/{path.name}"))
        w, h = png_size(path)
        body.append(para(f"{idx}. {title}", style="Heading1"))
        body.append(para(desc, size=22))
        if title == "Live Aviator Play Screen":
            bullets = [
                "Shows the completed live game layout with betting list, recent crash multipliers, balance, chat, and dual bet panels.",
                "The screen proves the application supports real-time style interaction and clear separation between game, betting, and communication areas.",
            ]
        elif "Wallet" in title or "Deposit" in title:
            bullets = [
                "Demonstrates wallet visibility, UGX currency handling, payment forms, payment request records, and deposit/withdraw controls.",
                "The backend supports completed sandbox deposits and pending withdrawals for operator review.",
            ]
        elif "Profile" in title or "Compliance" in title:
            bullets = [
                "Shows account identity, VIP progress, performance statistics, responsible-gaming limits, KYC submission, and self-exclusion controls.",
                "These features improve trust, safety, and traceability for a betting-style application.",
            ]
        elif "Chat" in title or "Emoji" in title:
            bullets = [
                "Shows the social layer: live messages, reactions, emoji picker, online counter, and chat rain reward distribution.",
                "The backend includes chat moderation checks, reward event creation, and wallet-backed reward distribution.",
            ]
        elif "Cash Out" in title or "Betting" in title:
            bullets = [
                "Shows active bet settlement, cash-out value updates, and provably fair hash visibility during gameplay.",
                "The system records bet debits, win credits, crash/cash-out outcomes, and round audit details.",
            ]
        else:
            bullets = [
                "This screen completes an important part of the user flow and supports the overall usability of the project.",
                "It demonstrates that navigation and feature access have been implemented beyond the basic game screen.",
            ]
        for item in bullets:
            body.append(bullet(item))
        body.append(image_xml(rid, w, h))
        body.append(page_break())

    body.append(para("Conclusion And Next Steps", style="Heading1"))
    for point in [
        "The project has progressed from a basic Django scaffold into a multi-feature Aviator platform with gameplay, wallet, rewards, compliance, and reporting capabilities.",
        "The screenshots confirm that the interface is visually consistent, responsive, and organized around real player workflows.",
        "Recommended next steps are final QA testing, deployment configuration, real payment gateway integration, improved WebSocket production setup, and final security hardening before public release.",
    ]:
        body.append(bullet(point))

    sect = '<w:sectPr><w:pgSz w:w="15840" w:h="12240" w:orient="landscape"/><w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720" w:header="360" w:footer="360" w:gutter="0"/></w:sectPr>'
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<w:body>{''.join(body)}{sect}</w:body></w:document>"""

    docx_path = OUT_DIR / "Aviator_Project_Progress_Report.docx"
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>""")
        z.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>""")
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles_xml())
        z.writestr("word/numbering.xml", numbering_xml())
        rel_xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>',
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>']
        for rid, target in rels:
            rel_xml.append(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{target}"/>')
        rel_xml.append("</Relationships>")
        z.writestr("word/_rels/document.xml.rels", "".join(rel_xml))
        z.writestr("docProps/core.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>Aviator Game System Project Progress Report</dc:title><dc:creator>Codex</dc:creator><cp:lastModifiedBy>Codex</cp:lastModifiedBy></cp:coreProperties>""")
        z.writestr("docProps/app.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Codex</Application></Properties>""")
        for _, _, path, _ in copied:
            z.write(path, f"word/media/{path.name}")
    return docx_path


def img_data_uri(path):
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def build_html(copied):
    feature_rows = "\n".join(
        f"<tr><td>{html.escape(a)}</td><td><span>{html.escape(b)}</span></td><td>{html.escape(c)}</td></tr>"
        for a, b, c in FEATURES
    )
    screenshot_sections = []
    for idx, title, path, desc in copied:
        screenshot_sections.append(f"""
<section class="page">
  <div class="page-label">Screenshot {idx}</div>
  <h2>{html.escape(title)}</h2>
  <p class="lead">{html.escape(desc)}</p>
  <img class="shot" src="{img_data_uri(path)}" alt="{html.escape(title)}">
  <div class="note"><strong>Progress shown:</strong> This page confirms implemented user interface work, backend connection points, and a complete player-facing workflow for this feature area.</div>
</section>
""")
    content = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Aviator Project Progress Report</title>
<style>
@page {{ size: A4 landscape; margin: 12mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: Arial, sans-serif; color: #111827; background: #f5f7f8; }}
.page {{ page-break-after: always; min-height: 180mm; padding: 18mm; background: white; position: relative; }}
.cover {{ background: linear-gradient(135deg, #101414 0%, #17251d 58%, #f0a500 58%, #f0a500 100%); color: white; }}
.cover h1 {{ font-size: 42pt; margin: 28mm 0 4mm; letter-spacing: 0; }}
.cover h2 {{ font-size: 19pt; margin: 0 0 10mm; color: #22e05f; }}
.cover p {{ width: 58%; font-size: 14pt; line-height: 1.45; }}
.tag {{ display: inline-block; padding: 6px 12px; border: 1px solid rgba(255,255,255,.35); border-radius: 999px; color: #e5e7eb; }}
h2 {{ font-size: 26pt; margin: 0 0 4mm; color: #111827; }}
h3 {{ font-size: 17pt; margin: 8mm 0 3mm; color: #16a34a; }}
.lead {{ font-size: 13.5pt; color: #4b5563; line-height: 1.45; max-width: 92%; }}
ul {{ font-size: 12.5pt; line-height: 1.55; }}
table {{ width: 100%; border-collapse: collapse; font-size: 10.5pt; }}
th, td {{ border: 1px solid #d6dce2; padding: 8px 10px; vertical-align: top; }}
th {{ background: #eaf8ef; text-align: left; }}
td span {{ color: #16a34a; font-weight: bold; }}
.shot {{ display: block; max-width: 100%; max-height: 112mm; margin: 6mm auto 4mm; border: 1px solid #d1d5db; box-shadow: 0 10px 22px rgba(17,24,39,.18); }}
.note {{ border-left: 5px solid #16a34a; padding: 10px 14px; background: #f0fdf4; font-size: 11.5pt; }}
.page-label {{ position: absolute; right: 18mm; top: 12mm; color: #16a34a; font-weight: bold; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 7mm; }}
.card {{ border: 1px solid #d6dce2; border-radius: 8px; padding: 8mm; background: #fbfcfd; }}
</style></head><body>
<section class="page cover">
  <div class="tag">Editable DOCX + PDF progress report</div>
  <h1>Aviator Game System</h1>
  <h2>Project Progress Report</h2>
  <p>This report explains the current project screenshots in page order and summarizes the implemented features across gameplay, wallet, profile, rewards, compliance, chat, rooms, and admin reporting.</p>
  <p><strong>Date:</strong> 10 July 2026</p>
</section>
<section class="page">
  <h2>Executive Summary</h2>
  <div class="grid">
    <div class="card"><h3>Current State</h3><p class="lead">The project is a working Django Aviator platform with the core player journey implemented: account access, game play, wallet, cash-out, history, chat, and user guidance.</p></div>
    <div class="card"><h3>Evidence Used</h3><p class="lead">The submitted screenshots were ordered by the visible page flow and cross-checked against project routes, views, forms, and models.</p></div>
    <div class="card"><h3>Major Strength</h3><p class="lead">The system now includes both player-facing screens and backend records for transactions, game rounds, KYC, self-exclusion, rewards, rooms, and audit logs.</p></div>
    <div class="card"><h3>Next Step</h3><p class="lead">Focus on final QA, production payment integration, deployment hardening, and complete admin/operator testing.</p></div>
  </div>
</section>
<section class="page">
  <h2>Implemented Feature Coverage</h2>
  <p class="lead">This table captures the progress implemented so far in the project.</p>
  <table><thead><tr><th>Feature Area</th><th>Status</th><th>Progress Explanation</th></tr></thead><tbody>{feature_rows}</tbody></table>
</section>
<section class="page">
  <h2>System Flow Covered By Screenshots</h2>
  <ul>
    <li>Game landing/play screen: live multiplier, dual betting, cash-out, history, and chat.</li>
    <li>Account navigation: settings, free bets, bet history, game limits, rules, and provably fair access.</li>
    <li>Wallet and transactions: balance, deposits, withdrawals, providers, payment requests, and ledger view.</li>
    <li>Profile and compliance: player metrics, VIP progress, KYC, limits, and self-exclusion.</li>
    <li>Social and reward features: chat, emoji picker, reactions, online count, and chat rain distribution.</li>
    <li>Support pages: how-to-play guidance and game rules access.</li>
  </ul>
</section>
{''.join(screenshot_sections)}
<section class="page">
  <h2>Conclusion</h2>
  <p class="lead">The Aviator project has reached a strong functional prototype stage. The screens show a coherent dark themed product with real application flows, while the backend provides persistence, auditability, and operational support.</p>
  <ul>
    <li>Gameplay and wallet flows are implemented and connected to transaction records.</li>
    <li>Responsible gaming and compliance controls are visible and backed by Django forms/models.</li>
    <li>Rewards, chat rain, rooms, and admin analytics expand the system beyond a simple single-player demo.</li>
    <li>The remaining work is mostly hardening, integration, QA, and deployment readiness.</li>
  </ul>
</section>
</body></html>"""
    html_path = OUT_DIR / "Aviator_Project_Progress_Report.html"
    html_path.write_text(content, encoding="utf-8")
    return html_path


def print_pdf(html_path):
    pdf_path = OUT_DIR / "Aviator_Project_Progress_Report.pdf"
    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    if not chrome.exists():
        chrome = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
    if not chrome.exists():
        return None
    subprocess.run([
        str(chrome),
        "--headless",
        "--disable-gpu",
        f"--print-to-pdf={pdf_path}",
        "--no-pdf-header-footer",
        html_path.resolve().as_uri(),
    ], check=True)
    return pdf_path


def main():
    copied = ensure_assets()
    docx_path = build_docx(copied)
    html_path = build_html(copied)
    pdf_path = print_pdf(html_path)
    print(f"DOCX={docx_path}")
    print(f"HTML={html_path}")
    print(f"PDF={pdf_path if pdf_path else 'not created'}")


if __name__ == "__main__":
    main()
